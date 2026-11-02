"""Linux CLI diagnostics must be inert before application initialization."""
from __future__ import annotations

import builtins
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import types

import pytest

from zcode_gateway import linux_cli, linux_config, linux_control


@pytest.fixture
def context(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(linux_config.pwd, "getpwuid", lambda uid: types.SimpleNamespace(pw_dir=str(home)))
    for name in tuple(os.environ):
        if name.startswith("ZCODE_GATEWAY_") or name.startswith("XDG_") or name == "AUTOZCODER_RUNTIME_ROOT":
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(linux_control.ServiceController, "status", lambda self: {
        "schema_version": 1, "ok": False, "code": "unit_stopped", "message": "Service stopped.",
        "properties": {"LoadState": "loaded", "ActiveState": "inactive", "UnitFileState": "disabled"}})
    monkeypatch.setattr(linux_cli, "_dependencies", lambda: [])
    return home


def config_at(home, text=None):
    path = home / ".config/autozcoder/config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text or "server:\n  host: 127.0.0.1\n  port: 8789\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def test_path_discovery_is_pure_and_matches_xdg(context, monkeypatch):
    before = list(context.rglob("*"))
    expected = context / ".config/autozcoder/config.yaml"
    assert linux_config.default_config_path() == expected
    assert linux_config.xdg_path("XDG_DATA_HOME", ".local/share") == context / ".local/share/autozcoder"
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative")
    assert linux_config.default_config_path() == expected
    monkeypatch.setenv("XDG_CONFIG_HOME", str(context / "custom"))
    assert linux_config.default_config_path() == context / "custom/autozcoder/config.yaml"
    assert list(context.rglob("*")) == before


@pytest.mark.parametrize("host,port,expected", [("127.0.0.1", 8789, "http://127.0.0.1:8789"), ("localhost", 8789, "http://localhost:8789"), ("::1", 8789, "http://[::1]:8789")])
def test_origin_loopback(host, port, expected):
    assert linux_config.origin_for(host, port) == expected


@pytest.mark.parametrize("host,port", [("0.0.0.0", 8789), ("127.0.0.2", 8789), ("evil.invalid", 8789), ("::", 8789), ("[::1]", 8789), ("127.0.0.1", True), ("127.0.0.1", "8789"), ("127.0.0.1", 0), ("127.0.0.1", 65536)])
def test_origin_rejects_without_echo(host, port):
    with pytest.raises(ValueError) as error:
        linux_config.origin_for(host, port)
    assert "evil.invalid" not in str(error.value)


def test_identity_tied_to_canonical_path_origin_context(context, monkeypatch):
    path = config_at(context)
    a = linux_config.config_identity(path, "http://127.0.0.1:8789")
    assert len(a) == 64
    assert linux_config.config_identity(path.parent / "../autozcoder/config.yaml", "http://127.0.0.1:8789/") == a
    assert linux_config.config_identity(path, "http://127.0.0.1:8790") != a
    assert linux_config.config_identity(path.with_name("different.yaml"), "http://127.0.0.1:8789") != a
    monkeypatch.setenv("XDG_CACHE_HOME", str(context / "cache"))
    assert linux_config.config_identity(path, "http://127.0.0.1:8789") != a
    with pytest.raises(ValueError):
        linux_config.config_identity(path, "http://evil.invalid:8789")


@pytest.mark.parametrize("name,value", [("XDG_CONFIG_HOME", "/tmp/custom-secret-value"), ("XDG_DATA_HOME", "relative"), ("XDG_CACHE_HOME", "/tmp/cache-secret-value"), ("XDG_RUNTIME_DIR", "/tmp/runtime-secret-value"), ("ZCODE_GATEWAY_SERVER__ADMIN_KEY", "private-key-value"), ("ZCODE_GATEWAY_CONFIG", "/tmp/config-secret-value"), ("AUTOZCODER_RUNTIME_ROOT", "/tmp/runtime-secret-value")])
def test_singleton_environment_rejection_sanitized(context, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError) as error:
        linux_config.validate_service_environment()
    assert value not in str(error.value)


def test_home_override_rejected(context, monkeypatch):
    monkeypatch.setenv("HOME", str(context / "PRIVATE-HOME"))
    with pytest.raises(ValueError) as error:
        linux_config.validate_service_environment()
    assert "PRIVATE-HOME" not in str(error.value)


def test_standard_environment_is_allowed(context, monkeypatch):
    for name, fallback in linux_config._XDG.items():
        monkeypatch.setenv(name, str(context / fallback))
    monkeypatch.setenv("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    monkeypatch.setenv("ZCODE_GATEWAY_CONFIG", str(context / ".config/autozcoder/config.yaml"))
    monkeypatch.setenv("AUTOZCODER_RUNTIME_ROOT", "/usr/lib64/autozcoder")
    linux_config.validate_service_environment()


def test_inspection_never_reads_account_device_or_db(context, monkeypatch):
    path = config_at(context, """server:
  host: ::1
  port: 8789
  admin_key: DO-NOT-PRINT-ADMIN
  client_keys:
    - key: DO-NOT-PRINT-CLIENT
      name: PRIVATE-ACCOUNT-NAME
accounts:
  dir: ./accounts
  pool_file: ./pool.json
  providers_json: ~/.zcode/v2/credentials.json
store:
  sqlite_path: ./gateway.db
upstream:
  fingerprint:
    device_mid: PRIVATE-DEVICE-ID
  base_url: https://SECRET-UPSTREAM.invalid
""")
    for item in ("gateway.db", "pool.json"):
        (path.parent / item).write_text("PRIVATE-STATE")
    (path.parent / "accounts").mkdir()
    (path.parent / "accounts/account.json").write_text("PRIVATE-ACCOUNT")
    opened = []
    real_os_open = os.open
    real_builtin_open = builtins.open

    def guarded_os_open(value, flags, *args, **kwargs):
        opened.append(Path(value))
        assert Path(value) == path
        assert flags & os.O_NONBLOCK
        return real_os_open(value, flags, *args, **kwargs)

    def guarded_open(value, *args, **kwargs):
        pytest.fail("Diagnostic attempted a general content read")

    monkeypatch.setattr(os, "open", guarded_os_open)
    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: pytest.fail("Diagnostic DNS lookup"))
    result = linux_config.inspect_config(path)
    assert result["ok"]
    assert result["server"]["origin"] == "http://[::1]:8789"
    assert result["paths"]["sqlite_path"]["exists"]
    assert opened == [path]
    output = json.dumps(result)
    assert "PRIVATE" not in output and "DO-NOT-PRINT" not in output and "SECRET" not in output
    assert str(context) not in output
    assert result["paths"]["accounts_dir"]["kind"] == "directory"


@pytest.mark.parametrize("text", ["server: [SECRET-LEAK", "server: null", "server:\n  port: true\n", "server:\n  port: 8789.0\n", "server:\n  host: SECRET-HOST\n", "server:\n  port: 8789\n  port: 8788\n", "server: &s {port: 8789}\naccounts: *s\n", "server:\n  host: {secret: SECRET-LEAK}\n", "server:\n  port: 8789\n---\nsecret: SECRET-LEAK\n", "- SECRET-LEAK\n", "" ])
def test_malformed_config_is_sanitized(context, text):
    path = config_at(context)
    path.write_text(text)
    result = linux_config.inspect_config(path)
    assert not result["ok"]
    assert "SECRET" not in json.dumps(result)
    assert str(path) not in json.dumps(result)


def test_bounded_yaml_inspection(context):
    path = config_at(context)
    path.write_text("secret: " + "x" * (linux_config.MAX_CONFIG_BYTES + 1))
    assert linux_config.inspect_config(path)["code"] == "config_too_large"
    path.write_text("secret: " + "[" * 40 + "0" + "]" * 40)
    assert linux_config.inspect_config(path)["code"] == "config_invalid"
    path.write_text("secret: [" + ",".join("0" for _ in range(9000)) + "]")
    assert linux_config.inspect_config(path)["code"] == "config_invalid"


def test_missing_and_special_config_are_inert(context):
    missing = context / "not-created/config.yaml"
    assert linux_config.inspect_config(missing)["code"] == "config_missing"
    assert not missing.parent.exists()
    fifo = context / "config-fifo"
    os.mkfifo(fifo)
    assert linux_config.inspect_config(fifo)["code"] == "config_not_regular"


def test_relative_paths_and_permissions_metadata(context):
    path = config_at(context)
    path.chmod(0o644)
    result = linux_config.inspect_config(path)
    assert result["ok"]
    assert result["checks"][0]["code"] == "config_permissions"
    path.chmod(0o600)
    path.write_text("server: {port: 8789}\nstore: {sqlite_path: '$SECRET_DB_PATH'}\n")
    assert linux_config.inspect_config(path)["checks"][0]["code"] == "path_unsupported"


def test_doctor_offline_no_probe_or_mutation(context, monkeypatch, capsys):
    path = config_at(context)
    before = {str(p): (p.stat().st_mtime_ns, p.read_bytes() if p.is_file() else None) for p in context.rglob("*")}
    monkeypatch.setattr(linux_control, "probe_instance", lambda *a, **k: pytest.fail("Offline doctor probed network"))
    monkeypatch.setattr(linux_control.ServiceController, "action", lambda *a: pytest.fail("Diagnostic mutated unit"))
    monkeypatch.setattr(linux_control.ServiceController, "reload", lambda *a: pytest.fail("Diagnostic reloaded manager"))
    assert linux_cli.main(["--json", "doctor"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["schema_version"] == 1 and report["command"] == "doctor"
    assert "probe" not in report
    after = {str(p): (p.stat().st_mtime_ns, p.read_bytes() if p.is_file() else None) for p in context.rglob("*")}
    assert before == after
    assert str(path) not in json.dumps(report)


def test_status_local_verified_probe_with_matching_unit(context, monkeypatch, capsys):
    path = config_at(context)
    monkeypatch.setattr(linux_control.ServiceController, "status", lambda self: {"ok": True, "code": "unit_active", "message": "Active.", "properties": {"ActiveState": "active", "LoadState": "loaded", "UnitFileState": "disabled"}})
    calls = []
    def probe(origin, expected):
        calls.append((origin, expected))
        return {"ok": True, "code": "instance_healthy", "message": "Healthy.", "state": "healthy", "instance": {"mode": "user-service"}}
    monkeypatch.setattr(linux_control, "probe_instance", probe)
    assert linux_cli.main(["status", "--json"]) == 0
    assert calls == [("http://127.0.0.1:8789", linux_config.config_identity(path, "http://127.0.0.1:8789"))]
    assert json.loads(capsys.readouterr().out)["ok"]


def test_explicit_config_online_never_inspects_unit(context, monkeypatch, capsys):
    path = config_at(context)
    monkeypatch.setenv("ZCODE_GATEWAY_SERVER__ADMIN_KEY", "PRIVATE-OVERRIDE")
    monkeypatch.setattr(linux_control.ServiceController, "status", lambda *a: pytest.fail("Custom diagnostic selected default unit"))
    monkeypatch.setattr(linux_control, "probe_instance", lambda *a: {"ok": True, "code": "instance_healthy", "message": "Healthy.", "instance": {"mode": "foreground"}})
    assert linux_cli.main(["--config", str(path), "doctor", "--online", "--json"]) == 0
    assert "PRIVATE" not in capsys.readouterr().out


def test_default_mismatched_xdg_skips_probe(context, monkeypatch, capsys):
    custom = context / "custom"
    path = custom / "autozcoder/config.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("server: {port: 8789}")
    path.chmod(0o600)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(custom))
    monkeypatch.setattr(linux_control, "probe_instance", lambda *a: pytest.fail("Mismatched service context attached listener"))
    assert linux_cli.main(["status", "--json"]) == 1
    assert any(c["code"] == "service_environment" for c in json.loads(capsys.readouterr().out)["checks"])


@pytest.mark.parametrize("argv", [["start", "--config", "SECRET"], ["--config", "SECRET", "autostart", "enable"], ["serve", "--config", "SECRET", "--user-service"], ["doctor", "--headless"], ["status", "--online"], ["autostart"], ["status", "enable"], ["SECRET-INVALID-COMMAND"], ["--SECRET-INVALID-FLAG"]])
def test_invalid_invocation_redacted(context, capsys, argv):
    assert linux_cli.main(argv) == 2
    captured = capsys.readouterr()
    assert "SECRET" not in captured.err + captured.out
    assert "Invalid AutoZcoder invocation" in captured.err


def test_late_launch_dispatch_and_flag_order(context, monkeypatch):
    calls = []
    fake_service = types.ModuleType("zcode_gateway.linux_service")
    fake_service.serve = lambda **kw: calls.append(("serve", kw)) or 73
    fake_desktop = types.ModuleType("zcode_gateway.linux_desktop")
    fake_desktop.open_dashboard = lambda **kw: calls.append(("ui", kw)) or 0
    monkeypatch.setitem(sys.modules, "zcode_gateway.linux_service", fake_service)
    monkeypatch.setitem(sys.modules, "zcode_gateway.linux_desktop", fake_desktop)
    assert linux_cli.main([]) == 0
    assert linux_cli.main(["--headless"]) == 73
    assert linux_cli.main(["--user-service", "serve"]) == 73
    assert linux_cli.main(["--config", "/tmp/custom", "ui"]) == 0
    assert calls == [("ui", {"config_path": None}), ("serve", {"config_path": None, "user_service": False}), ("serve", {"config_path": None, "user_service": True}), ("ui", {"config_path": Path("/tmp/custom")})]


def test_autostart_only_requested_action(context, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(linux_control.ServiceController, "action", lambda self, verb: calls.append(verb) or {"ok": True, "code": "action_ok", "message": "Changed future autostart."})
    assert linux_cli.main(["--json", "autostart", "disable"]) == 0
    assert calls == ["disable"]
    assert json.loads(capsys.readouterr().out)["command"] == "autostart"


def test_unexpected_diagnostic_failure_redacted(context, monkeypatch, capsys):
    monkeypatch.setattr(linux_cli, "inspect_config", lambda *a: (_ for _ in ()).throw(RuntimeError("SECRET-PARSER-INPUT")))
    assert linux_cli.main(["doctor", "--json"]) == 2
    assert "SECRET" not in capsys.readouterr().out


def test_fresh_diagnostics_block_runtime_imports_and_writes(tmp_path):
    home = tmp_path / "isolated"
    home.mkdir()
    root = Path(__file__).resolve().parents[1]
    script = r'''
import importlib.abc, json, os, pathlib, socket, subprocess, sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in ('zcode_gateway.config','zcode_gateway.linux_desktop','zcode_gateway.linux_service','zcode_gateway.app','zcode_gateway.state','zcode_gateway.zcode_device','yaml','sqlite3','webview','gi','uvicorn','fastapi','pydantic','httpx'):
            raise AssertionError('forbidden import: ' + fullname)
sys.meta_path.insert(0, Block())
def blocked(*a,**k): raise AssertionError('forbidden side effect')
pathlib.Path.mkdir = blocked
socket.getaddrinfo = blocked
class ForbiddenSocket(socket.socket):
    def __init__(self, *a, **k): blocked()
socket.socket = ForbiddenSocket
subprocess.run = lambda *a, **k: subprocess.CompletedProcess(a[0], 1, '', '')
from zcode_gateway.linux_cli import main
assert main(['doctor','--json']) == 1
assert main(['--version']) == 0
'''
    env = {**os.environ, "HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(root)}
    for name in tuple(env):
        if name.startswith("XDG_") or name.startswith("ZCODE_GATEWAY_") or name == "AUTOZCODER_RUNTIME_ROOT":
            env.pop(name)
    result = subprocess.run([sys.executable, "-c", script], cwd=root, env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert "config_missing" in result.stdout
    assert list(home.rglob("*")) == []
