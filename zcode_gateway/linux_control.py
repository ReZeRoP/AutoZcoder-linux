"""Exact user-unit controls and bounded, unauthenticated loopback identity probes.

Identity is not authentication. UI attachment must additionally perform an
admin-authenticated dashboard handshake. Diagnostics never request credentials,
readiness/account statistics, settings, journal messages or upstream endpoints.
"""
from __future__ import annotations

import http.client
import json
import os
import pwd
import re
import socket
import subprocess
import time
from urllib.parse import urlsplit

from .linux_config import origin_for, validate_service_environment

UNIT = "autozcoder.service"
SYSTEMCTL = "/usr/bin/systemctl"
CONTROL_TIMEOUT = 8.0
PROBE_TIMEOUT = 2.0
MAX_RESPONSE_BYTES = 16384
PROPERTIES = ("LoadState", "ActiveState", "SubState", "UnitFileState", "MainPID", "Result", "ExecMainStatus")
_ACTIONS = frozenset(("start", "stop", "restart", "enable", "disable"))
_ENUMS = {
    "LoadState": {"loaded", "not-found", "error", "masked", "bad-setting", "merged", "stub"},
    "ActiveState": {"active", "inactive", "failed", "activating", "deactivating", "reloading", "maintenance", "refreshing"},
    "SubState": {"running", "dead", "failed", "exited", "start", "start-pre", "start-post", "stop", "stop-sigterm", "stop-sigkill", "stop-post", "auto-restart", "auto-restart-queued", "reload", "cleaning", "condition", "final-sigterm", "final-sigkill"},
    "UnitFileState": {"enabled", "enabled-runtime", "disabled", "static", "indirect", "masked", "masked-runtime", "linked", "linked-runtime", "alias", "generated", "transient", "bad", "not-found"},
    "Result": {"success", "resources", "timeout", "exit-code", "signal", "core-dump", "watchdog", "start-limit-hit", "protocol", "oom-kill", "exec-condition", "assert", "condition", "skipped"},
}


def _result(ok: bool, code: str, message: str, **extra) -> dict:
    return {"schema_version": 1, "ok": ok, "code": code, "message": message, **extra}


class ServiceController:
    """Side-effect-free constructor; all subprocess argv are fixed/allowlisted."""

    def __init__(self, *, timeout: float = CONTROL_TIMEOUT):
        if not isinstance(timeout, (int, float)) or not 0 < timeout <= 60:
            raise ValueError("Invalid service command timeout.")
        self.timeout = float(timeout)

    def _run(self, args: list[str]) -> dict:
        # Never inherit arbitrary client keys/proxy variables or a shell-provided
        # bus pointing at a different manager. This service is per real user.
        runtime = f"/run/user/{os.getuid()}"
        try:
            home = pwd.getpwuid(os.getuid()).pw_dir
        except (KeyError, OSError):
            return _result(False, "manager_unavailable", "The local user-service context is unavailable.")
        env = {"PATH": "/usr/bin:/bin", "HOME": home,
               "LANG": "C", "LC_ALL": "C", "XDG_RUNTIME_DIR": runtime,
               "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime}/bus",
               "SYSTEMD_PAGER": "", "SYSTEMD_COLORS": "0"}
        try:
            proc = subprocess.run([SYSTEMCTL, "--user", "--no-pager", "--no-ask-password", *args],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                                  timeout=self.timeout, env=env, check=False)
        except FileNotFoundError:
            return _result(False, "systemctl_unavailable", "User service control is not installed; use explicit foreground serve.")
        except subprocess.TimeoutExpired:
            return _result(False, "manager_timeout", "The user service manager did not respond before the timeout.")
        except OSError:
            return _result(False, "manager_unavailable", "The user service manager is unavailable; use explicit foreground serve.")
        if len(proc.stdout or "") > MAX_RESPONSE_BYTES:
            return _result(False, "manager_response_invalid", "The user service manager returned an invalid response.")
        return {"returncode": proc.returncode, "stdout": proc.stdout or ""}

    def status(self) -> dict:
        """Read only selected scalar properties; never query status/journal text."""
        result = self._run(["show", "--property=" + ",".join(PROPERTIES), UNIT])
        if "returncode" not in result:
            return {**result, "properties": {}}
        properties = {}
        for line in result["stdout"].splitlines():
            key, sep, value = line.partition("=")
            if not sep or key not in PROPERTIES:
                continue
            if key in properties:
                return _result(False, "manager_response_invalid", "The user service manager returned an invalid response.", properties={})
            if key in ("MainPID", "ExecMainStatus"):
                properties[key] = int(value) if re.fullmatch(r"[0-9]{1,10}", value) else None
            else:
                properties[key] = value if value in _ENUMS[key] else "unknown"
        if properties.get("LoadState") == "not-found":
            return _result(False, "unit_missing", "The AutoZcoder user service unit is not installed.", properties=properties)
        if result["returncode"] != 0:
            return _result(False, "manager_unavailable", "The user service manager is unavailable; use explicit foreground serve.", properties={})
        if "LoadState" not in properties or "ActiveState" not in properties:
            return _result(False, "manager_response_invalid", "The user service manager returned an invalid response.", properties={})
        if properties["LoadState"] != "loaded":
            return _result(False, "unit_unavailable", "The AutoZcoder user service unit cannot be loaded.", properties=properties)
        active = properties["ActiveState"]
        if active == "active":
            return _result(True, "unit_active", "The AutoZcoder user service is active.", properties=properties)
        if active == "failed":
            return _result(False, "unit_failed", "The AutoZcoder user service has failed.", properties=properties)
        return _result(False, "unit_stopped", "The AutoZcoder user service is stopped or transitioning.", properties=properties)

    def action(self, verb: str) -> dict:
        """Explicit lifecycle/autostart mutation for only the default user unit."""
        if not isinstance(verb, str) or verb not in _ACTIONS:
            raise ValueError("Unsupported AutoZcoder user-service action.")
        try:
            validate_service_environment()
        except ValueError:
            return _result(False, "service_environment", "The default user service requires the standard environment; use explicit --config foreground mode.", action=verb)
        result = self._run([verb, UNIT])
        if "returncode" not in result:
            return {**result, "action": verb}
        if result["returncode"] != 0:
            # A bounded read separates missing unit/manager without echoing any
            # manager error message or making a second mutation attempt.
            current = self.status()
            code = current["code"] if current["code"] in ("unit_missing", "manager_unavailable", "systemctl_unavailable", "manager_timeout", "unit_unavailable") else "action_failed"
            return _result(False, code, "The requested AutoZcoder user-service action could not complete.", action=verb)
        message = {"enable": "Login autostart enabled; the current service was not started or restarted.",
                   "disable": "Login autostart disabled; the current service was not stopped.",
                   "start": "AutoZcoder user-service start requested.",
                   "stop": "AutoZcoder user-service stop requested.",
                   "restart": "AutoZcoder user-service restart requested."}[verb]
        return _result(True, "action_ok", message, action=verb)

    def reload(self) -> dict:
        """Explicit setup-only manager reload; never called by diagnostics."""
        try:
            validate_service_environment()
        except ValueError:
            return _result(False, "service_environment", "The default user service requires the standard environment.")
        result = self._run(["daemon-reload"])
        if "returncode" not in result:
            return result
        return _result(result["returncode"] == 0,
                       "reload_ok" if result["returncode"] == 0 else "reload_failed",
                       "User-manager reload completed." if result["returncode"] == 0 else "User-manager reload could not complete.")


class _DeadlineSocket(socket.socket):
    """Enforce a total deadline even when HTTP headers arrive one byte at a time."""

    def __init__(self, family, kind, *, deadline):
        super().__init__(family, kind)
        self.deadline = deadline

    def _remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        self.settimeout(remaining)

    def recv_into(self, buffer, nbytes=0, flags=0):
        # socket.makefile()/HTTPResponse read through SocketIO.recv_into.
        self._remaining()
        return super().recv_into(buffer, nbytes, flags)

    def sendall(self, data, flags=0):
        self._remaining()
        return super().sendall(data, flags)


class _LoopbackConnection(http.client.HTTPConnection):
    """Connect to numeric loopback directly, without DNS or proxy handling."""

    def connect(self):
        addresses = ("127.0.0.1", "::1") if self.host == "localhost" else (self.host,)
        deadline = time.monotonic() + self.timeout
        last_error = None
        for address in addresses:
            sock = _DeadlineSocket(socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, deadline=deadline)
            try:
                sock.settimeout(max(0.001, deadline - time.monotonic()))
                sock.connect((address, self.port))
                self.sock = sock
                return
            except OSError as exc:
                last_error = exc
                sock.close()
        raise last_error or OSError("Loopback unavailable")


def _request_json(origin: str, path: str, deadline: float) -> tuple[int, object]:
    parsed = urlsplit(origin)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    connection = _LoopbackConnection(parsed.hostname, parsed.port, timeout=remaining)
    try:
        connection.request("GET", path, headers={"Host": parsed.netloc,
                            "Accept": "application/json", "Connection": "close"})
        response = connection.getresponse()
        # Redirects are never followed, and no foreign error body is exposed.
        if response.status != 200:
            return response.status, None
        if response.getheader("Content-Encoding", "identity").lower() not in ("", "identity"):
            raise ValueError
        content_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise ValueError
        length = response.getheader("Content-Length")
        if length is not None and (not length.isdigit() or int(length) > MAX_RESPONSE_BYTES):
            raise ValueError
        body = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            # response.fp retains the socket even after Connection: close detaches
            # it from HTTPConnection; set the underlying raw socket's deadline.
            raw_socket = getattr(getattr(response.fp, "raw", None), "_sock", None)
            if raw_socket is not None:
                raw_socket.settimeout(remaining)
            chunk = response.read1(min(4096, MAX_RESPONSE_BYTES + 1 - len(body)))
            if not chunk:
                break
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError
        return 200, json.loads(body.decode("utf-8"))
    finally:
        connection.close()


def probe_instance(origin: str, expected_config_id: str | None = None) -> dict:
    """Inspect only validated loopback identity and health, without authentication."""
    extra = {"state": "unavailable", "instance": None}
    try:
        parsed = urlsplit(origin)
        normalized = origin_for(parsed.hostname, parsed.port)
        if parsed.scheme != "http" or parsed.netloc != normalized[7:] or parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.username is not None:
            raise ValueError
        if expected_config_id is not None and not re.fullmatch(r"[a-f0-9]{64}", expected_config_id):
            raise ValueError
    except (ValueError, TypeError):
        return _result(False, "origin_invalid", "The probe requires a valid loopback gateway origin.", **extra)
    deadline = time.monotonic() + PROBE_TIMEOUT
    try:
        status, identity = _request_json(normalized, "/health/instance", deadline)
        if status != 200 or not isinstance(identity, dict):
            return _result(False, "instance_foreign", "The local listener did not provide a verified AutoZcoder identity.", state="foreign", instance=None)
        if (type(identity.get("schema_version")) is not int or identity["schema_version"] != 1 or identity.get("product") != "autozcoder-linux"
                or not isinstance(identity.get("version"), str) or not re.fullmatch(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}(?:[-+][a-zA-Z0-9.]{1,32})?", identity["version"])
                or not isinstance(identity.get("instance_id"), str) or not re.fullmatch(r"[a-zA-Z0-9_-]{8,128}", identity["instance_id"])
                or not isinstance(identity.get("config_id"), str) or not re.fullmatch(r"[a-f0-9]{64}", identity["config_id"])
                or identity.get("mode") not in ("user-service", "foreground") or type(identity.get("stopping")) is not bool
                or expected_config_id is not None and identity["config_id"] != expected_config_id):
            return _result(False, "instance_foreign", "The local listener identity does not match the expected gateway.", state="foreign", instance=None)
        safe_identity = {key: identity[key] for key in ("schema_version", "product", "version", "instance_id", "config_id", "mode", "stopping")}
        if identity["stopping"]:
            return _result(False, "instance_stopping", "The verified gateway is stopping.", state="degraded", instance=safe_identity)
        status, health = _request_json(normalized, "/health", deadline)
        healthy = status == 200 and isinstance(health, dict) and health.get("status") == "ok"
        return _result(healthy, "instance_healthy" if healthy else "instance_degraded",
                       "The verified local gateway is healthy." if healthy else "The verified local gateway is degraded or unavailable.",
                       state="healthy" if healthy else "degraded", instance=safe_identity)
    except (OSError, TimeoutError, http.client.HTTPException):
        return _result(False, "instance_unavailable", "The local gateway did not respond before the timeout or is unavailable.", **extra)
    except (ValueError, UnicodeError, RecursionError):
        return _result(False, "instance_foreign", "The local listener returned an invalid gateway identity.", state="foreign", instance=None)
