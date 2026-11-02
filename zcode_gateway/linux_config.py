"""Pure Linux path discovery and bounded, credential-free config diagnostics.

Importing this module never imports Config, initializes a device, or creates state.
Only the explicitly selected YAML config is opened; paths referenced by it are
inspected with stat/access, never opened or enumerated.
"""
from __future__ import annotations

import hashlib
import json
import os
import pwd
import stat
from pathlib import Path

MAX_CONFIG_BYTES = 256 * 1024
MAX_YAML_EVENTS = 8192
MAX_YAML_DEPTH = 32
_XDG = {
    "XDG_CONFIG_HOME": ".config",
    "XDG_DATA_HOME": ".local/share",
    "XDG_CACHE_HOME": ".cache",
    "XDG_STATE_HOME": ".local/state",
}
_ALLOWED = {
    ("server", "host"), ("server", "port"),
    ("accounts", "dir"), ("accounts", "pool_file"),
    ("logging", "capture_dir"), ("store", "sqlite_path"),
}


def xdg_path(variable: str, fallback: str) -> Path:
    """Return an XDG app directory, without creating or resolving anything."""
    value = os.environ.get(variable, "")
    base = Path(value) if value and Path(value).is_absolute() else Path.home() / fallback
    return base / "autozcoder"


def default_config_path() -> Path:
    return xdg_path("XDG_CONFIG_HOME", ".config") / "config.yaml"


def origin_for(host: str, port: int) -> str:
    """Accept only supported loopback binds; never resolve user hostnames."""
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("The gateway must use a supported loopback address.")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("The gateway port must be an integer from 1 to 65535.")
    return f"http://{'[' + host + ']' if host == '::1' else host}:{port}"


def config_identity(path: str | os.PathLike, origin: str) -> str:
    """Opaque identity, tied to canonical config, origin and effective XDG roots."""
    # Canonicalize localhost as a bind name, not as a DNS lookup/address guess.
    from urllib.parse import urlsplit
    try:
        parsed = urlsplit(origin)
        canonical_origin = origin_for(parsed.hostname, parsed.port)
        if parsed.scheme != "http" or parsed.netloc != canonical_origin[7:] or parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.username is not None:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("Invalid loopback gateway origin.") from None
    roots = {name: str(xdg_path(name, fallback).parent.resolve())
             for name, fallback in _XDG.items()}
    roots["XDG_RUNTIME_DIR"] = str(Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}").resolve())
    payload = [str(Path(path).expanduser().resolve()), canonical_origin, roots]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_service_environment() -> None:
    """Reject a terminal context that would silently repoint the singleton unit.

    No environment values (which may be credentials) are included in errors.
    Explicit --config foreground serving/attach-only UI remains available.
    """
    try:
        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (KeyError, OSError):
        raise ValueError("The default user service requires a valid local user home.") from None
    if os.path.normpath(str(Path.home())) != str(home):
        raise ValueError("The default user service requires the user's standard home; use explicit --config foreground mode.")
    for name, fallback in _XDG.items():
        value = os.environ.get(name)
        if value and (not Path(value).is_absolute() or os.path.normpath(value) != str(Path.home() / fallback)):
            raise ValueError("The default user service requires standard XDG directories; use an explicit --config with foreground serve or attach-only ui.")
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and os.path.normpath(runtime) != f"/run/user/{os.getuid()}":
        raise ValueError("The default user service requires the standard user runtime directory; use explicit --config foreground mode.")
    for name, value in os.environ.items():
        if not name.startswith("ZCODE_GATEWAY_"):
            continue
        if name == "ZCODE_GATEWAY_CONFIG" and value and os.path.normpath(value) == str(Path.home() / ".config/autozcoder/config.yaml"):
            continue
        raise ValueError("Runtime configuration overrides are not supported by the default user service; use explicit --config foreground mode.")
    root = os.environ.get("AUTOZCODER_RUNTIME_ROOT")
    if root and os.path.normpath(root) != "/usr/lib64/autozcoder":
        raise ValueError("A custom managed runtime requires explicit --config foreground mode.")


def _metadata(path: Path) -> dict:
    """Return metadata only, with no content reads or directory listing."""
    out = {"exists": False, "kind": "missing", "readable": False,
           "writable": False, "private": None, "owned": None}
    try:
        info = path.stat()
    except FileNotFoundError:
        return out
    except OSError:
        out["kind"] = "inaccessible"
        return out
    out.update(exists=True,
               kind="file" if stat.S_ISREG(info.st_mode) else "directory" if stat.S_ISDIR(info.st_mode) else "special",
               readable=os.access(path, os.R_OK), writable=os.access(path, os.W_OK),
               private=not bool(info.st_mode & 0o077), owned=info.st_uid == os.getuid())
    return out


def _selected_yaml(text: str) -> dict:
    """Parse bounded events, retaining only allowlisted scalar settings.

    Unlike safe_load this never constructs credential/account sections, and it
    disallows aliases/anchors and deep documents to avoid resource amplification.
    PyYAML is imported only when an existing config needs inspection.
    """
    import yaml
    events = iter(yaml.parse(text, Loader=yaml.SafeLoader))
    count = 0
    values = {}

    def take():
        nonlocal count
        event = next(events)
        count += 1
        if count > MAX_YAML_EVENTS or getattr(event, "anchor", None) or isinstance(event, yaml.events.AliasEvent):
            raise ValueError
        return event

    def node(event, path=(), depth=0):
        if depth > MAX_YAML_DEPTH:
            raise ValueError
        if isinstance(event, yaml.events.ScalarEvent):
            if path in _ALLOWED:
                if path in values or len(event.value) > 4096:
                    raise ValueError
                values[path] = event.value
            return
        if isinstance(event, yaml.events.MappingStartEvent):
            keys = set()
            while True:
                key = take()
                if isinstance(key, yaml.events.MappingEndEvent):
                    return
                if not isinstance(key, yaml.events.ScalarEvent):
                    raise ValueError
                # Retain only selected schema names, never account identifiers.
                relevant = depth < 2 and (path == () and key.value in {p[0] for p in _ALLOWED} or path and (path[0], key.value) in _ALLOWED)
                if relevant:
                    if key.value in keys:
                        raise ValueError
                    keys.add(key.value)
                child_path = path + (key.value,) if relevant else ("<ignored>",) * 3
                child = take()
                if child_path in _ALLOWED and not isinstance(child, yaml.events.ScalarEvent):
                    raise ValueError
                if depth == 0 and relevant and not isinstance(child, yaml.events.MappingStartEvent):
                    raise ValueError
                node(child, child_path, depth + 1)
        elif isinstance(event, yaml.events.SequenceStartEvent):
            while True:
                child = take()
                if isinstance(child, yaml.events.SequenceEndEvent):
                    return
                node(child, ("<ignored>",) * 3, depth + 1)
        else:
            raise ValueError

    if not isinstance(take(), yaml.events.StreamStartEvent) or not isinstance(take(), yaml.events.DocumentStartEvent):
        raise ValueError
    root = take()
    if not isinstance(root, yaml.events.MappingStartEvent):
        raise ValueError
    node(root)
    if not isinstance(take(), yaml.events.DocumentEndEvent) or not isinstance(take(), yaml.events.StreamEndEvent):
        raise ValueError
    return values


def inspect_config(path: str | os.PathLike | None = None) -> dict:
    """Inspect config/server/path metadata without loading application state.

    All failures use fixed messages. Paths and unvalidated scalar values never
    appear in the result; diagnostics may safely serialize this entire dict.
    """
    out = {"schema_version": 1, "ok": False, "code": "config_missing",
           "message": "Configuration is missing; launch the dashboard or foreground server to set up.",
           "path": {}, "server": None, "paths": {}, "checks": []}
    try:
        selected = Path(path).expanduser().absolute() if path is not None else default_config_path()
        out["path"] = _metadata(selected)
        if out["path"]["exists"] and out["path"]["kind"] != "file":
            out.update(code="config_not_regular", message="Configuration must be a regular file.")
            return out
        fd = os.open(selected, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except FileNotFoundError:
        return out
    except (OSError, ValueError, TypeError, RuntimeError):
        out.update(code="config_unreadable", message="Configuration cannot be read.")
        return out
    try:
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                out.update(code="config_not_regular", message="Configuration must be a regular file.")
                return out
            if info.st_size > MAX_CONFIG_BYTES:
                out.update(code="config_too_large", message="Configuration exceeds the diagnostic size limit.")
                return out
            data = stream.read(MAX_CONFIG_BYTES + 1)
        if len(data) > MAX_CONFIG_BYTES:
            out.update(code="config_too_large", message="Configuration exceeds the diagnostic size limit.")
            return out
        values = _selected_yaml(data.decode("utf-8"))
    except ImportError:
        out.update(code="yaml_unavailable", message="The YAML inspection dependency is not installed.")
        return out
    except Exception:
        out.update(code="config_invalid", message="Configuration is malformed or unsupported by safe diagnostics.")
        return out
    try:
        host = values.get(("server", "host"), "127.0.0.1")
        raw_port = values.get(("server", "port"), "8788")
        # Match common YAML integer spellings but not bool/float or exponent values.
        import re
        if not re.fullmatch(r"[+]?[0-9]{1,5}", raw_port):
            raise ValueError
        port = int(raw_port)
        origin = origin_for(host, port)
    except (ValueError, TypeError):
        out.update(code="server_invalid", message="Configuration must specify a supported loopback bind and a valid port.")
        return out
    out["server"] = {"host": host, "port": port, "origin": origin}
    defaults = {"accounts_dir": (("accounts", "dir"), "./accounts"),
                "pool_file": (("accounts", "pool_file"), "./pool.json"),
                "capture_dir": (("logging", "capture_dir"), "./logs"),
                "sqlite_path": (("store", "sqlite_path"), "./gateway.db")}
    for name, (key, default) in defaults.items():
        raw = values.get(key, default)
        # Never expand arbitrary environment variables into diagnostic paths.
        if not raw or "\x00" in raw or "$" in raw or len(raw) > 4096:
            out["paths"][name] = {"kind": "unsupported", "exists": False}
            out["checks"].append({"code": "path_unsupported", "status": "warning", "message": "A configured path cannot be inspected safely."})
            continue
        target = Path(raw).expanduser()
        if not target.is_absolute():
            target = selected.parent / target
        out["paths"][name] = _metadata(target)
    out.update(ok=True, code="config_ok", message="Configuration server and path metadata inspected.")
    if not out["path"].get("private") or not out["path"].get("owned"):
        out["checks"].append({"code": "config_permissions", "status": "warning", "message": "Configuration should be owned by this user and readable only by this user."})
    return out
