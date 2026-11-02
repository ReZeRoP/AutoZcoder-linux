"""Synthetic desktop-client contracts: no GTK display or real user service."""
import json
from pathlib import Path

import pytest

from zcode_gateway import linux_config, linux_desktop


class Controller:
    def __init__(self, active="inactive"):
        self.active = active
        self.actions = []
        self.enabled = False

    def status(self):
        return {"ok": True, "properties": {"ActiveState": self.active,
                "UnitFileState": "enabled" if self.enabled else "disabled"}}

    def action(self, verb):
        self.actions.append(verb)
        if verb in ("start", "restart"):
            self.active = "active"
        elif verb == "stop":
            self.active = "inactive"
        elif verb in ("enable", "disable"):
            self.enabled = verb == "enable"
        return {"ok": True, "message": "Service action completed."}


@pytest.fixture
def synthetic(tmp_path, monkeypatch):
    monkeypatch.setattr(linux_config, "validate_service_environment", lambda: None)
    path = tmp_path / "config.yaml"
    origin = "http://127.0.0.1:18789"
    controller = Controller()
    identity = {"schema_version": 1, "product": "autozcoder-linux", "version": "1.3.0",
                "instance_id": "synthetic-one", "config_id": linux_config.config_identity(path, origin),
                "mode": "user-service", "stopping": False}
    result = {"ok": False, "state": "unavailable", "message": "Unavailable", "instance": None}
    monkeypatch.setattr(linux_config, "default_config_path", lambda: path)
    def monitor(custom=False, handshake=lambda *args: True):
        return linux_desktop.DesktopMonitor(path if custom else None, controller=controller,
            inspect=lambda _: {"ok": True, "server": {"origin": origin}},
            probe=lambda *_: result.copy(), handshake=handshake)
    return monitor, controller, identity, result


def test_launch_starts_default_once_and_close_never_stops(synthetic):
    make, controller, identity, probe = synthetic
    first = make()
    first.deliberate_start(); first.deliberate_start()
    assert controller.actions == ["start"]
    probe.update(ok=True, state="healthy", instance=identity)
    assert first.snapshot()["attach"] is True
    first.attached = True
    first.close()
    assert controller.actions == ["start"]
    reopened = make()
    reopened.deliberate_start()
    assert reopened.snapshot()["attach"] is True
    assert controller.actions == ["start"]  # reopening does not create a second backend


def test_custom_is_attach_only_even_on_stop_restart_autostart(synthetic):
    make, controller, identity, probe = synthetic
    client = make(custom=True)
    client.deliberate_start()
    identity["mode"] = "foreground"
    probe.update(ok=True, state="healthy", instance=identity)
    assert client.snapshot()["attach"] is True
    for action in ("start", "stop", "restart", "enable", "disable", "rm -rf ~"):
        assert client.action(action)["ok"] is False
    client.close()
    assert controller.actions == []


def test_foreign_listener_is_not_adopted_or_started(synthetic):
    make, controller, identity, probe = synthetic
    probe.update(state="foreign", message="Foreign listener")
    client = make()
    client.deliberate_start()
    assert client.snapshot()["state"] == "foreign"
    assert client.action("start")["ok"] is False
    assert client.action("restart")["ok"] is False
    assert controller.actions == []


def test_valid_degraded_gateway_attaches_after_auth(synthetic):
    make, controller, identity, probe = synthetic
    controller.active = "active"
    probe.update(ok=False, state="degraded", instance=identity)
    calls = []
    client = make(handshake=lambda *args: calls.append(args) or True)
    client.deliberate_start()
    assert client.snapshot()["state"] == "degraded"
    assert client.snapshot()["attach"] is True
    assert len(calls) == 1
    assert controller.actions == []


def test_degraded_foreground_is_not_default_unit(synthetic):
    make, controller, identity, probe = synthetic
    identity["mode"] = "foreground"
    probe.update(ok=False, state="degraded", instance=identity)
    client = make()
    client.deliberate_start()
    assert client.snapshot()["state"] == "foreign"
    assert controller.actions == []


def test_identity_requires_authenticated_handshake(synthetic):
    make, controller, identity, probe = synthetic
    controller.active = "active"
    probe.update(ok=True, state="healthy", instance=identity)
    client = make(handshake=lambda *_: False)
    assert client.snapshot()["state"] == "unauthorized"
    assert client.snapshot()["attach"] is False


def test_explicit_stop_never_auto_restarts_and_autostart_is_separate(synthetic):
    make, controller, identity, probe = synthetic
    client = make()
    client.deliberate_start()
    assert client.action("enable")["ok"] is True
    assert controller.active == "active"
    assert client.action("stop")["ok"] is True
    for _ in range(3):
        client.deliberate_start()
        assert client.snapshot()["state"] == "stopped"
    assert controller.actions == ["start", "enable", "stop"]
    assert client.action("disable")["ok"] is True
    assert controller.active == "inactive"
    assert client.action("start")["ok"] is True
    assert client.intentional_stop is False


def test_load_failure_resets_attachment_without_backend_action(synthetic):
    make, controller, identity, probe = synthetic
    controller.active = "active"
    probe.update(ok=True, state="healthy", instance=identity)
    calls = []
    client = make(handshake=lambda *args: calls.append(args) or True)
    client.snapshot(); client.attached = True
    client.load_failed()
    assert client.snapshot()["attach"] is True
    assert len(calls) == 2
    assert controller.actions == []


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:18789/dash", "http://127.0.0.1:18789/dash#server",
])
def test_exact_dashboard_navigation(url):
    assert linux_desktop.trusted_dashboard_url(url, "http://127.0.0.1:18789")


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8788/dash", "http://127.0.0.1:8789/dash",
    "http://127.0.0.1:18789/dash?redirect=evil", "http://127.0.0.1:18789/other",
    "https://127.0.0.1:18789/dash", "http://evil.test:18789/dash",
    "http://user@127.0.0.1:18789/dash", "file:///tmp/dash", "javascript:alert(1)",
    "http://127.0.0.1:18789/%64ash",
])
def test_arbitrary_navigation_rejected(url):
    assert not linux_desktop.trusted_dashboard_url(url, "http://127.0.0.1:18789")


def test_ipv6_exact_origin():
    assert linux_desktop.trusted_dashboard_url("http://[::1]:18789/dash", "http://[::1]:18789")
    assert not linux_desktop.trusted_dashboard_url("http://127.0.0.1:18789/dash", "http://[::1]:18789")


def test_handshake_parses_inert_json_then_authenticates(synthetic, monkeypatch):
    _, _, identity, _ = synthetic
    calls = []
    def get(origin, path, key=None, limit=8192):
        calls.append((path, key))
        if path == "/dash":
            return '<script id="autozcoder-bootstrap" type="application/json">' + json.dumps(
                {"key": "synthetic-key", "instance": identity}) + '</script>'
        return json.dumps(identity)
    monkeypatch.setattr(linux_desktop, "_local_get", get)
    assert linux_desktop.dashboard_handshake("http://127.0.0.1:18789", identity)
    assert calls == [("/dash", None), ("/admin/dash/lifecycle", "synthetic-key")]
    identity["stopping"] = True
    assert not linux_desktop.dashboard_handshake("http://127.0.0.1:18789", identity)


def test_missing_bootstrap_never_sends_credentials(monkeypatch):
    calls = []
    monkeypatch.setattr(linux_desktop, "_local_get", lambda *args, **kwargs: calls.append(args) or "<h1>No bootstrap</h1>")
    assert not linux_desktop.dashboard_handshake("http://127.0.0.1:18789", {})
    assert len(calls) == 1


def test_environment_conflict_never_starts_or_attaches(synthetic, monkeypatch):
    make, controller, _, _ = synthetic
    def invalid():
        raise ValueError("Use explicit --config foreground mode.")
    monkeypatch.setattr(linux_config, "validate_service_environment", invalid)
    client = make()
    client.deliberate_start()
    assert client.snapshot()["state"] == "foreign"
    assert not client.action("start")["ok"]
    assert controller.actions == []


def test_desktop_module_never_imports_runtime_and_has_no_bridge():
    source = Path(linux_desktop.__file__).read_text()
    assert "import uvicorn" not in source
    assert "from .app import" not in source
    assert "load_config(" not in source
    assert "js_api=" not in source
    assert "register_script_message_handler" not in source
    assert "desktop_shutdown" not in source
