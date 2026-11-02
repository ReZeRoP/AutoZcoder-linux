"""Offline acquisition rollback and cooperative shutdown tests."""
import asyncio
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from zcode_gateway import api_dash, linux_service
from zcode_gateway.app import create_app
from zcode_gateway.config import load_config
from zcode_gateway.generator import GeneratorManager
from zcode_gateway.relay_manager import RelayManager
from zcode_gateway.state import AppState


@pytest.fixture(autouse=True)
def restore_process_state(monkeypatch):
    """Managers mutate generator environment and its pool module singleton."""
    import os
    import sys
    previous_umask = os.umask(0o077)
    os.umask(previous_umask)
    previous_path = list(sys.path)
    previous_pool = sys.modules.get("proxy_pool")
    previous_output = getattr(previous_pool, "OUTPUT_DIR", None)
    monkeypatch.setenv("ZAI_OUTPUT_DIR", os.environ.get("ZAI_OUTPUT_DIR", ""))
    try:
        yield
    finally:
        os.umask(previous_umask)
        sys.path[:] = previous_path
        if previous_pool is not None and previous_output is not None:
            previous_pool.set_output_dir(previous_output)
        elif previous_pool is None:
            sys.modules.pop("proxy_pool", None)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    for variable, suffix in (("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache")):
        monkeypatch.setenv(variable, str(tmp_path / suffix))
    cfg = load_config(linux_service.bootstrap_config())
    cfg.pool.balance_poll_seconds = 0
    return cfg


@pytest.mark.parametrize("failure", ["capture", "client", "updater"])
async def test_startup_failure_closes_every_acquired_resource(isolated, monkeypatch, failure):
    import zcode_gateway.app as app_module
    import zcode_gateway.updater as updater_module
    import zcode_gateway.relay_manager as relay_module
    closed = []
    class Store:
        def __init__(self, path):
            closed.append("store-open")
        def close(self):
            closed.append("store-close")
    class Catalog:
        def __init__(self, *args):
            pass
        async def aclose(self):
            closed.append("catalog-close")
            raise RuntimeError("cleanup failure should not mask original startup error")
    class Tokens:
        def __init__(self, *args):
            pass
        async def aclose(self):
            closed.append("tokens-close")
    class Client:
        def __init__(self, *args, **kwargs):
            if failure == "client":
                raise RuntimeError("synthetic startup failure")
        async def aclose(self):
            closed.append("client-close")
    class Relay:
        def __init__(self, *args, **kwargs):
            self._health_stop = threading.Event()
        def start_health_loop(self, *args):
            closed.append("relay-start")
        def stop_health_loop(self, **kwargs):
            closed.append("relay-stop")
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic startup failure")
    monkeypatch.setattr(app_module, "Store", Store)
    monkeypatch.setattr(app_module, "ModelCatalog", Catalog)
    monkeypatch.setattr(app_module, "TokenManager", Tokens)
    monkeypatch.setattr(app_module, "UpstreamClient", Client)
    monkeypatch.setattr(relay_module, "RelayManager", Relay)
    if failure == "capture":
        monkeypatch.setattr(app_module, "JsonlCapture", fail)
    if failure == "updater":
        monkeypatch.setattr(updater_module, "UpdateChecker", fail)
    app = create_app(isolated)
    with pytest.raises(RuntimeError, match="synthetic startup failure"):
        async with app.router.lifespan_context(app):
            pytest.fail("startup should fail")
    assert closed.count("store-close") == 1
    if failure != "capture":
        assert "catalog-close" in closed
        assert "tokens-close" in closed
        assert "relay-stop" in closed
    if failure == "updater":
        assert "client-close" in closed


async def test_callbacks_cancelled_and_awaited_before_store_close(isolated, monkeypatch):
    import zcode_gateway.app as app_module
    original = app_module.Store
    order = []
    class Store(original):
        def close(self):
            order.append("store-close")
            super().close()
    monkeypatch.setattr(app_module, "Store", Store)
    app = create_app(isolated)
    started = asyncio.Event()
    async def work():
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            order.append("callback-end")
    async with app.router.lifespan_context(app):
        state = app.state.app_state
        state.schedule_background(work(), asyncio.get_running_loop())
        await started.wait()
        assert len(state.async_tasks) >= 1
    assert order == ["callback-end", "store-close"]
    assert not state.async_tasks
    assert state.stopping
    assert not app.state.cleanup_failed


def test_stopping_rejects_mutations_and_lazy_managers(isolated, monkeypatch):
    app = create_app(isolated)
    auth = {"Authorization": "Bearer " + isolated.server.admin_key}
    monkeypatch.setattr(api_dash, "_generator", lambda request: pytest.fail("no lazy manager on stop"))
    with TestClient(app) as client:
        app.state.app_state.begin_stopping()
        app.state.app_state.begin_stopping()
        for path, method in (("/v1/chat/completions", "post"), ("/admin/dash/settings", "put"),
                             ("/admin/dash/generator/status", "get"), ("/admin/dash/setup/install", "post")):
            assert getattr(client, method)(path, headers=auth).status_code == 503
        assert app.state.app_state.generator is None
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 503


def test_linux_shutdown_no_hard_exit_and_callback_missing_is_error(isolated, monkeypatch):
    app = create_app(isolated)
    app.state.linux_mode = "foreground"
    auth = {"Authorization": "Bearer " + isolated.server.admin_key}
    monkeypatch.setattr(api_dash.os, "_exit", lambda *args: pytest.fail("Linux cannot hard-exit"))
    with TestClient(app) as client:
        assert client.post("/admin/dash/shutdown", headers=auth).status_code == 503
        assert not app.state.app_state.stopping
        assert client.post("/admin/dash/update/apply", headers=auth).status_code == 409


def test_generator_shutdown_tracks_existing_threads_and_disables_producers(tmp_path):
    manager = GeneratorManager(tmp_path / "generator", data_dir=tmp_path / "data")
    entered = threading.Event()
    def callback():
        entered.set()
        manager._shutdown_stop.wait(5)
    thread = manager.spawn_background(callback)
    assert entered.wait(1)
    api_dash._start_referral_finisher(manager)
    finisher = manager._finisher_thread
    manager.shutdown(deadline=time.monotonic() + 1)
    manager.shutdown(deadline=time.monotonic() + 1)
    assert not thread.is_alive()
    assert not finisher.is_alive()
    assert not manager._background_threads
    assert manager.spawn_background(lambda: pytest.fail("new work after stop")) is None
    assert manager.start_workers()["error"] == "gateway is stopping"
    assert manager.checker.run()["error"] == "gateway is stopping"


def test_generator_child_stops_concurrently_with_bounded_deadline(tmp_path):
    manager = GeneratorManager(tmp_path / "generator", data_dir=tmp_path / "data")
    stopped = []
    class Child:
        def stop(self):
            stopped.append(threading.get_ident())
        def alive(self):
            return True
    manager.workers = {str(n): Child() for n in range(8)}
    manager.shutdown(deadline=time.monotonic() + 1)
    assert len(stopped) == 8


def test_relay_stop_joins_idle_thread_without_ping(tmp_path, monkeypatch):
    manager = RelayManager(tmp_path)
    monkeypatch.setattr(manager, "ping_all", lambda: pytest.fail("no live pings"))
    manager.start_health_loop(300)
    thread = manager._health_thread
    manager.stop_health_loop(deadline=time.monotonic() + 1)
    manager.stop_health_loop(deadline=time.monotonic() + 1)
    assert not thread.is_alive()
    assert manager._health_stop.is_set()


async def test_uncooperative_worker_reports_deadline_not_fake_cancellation(isolated, monkeypatch):
    monkeypatch.setattr(linux_service, "CLEANUP_SECONDS", 0.1)
    app = create_app(isolated)
    release = threading.Event()
    entered = threading.Event()
    class Worker:
        def request_shutdown(self):
            pass
        def shutdown(self, **kwargs):
            entered.set()
            release.wait(2)
    start = time.monotonic()
    try:
        async with app.router.lifespan_context(app):
            app.state.app_state.generator = Worker()
        assert entered.is_set()
        assert app.state.cleanup_failed
        assert time.monotonic() - start < 1
    finally:
        release.set()
