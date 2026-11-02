"""Offline exact-unit control and local-probe safety regressions."""
from __future__ import annotations

import http.client
import io
import json
import os
import socket
import subprocess
import time
import types

import pytest

from zcode_gateway import linux_control


@pytest.fixture(autouse=True)
def standard_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(linux_control.pwd, "getpwuid", lambda uid: types.SimpleNamespace(pw_dir=str(tmp_path)))
    for name in tuple(os.environ):
        if name.startswith("XDG_") or name.startswith("ZCODE_GATEWAY_") or name == "AUTOZCODER_RUNTIME_ROOT":
            monkeypatch.delenv(name, raising=False)


def props(active="active", enabled="disabled", load="loaded"):
    return f"LoadState={load}\nActiveState={active}\nSubState={'running' if active == 'active' else 'dead'}\nUnitFileState={enabled}\nMainPID=1234\nResult=success\nExecMainStatus=0\n"


def instance(**extra):
    return {"schema_version": 1, "product": "autozcoder-linux", "version": "1.3.0", "instance_id": "instance_123456789", "config_id": "a" * 64, "mode": "user-service", "stopping": False, **extra}


def test_constructor_no_effects(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("Constructor launched process"))
    controller = linux_control.ServiceController()
    assert controller.timeout == linux_control.CONTROL_TIMEOUT


@pytest.mark.parametrize("verb", ["start", "stop", "restart", "enable", "disable"])
def test_action_exact_unit_no_shell_or_now(monkeypatch, verb):
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(subprocess, "run", run)
    assert linux_control.ServiceController().action(verb)["ok"]
    argv, kwargs = calls[0]
    assert argv == ["/usr/bin/systemctl", "--user", "--no-pager", "--no-ask-password", verb, "autozcoder.service"]
    assert len(calls) == 1
    assert "--now" not in argv and "sudo" not in argv
    assert kwargs.get("shell", False) is False
    assert kwargs["timeout"] <= 8
    assert kwargs["stdin"] == subprocess.DEVNULL and kwargs["stderr"] == subprocess.DEVNULL
    assert kwargs["env"]["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path=/run/user/{os.getuid()}/bus"


@pytest.mark.parametrize("verb", ["daemon-reload", "kill", "reset-failed", "start autozcoder.service", "start;SECRET", "enable --now", "autozcoder.service", "stop other.service"])
def test_reject_arbitrary_actions(monkeypatch, verb):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("Unallowlisted action executed"))
    with pytest.raises(ValueError) as error:
        linux_control.ServiceController().action(verb)
    assert "SECRET" not in str(error.value)


def test_reject_custom_context_before_mutation(monkeypatch):
    monkeypatch.setenv("ZCODE_GATEWAY_SERVER__ADMIN_KEY", "SECRET-CREDENTIAL")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("Custom service context mutated"))
    result = linux_control.ServiceController().action("start")
    assert result["code"] == "service_environment"
    assert "SECRET" not in json.dumps(result)


def test_status_allowlisted_scalar_output_and_environment(monkeypatch):
    calls = []
    monkeypatch.setenv("HTTP_PROXY", "http://SECRET-PROXY")
    monkeypatch.setenv("ZCODE_GATEWAY_SERVER__ADMIN_KEY", "SECRET-CREDENTIAL")
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        text = props() + "Environment=SECRET-CREDENTIAL\nExecStart=SECRET-COMMAND\nDescription=SECRET-DESCRIPTION\n"
        return subprocess.CompletedProcess(argv, 0, text, "SECRET-ERROR")
    monkeypatch.setattr(subprocess, "run", run)
    result = linux_control.ServiceController().status()
    assert result["ok"] and result["code"] == "unit_active"
    assert "SECRET" not in json.dumps(result)
    assert set(result["properties"]) == set(linux_control.PROPERTIES)
    assert result["properties"]["MainPID"] == 1234
    argv, kwargs = calls[0]
    assert argv[-1] == "autozcoder.service" and "show" in argv
    assert "status" not in argv and "--all" not in argv
    assert "ZCODE_GATEWAY_SERVER__ADMIN_KEY" not in kwargs["env"]
    assert "HTTP_PROXY" not in kwargs["env"]


@pytest.mark.parametrize("active,enabled,load,code", [("active", "disabled", "loaded", "unit_active"), ("inactive", "disabled", "loaded", "unit_stopped"), ("inactive", "enabled", "loaded", "unit_stopped"), ("failed", "enabled", "loaded", "unit_failed"), ("inactive", "disabled", "not-found", "unit_missing"), ("inactive", "masked", "masked", "unit_unavailable")])
def test_status_states(monkeypatch, active, enabled, load, code):
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, props(active, enabled, load), ""))
    result = linux_control.ServiceController().status()
    assert result["code"] == code
    assert result["properties"]["UnitFileState"] == enabled


@pytest.mark.parametrize("error,code", [(FileNotFoundError("SECRET-PATH"), "systemctl_unavailable"), (PermissionError("SECRET-PATH"), "manager_unavailable"), (subprocess.TimeoutExpired("SECRET-COMMAND", 8, output="SECRET-OUTPUT"), "manager_timeout")])
def test_manager_errors_sanitized(monkeypatch, error, code):
    def run(*a, **k):
        raise error
    monkeypatch.setattr(subprocess, "run", run)
    result = linux_control.ServiceController().status()
    assert result["code"] == code
    assert "SECRET" not in json.dumps(result)


def test_controller_status_uses_passwd_home_not_override(monkeypatch):
    monkeypatch.setenv("HOME", "/tmp/SECRET-HOME")
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: calls.append(kw) or subprocess.CompletedProcess(argv, 0, props(), ""))
    assert linux_control.ServiceController().status()["ok"]
    assert calls[0]["env"]["HOME"] != "/tmp/SECRET-HOME"


def test_socket_deadline_expires_without_reading(monkeypatch):
    # Real socket object is never connected; timeout fails before recv/send.
    sock = linux_control._DeadlineSocket(socket.AF_INET, socket.SOCK_STREAM, deadline=0)
    try:
        with pytest.raises(TimeoutError):
            sock.recv_into(bytearray(1))
        with pytest.raises(TimeoutError):
            sock.sendall(b"x")
    finally:
        sock.close()


def test_missing_unit_and_unknown_values_sanitized(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 1, props(load="not-found"), "SECRET"))
    assert linux_control.ServiceController().status()["code"] == "unit_missing"
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, props().replace("running", "SECRET-STATE").replace("1234", "SECRET-PID"), ""))
    result = linux_control.ServiceController().status()
    assert result["properties"]["SubState"] == "unknown"
    assert result["properties"]["MainPID"] is None
    assert "SECRET" not in json.dumps(result)


def test_action_failure_reads_status_but_does_not_retry(monkeypatch):
    calls = []
    def run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, props(load="not-found") if "show" in argv else "", "SECRET")
    monkeypatch.setattr(subprocess, "run", run)
    result = linux_control.ServiceController().action("restart")
    assert result["code"] == "unit_missing"
    assert len(calls) == 2
    assert "restart" in calls[0] and "show" in calls[1]
    assert "SECRET" not in json.dumps(result)


def test_reload_separate_explicit(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
    assert linux_control.ServiceController().reload()["ok"]
    assert calls == [["/usr/bin/systemctl", "--user", "--no-pager", "--no-ask-password", "daemon-reload"]]


@pytest.mark.parametrize("origin", ["https://127.0.0.1:8789", "http://evil.invalid:8789", "http://127.0.0.2:8789", "http://user:SECRET@127.0.0.1:8789", "http://127.0.0.1:8789/private", "http://127.0.0.1:8789?secret", "http://127.0.0.1:8789#secret", "http://127.0.0.1:0", "http://127.0.0.1:65536", "http://[::]:8789"])
def test_probe_origin_allowlist_no_dns_or_connect(monkeypatch, origin):
    monkeypatch.setattr(socket, "socket", lambda *a, **k: pytest.fail("Invalid origin connected"))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: pytest.fail("Invalid origin DNS"))
    result = linux_control.probe_instance(origin)
    assert result["code"] == "origin_invalid"
    assert "SECRET" not in json.dumps(result)


@pytest.mark.parametrize("origin", ["http://127.0.0.1:8789", "http://localhost:8789", "http://[::1]:8789"])
def test_probe_identity_then_health_only(monkeypatch, origin):
    calls = []
    monkeypatch.setenv("HTTP_PROXY", "http://SECRET-PROXY")
    def request(origin, path, deadline):
        calls.append((origin, path, deadline))
        return 200, instance(extra_secret="SECRET") if path == "/health/instance" else {"status": "ok", "accounts": "SECRET"}
    monkeypatch.setattr(linux_control, "_request_json", request)
    result = linux_control.probe_instance(origin, "a" * 64)
    assert result["ok"] and result["state"] == "healthy"
    assert [call[1] for call in calls] == ["/health/instance", "/health"]
    assert calls[0][2] == calls[1][2]
    assert "SECRET" not in json.dumps(result)
    assert set(result["instance"]) == {"schema_version", "product", "version", "instance_id", "config_id", "mode", "stopping"}


@pytest.mark.parametrize("bad", [None, {}, {"schema_version": True}, {"schema_version": 2}, {"product": "foreign"}, {"config_id": "b" * 64}, {"mode": "other"}, {"stopping": "false"}, {"instance_id": "SECRET id"}, {"version": "SECRET-version"}])
def test_probe_foreign_never_reads_health(monkeypatch, bad):
    calls = []
    def request(origin, path, deadline):
        calls.append(path)
        return 200, bad if bad in (None, {}) else instance(**bad)
    monkeypatch.setattr(linux_control, "_request_json", request)
    result = linux_control.probe_instance("http://127.0.0.1:8789", "a" * 64)
    assert result["state"] == "foreign" and not result["ok"]
    assert calls == ["/health/instance"]
    assert "SECRET" not in json.dumps(result)


@pytest.mark.parametrize("status", [301, 302, 307, 308, 401, 404, 500])
def test_probe_http_errors_no_follow(monkeypatch, status):
    calls = []
    monkeypatch.setattr(linux_control, "_request_json", lambda origin, path, deadline: calls.append(path) or (status, None))
    assert linux_control.probe_instance("http://127.0.0.1:8789")["state"] == "foreign"
    assert calls == ["/health/instance"]


def test_probe_stopping_and_degraded(monkeypatch):
    monkeypatch.setattr(linux_control, "_request_json", lambda *a: (200, instance(stopping=True)))
    result = linux_control.probe_instance("http://127.0.0.1:8789")
    assert result["code"] == "instance_stopping" and result["state"] == "degraded"
    monkeypatch.setattr(linux_control, "_request_json", lambda origin, path, deadline: (200, instance() if path == "/health/instance" else {"status": "degraded", "edge_block": {"private": "SECRET"}}))
    result = linux_control.probe_instance("http://127.0.0.1:8789")
    assert result["code"] == "instance_degraded" and "SECRET" not in json.dumps(result)


@pytest.mark.parametrize("error,code", [(ConnectionRefusedError("SECRET"), "instance_unavailable"), (socket.timeout("SECRET"), "instance_unavailable"), (http.client.BadStatusLine("SECRET"), "instance_unavailable"), (ValueError("SECRET"), "instance_foreign")])
def test_probe_sanitized_failures(monkeypatch, error, code):
    def request(*a):
        raise error
    monkeypatch.setattr(linux_control, "_request_json", request)
    result = linux_control.probe_instance("http://127.0.0.1:8789")
    assert result["code"] == code and "SECRET" not in json.dumps(result)


@pytest.mark.parametrize("host,addresses", [("127.0.0.1", ["127.0.0.1"]), ("::1", ["::1"]), ("localhost", ["127.0.0.1", "::1"])])
def test_connections_numeric_loopback_without_dns(monkeypatch, host, addresses):
    connected = []
    class FakeSocket:
        def __init__(self, family, kind, *, deadline):
            self.family = family
        def settimeout(self, timeout):
            assert 0 < timeout <= 2
        def connect(self, address):
            connected.append(address[0])
            if host == "localhost" and address[0] == "127.0.0.1":
                raise ConnectionRefusedError
        def close(self):
            pass
    monkeypatch.setattr(linux_control, "_DeadlineSocket", FakeSocket)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: pytest.fail("Probe used DNS"))
    connection = linux_control._LoopbackConnection(host, 8789, timeout=2)
    connection.connect()
    assert connected == addresses
    assert connection.sock is not None


class FakeResponse:
    def __init__(self, body=b'{"status":"ok"}', status=200, headers=None):
        self.status = status
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        self.body = io.BytesIO(body)
        self.fp = None
    def getheader(self, name, default=None):
        return self.headers.get(name, default)
    def read1(self, amount):
        return self.body.read(amount)


def install_response(monkeypatch, response):
    calls = []
    class Connection:
        def __init__(self, host, port, timeout):
            assert host == "127.0.0.1" and port == 8789
            assert timeout <= 2
        def request(self, method, path, headers):
            calls.append((method, path, headers))
        def getresponse(self):
            return response
        def close(self):
            calls.append("close")
    monkeypatch.setattr(linux_control, "_LoopbackConnection", Connection)
    return calls


def test_http_transport_ignores_proxies_no_auth(monkeypatch):
    monkeypatch.setenv("http_proxy", "http://SECRET")
    calls = install_response(monkeypatch, FakeResponse())
    status, body = linux_control._request_json("http://127.0.0.1:8789", "/health/instance", time.monotonic() + 2)
    assert status == 200 and body == {"status": "ok"}
    assert calls[0] == ("GET", "/health/instance", {"Host": "127.0.0.1:8789", "Accept": "application/json", "Connection": "close"})
    assert calls[-1] == "close"


@pytest.mark.parametrize("body,headers", [(b"x" * (linux_control.MAX_RESPONSE_BYTES + 1), {}), (b'{}', {"Content-Length": "999999"}), (b'{}', {"Content-Length": "SECRET"}), (b'{}', {"Content-Encoding": "gzip"}), (b'{}', {"Content-Type": "text/html"}), (b'SECRET-INVALID-JSON', {}), (b'\xff', {})])
def test_http_body_bounded_and_validated(monkeypatch, body, headers):
    calls = install_response(monkeypatch, FakeResponse(body, headers=headers))
    with pytest.raises((ValueError, UnicodeError)):
        linux_control._request_json("http://127.0.0.1:8789", "/health/instance", time.monotonic() + 2)
    assert calls[-1] == "close"


def test_transport_redirect_skips_body(monkeypatch):
    response = FakeResponse(status=302, headers={"Location": "https://SECRET.invalid"})
    response.read1 = lambda *a: pytest.fail("Read redirect/error body")
    calls = install_response(monkeypatch, response)
    assert linux_control._request_json("http://127.0.0.1:8789", "/health/instance", time.monotonic() + 2) == (302, None)
    assert len([c for c in calls if isinstance(c, tuple)]) == 1


def test_absolute_read_deadline(monkeypatch):
    times = iter([0.0, 0.1, 1.1])
    response = FakeResponse(b"{}")
    calls = install_response(monkeypatch, response)
    monkeypatch.setattr(time, "monotonic", lambda: next(times))
    with pytest.raises(TimeoutError):
        linux_control._request_json("http://127.0.0.1:8789", "/health/instance", 1.0)
    assert calls[-1] == "close"
