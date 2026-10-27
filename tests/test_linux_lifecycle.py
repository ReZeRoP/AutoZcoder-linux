"""Offline lifecycle checks: stopping never creates or restarts background work."""
from pathlib import Path
import threading
from types import SimpleNamespace

from fastapi.testclient import TestClient

from zcode_gateway import api_dash, linux_desktop
from zcode_gateway.app import create_app
from zcode_gateway.config import load_config
from zcode_gateway.generator import GeneratorManager


def isolated_app(tmp_path, monkeypatch):
    for name, folder in [("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data")]:
        monkeypatch.setenv(name, str(tmp_path / folder))
    cfg = load_config(linux_desktop.bootstrap_config())
    cfg.pool.balance_poll_seconds = 0
    cfg.update.enabled = False
    return cfg, create_app(cfg)


def test_shutdown_uses_desktop_callback(tmp_path, monkeypatch):
    cfg, app = isolated_app(tmp_path, monkeypatch)
    called = []
    app.state.desktop_shutdown = lambda: called.append("shutdown")

    class Timer:
        def __init__(self, delay, callback):
            assert delay == 1.0
            self.callback = callback
        def start(self):
            self.callback()

    monkeypatch.setattr(api_dash.threading, "Timer", Timer)
    with TestClient(app) as client:
        response = client.post("/admin/dash/shutdown",
                               headers={"Authorization": "Bearer " + cfg.server.admin_key})
        assert response.json()["stopping"]
    assert called == ["shutdown"]


def test_stopall_does_not_initialize_manager(tmp_path, monkeypatch):
    cfg, app = isolated_app(tmp_path, monkeypatch)
    def forbidden(*args):
        raise AssertionError("stopping must not initialize background work")
    monkeypatch.setattr(api_dash, "_generator", forbidden)
    with TestClient(app) as client:
        assert client.post("/admin/dash/generator/stopall",
                           headers={"Authorization": "Bearer " + cfg.server.admin_key}).status_code == 200


def test_setup_install_runs_off_event_loop(tmp_path, monkeypatch):
    cfg, app = isolated_app(tmp_path, monkeypatch)
    threads = []
    class Setup:
        def start_install(self):
            threads.append(threading.get_ident())
            return False
        def status(self):
            threads.append(threading.get_ident())
            return {}
    monkeypatch.setattr(api_dash, "_env_setup_shared", lambda request: Setup())
    with TestClient(app) as client:
        assert client.post("/admin/dash/setup/install",
                           headers={"Authorization": "Bearer " + cfg.server.admin_key}).status_code == 200
    assert len(threads) == 2 and threads[0] != threads[1]


def test_first_config_is_complete_before_publication(tmp_path, monkeypatch):
    for name, folder in [("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data")]:
        monkeypatch.setenv(name, str(tmp_path / folder))
    original = linux_desktop.os.link
    def publish(source, destination):
        import yaml
        assert not Path(destination).exists()
        data = yaml.safe_load(Path(source).read_text())
        assert data["server"]["port"] == 8789
        assert data["server"]["admin_key"].startswith("gw-admin-")
        original(source, destination)
    monkeypatch.setattr(linux_desktop.os, "link", publish)
    path = linux_desktop.bootstrap_config()
    assert path.stat().st_mode & 0o777 == 0o600
    assert not list(path.parent.glob(".config-*"))


def test_stopall_cancels_pending_batch_without_starting_next(tmp_path, monkeypatch):
    mgr = GeneratorManager(tmp_path / "source", data_dir=tmp_path / "data")
    monkeypatch.setattr(mgr, "pending_accounts", lambda: [
        {"email": "first@example.invalid"}, {"email": "second@example.invalid"}])
    launched = []
    class Job:
        email = "first@example.invalid"
        running = True
        def alive(self):
            return self.running
        def stop(self):
            self.running = False
    def start(email, mode):
        launched.append(email)
        mgr.claims["fake"] = Job()
        return {"started": "fake"}
    monkeypatch.setattr(mgr, "start_claim_start", start)
    monkeypatch.setattr("zcode_gateway.generator.time.sleep", lambda seconds: mgr.stop_all())
    result = mgr.claim_all_pending()
    assert launched == ["first@example.invalid"]
    assert result["started"] == launched
    assert mgr.checker.auto_verify is False
    assert mgr.checker._auto_stop.is_set()
