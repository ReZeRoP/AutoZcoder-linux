"""Synthetic foreground ownership/identity tests; never bind a real listener."""
from pathlib import Path
import errno
import signal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from zcode_gateway import linux_config, linux_service
from zcode_gateway.app import create_app
from zcode_gateway.config import load_config


@pytest.fixture(autouse=True)
def restore_process_state(monkeypatch):
    """Real serve changes umask; synthetic tests must restore process state."""
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
def private_home(tmp_path, monkeypatch):
    import os
    import pwd
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AUTOZCODER_PACKAGE_MANAGED", "0")
    # Record every variable serve/configure_runtime writes for teardown.
    for name in ("ZCODE_GATEWAY_CONFIG", "PLAYWRIGHT_BROWSERS_PATH",
                 "AUTOZCODER_CHROMIUM_EXECUTABLE", "WRANGLER_SEND_METRICS"):
        monkeypatch.setenv(name, os.environ.get(name, ""))
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(tmp_path)))
    for name in list(os.environ):
        if name.startswith(("ZCODE_GATEWAY_", "XDG_")) or name == "AUTOZCODER_RUNTIME_ROOT":
            monkeypatch.delenv(name, raising=False)
    return tmp_path


@pytest.fixture
def fake_runtime(private_home, monkeypatch):
    import uvicorn
    captured = {}
    class Socket:
        def __init__(self, *args):
            captured["socket_args"] = args
        def setsockopt(self, *args):
            pass
        def bind(self, address):
            captured["bound"] = address
        def close(self):
            captured["socket_closed"] = True
    class Server:
        def __init__(self, cfg):
            self.config = cfg
            self.started = False
            self.should_exit = False
            captured["server"] = self
        def handle_exit(self, sig, frame):
            captured["signal"] = sig
            self.should_exit = True
        def run(self, sockets):
            app = self.config.app
            captured["app"] = app
            assert callable(app.state.desktop_shutdown)
            self.started = True
            app.state.desktop_shutdown()
            assert app.state.stopping
    monkeypatch.setattr(linux_service.socket, "socket", Socket)
    monkeypatch.setattr(uvicorn, "Server", Server)
    return captured


def test_foreground_owns_one_socket_and_callback(fake_runtime):
    assert linux_service.serve() == 0
    app = fake_runtime["app"]
    assert fake_runtime["bound"] == ("127.0.0.1", 8789)
    assert fake_runtime["socket_closed"]
    assert app.state.linux_mode == "foreground"
    assert not app.state.linux_managed_default
    assert fake_runtime["server"].config.proxy_headers is False
    assert fake_runtime["server"].config.timeout_graceful_shutdown + linux_service.CLEANUP_SECONDS < 20
    lock = linux_service.InstanceLock(linux_config.default_config_path())
    assert lock.acquire()
    lock.close()


def test_managed_default_identity(fake_runtime):
    assert linux_service.serve(user_service=True) == 0
    app = fake_runtime["app"]
    assert app.state.linux_mode == "user-service"
    assert app.state.linux_managed_default
    assert app.state.linux_identity["schema_version"] == 1
    assert len(app.state.linux_identity["config_id"]) == 64


def test_missing_explicit_config_does_not_bootstrap(private_home, monkeypatch):
    monkeypatch.setattr(linux_service, "bootstrap_config", lambda: pytest.fail("explicit path must not bootstrap"))
    assert linux_service.serve(private_home / "missing.yaml") == 78
    assert not linux_config.default_config_path().exists()


@pytest.mark.parametrize("text", ["secret-malformed: [", "- secret-value", "", "server: {host: 0.0.0.0}", "server: {port: 0}"])
def test_invalid_config_is_sanitized(private_home, capsys, text):
    path = private_home / "invalid.yaml"
    path.write_text(text)
    assert linux_service.serve(path) == 78
    assert "secret" not in capsys.readouterr().err


def test_managed_environment_rejected_before_bootstrap(private_home, monkeypatch):
    monkeypatch.setenv("ZCODE_GATEWAY_SERVER__ADMIN_KEY", "never-print-me")
    assert linux_service.serve(user_service=True) == 78
    assert not linux_config.default_config_path().exists()


def test_managed_custom_config_rejected_before_load(private_home):
    path = private_home / "custom.yaml"
    path.write_text("server: {port: 8790}")
    assert linux_service.serve(path, user_service=True) == 78
    assert not linux_config.default_config_path().exists()


def test_duplicate_lock_is_permanent_conflict(fake_runtime):
    path = linux_service.bootstrap_config()
    lock = linux_service.InstanceLock(path)
    assert lock.acquire()
    try:
        assert linux_service.serve(path) == 73
        assert "bound" not in fake_runtime
    finally:
        lock.close()


def test_port_conflict_does_not_adopt_listener(fake_runtime, monkeypatch):
    def conflict(*args):
        raise OSError(errno.EADDRINUSE, "untrusted secret listener")
    monkeypatch.setattr(linux_service.socket, "socket", conflict)
    assert linux_service.serve() == 73


def test_return_without_stop_is_crash(fake_runtime, monkeypatch):
    import uvicorn
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, sockets: setattr(self, "started", True))
    assert linux_service.serve() == 1


def test_signal_handler_is_single_owner_and_no_signal_replay(fake_runtime, monkeypatch):
    import uvicorn
    changed = []
    monkeypatch.setattr(linux_service.signal, "signal", lambda sig, handler: changed.append((sig, handler)) or "old")
    def run(self, sockets):
        with self.capture_signals():
            self.handle_exit(signal.SIGTERM, None)
    monkeypatch.setattr(uvicorn.Server, "run", run)
    assert linux_service.serve() == 0
    assert [item[0] for item in changed] == [signal.SIGINT, signal.SIGTERM, signal.SIGINT, signal.SIGTERM]
    assert changed[-1][1] == "old"


def test_deliberate_stop_cleanup_warning_cannot_restart(fake_runtime, monkeypatch):
    import uvicorn
    def run(self, sockets):
        self.config.app.state.desktop_shutdown()
        self.config.app.state.cleanup_failed = True
    monkeypatch.setattr(uvicorn.Server, "run", run)
    assert linux_service.serve() == 0


@pytest.mark.parametrize("kind", ["directory", "broken-link"])
def test_existing_invalid_default_never_replaced(private_home, kind):
    path = linux_config.default_config_path()
    path.parent.mkdir(parents=True)
    if kind == "directory":
        path.mkdir()
    else:
        path.symlink_to(private_home / "nonexistent")
    with pytest.raises(ValueError):
        linux_service.bootstrap_config()
    assert not (private_home / ".local/share/autozcoder").exists()


def instance_app(private_home, host="127.0.0.1", mode="foreground"):
    cfg = load_config(linux_service.bootstrap_config())
    cfg.pool.balance_poll_seconds = 0
    cfg.server.host = host
    app = create_app(cfg)
    app.state.linux_mode = mode
    app.state.linux_managed_default = mode == "user-service"
    app.state.linux_identity = {"version": "1.3.0", "instance_id": "a" * 32,
                               "config_id": "b" * 64, "mode": mode,
                               "secret": "not-for-response", "path": "/private"}
    return cfg, app


@pytest.mark.parametrize("bind,host,peer,expected", [
    ("127.0.0.1", "127.0.0.1", "127.0.0.1", 200),
    ("::1", "[::1]", "::1", 200),
    ("localhost", "localhost", "127.0.0.1", 200),
    ("0.0.0.0", "127.0.0.1", "127.0.0.1", 403),
    ("127.0.0.1", "evil.test", "127.0.0.1", 403),
    ("127.0.0.1", "127.0.0.1", "192.0.2.5", 403),
])
def test_instance_three_loopback_checks(private_home, bind, host, peer, expected):
    cfg, app = instance_app(private_home, bind)
    with TestClient(app, base_url=f"http://{host}:8789", client=(peer, 123)) as client:
        r = client.get("/health/instance", headers={"X-Forwarded-For": "127.0.0.1"})
        assert r.status_code == expected
        if expected == 200:
            assert set(r.json()) == {"schema_version", "product", "version", "instance_id", "config_id", "mode", "stopping"}
            assert r.headers["cache-control"] == "no-store"
            assert r.json()["product"] == "autozcoder-linux"
            assert client.get("/ready").status_code == 503
            assert client.get("/health").status_code == 200
        auth = {"Authorization": "Bearer " + cfg.server.admin_key}
        assert client.get("/admin/dash/lifecycle").status_code == 403
        assert client.get("/admin/dash/lifecycle", headers=auth).json()["instance_id"] == "a" * 32


def test_autostart_mode_boolean_and_exact_controller(private_home, monkeypatch):
    from zcode_gateway import linux_control
    actions = []
    class Controller:
        def status(self):
            return {"ok": True, "code": "ok", "properties": {"UnitFileState": "disabled"}}
        def action(self, verb):
            actions.append(verb)
            return {"ok": True}
    monkeypatch.setattr(linux_control, "ServiceController", Controller)
    cfg, app = instance_app(private_home, mode="user-service")
    auth = {"Authorization": "Bearer " + cfg.server.admin_key}
    with TestClient(app) as client:
        assert client.get("/admin/dash/service/autostart").status_code == 403
        assert client.get("/admin/dash/service/autostart", headers=auth).json()["enabled"] is False
        for value in ("true", 1, None):
            assert client.put("/admin/dash/service/autostart", headers=auth, json={"enabled": value}).status_code == 400
        assert client.put("/admin/dash/service/autostart", headers=auth, json={"enabled": True, "unit": "other"}).status_code == 400
        for enabled in (True, False):
            assert client.put("/admin/dash/service/autostart", headers=auth, json={"enabled": enabled}).status_code == 200
        app.state.linux_mode = "foreground"
        assert client.get("/admin/dash/service/autostart", headers=auth).json()["available"] is False
        assert client.put("/admin/dash/service/autostart", headers=auth, json={"enabled": True}).status_code == 409
    assert actions == ["enable", "disable"]
