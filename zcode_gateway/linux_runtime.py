"""Paths and child-process helpers for the RPM-owned optional runtime."""
from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path


def managed() -> bool:
    return os.environ.get("AUTOZCODER_PACKAGE_MANAGED") == "1"


def runtime_root() -> Path:
    return Path(os.environ.get("AUTOZCODER_RUNTIME_ROOT", "/usr/lib64/autozcoder"))


def generator_python() -> str:
    if not managed():
        return ""
    candidate = runtime_root() / "generator-python"
    return str(candidate) if candidate.is_file() else ""


def configure_runtime() -> None:
    if not managed():
        return
    browsers = runtime_root() / "browsers"
    if browsers.is_dir():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers)
        executables = sorted(browsers.glob("chromium-*/chrome-linux64/chrome"))
        if executables:
            os.environ["AUTOZCODER_CHROMIUM_EXECUTABLE"] = str(executables[-1])
    # Disable optional telemetry; opening local status pages must stay local.
    os.environ.setdefault("WRANGLER_SEND_METRICS", "false")


def child_session() -> dict:
    return {"start_new_session": True} if os.name != "nt" else {}


def stop_process(proc: subprocess.Popen, timeout: float = 3) -> None:
    """Stop only the process group established for this application's child."""
    group = None
    if os.name == "nt":
        if proc.poll() is None:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        try:
            # Record ownership before waiting: the group can outlive its leader.
            if os.getpgid(proc.pid) == proc.pid:
                group = proc.pid
                os.killpg(group, signal.SIGTERM)
            elif proc.poll() is None:
                proc.terminate()
        except ProcessLookupError:
            return
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            if group is not None:
                os.killpg(group, signal.SIGKILL)
            else:
                proc.kill()
        except ProcessLookupError:
            pass
        proc.wait(timeout=timeout)
    finally:
        if group is not None:
            # A browser descendant may ignore TERM even when its parent exits.
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                pass
