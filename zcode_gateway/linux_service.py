"""Foreground Linux backend. No GUI imports and no detached daemon ownership."""
from __future__ import annotations

import errno
import hashlib
import os
import secrets
import signal
import threading
from contextlib import contextmanager
import socket
import sys
import tempfile
from pathlib import Path

EXIT_FAILURE = 1
EXIT_CONFLICT = 73
EXIT_CONFIG = 78
DRAIN_SECONDS = 5
CLEANUP_SECONDS = 12  # drain + cleanup must fit TimeoutStopSec=20


def bootstrap_config() -> Path:
    """Atomically initialize private XDG state on deliberate startup only."""
    import yaml
    from .config import ClientKey, Config
    from .linux_config import default_config_path, xdg_path
    path = default_config_path()
    if path.is_file():
        return path
    if path.exists() or path.is_symlink():
        raise ValueError("existing configuration is not a readable regular file")
    config_dir = path.parent
    data_dir = xdg_path("XDG_DATA_HOME", ".local/share")
    config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    cfg = Config()
    cfg.server.port = 8789
    cfg.server.admin_key = "gw-admin-" + secrets.token_hex(24)
    cfg.server.client_keys = [ClientKey(key="gw-client-" + secrets.token_hex(24), name="desktop")]
    cfg.accounts.dir = str(data_dir / "accounts")
    cfg.accounts.pool_file = str(data_dir / "pool.json")
    cfg.logging.capture_dir = str(data_dir / "logs")
    cfg.store.sqlite_path = str(data_dir / "gateway.db")
    cfg.update.enabled = False
    cfg.generator.enabled = False
    cfg.models.aliases = {"glm-5.3": "GLM-5.3", "glm-5.3-flash": "GLM-5.3-Flash"}
    fd, temporary = tempfile.mkstemp(prefix=".config-", dir=config_dir)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            yaml.safe_dump(cfg.model_dump(), fh, sort_keys=False)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.link(temporary, path)  # never overwrite a competing launch
        except FileExistsError:
            if not path.is_file():
                raise ValueError("configuration changed during initialization") from None
    finally:
        os.unlink(temporary)
    return path


class InstanceLock:
    """Per-config flock compatible with the former embedded desktop backend."""
    def __init__(self, config_path: Path):
        from .linux_config import xdg_path
        digest = hashlib.sha256(str(Path(config_path).resolve()).encode()).hexdigest()[:16]
        self.path = xdg_path("XDG_CACHE_HOME", ".cache") / f"instance-{digest}.lock"
        self.file = None

    def acquire(self) -> bool:
        import fcntl
        if self.file is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        self.file = os.fdopen(fd, "a", encoding="utf-8")
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.close()
            return False
        return True

    def close(self) -> None:
        if self.file is not None:
            self.file.close()
            self.file = None


def _error(message: str, code: int) -> int:
    # Exception text can contain YAML values, paths or environment secrets.
    print(message, file=sys.stderr)
    return code


def serve(config_path: str | Path | None = None, user_service: bool = False) -> int:
    """Run Uvicorn on this thread, with its own single signal handler owner."""
    from .linux_config import (config_identity, default_config_path, origin_for,
                               validate_service_environment)
    from .linux_runtime import configure_runtime, generator_python
    lock = None
    listener = None
    try:
        # Runtime setup precedes Config construction/device discovery. Validation
        # must precede bootstrap so invalid singleton environments write nothing.
        os.environ["AUTOZCODER_PACKAGE_MANAGED"] = "1"
        if user_service:
            validate_service_environment()
            if config_path is not None and Path(config_path).expanduser().resolve() != default_config_path().resolve():
                raise ValueError("custom configuration requires foreground mode")
        configure_runtime()
        os.umask(0o077)
        path = Path(config_path).expanduser().resolve() if config_path is not None else bootstrap_config()
        if not path.is_file():
            return _error("Configuration file is missing or unreadable.", EXIT_CONFIG)
        # Inspect the intended file BEFORE load_config, which otherwise falls
        # back to cwd/environment on a missing path. Recheck the actual identity.
        import yaml
        with path.open(encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
        if not isinstance(raw, dict):
            raise ValueError("configuration must be a mapping")
        from .config import load_config
        cfg = load_config(path)
        if cfg._config_file != path.resolve():
            raise ValueError("configuration changed during startup")
        origin = origin_for(cfg.server.host, cfg.server.port)
        cfg.update.enabled = False
        bundled_gen = Path(__file__).resolve().parent.parent / "generator"
        if (bundled_gen / "main.py").is_file():
            if not cfg.generator.zcodeauto_dir:
                cfg.generator.zcodeauto_dir = str(bundled_gen)
            if not cfg.generator.python_exe:
                cfg.generator.python_exe = generator_python()
        os.environ["ZCODE_GATEWAY_CONFIG"] = str(path)
    except Exception:
        return _error("Invalid Linux service configuration or environment; use doctor for safe checks.", EXIT_CONFIG)

    try:
        lock = InstanceLock(path)
        if not lock.acquire():
            return _error("Another AutoZcoder backend owns this configuration.", EXIT_CONFLICT)
        # Retain the bound socket until Uvicorn takes it: no probe/bind race and
        # never adopt/kill the listener at the requested port. localhost uses a
        # numeric loopback rather than host-resolution/environment surprises.
        host = "127.0.0.1" if cfg.server.host == "localhost" else cfg.server.host
        family = socket.AF_INET6 if host == "::1" else socket.AF_INET
        listener = socket.socket(family, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, cfg.server.port))
        import uvicorn
        from .app import create_app
        from . import __version__
        app = create_app(cfg)
        app.state.linux_mode = "user-service" if user_service else "foreground"
        app.state.linux_managed_default = user_service and path.resolve() == default_config_path().resolve()
        app.state.linux_identity = {
            "schema_version": 1, "product": "autozcoder-linux", "version": __version__,
            "instance_id": secrets.token_hex(16), "config_id": config_identity(path, origin),
            "mode": app.state.linux_mode,
        }
        intentional = False

        def stop(*_):
            nonlocal intentional
            intentional = True
            app.state.stopping = True
            state = getattr(app.state, "app_state", None)
            if state is not None:
                state.begin_stopping()
            server.should_exit = True

        class ForegroundServer(uvicorn.Server):
            @contextmanager
            def capture_signals(self):
                # Uvicorn normally re-raises captured signals at exit (SIGTERM
                # would become 143). This foreground owner restores handlers
                # without replay so an intentional service stop exits zero.
                if threading.current_thread() is not threading.main_thread():
                    raise RuntimeError("foreground service requires the main thread")
                previous = {sig: signal.signal(sig, self.handle_exit)
                            for sig in (signal.SIGINT, signal.SIGTERM)}
                try:
                    yield
                finally:
                    for sig, handler in previous.items():
                        signal.signal(sig, handler)

            def handle_exit(self, sig, frame):
                super().handle_exit(sig, frame)
                stop()

        server = ForegroundServer(uvicorn.Config(
            app, host=host, port=cfg.server.port, access_log=False,
            timeout_graceful_shutdown=DRAIN_SECONDS, forwarded_allow_ips="",
            proxy_headers=False, lifespan="on"))
        # Installed BEFORE run/startup; dashboard shutdown never needs hard exit.
        app.state.desktop_shutdown = stop
        try:
            server.run(sockets=[listener])
        except (KeyboardInterrupt, SystemExit):
            if not intentional:
                return _error("Linux backend startup failed.", EXIT_FAILURE)
        if getattr(app.state, "cleanup_failed", False):
            # An explicit dashboard Stop must not trigger Restart=on-failure.
            # systemd's cgroup handles surviving children on service exit.
            return _error("Linux backend cleanup exceeded its deadline; process containment is required.",
                          0 if intentional else EXIT_FAILURE)
        return 0 if intentional else EXIT_FAILURE
    except OSError as exc:
        code = EXIT_CONFLICT if exc.errno in (errno.EADDRINUSE, errno.EADDRNOTAVAIL) else EXIT_FAILURE
        return _error("Cannot bind the local gateway listener; no existing process was changed.", code)
    except Exception:
        return _error("Linux backend failed unexpectedly.", EXIT_FAILURE)
    finally:
        if listener is not None:
            listener.close()
        if lock is not None:
            lock.close()
