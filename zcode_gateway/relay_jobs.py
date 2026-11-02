"""In-memory ownership for relay deployments, not a general-purpose task queue.

Only safe status survives the worker. Admission IDs are instance-scoped: after
restart/eviction an unknown old ID requires reconciliation, never another POST.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
import uuid
from collections import OrderedDict
from typing import Any, Callable
from urllib.parse import urlsplit

from .relay_deploy import DEPLOY_ERROR_MESSAGES, DeployControl, RelayDeployError

DEPLOY_TIMEOUT_S = 300.0
MAX_TERMINAL_JOBS = 32
REMOTE_KEYS = frozenset({
    "provider", "name", "project_id", "deployment_id", "app_id", "revision_id",
    "script_name", "url", "site_id", "account_id", "id",
})
ERROR_MESSAGES = {
    **DEPLOY_ERROR_MESSAGES,
    "deployment_stopped": "Deployment stopped locally; inspect the provider before creating another resource.",
    "deployment_timeout": "Deployment exceeded its deadline; inspect the provider before creating another resource.",
    "deployment_failed": "Deployment failed. Check the provider dashboard before retrying.",
    "provider_http_error": "The provider rejected a deployment request.",
    "provider_transport_error": "The provider request could not be completed.",
    "deployment_outcome_unknown": "The remote outcome is unknown. Reconcile in the provider dashboard; do not blindly retry.",
    "invalid_provider_response": "The provider returned an invalid deployment result. Check its dashboard.",
    "registration_failed": "The relay was deployed, but saving it locally failed. Retry registration only.",
    "worker_start_failed": "The local deployment worker could not start.",
}


class DeploymentJobError(RuntimeError):
    """A safe HTTP-facing admission/status error (never a provider response)."""

    def __init__(self, status: int, code: str, message: str,
                 active_id: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.active_id = active_id


def _safe_remote(fields: dict, credentials=()) -> dict[str, str]:
    """Keep identifiers only; drop credentials, query strings and arbitrary text."""
    safe = {}
    if not isinstance(fields, dict):
        return safe
    for key in REMOTE_KEYS:
        value = fields.get(key)
        if not isinstance(value, str) or not value or len(value) > 512:
            continue
        if any(secret and secret in value for secret in credentials):
            continue
        if key == "url":
            try:
                parsed = urlsplit(value)
                if (parsed.scheme not in ("https", "http") or not parsed.hostname
                        or parsed.username is not None or parsed.password is not None
                        or parsed.query or parsed.fragment or any(c.isspace() for c in value)):
                    continue
                if not re.fullmatch(r"[A-Za-z0-9.:/-]+", value):
                    continue
            except ValueError:
                continue
        elif not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", value):
            continue
        safe[key] = value
    return safe


def _prepare(relay_type: str, params: dict) -> tuple[str, dict]:
    if not isinstance(relay_type, str) or not isinstance(params, dict):
        raise DeploymentJobError(400, "invalid_deployment", "Deployment type and parameters are required.")
    provider = relay_type.strip().lower()
    if provider == "netlify":
        raise DeploymentJobError(400, "netlify_streaming_unsupported",
                                 "New Netlify deployments are disabled: the bundled template buffers responses and is not streaming-supported.")
    if provider not in ("cloudflare", "vercel", "deno"):
        raise DeploymentJobError(400, "invalid_provider", "Choose Cloudflare, Vercel or Deno.")
    def value(*names):
        result = next((params[k] for k in names if params.get(k)), "")
        if not isinstance(result, str) or len(result) > 8192:
            raise DeploymentJobError(400, "invalid_deployment", "Deployment fields must be bounded strings.")
        return result.strip()

    name = value("projectName", "project_name", "app_name")
    if name and not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9-]{0,62}", name):
        raise DeploymentJobError(400, "invalid_name", "Use a project name of at most 63 letters, digits or hyphens.")
    if provider == "cloudflare":
        payload = {"account_id": value("accountId", "account_id"),
                   "api_token": value("apiToken", "api_token"), "project_name": name}
        if not payload["account_id"] or not payload["api_token"]:
            raise DeploymentJobError(400, "missing_credentials", "Cloudflare Account ID and API Token are required.")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", payload["account_id"]):
            raise DeploymentJobError(400, "invalid_account", "The Cloudflare account ID is invalid.")
    elif provider == "vercel":
        payload = {"vercel_token": value("vercelToken", "token"), "project_name": name}
        if not payload["vercel_token"]:
            raise DeploymentJobError(400, "missing_credentials", "Vercel API token is required.")
    else:
        payload = {"deno_token": value("denoToken", "token"),
                   "org_domain": value("orgDomain", "org_domain"), "app_name": name}
        if not payload["deno_token"] or not payload["org_domain"]:
            raise DeploymentJobError(400, "missing_credentials", "Deno API token and organization domain are required.")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", payload["org_domain"]):
            raise DeploymentJobError(400, "invalid_organization", "The Deno organization domain is invalid.")
    return provider, payload


class RelayDeploymentJobs:
    def __init__(self, register: Callable, deploy: Callable, lock: threading.RLock,
                 stop_event: threading.Event) -> None:
        self._register = register
        self._deploy = deploy
        self._lock = lock
        self._stop = stop_event
        self._generation = uuid.uuid4().hex
        self._digest_key = secrets.token_bytes(32)
        self._records: OrderedDict[str, dict] = OrderedDict()
        self._active_id: str | None = None
        self._thread: threading.Thread | None = None
        self._control: DeployControl | None = None

    def _reap(self) -> None:
        # A terminal status is NOT proof that its thread exited. Only is_alive
        # releases admission; a resistant HTTP operation stays owned on timeout.
        if self._thread is not None and not self._thread.is_alive():
            self._thread = None
            self._active_id = None
            self._control = None
        terminal = [key for key, record in self._records.items()
                    if key != self._active_id and record["job"]["status"] != "running"]
        if len(terminal) > MAX_TERMINAL_JOBS:
            self._generation = uuid.uuid4().hex
            for key in terminal[:-MAX_TERMINAL_JOBS]:
                del self._records[key]
        if self._active_id and self._control:
            record = self._records[self._active_id]
            if record["job"]["status"] == "running":
                try:
                    self._control.check()
                except RelayDeployError as exc:
                    self._failed(record, exc, self._control, finished=False)

    def _lookup(self, operation_id: str) -> dict:
        self._validate_id(operation_id)
        if operation_id in self._records:
            return self._records[operation_id]
        if operation_id.split(":", 1)[0] != self._generation:
            raise DeploymentJobError(410, "deployment_outcome_unknown",
                                     "This operation belongs to an expired server generation. Reconcile in the provider dashboard; do not resubmit.")
        raise DeploymentJobError(404, "deployment_not_found", "Deployment operation was not found.")

    @staticmethod
    def _validate_id(operation_id: str) -> None:
        try:
            generation, suffix = operation_id.split(":")
            uuid.UUID(generation)
            uuid.UUID(suffix)
        except (AttributeError, TypeError, ValueError):
            raise DeploymentJobError(400, "invalid_operation_id", "Use the server generation followed by a colon and a UUID.") from None

    def _snapshot(self, operation_id: str | None = None) -> dict:
        record = self._lookup(operation_id) if operation_id is not None else (
            self._records.get(self._active_id) if self._active_id else
            next(reversed(self._records.values()), None))
        return {"generation": self._generation, "active_id": self._active_id,
                "job": copy.deepcopy(record["job"]) if record else None}

    def snapshot(self, operation_id: str | None = None) -> dict:
        with self._lock:
            self._reap()
            return self._snapshot(operation_id)

    def _admit(self) -> None:
        if self._stop.is_set():
            raise DeploymentJobError(503, "deployment_stopped", "The relay manager is stopping.", self._active_id)
        if self._active_id is not None:
            raise DeploymentJobError(409, "deployment_active", "Another deployment worker is still active.", self._active_id)

    def _fingerprint(self, relay_type: str, params: dict) -> bytes:
        if not isinstance(relay_type, str) or not isinstance(params, dict):
            raise DeploymentJobError(400, "invalid_deployment", "Deployment type and parameters are required.")
        try:
            canonical = json.dumps([relay_type.strip().lower(), {
                key: value for key, value in params.items() if key not in ("operation_id", "operationId")
            }], sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()
        except (TypeError, ValueError):
            raise DeploymentJobError(400, "invalid_deployment", "Deployment parameters must be JSON values.") from None
        if len(canonical) > 65536:
            raise DeploymentJobError(400, "invalid_deployment", "Deployment parameters are too large.")
        return hmac.digest(self._digest_key, canonical, hashlib.sha256)

    def start(self, relay_type: str, params: dict, operation_id: str) -> dict:
        self._validate_id(operation_id)
        params = copy.deepcopy(params)
        with self._lock:
            self._reap()
            if operation_id in self._records:
                digest = self._fingerprint(relay_type, params)
                if not hmac.compare_digest(self._records[operation_id]["digest"], digest):
                    raise DeploymentJobError(409, "operation_payload_conflict", "This operation ID was already used with different parameters.", self._active_id)
                return self._snapshot(operation_id)
            if operation_id.split(":", 1)[0] != self._generation:
                raise DeploymentJobError(410, "deployment_outcome_unknown", "This operation belongs to an expired server generation. Reconcile before starting a new deployment.")
            digest = self._fingerprint(relay_type, params)
            provider, payload = _prepare(relay_type, params)
            try:
                self._admit()
            except DeploymentJobError:
                payload.clear()
                raise
            name_key = "app_name" if provider == "deno" else "project_name"
            name = payload[name_key] or f"{provider}-relay-{int(time.time())}"
            payload[name_key] = name
            tokens = tuple(value for key, value in payload.items() if "token" in key)
            public_name = name if not any(token and token in name for token in tokens) else f"{provider}-relay"
            job = {"id": operation_id, "provider": provider, "name": public_name,
                   "status": "running", "phase": "starting", "started_at": time.time(),
                   "finished_at": None, "error": None, "result": None, "remote": {}}
            record = {"job": job, "digest": digest, "registration": None}
            self._records[operation_id] = record
            self._launch(record, payload)
            return self._snapshot(operation_id)

    def submit_legacy(self, relay_type: str, params: dict) -> dict:
        # No browser-supplied ID means no lost-ack guarantee. Still one owner.
        with self._lock:
            self._reap()
            operation_id = f"{self._generation}:{uuid.uuid4()}"
            return self.start(relay_type, params, operation_id)

    def retry_registration(self, operation_id: str) -> dict:
        with self._lock:
            self._reap()
            record = self._lookup(operation_id)
            if record["job"]["status"] == "succeeded":
                return self._snapshot(operation_id)
            if self._active_id == operation_id:
                return self._snapshot(operation_id)
            if record["job"]["status"] != "registration_failed" or not record["registration"]:
                raise DeploymentJobError(409, "registration_not_retryable", "Only a known deployed relay whose local save failed can retry registration.", self._active_id)
            self._admit()
            record["job"].update(status="running", phase="registering", finished_at=None, error=None)
            self._launch(record, {}, register_only=True)
            return self._snapshot(operation_id)

    def _launch(self, record: dict, payload: dict, *, register_only: bool = False) -> None:
        def credentials():
            return tuple(value for key, value in payload.items() if "token" in key)

        def on_phase(phase: str, **fields):
            with self._lock:
                record["job"]["remote"].update(_safe_remote(fields, credentials()))
                # Only bounded machine phases, never provider error text.
                if record["job"]["status"] == "running" and phase in {
                    "starting", "creating", "uploading", "configuring", "waiting", "ready",
                    "registering", "deploying", "enabling", "polling", "created", "remote_created",
                    "uploading_script", "enabling_route", "reading_subdomain",
                    "creating_deployment", "configuring_project", "waiting_ready",
                    "creating_app", "deploying_revision", "waiting_revision",
                }:
                    record["job"]["phase"] = phase

        control = DeployControl(timeout=DEPLOY_TIMEOUT_S, stop_event=self._stop, on_phase=on_phase)
        thread = threading.Thread(target=self._run, args=(record, payload, control, register_only),
                                  name="relay-deployment", daemon=True)
        # Publish ownership BEFORE start; observers/admission share this lock.
        self._control = control
        self._active_id = record["job"]["id"]
        self._thread = thread
        try:
            thread.start()
        except Exception:
            payload.clear()
            control.on_phase = None
            self._thread = None
            self._active_id = None
            self._control = None
            record["job"].update(status="failed", phase="finished", finished_at=time.time(),
                                 error={"code": "worker_start_failed", "message": ERROR_MESSAGES["worker_start_failed"]})

    @staticmethod
    def _failed(record: dict, exc: Exception, control: DeployControl, *, finished=True) -> None:
        unknown = bool(exc.outcome_unknown if isinstance(exc, RelayDeployError)
                       else control.mutation_attempted)
        code = getattr(exc, "code", "deployment_failed")
        if code not in ERROR_MESSAGES:
            code = "deployment_outcome_unknown" if unknown else "deployment_failed"
        record["job"].update(status="outcome_unknown" if unknown else "failed",
                             phase="finished" if finished else "stopping",
                             error={"code": code, "message": ERROR_MESSAGES[code]})
        if finished:
            record["job"]["finished_at"] = time.time()

    def _run(self, record: dict, payload: dict, control: DeployControl, register_only: bool) -> None:
        try:
            control.check()
            if not register_only:
                result = self._deploy(record["job"]["provider"], payload, control)
                credentials = tuple(value for key, value in payload.items() if "token" in key)
                safe = _safe_remote(result if isinstance(result, dict) else {}, credentials)
                with self._lock:
                    record["job"]["remote"].update(_safe_remote(control.remote, credentials))
                    record["job"]["remote"].update(safe)
                if not isinstance(result, dict) or not result.get("ok") or not safe.get("url"):
                    raise RelayDeployError("Invalid deployment result", code="provider_response_invalid",
                                           outcome_unknown=True)
                control.mutation_attempted = True
                record["registration"] = {"name": safe.get("name") or record["job"]["name"],
                                          "url": safe["url"], "type_name": record["job"]["provider"]}
                result = None
                credentials = ()
            with self._lock:
                control.check()
                record["job"]["phase"] = "registering"
                try:
                    # This callback checks the stop/deadline while holding the
                    # same transaction lock immediately before local mutation.
                    entry = self._register(record["registration"], control)
                except RelayDeployError:
                    raise
                except Exception:
                    record["job"].update(status="registration_failed", phase="finished",
                                         finished_at=time.time(), error={"code": "registration_failed", "message": ERROR_MESSAGES["registration_failed"]})
                else:
                    record["job"].update(status="succeeded", phase="finished", finished_at=time.time(),
                                         error=None, result={"relay": copy.deepcopy(entry)})
                    record["registration"] = None
        except Exception as exc:
            with self._lock:
                credentials = tuple(value for key, value in payload.items() if "token" in key)
                record["job"]["remote"].update(_safe_remote(control.remote, credentials))
                record["job"]["remote"].update(_safe_remote(getattr(exc, "remote", {}) or {}, credentials))
                self._failed(record, exc, control)
        finally:
            # Do not drop ownership here: this thread has not exited yet.
            # Reaping checks is_alive(), including after shutdown join timeout.
            payload.clear()
            control.remote.clear()
            control.on_phase = None

    def request_shutdown(self) -> None:
        self._stop.set()

    def shutdown(self, *, deadline: float | None = None) -> bool:
        self.request_shutdown()
        deadline = time.monotonic() + 4 if deadline is None else deadline
        if not self._lock.acquire(timeout=max(0, deadline - time.monotonic())):
            raise TimeoutError("relay deployment transaction is still stopping")
        try:
            thread = self._thread
        finally:
            self._lock.release()
        if thread is not None and thread is not threading.current_thread():
            thread.join(max(0, deadline - time.monotonic()))
        if thread is not None and thread.is_alive():
            raise TimeoutError("relay deployment worker is still stopping")
        if not self._lock.acquire(timeout=max(0, deadline - time.monotonic())):
            raise TimeoutError("relay deployment transaction is still stopping")
        try:
            self._reap()
        finally:
            self._lock.release()
        return True
