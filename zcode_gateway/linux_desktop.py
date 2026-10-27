"""Linux desktop entry point with private XDG state and a native GTK window."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import logging
import os
import secrets
import signal
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import yaml

from . import __version__
from .config import ClientKey, Config, load_config
from .linux_runtime import configure_runtime, generator_python

log = logging.getLogger("zcode_gateway.desktop")


def xdg_dir(variable: str, fallback: str) -> Path:
    value = os.environ.get(variable, "")
    base = Path(value) if value and Path(value).is_absolute() else Path.home() / fallback
    directory = base / "autozcoder"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory


def bootstrap_config() -> Path:
    """Create an independent desktop config; never import existing accounts."""
    config_dir = xdg_dir("XDG_CONFIG_HOME", ".config")
    data_dir = xdg_dir("XDG_DATA_HOME", ".local/share")
    path = config_dir / "config.yaml"
    if not path.exists():
        cfg = Config()
        cfg.server.port = 8789
        cfg.server.admin_key = "gw-admin-" + secrets.token_hex(24)
        cfg.server.client_keys = [ClientKey(key="gw-client-" + secrets.token_hex(24),
                                           name="desktop")]
        cfg.accounts.dir = str(data_dir / "accounts")
        cfg.accounts.pool_file = str(data_dir / "pool.json")
        cfg.logging.capture_dir = str(data_dir / "logs")
        cfg.store.sqlite_path = str(data_dir / "gateway.db")
        cfg.update.enabled = False
        cfg.generator.enabled = False
        cfg.models.aliases = {"glm-5.3": "GLM-5.3", "glm-5.3-flash": "GLM-5.3-Flash"}
        data = cfg.model_dump()
        fd, temporary = tempfile.mkstemp(prefix=".config-", dir=config_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                yaml.safe_dump(data, fh, sort_keys=False)
                fh.flush()
                os.fsync(fh.fileno())
            try:
                # Publish a complete file atomically without replacing another launch's config.
                os.link(temporary, path)
            except FileExistsError:
                pass
        finally:
            os.unlink(temporary)
    return path


class InstanceLock:
    """A per-config flock, automatically released even after a crash."""

    def __init__(self, config_path: Path):
        digest = hashlib.sha256(str(config_path.resolve()).encode()).hexdigest()[:16]
        self.path = xdg_dir("XDG_CACHE_HOME", ".cache") / f"instance-{digest}.lock"
        self.file = None

    def acquire(self) -> bool:
        self.file = open(self.path, "a", encoding="utf-8")
        os.chmod(self.path, 0o600)
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.file.close()
            self.file = None
            return False
        return True

    def close(self) -> None:
        if self.file is not None:
            self.file.close()
            self.file = None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autozcoder", description="AutoZcoder Linux desktop")
    parser.add_argument("--config", type=Path, help="use a separate configuration file")
    parser.add_argument("--headless", action="store_true", help="serve the dashboard without a window")
    parser.add_argument("--version", action="version", version=f"AutoZcoder {__version__}")
    args = parser.parse_args(argv)
    os.umask(0o077)
    os.environ["AUTOZCODER_PACKAGE_MANAGED"] = "1"
    configure_runtime()
    cfg_path = args.config.resolve() if args.config else bootstrap_config()
    if not cfg_path.is_file():
        parser.error(f"configuration file does not exist: {cfg_path}")
    os.environ["ZCODE_GATEWAY_CONFIG"] = str(cfg_path)
    cfg = load_config(cfg_path)
    cfg.update.enabled = False
    bundled_gen = Path(__file__).resolve().parent.parent / "generator"
    if (bundled_gen / "main.py").is_file():
        if not cfg.generator.zcodeauto_dir:
            cfg.generator.zcodeauto_dir = str(bundled_gen)
        if not cfg.generator.python_exe:
            cfg.generator.python_exe = generator_python()
    if cfg.server.host not in ("127.0.0.1", "localhost", "::1"):
        parser.error("the desktop gateway must bind to a loopback address")
    host = f"[{cfg.server.host}]" if ":" in cfg.server.host else cfg.server.host
    origin = f"http://{host}:{cfg.server.port}"
    url = origin + "/dash"
    lock = InstanceLock(cfg_path)
    if not lock.acquire():
        print("AutoZcoder is already running for this configuration.", file=sys.stderr)
        return 0

    # Fail before opening the UI if an unrelated application owns the port.
    family = socket.AF_INET6 if ":" in cfg.server.host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((cfg.server.host, cfg.server.port))
    except OSError as exc:
        lock.close()
        print(f"Cannot use {origin}: {exc}. Change server.port in {cfg_path}.", file=sys.stderr)
        return 1

    import uvicorn
    from .app import create_app

    app = create_app(cfg)
    server = uvicorn.Server(uvicorn.Config(app, host=cfg.server.host,
                                         port=cfg.server.port, access_log=False,
                                         timeout_graceful_shutdown=5))
    errors = []

    def run_server():
        try:
            server.run()
        except BaseException as exc:
            errors.append(exc)
            log.exception("gateway startup failed")

    thread = threading.Thread(target=run_server, name="gateway", daemon=True)
    thread.start()
    window = None

    def stop(*_):
        server.should_exit = True
        if window is not None:
            window.destroy()

    app.state.desktop_shutdown = stop
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        deadline = time.monotonic() + 30
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.1)
        if not server.started or errors:
            raise RuntimeError(f"gateway failed to start at {origin}")
        if args.headless:
            print(f"AutoZcoder dashboard: {url}", flush=True)
            while thread.is_alive():
                thread.join(0.5)
            return 1 if errors else 0
        import webview
        # NVIDIA's GBM path can leave WebKit blank on Wayland/XWayland.
        if Path("/sys/module/nvidia").exists():
            os.environ.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
        window = webview.create_window("AutoZcoder", url, width=1280, height=800,
                                       min_size=(980, 620), background_color="#0f1115",
                                       text_select=True)
        window.events.closed += lambda: setattr(server, "should_exit", True)
        webview.start(gui="gtk", private_mode=False,
                      storage_path=str(xdg_dir("XDG_CACHE_HOME", ".cache") / "webview"))
        return 0
    except Exception:
        log.exception("AutoZcoder could not start")
        return 1
    finally:
        # Lifespan stops existing workers; never initialize a manager just to exit.
        server.should_exit = True
        thread.join()
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
