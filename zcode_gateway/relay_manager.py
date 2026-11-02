"""Transactional relay configuration, rotation and owned deployment workers.

Forwarding probes are connectivity observations, not inference/streaming health.
Inference failures and probe failures have separate one-minute cooldowns.
"""
from __future__ import annotations

import copy
import json
import math
import random
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .relay_deploy import (
    deploy_cloudflare, deploy_deno, deploy_netlify, deploy_vercel, ping_relay,
)
from .relay_jobs import DeploymentJobError, RelayDeploymentJobs

STRATEGIES = ("single", "round_robin", "random")
RELAY_TYPES = ("cloudflare", "vercel", "deno", "netlify", "custom")
FAIL_COOLDOWN_S = 60.0


class RelayManager:
    """Manages serverless edge relay configurations and model routing."""

    def __init__(self, data_dir: Path | str | None = None) -> None:
        self.data_dir = Path(data_dir) if data_dir else Path("./generator-data")
        self.path = self.data_dir / "relays.json"
        self._lock = threading.RLock()
        self._cache: dict[str, Any] | None = None
        self._rr_index = 0
        self._failed: dict[str, float] = {}
        self._probe_failed: dict[str, float] = {}
        self._probe_serial = 0
        self._probe_latest: dict[tuple[str, str], int] = {}
        self._notify = None
        self._health_thread: threading.Thread | None = None
        self._health_stop = threading.Event()
        self._shutdown_stop = threading.Event()
        self._deployments = RelayDeploymentJobs(self._register_deployment, self._deploy,
                                               self._lock, self._shutdown_stop)

    def start_health_loop(self, interval_s: int = 300) -> None:
        """Ping all relays every interval (default 5 min). Idempotent."""
        with self._lock:
            if self._shutdown_stop.is_set() or (self._health_thread and self._health_thread.is_alive()):
                return
            self._health_stop.clear()
            self._health_interval = max(60, int(interval_s))

            def _loop():
                while not self._health_stop.wait(self._health_interval):
                    try:
                        self.ping_all()
                    except Exception:
                        pass

            self._health_thread = threading.Thread(target=_loop, name="relay-health", daemon=True)
            self._health_thread.start()

    def stop_health_loop(self, *, deadline: float | None = None) -> None:
        """Cooperatively stop timer/pings, then join within the shared deadline."""
        self._health_stop.set()
        thread = self._health_thread
        if thread is not None and thread is not threading.current_thread():
            deadline = time.monotonic() + 4 if deadline is None else deadline
            thread.join(timeout=max(0, deadline - time.monotonic()))
            if thread.is_alive():
                raise TimeoutError("relay health worker is still stopping")

    def request_shutdown(self) -> None:
        """Nonblocking fence; existing threads remain owned until they exit."""
        self._shutdown_stop.set()
        self._health_stop.set()
        self._deployments.request_shutdown()

    def shutdown(self, *, deadline: float | None = None) -> bool:
        self.request_shutdown()
        deadline = time.monotonic() + 4 if deadline is None else deadline
        errors = []
        for stop in (self.stop_health_loop, self._deployments.shutdown):
            try:
                stop(deadline=deadline)
            except TimeoutError as exc:
                errors.append(str(exc))
        if errors:
            raise TimeoutError("; ".join(errors))
        return True

    def _default_settings(self) -> dict[str, Any]:
        return {"enabled": True, "active_url": "", "active_id": "",
                "models": ["GLM-5.3"], "strategy": "single", "relays": []}

    @staticmethod
    def _validate_settings(settings: dict[str, Any]) -> None:
        if not isinstance(settings, dict):
            raise ValueError("Relay settings must be an object")
        if not isinstance(settings.get("relays"), list) or any(
                not isinstance(r, dict) or not isinstance(r.get("id"), str)
                or not isinstance(r.get("url"), str) for r in settings["relays"]):
            raise ValueError("Relays must be a list of entries with IDs and URLs")
        if not isinstance(settings.get("models"), list) or any(
                not isinstance(m, str) for m in settings["models"]):
            raise ValueError("Models must be a list of strings")
        if not isinstance(settings.get("enabled"), bool):
            raise ValueError("Relay enabled must be a boolean")
        if settings.get("strategy") not in STRATEGIES:
            raise ValueError("Unknown relay strategy")
        if any(not isinstance(settings.get(k), str) for k in ("active_url", "active_id")):
            raise ValueError("Active relay must be an ID and URL")
        # Legacy provider types are data, not rewritten or rejected on reads.
        # Strict JSON prevents NaN/non-JSON values leaking to status responses.
        try:
            json.dumps(settings, allow_nan=False)
        except (TypeError, ValueError):
            raise ValueError("Relay settings must contain finite JSON values") from None

    def get_settings(self) -> dict[str, Any]:
        """Read-only, independent nested snapshot. Never repairs/writes a file."""
        with self._lock:
            if self._cache is None:
                settings = self._default_settings()
                if self.path.exists():
                    try:
                        data = json.loads(self.path.read_text(encoding="utf-8"))
                        if not isinstance(data, dict):
                            raise ValueError("Invalid relay settings")
                        settings.update(data)
                        self._validate_settings(settings)
                    except (OSError, ValueError, TypeError):
                        settings = self._default_settings()
                self._cache = settings
            return copy.deepcopy(self._cache)

    def save_settings(self, settings: dict[str, Any], *, _control=None) -> None:
        """Commit cache only after successful atomic disk replacement."""
        with self._lock:
            candidate = copy.deepcopy(settings)
            self._validate_settings(candidate)
            encoded = json.dumps(candidate, indent=2, ensure_ascii=False, allow_nan=False)
            self.data_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            try:
                tmp.write_text(encoded, encoding="utf-8")
                if _control is not None:
                    _control.check()
                tmp.replace(self.path)
            finally:
                # A failed write/replace must neither poison the cache nor leave
                # an uncommitted settings file behind. Do not mask its error.
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
            self._cache = candidate
            live = {(r["id"], r["url"]) for r in candidate["relays"]}
            self._probe_latest = {key: seq for key, seq in self._probe_latest.items() if key in live}

    def _model_matches(self, model: str, models: list[str]) -> bool:
        norm = model.lower().strip()
        lowered = [m.lower() for m in models]
        if norm in lowered or model in models:
            return True
        if "glm-5.3" in norm and "flash" not in norm and "glm-5.3" in lowered:
            return True
        if "flash" in norm and "glm-5.3-flash" in lowered:
            return True
        return False

    def _failed_keys(self) -> set[str]:
        now = time.time()
        with self._lock:
            for cooldowns in (self._failed, self._probe_failed):
                for key in [k for k, until in cooldowns.items() if until <= now]:
                    cooldowns.pop(key, None)
            return set(self._failed) | set(self._probe_failed)

    def _available_relays(self, st: dict[str, Any], exclude_urls=None) -> list[dict[str, Any]]:
        failed = self._failed_keys()
        excluded = set(exclude_urls or ())
        return [r for r in st["relays"] if r.get("url") and r["url"].strip() not in excluded
                and r.get("id") not in failed and r.get("url") not in failed]

    def _pick_relay(self, st: dict[str, Any], exclude_urls=None) -> dict[str, Any] | None:
        strategy = st.get("strategy") or "single"
        excluded = set(exclude_urls or ())
        relays = self._available_relays(st, excluded)
        if not relays:
            # Intentional all-cooled fallback, but never a request's attempted URL.
            relays = [r for r in st["relays"] if r.get("url") and r["url"].strip() not in excluded]
            if not relays:
                return None
        if strategy == "round_robin" and len(relays) > 1:
            self._rr_index %= len(relays)
            pick = relays[self._rr_index]
            self._rr_index += 1
            return pick
        if strategy == "random" and len(relays) > 1:
            return random.choice(relays)
        return next((r for r in relays if r.get("id") == st.get("active_id")), relays[0])

    def mark_relay_failed(self, relay_url: str) -> None:
        """Inference failure cooldown; probes cannot clear it."""
        if not relay_url:
            return
        with self._lock:
            first_fail = relay_url not in self._failed
            self._failed[relay_url] = time.time() + FAIL_COOLDOWN_S
        if first_fail and self._notify is not None:
            try:
                host = urlsplit(relay_url).hostname or "relay"
                self._notify.emit("relay_cooled", f"relay {host} cooled {FAIL_COOLDOWN_S:.0f}s "
                                  "after a failure — rotation switched", level="warning")
            except Exception:
                pass

    def mark_relay_ok(self, relay_url: str) -> None:
        with self._lock:
            self._failed.pop(relay_url, None)

    def get_relay_count(self) -> int:
        return len(self.get_settings()["relays"])

    def relay_required_for_model(self, model: str) -> bool:
        """Configured applicable relay path, even if every candidate is exhausted."""
        with self._lock:
            st = self.get_settings()
            return bool(model and st["enabled"] and self._model_matches(model, st["models"])
                        and (st["relays"] or st["active_url"].strip()))

    def get_relay_for_model(self, model: str, exclude_urls=None) -> str | None:
        with self._lock:
            st = self.get_settings()
            if not model or not st["enabled"] or not self._model_matches(model, st["models"]):
                return None
            excluded = set(exclude_urls or ())
            if not st["relays"]:
                url = st["active_url"].strip()
                return url if url and url not in excluded else None
            pick = self._pick_relay(st, excluded)
            return pick["url"].strip() if pick else None

    def add_relay(self, name: str, url: str, type_name: str = "custom", *, _control=None) -> dict[str, Any]:
        if not isinstance(type_name, str) or type_name not in RELAY_TYPES:
            raise ValueError(f"Relay type must be one of {', '.join(RELAY_TYPES)}")
        if not isinstance(name, str) or not isinstance(url, str):
            raise ValueError("Relay name and URL must be strings")
        url = url.strip()
        try:
            parsed = urlsplit(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname or any(c.isspace() for c in url):
                raise ValueError
        except ValueError:
            raise ValueError("Relay URL must be an HTTP or HTTPS URL") from None
        with self._lock:
            if _control is not None:
                _control.check()
            st = self.get_settings()
            entry = {"id": f"relay-{uuid.uuid4().hex[:12]}", "name": name or f"{type_name}-relay",
                     "type": type_name, "url": url, "created_at": time.time(),
                     "last_status": None, "last_error": None, "last_latency_ms": None,
                     "last_tested_at": None}
            st["relays"].append(entry)
            if not st["active_url"]:
                st["active_url"] = entry["url"]
                st["active_id"] = entry["id"]
            self.save_settings(st, _control=_control)
            return copy.deepcopy(entry)

    def delete_relay(self, relay_id: str) -> bool:
        return bool(self.delete_relays([relay_id]))

    def delete_relays(self, ids=None, *, all_relays: bool = False) -> int:
        with self._lock:
            st = self.get_settings()
            before = st["relays"]
            wanted = {str(i) for i in (ids or [])}
            st["relays"] = [] if all_relays else [r for r in before if r["id"] not in wanted]
            removed = len(before) - len(st["relays"])
            if not removed:
                return 0
            if not any(r["id"] == st["active_id"] for r in st["relays"]):
                st["active_id"] = st["relays"][0]["id"] if st["relays"] else ""
                st["active_url"] = st["relays"][0]["url"] if st["relays"] else ""
            self.save_settings(st)
            keep = {r["id"] for r in st["relays"]} | {r["url"] for r in st["relays"]}
            for cooldowns in (self._failed, self._probe_failed):
                for key in list(cooldowns):
                    if key not in keep:
                        cooldowns.pop(key, None)
            return removed

    def set_active(self, relay_id: str) -> bool:
        with self._lock:
            st = self.get_settings()
            for relay in st["relays"]:
                if relay["id"] == relay_id:
                    st["active_id"], st["active_url"] = relay["id"], relay["url"]
                    self.save_settings(st)
                    return True
            return False

    def update_options(self, models: list[str] | None = None, enabled: bool | None = None,
                       active_id: str | None = None, strategy: str | None = None) -> dict[str, Any]:
        with self._lock:
            st = self.get_settings()
            if models is not None:
                if not isinstance(models, list) or any(not isinstance(m, str) for m in models):
                    raise ValueError("Models must be a list of strings")
                st["models"] = list(dict.fromkeys(models))
            if enabled is not None:
                if not isinstance(enabled, bool):
                    raise ValueError("Enabled must be a boolean")
                st["enabled"] = enabled
            if strategy is not None:
                if not isinstance(strategy, str) or strategy.lower().strip() not in STRATEGIES:
                    raise ValueError(f"strategy must be one of {', '.join(STRATEGIES)}")
                st["strategy"] = strategy.lower().strip()
            if active_id is not None:
                relay = next((r for r in st["relays"] if r["id"] == active_id), None)
                if not relay:
                    raise ValueError("Active relay was not found")
                st["active_id"], st["active_url"] = relay["id"], relay["url"]
            self.save_settings(st)
            return copy.deepcopy(st)

    def _begin_probe(self, relay_id: str, url: str | None = None):
        with self._lock:
            if self._shutdown_stop.is_set():
                return None
            target = next((r for r in self.get_settings()["relays"] if r["id"] == relay_id
                           and (url is None or r["url"] == url)), None)
            if target is None:
                return None
            key = (target["id"], target["url"])
            self._probe_serial += 1
            self._probe_latest[key] = self._probe_serial
            return key, self._probe_serial

    def _merge_probe(self, key: tuple[str, str], sequence: int, result: dict) -> bool:
        with self._lock:
            if self._shutdown_stop.is_set() or self._probe_latest.get(key) != sequence:
                return False
            st = self.get_settings()
            target = next((r for r in st["relays"] if (r["id"], r["url"]) == key), None)
            if target is None:
                return False
            status, latency = result.get("status"), result.get("elapsed_ms")
            if not isinstance(status, int) or isinstance(status, bool):
                status = None
            if not isinstance(latency, (int, float)) or not math.isfinite(latency):
                latency = None
            target.update(last_status=status, last_latency_ms=latency, last_tested_at=time.time(),
                          last_error=None if result.get("ok") else (result.get("error") or "Relay probe failed"),
                          last_probe_kind="forwarding", last_probe_verified=bool(result.get("probe_verified", False)),
                          last_error_code=result.get("error_code") or "", model_capability="not_tested",
                          streaming_capability="not_tested")
            self.save_settings(st)
            if result.get("ok"):
                self._probe_failed.pop(key[1], None)
                self._probe_failed.pop(key[0], None)
            else:
                self._probe_failed[key[1]] = time.time() + FAIL_COOLDOWN_S
            return True

    @staticmethod
    def _ping(url: str, timeout: float) -> dict:
        try:
            return ping_relay(url, timeout=timeout)
        except Exception:
            # Never persist arbitrary exception text (it can contain URL secrets).
            return {"ok": False, "status": None, "error": "Relay probe failed",
                    "elapsed_ms": None, "error_code": "probe_failed", "probe_verified": False}

    def test_relay_by_id(self, relay_id: str) -> dict[str, Any]:
        probe = self._begin_probe(relay_id)
        if probe is None:
            return {"ok": False, "error": "Relay not found or manager is stopping"}
        key, sequence = probe
        result = self._ping(key[1], 12.0)
        self._merge_probe(key, sequence, result)
        return result

    def ping_all(self) -> dict[str, dict]:
        out = {}
        for relay in self.get_settings()["relays"]:
            if self._health_stop.is_set() or self._shutdown_stop.is_set():
                break
            probe = self._begin_probe(relay["id"], relay["url"])
            if probe is None:
                continue
            key, sequence = probe
            result = self._ping(key[1], 3.0)
            if self._health_stop.is_set():
                break
            if self._merge_probe(key, sequence, result):
                out[key[0]] = {"ok": bool(result.get("ok")), "latency_ms": result.get("elapsed_ms")}
        return out

    @staticmethod
    def _deploy(provider: str, payload: dict, control):
        deployer = {"cloudflare": deploy_cloudflare, "vercel": deploy_vercel,
                    "deno": deploy_deno, "netlify": deploy_netlify}[provider]
        return deployer(**payload, control=control)

    def _register_deployment(self, registration: dict, control) -> dict:
        return self.add_relay(**registration, _control=control)

    def start_deployment(self, relay_type: str, params: dict, operation_id: str) -> dict:
        return self._deployments.start(relay_type, params, operation_id)

    def deployment_snapshot(self, operation_id: str | None = None) -> dict:
        return self._deployments.snapshot(operation_id)

    def retry_deployment_registration(self, operation_id: str) -> dict:
        return self._deployments.retry_registration(operation_id)

    def submit_legacy_deployment(self, relay_type: str, params: dict) -> dict:
        return self._deployments.submit_legacy(relay_type, params)

    def deploy_and_add(self, relay_type: str, params: dict[str, Any]) -> dict[str, Any]:
        """Compatibility path; callers without an ID have no lost-ACK guarantee."""
        snapshot = self.submit_legacy_deployment(relay_type, params)
        operation_id = snapshot["job"]["id"]
        while snapshot["job"]["status"] == "running":
            time.sleep(0.05)
            snapshot = self.deployment_snapshot(operation_id)
        job = snapshot["job"]
        if job["status"] == "succeeded":
            return {"ok": True, "relay": job["result"]["relay"]}
        error = job["error"]
        raise DeploymentJobError(502, error["code"], error["message"], snapshot["active_id"])
