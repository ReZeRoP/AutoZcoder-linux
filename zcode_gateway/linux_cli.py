"""Import-light Linux CLI; diagnostics precede all GUI/runtime initialization.

Exit codes: 0 successful/healthy, 1 actionable stopped/degraded/setup issue,
2 invalid invocation or diagnostic failure. Foreground service may additionally
return its permanent-failure statuses (73 ownership conflict, 78 setup failure).
"""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import sys
from pathlib import Path

from . import __version__
from .linux_config import config_identity, default_config_path, inspect_config, validate_service_environment


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse ordinarily echoes invalid argument values, potentially keys.
        self.exit(2, "Invalid AutoZcoder invocation; use --help for supported commands and options.\n")


def _check(code: str, status: str, message: str) -> dict:
    return {"code": code, "status": status, "message": message}


def _environment_check() -> dict:
    try:
        validate_service_environment()
        return _check("service_environment", "ok", "The default user-service environment is supported.")
    except ValueError:
        return _check("service_environment", "error", "The default user service requires standard XDG/runtime settings; use explicit --config foreground serve or attach-only ui.")


def _dependencies() -> list[dict]:
    checks = []
    for distribution in ("PyYAML", "fastapi", "uvicorn", "pydantic", "httpx"):
        try:
            importlib.metadata.version(distribution)
            checks.append(_check("dependency_" + distribution.lower(), "ok", distribution + " is installed."))
        except importlib.metadata.PackageNotFoundError:
            checks.append(_check("dependency_" + distribution.lower(), "error", distribution + " is not installed."))
        except Exception:
            checks.append(_check("dependency_" + distribution.lower(), "error", "Installed dependency metadata could not be inspected."))
    # Metadata discovery never imports/executes optional desktop modules.
    for name, distribution in (("gi", "PyGObject"), ("webview", "pywebview")):
        try:
            importlib.metadata.version(distribution)
            available = True
        except Exception:
            available = False
        checks.append(_check("desktop_" + name, "ok" if available else "warning",
                             "Optional desktop dependency is available." if available else "An optional desktop dependency is unavailable; foreground serving remains supported."))
    # Optional worker/browser existence is metadata, not a worker launch/import.
    root = Path("/usr/lib64/autozcoder")
    try:
        installed = root.is_dir()
        generator = (root / "generator-python").is_file()
        browsers = (root / "browsers").is_dir()
    except OSError:
        installed = generator = browsers = False
    checks.append(_check("managed_runtime", "ok" if installed else "warning",
                         "The packaged optional runtime is present." if installed else "The packaged optional runtime is absent; source-only gateway operation may still work."))
    if installed:
        checks.append(_check("generator_runtime", "ok" if generator and browsers else "warning",
                             "Optional generator runtime metadata is present." if generator and browsers else "Optional generator runtime metadata is incomplete; no workers were started."))
    return checks


def _diagnostic(command: str, config_path: Path | None, online: bool) -> tuple[dict, int]:
    from .linux_control import ServiceController, probe_instance
    report = {"schema_version": 1, "product": "autozcoder-linux", "version": __version__,
              "command": command, "ok": False, "checks": []}
    config = inspect_config(config_path)
    report["config"] = config
    report["checks"].append(_check(config["code"], "ok" if config["ok"] else "error", config["message"]))
    report["checks"].extend(config.get("checks", ()))
    default_mode = config_path is None
    environment = _environment_check() if default_mode else _check("custom_configuration", "ok", "Explicit configuration selected; the default user service will not be managed.")
    report["checks"].append(environment)
    if command == "doctor":
        report["checks"].extend(_dependencies())
    unit = None
    if default_mode:
        # Reading the canonical unit is safe even if terminal XDG differs; never
        # adopt its listener when that context cannot be matched.
        unit = ServiceController().status()
        report["service"] = unit
        # A disabled/stopped installed unit is a healthy setup (doctor), not a
        # healthy running gateway (status). Autostart is opt-in, never an error.
        setup_ok = unit["code"] in ("unit_active", "unit_stopped")
        report["checks"].append(_check(unit["code"], "ok" if (setup_ok if command == "doctor" else unit["ok"]) else "error", unit["message"]))
    else:
        report["service"] = {"schema_version": 1, "ok": True, "code": "service_not_selected", "message": "Explicit configuration does not select the default user service.", "properties": {}}
    should_probe = command == "status" or online
    if should_probe and config["ok"] and environment["status"] == "ok":
        path = config_path if config_path is not None else default_config_path()
        origin = config["server"]["origin"]
        probe = probe_instance(origin, config_identity(path, origin))
        report["probe"] = probe
        report["checks"].append(_check(probe["code"], "ok" if probe["ok"] else "error", probe["message"]))
        if default_mode and probe.get("instance") and probe["instance"]["mode"] != "user-service":
            report["checks"].append(_check("ownership_mismatch", "error", "The listener is a foreground gateway, not the selected user service; close the old embedded or manual instance yourself."))
        if default_mode and unit and not unit["ok"] and probe["ok"]:
            report["checks"].append(_check("unit_context_mismatch", "error", "A gateway listener exists without an active matching user service; it was not adopted or stopped."))
    elif should_probe:
        report["checks"].append(_check("probe_skipped", "warning", "Local probing was skipped because configuration or service context is invalid."))
    report["ok"] = all(check["status"] == "ok" for check in report["checks"] if check["status"] != "warning")
    # Permission/path warnings are actionable, unlike optional desktop/runtime
    # availability. They do not indicate an unexpected diagnostic exception.
    if any(check["code"] in ("config_permissions", "path_unsupported") for check in report["checks"]):
        report["ok"] = False
    return report, 0 if report["ok"] else 1


def _emit(report: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, sort_keys=True, separators=(",", ":")))
        return
    print(f"AutoZcoder {__version__} — {report.get('command', 'control')}")
    if "checks" in report:
        for check in report["checks"]:
            print(f"[{check['status']}] {check['code']}: {check['message']}")
        service = report.get("service", {})
        properties = service.get("properties", {})
        if properties:
            print("User service: " + properties.get("ActiveState", "unknown") + "; login autostart: " + properties.get("UnitFileState", "unknown"))
    else:
        print(report["message"])


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(prog="autozcoder", description="AutoZcoder Linux always-on gateway and dashboard",
                     epilog="Exit status: 0 successful/healthy, 1 stopped/degraded/setup issue, 2 invalid invocation/diagnostic failure. Autostart is opt-in and never starts or stops the current service.")
    parser.add_argument("command", nargs="?", choices=("ui", "serve", "status", "doctor", "start", "stop", "restart", "autostart"), default=None)
    parser.add_argument("action", nargs="?", choices=("enable", "disable"))
    parser.add_argument("--config", type=Path, help="explicit config for diagnostics, foreground serving or attach-only UI")
    parser.add_argument("--headless", action="store_true", help="legacy alias for foreground serve")
    parser.add_argument("--user-service", action="store_true", help="run the default foreground backend for the user unit")
    parser.add_argument("--online", action="store_true", help="doctor: also check local loopback identity/liveness, never upstream")
    parser.add_argument("--json", action="store_true", help="emit versioned JSON for diagnostics or explicit controls")
    parser.add_argument("--version", action="version", version=f"AutoZcoder {__version__}")
    try:
        # intermixed parsing supports --config/--json before or after commands.
        args = parser.parse_intermixed_args(argv)
        command = args.command or ("serve" if args.headless or args.user_service else "ui")
        if args.headless and command != "serve" or args.user_service and command != "serve" or args.online and command != "doctor":
            parser.error("incompatible options")
        if command == "autostart":
            if args.action is None:
                parser.error("autostart requires enable or disable")
        elif args.action is not None:
            parser.error("unexpected action")
        if args.config is not None and (command in ("start", "stop", "restart", "autostart") or args.user_service):
            parser.error("custom config cannot manage the singleton default unit")
        if args.json and command in ("ui", "serve"):
            parser.error("JSON is for diagnostics and controls")
    except SystemExit as exc:
        return int(exc.code or 0)
    if command in ("status", "doctor"):
        try:
            report, code = _diagnostic(command, args.config, args.online)
        except Exception:
            report = {"schema_version": 1, "product": "autozcoder-linux", "version": __version__,
                      "command": command, "ok": False, "code": "diagnostic_failed",
                      "message": "Diagnostics could not complete; no configuration or service changes were made."}
            code = 2
        _emit(report, args.json)
        return code
    if command in ("start", "stop", "restart", "autostart"):
        from .linux_control import ServiceController
        try:
            result = ServiceController().action(args.action if command == "autostart" else command)
        except Exception:
            result = {"schema_version": 1, "ok": False, "code": "control_failed", "message": "User-service control could not complete."}
            _emit({**result, "command": command}, args.json)
            return 2
        _emit({**result, "command": command}, args.json)
        return 0 if result["ok"] else 1
    # Runtime/Config/GTK imports happen only in these explicit launch branches.
    try:
        if command == "serve":
            from .linux_service import serve
            return serve(config_path=args.config, user_service=args.user_service)
        from .linux_desktop import open_dashboard
        return open_dashboard(config_path=args.config)
    except Exception:
        print("AutoZcoder could not launch; check configuration, installed runtime and user-service availability with doctor.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
