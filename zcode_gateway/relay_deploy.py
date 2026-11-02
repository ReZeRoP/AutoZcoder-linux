"""Edge relay deployers and testing for AutoZCoder / ZcodeGateway.

Deploys serverless edge worker relays to Cloudflare Workers, Vercel Edge,
and Deno Deploy. New deployments of the retained buffered Netlify template
are disabled. All templates implement the shared 9Router specification:
    x-relay-target: https://zcode.z.ai
    x-relay-path:   /api/v1/zcode-plan/anthropic/v1/messages
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import re
import secrets
import threading
import time
import zipfile
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit, urlunsplit

from urllib3.util import Timeout

# ---------------------------------------------------------------------------
# HTTP session: verify TLS against the Windows system cert store.
#
# A frozen PyInstaller build has no certifi bundle next to it, and some AV /
# corporate proxies re-sign TLS chains — both surface as
# "CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate" on the
# deploy APIs (api.netlify.com etc). truststore makes requests use the OS
# trust store instead, which is what browsers do.
# ---------------------------------------------------------------------------
try:
    import truststore
    import requests

    truststore.inject_into_ssl()
    _DEPLOY_SESSION = requests.Session()
except Exception:  # truststore not installed or not applicable
    import requests

    _DEPLOY_SESSION = requests.Session()
    try:
        import certifi
        _DEPLOY_SESSION.verify = certifi.where()
    except Exception:
        pass

# back-compat module-level names (deploy code calls requests.* directly)
get = _DEPLOY_SESSION.get
post = _DEPLOY_SESSION.post
put = _DEPLOY_SESSION.put
patch = _DEPLOY_SESSION.patch
delete = _DEPLOY_SESSION.delete

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

from .neterr import describe_http_error, is_transient


# Job status must use this allowlist, never exception text or provider bodies.
DEPLOY_ERROR_MESSAGES = {
    "deployment_stopped": "Deployment stopped locally. Check any remote resources before retrying.",
    "deployment_timeout": "The deployment time limit expired. Check the provider before retrying.",
    "remote_outcome_unknown": "The remote outcome is unknown. Reconcile in the provider dashboard before retrying.",
    "provider_transport_error": "The provider could not be reached securely or did not answer in time.",
    "provider_response_invalid": "The provider returned an invalid deployment response.",
    "provider_error": "The provider refused the deployment request.",
    "provider_auth": "The provider rejected the API token or its permissions.",
    "provider_rate_limited": "The provider API rate limit was reached.",
    "provider_conflict": "The provider reported a conflicting or invalid resource name.",
    "deployment_not_ready": "The remote deployment is not ready. Check the provider before retrying.",
    "deployment_failed": "The provider reported that the deployment failed.",
    "route_enable_failed": "Worker deployed but enabling its workers.dev route failed.",
    "subdomain_unavailable": "Worker deployed but its workers.dev subdomain could not be retrieved.",
    "netlify_streaming_unsupported": "New Netlify deployments are disabled: the bundled template buffers responses and has no verified streaming support.",
}
REMOTE_FIELDS = frozenset({
    "provider", "name", "id", "url", "account_id", "project_id", "deployment_id",
    "app_id", "revision_id", "site_id", "script_name",
})


def safe_remote(fields: dict[str, Any] | None) -> dict[str, str]:
    """Keep only bounded identifiers and credential-free public URLs.

    These fields are diagnostic hints, not authoritative proof of creation.
    Never copy provider dictionaries or raw error text to the job record.
    """
    out = {}
    for key, value in (fields or {}).items():
        if key not in REMOTE_FIELDS or not isinstance(value, str) or not value:
            continue
        if key == "url":
            try:
                parts = urlsplit(value)
                if (parts.scheme != "https" or not parts.hostname or parts.username
                        or parts.password or parts.port not in (None, 443)
                        or len(value) > 2048 or any(ord(c) < 33 for c in value)):
                    continue
                out[key] = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
            except ValueError:
                continue
        elif len(value) <= 128 and re.fullmatch(r"[A-Za-z0-9_.-]+", value):
            out[key] = value
    return out


class RelayDeployError(RuntimeError):
    """Legacy readable message plus safe structured deployment metadata.

    user_message/detail remain for the synchronous API's compatibility. Job
    status must project code through DEPLOY_ERROR_MESSAGES, never those strings.
    """

    def __init__(self, user_message: str, detail: str = "", *,
                 code: str = "provider_error", outcome_unknown: bool = False,
                 remote: dict[str, Any] | None = None) -> None:
        super().__init__(user_message)
        self.user_message = user_message
        self.detail = detail
        self.code = code if code in DEPLOY_ERROR_MESSAGES else "provider_error"
        self.outcome_unknown = bool(outcome_unknown)
        self.remote = safe_remote(remote)


class DeployControl:
    """One worker's finite budget, cooperative stop and mutation fence.

    check() raises; remaining() merely reports nonnegative monotonic time.
    checkpoint() records acknowledged remote IDs *before* checking stop so a
    late creation response remains available for manual reconciliation.
    """

    def __init__(self, timeout: float = 300, stop_event=None,
                 on_phase: Callable[..., None] | None = None) -> None:
        timeout = float(timeout)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Deployment timeout must be finite and positive")
        self.deadline = time.monotonic() + min(timeout, 300.0)
        self.stop_event = stop_event if stop_event is not None else threading.Event()
        self.on_phase = on_phase
        self.remote: dict[str, str] = {}
        self.mutation_attempted = False
        self._fenced = False

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def error(self, code: str, *, outcome_unknown: bool | None = None,
              message: str | None = None, detail: str = "") -> RelayDeployError:
        return RelayDeployError(
            message or DEPLOY_ERROR_MESSAGES[code], detail, code=code,
            outcome_unknown=self.mutation_attempted if outcome_unknown is None else outcome_unknown,
            remote=self.remote,
        )

    def check(self) -> None:
        if self.stop_event.is_set():
            raise self.error("deployment_stopped")
        if not self.remaining():
            raise self.error("deployment_timeout")
        if self._fenced:
            raise self.error("remote_outcome_unknown", outcome_unknown=True)

    def wait(self, seconds: float) -> None:
        self.check()
        seconds = float(seconds)
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("Deployment wait must be finite and nonnegative")
        self.stop_event.wait(min(seconds, self.remaining()))
        self.check()

    def checkpoint(self, phase: str, **safe_fields) -> None:
        fields = safe_remote(safe_fields)
        self.remote.update(fields)
        if self.on_phase is not None:
            self.on_phase(phase, **fields)
        self.check()

    def fence(self) -> None:
        self._fenced = True


def _close(response) -> None:
    # Some legacy response stubs do not define close().
    close = getattr(response, "close", None)
    if callable(close):
        close()


def _call(method: str, url: str, *, attempts: int = 4,
          control: DeployControl | None = None, **kw):
    """Only read-only requests may replay. A mutation can have acted even on
    a connection/SSL error or a 5xx, so fence rather than guessing or deleting.
    Every request and retry shares the worker's remaining monotonic budget.
    """
    control = control or DeployControl()
    verb = method.lower()
    mutation = verb not in ("get", "head", "options")
    call = getattr(requests, verb)
    requested_timeout = kw.pop("timeout", 30.0)
    if isinstance(requested_timeout, tuple):
        requested_timeout = min(requested_timeout)
    requested_timeout = float(requested_timeout)
    if not math.isfinite(requested_timeout) or requested_timeout <= 0:
        raise ValueError("Request timeout must be finite and positive")
    kw["allow_redirects"] = False
    kw["verify"] = True
    for i in range(max(1, attempts)):
        control.check()
        remaining = min(requested_timeout, control.remaining())
        kw["timeout"] = Timeout(total=remaining, connect=min(10.0, remaining), read=remaining)
        if mutation:
            control.mutation_attempted = True
        try:
            res = call(url, **kw)
        except requests.exceptions.RequestException as exc:
            if mutation:
                control.fence()
                raise control.error("remote_outcome_unknown", detail=type(exc).__name__) from exc
            control.check()
            if is_transient(exc) and i + 1 < attempts:
                control.wait(1.5 * (i + 1))
                continue
            # URL is an internal provider endpoint, never parsed from exception text.
            host = urlsplit(url).hostname or "the provider"
            raise control.error(
                "provider_transport_error", detail=type(exc).__name__,
                message=f"Could not reach {host} securely or it did not answer in time.",
            ) from exc
        if mutation and (res.status_code >= 500 or 300 <= res.status_code < 400):
            _close(res)
            control.fence()
            raise control.error("remote_outcome_unknown", outcome_unknown=True)
        if not mutation and res.status_code in (502, 503, 504) and i + 1 < attempts:
            _close(res)
            control.wait(1.5 * (i + 1))
            continue
        # Buffered requests have completed reading; JSON/text remain usable after
        # closing. Providers record IDs before their next checkpoint checks stop.
        _close(res)
        return res


def _api_error(provider: str, res, doing: str, control: DeployControl) -> RelayDeployError:
    status = res.status_code
    code = ("provider_auth" if status in (401, 403) else
            "provider_rate_limited" if status == 429 else
            "provider_conflict" if status in (409, 422) else "provider_error")
    return control.error(code, outcome_unknown=False,
                         message=describe_http_error(provider, status, "", doing))


def _response_json(res, control: DeployControl, *, creation: bool = False) -> dict:
    try:
        data = res.json()
        if not isinstance(data, dict):
            raise ValueError("not an object")
        return data
    except (ValueError, TypeError) as exc:
        if creation:
            control.fence()
        raise control.error("provider_response_invalid",
                            outcome_unknown=creation or control.mutation_attempted) from exc


def _remote_id(value, control: DeployControl) -> str:
    if not safe_remote({"id": value}):
        control.fence()
        raise control.error("provider_response_invalid", outcome_unknown=True)
    return value


def _remote_url(value: str, control: DeployControl) -> str:
    cleaned = safe_remote({"url": value}).get("url")
    if not cleaned or cleaned != value:
        control.fence()
        raise control.error("provider_response_invalid", outcome_unknown=True)
    return cleaned

# ---------------------------------------------------------------------------
# Worker Codes
# ---------------------------------------------------------------------------

# Provenance is scoped to cooperating templates, not a cryptographic attestation.
# Always overwrite upstream markers so a provider cannot accidentally masquerade
# as a local relay error. Native platform errors outside these handlers are legacy
# / unclassified unless a separately recognized host-specific signature exists.
_RELAY_HANDLER = """async function relay(request) {
  const localHeaders = {
    "content-type": "application/json",
    "x-autozcoder-response-origin": "relay",
    "x-autozcoder-relay-version": "1",
  };
  const target = request.headers.get("x-relay-target");
  const relayPath = request.headers.get("x-relay-path") || "/";
  if (!target) {
    return new Response(JSON.stringify({ error: "Missing x-relay-target header" }), {
      status: 400, headers: localHeaders,
    });
  }
  const targetUrl = target.replace(/\\/$/, "") + relayPath;
  const headers = new Headers(request.headers);
  for (const name of ["x-relay-target", "x-relay-path", "host",
                      "x-autozcoder-response-origin", "x-autozcoder-relay-version"]) {
    headers.delete(name);
  }
  const init = { method: request.method, headers };
  if (request.method !== "GET" && request.method !== "HEAD") {
    init.body = request.body;
    init.duplex = "half";
  }
  try {
    const response = await fetch(targetUrl, init);
    const responseHeaders = new Headers(response.headers);
    responseHeaders.set("x-autozcoder-response-origin", "upstream");
    responseHeaders.set("x-autozcoder-relay-version", "1");
    return new Response(response.body, {
      status: response.status, headers: responseHeaders,
    });
  } catch (error) {
    return new Response(JSON.stringify({ error: "Relay fetch failed" }), {
      status: 502, headers: localHeaders,
    });
  }
}
"""

CLOUDFLARE_WORKER_CODE = _RELAY_HANDLER + "\nexport default { fetch: relay };\n"
VERCEL_RELAY_CODE = ('export const config = { runtime: "edge" };\n'
                     + _RELAY_HANDLER + "\nexport default relay;\n")
DENO_RELAY_CODE = _RELAY_HANDLER + "\nDeno.serve(relay);\n"

NETLIFY_RELAY_CODE = """exports.handler = async (event) => {
  const headers = {};
  for (const [k, v] of Object.entries(event.headers || {})) {
    headers[k.toLowerCase()] = v;
  }
  const target = headers["x-relay-target"];
  const relayPath = headers["x-relay-path"] || "/";
  if (!target) {
    return {
      statusCode: 400,
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ error: "Missing x-relay-target header" }),
    };
  }
  const targetUrl = target.replace(/\\/$/, "") + relayPath;
  const forwardHeaders = { ...event.headers };
  for (const k of Object.keys(forwardHeaders)) {
    const lower = k.toLowerCase();
    if (lower === "x-relay-target" || lower === "x-relay-path" || lower === "host") {
      delete forwardHeaders[k];
    }
  }
  try {
    const upstream = await fetch(targetUrl, {
      method: event.httpMethod,
      headers: forwardHeaders,
      body: event.httpMethod !== "GET" && event.httpMethod !== "HEAD" && event.body
        ? event.isBase64Encoded
          ? Buffer.from(event.body, "base64")
          : event.body
        : undefined,
    });
    const responseHeaders = {};
    upstream.headers.forEach((value, key) => {
      responseHeaders[key] = value;
    });
    const contentType = upstream.headers.get("content-type") || "";
    const isText = new RegExp("^(text/|[^;]*json|[^;]*event-stream|[^;]*javascript|[^;]*xml|[^;]*urlencoded)", "i").test(contentType);
    const rawBody = await upstream.arrayBuffer();
    const responseBody = isText
      ? Buffer.from(rawBody).toString("utf8")
      : Buffer.from(rawBody).toString("base64");
    return {
      statusCode: upstream.status,
      headers: responseHeaders,
      body: responseBody,
      isBase64Encoded: !isText,
    };
  } catch (error) {
    return {
      statusCode: 502,
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ error: error.message || "Relay fetch failed" }),
    };
  }
};
"""

NETLIFY_INDEX_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>AutoZCoder Relay</title></head>
<body><p>AutoZCoder relay function lives at <code>/.netlify/functions/relay</code>.</p></body></html>
"""


# ---------------------------------------------------------------------------
# Cloudflare Deploy
# ---------------------------------------------------------------------------

def deploy_cloudflare(account_id: str, api_token: str, project_name: str = "", *,
                      control: DeployControl | None = None) -> dict[str, Any]:
    control = control or DeployControl()
    account_id = (account_id or "").strip()
    api_token = (api_token or "").strip()
    project_name = (project_name or "").strip() or f"cloudflare-relay-{int(time.time())}"
    if not account_id or not api_token:
        raise ValueError("Cloudflare Account ID and API Token are required")
    _remote_id(account_id, control)
    _remote_id(project_name, control)
    control.checkpoint("uploading", provider="cloudflare", name=project_name)

    script_url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/workers/scripts/{project_name}"
    headers = {"Authorization": f"Bearer {api_token}", "User-Agent": UA}
    metadata = {
        "main_module": "index.js",
        "compatibility_date": "2024-03-20",
        "observability": {"enabled": True},
    }
    files = {
        "index.js": ("index.js", CLOUDFLARE_WORKER_CODE.encode("utf-8"), "application/javascript+module"),
        "metadata": ("metadata.json", json.dumps(metadata).encode("utf-8"), "application/json"),
    }
    upload_res = _call("put", script_url, headers=headers, files=files, timeout=45, control=control)
    if not upload_res.ok:
        raise _api_error("Cloudflare", upload_res, "upload Workers scripts for this account id", control)
    uploaded = _response_json(upload_res, control, creation=True)
    if uploaded.get("success") is not True:
        control.fence()
        raise control.error("provider_response_invalid", outcome_unknown=True)
    control.checkpoint("remote_created", account_id=account_id, script_name=project_name)

    control.checkpoint("configuring")
    enable_res = _call("post", f"{script_url}/subdomain", headers=headers,
                       json={"enabled": True}, timeout=30, control=control)
    if not enable_res.ok:
        raise control.error("route_enable_failed", outcome_unknown=False)
    enabled = _response_json(enable_res, control, creation=True).get("result")
    if not isinstance(enabled, dict) or enabled.get("enabled") is not True:
        raise control.error("route_enable_failed", outcome_unknown=False)
    control.check()

    account_subdomain_url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/workers/subdomain"
    sub_res = _call("get", account_subdomain_url, headers=headers, timeout=30, control=control)
    data = _response_json(sub_res, control).get("result") if sub_res.ok else None
    subdomain = data.get("subdomain") if isinstance(data, dict) else None
    if not subdomain:
        raise control.error("subdomain_unavailable", outcome_unknown=False)
    _remote_id(subdomain, control)
    deploy_url = _remote_url(f"https://{project_name}.{subdomain}.workers.dev", control)
    control.checkpoint("ready", url=deploy_url)
    return {"ok": True, "name": project_name, "type": "cloudflare", "url": deploy_url}


# ---------------------------------------------------------------------------
# Vercel Deploy
# ---------------------------------------------------------------------------

def deploy_vercel(vercel_token: str, project_name: str = "", *,
                  control: DeployControl | None = None) -> dict[str, Any]:
    control = control or DeployControl()
    vercel_token = (vercel_token or "").strip()
    project_name = (project_name or "").strip() or f"vercel-relay-{int(time.time())}"
    if not vercel_token:
        raise ValueError("Vercel API token is required")
    _remote_id(project_name, control)
    control.checkpoint("creating", provider="vercel", name=project_name)
    headers = {
        "Authorization": f"Bearer {vercel_token}",
        "Content-Type": "application/json",
        "User-Agent": UA,
    }
    payload = {
        "name": project_name,
        "files": [
            {"file": "api/relay.js", "data": VERCEL_RELAY_CODE},
            {"file": "package.json", "data": json.dumps({"name": project_name, "version": "1.0.0"})},
            {"file": "vercel.json", "data": json.dumps({"rewrites": [{"source": "/(.*)", "destination": "/api/relay"}]})},
        ],
        "projectSettings": {"framework": None},
        "target": "production",
    }
    res = _call("post", "https://api.vercel.com/v13/deployments", headers=headers,
                json=payload, timeout=45, control=control)
    if not res.ok:
        raise _api_error("Vercel", res, "create deployments", control)
    deployment = _response_json(res, control, creation=True)
    deployment_id = deployment.get("id") or deployment.get("uid")
    project_id = deployment.get("projectId") or project_name
    fields = {"deployment_id": deployment_id, "project_id": project_id}
    if isinstance(deployment.get("url"), str):
        fields["url"] = f"https://{deployment['url']}"
    # Save every safe hint even if a sibling field is malformed, or stop arrived
    # while the creation response was in flight. Never lose a known deployment ID.
    control.checkpoint("remote_created", **fields)
    deployment_id = _remote_id(deployment_id, control)
    project_id = _remote_id(project_id, control)
    if "url" in fields:
        _remote_url(fields["url"], control)

    # No broad catch here: losing the configuration response is also ambiguous.
    control.checkpoint("configuring")
    protection = _call("patch", f"https://api.vercel.com/v9/projects/{project_id}",
                       headers=headers, json={"ssoProtection": None}, timeout=15, control=control)
    if not protection.ok:
        raise _api_error("Vercel", protection, "disable deployment protection", control)

    deadline = min(control.deadline, time.monotonic() + 120)
    control.checkpoint("waiting_ready")
    while time.monotonic() < deadline:
        control.check()
        try:
            poll = _call("get", f"https://api.vercel.com/v13/deployments/{deployment_id}",
                         headers=headers, timeout=min(20, deadline - time.monotonic()),
                         attempts=1, control=control)
        except RelayDeployError as exc:
            if exc.code != "provider_transport_error":
                raise
        else:
            if poll.ok:
                data = _response_json(poll, control)
                state = data.get("readyState")
                if state == "READY":
                    value = data.get("url")
                    if not isinstance(value, str):
                        raise control.error("provider_response_invalid")
                    deploy_url = _remote_url(f"https://{value}", control)
                    control.checkpoint("ready", url=deploy_url)
                    return {"ok": True, "name": project_name, "type": "vercel", "url": deploy_url}
                if state in ("ERROR", "CANCELED"):
                    raise control.error("deployment_failed", outcome_unknown=False)
            elif poll.status_code in (401, 403):
                raise _api_error("Vercel", poll, "check deployment readiness", control)
        control.wait(min(3, max(0, deadline - time.monotonic())))
    control.check()
    control.fence()
    raise control.error("deployment_not_ready", outcome_unknown=True)


# ---------------------------------------------------------------------------
# Deno Deploy
# ---------------------------------------------------------------------------

def deploy_deno(deno_token: str, org_domain: str, app_name: str = "", *,
                control: DeployControl | None = None) -> dict[str, Any]:
    control = control or DeployControl()
    deno_token = (deno_token or "").strip()
    org_domain = (org_domain or "").strip()
    app_name = (app_name or "").strip() or f"deno-relay-{int(time.time())}"
    if not deno_token:
        raise ValueError("Deno Deploy API token is required")
    if not org_domain:
        raise ValueError("Organization domain is required (e.g. your-org.deno.net)")
    _remote_id(app_name, control)
    org_slug = org_domain.split(".")[0].strip()
    _remote_id(org_slug, control)
    deploy_url = _remote_url(f"https://{app_name}.{org_slug}.deno.net", control)
    control.checkpoint("creating", provider="deno", name=app_name)
    headers = {
        "Authorization": f"Bearer {deno_token}",
        "Content-Type": "application/json",
        "User-Agent": UA,
    }
    app_res = _call("post", "https://api.deno.com/v2/apps", headers=headers, json={
        "slug": app_name,
        "labels": {"custom.kind": "autozcoder-relay"},
        "config": {"install": "deno install", "runtime": {"type": "dynamic", "entrypoint": "main.ts"}},
    }, timeout=30, control=control)
    if not app_res.ok:
        raise _api_error("Deno Deploy", app_res, "create apps in this organization", control)
    app_data = _response_json(app_res, control, creation=True)
    app_id = _remote_id(app_data.get("id"), control)
    control.checkpoint("remote_created", app_id=app_id, url=deploy_url)

    control.checkpoint("uploading")
    deploy_res = _call("post", f"https://api.deno.com/v2/apps/{app_id}/deploy", headers=headers, json={
        "assets": {
            "main.ts": {"kind": "file", "content": DENO_RELAY_CODE, "encoding": "utf-8"},
        }
    }, timeout=45, control=control)
    if not deploy_res.ok:
        # No destructive rollback: creation happened and its state must remain
        # available for reconciliation, including failures and local cancellation.
        raise _api_error("Deno Deploy", deploy_res, "deploy to this app", control)
    revision = _response_json(deploy_res, control, creation=True)
    revision_id = _remote_id(revision.get("id"), control)
    control.checkpoint("remote_created", revision_id=revision_id)

    deadline = min(control.deadline, time.monotonic() + 60)
    control.checkpoint("waiting_ready")
    while time.monotonic() < deadline:
        control.check()
        try:
            rev_res = _call("get", f"https://api.deno.com/v2/revisions/{revision_id}",
                            headers=headers, timeout=min(15, deadline - time.monotonic()),
                            attempts=1, control=control)
        except RelayDeployError as exc:
            if exc.code != "provider_transport_error":
                raise
        else:
            if rev_res.ok:
                state = _response_json(rev_res, control).get("status")
                if state == "succeeded":
                    control.checkpoint("ready", url=deploy_url)
                    return {"ok": True, "name": app_name, "type": "deno", "url": deploy_url}
                if state in ("failed", "error", "canceled", "cancelled"):
                    raise control.error("deployment_failed", outcome_unknown=False)
                if state not in ("queued", "building"):
                    raise control.error("provider_response_invalid")
            elif rev_res.status_code in (401, 403):
                raise _api_error("Deno Deploy", rev_res, "check deployment readiness", control)
        control.wait(min(2, max(0, deadline - time.monotonic())))
    control.check()
    control.fence()
    raise control.error("deployment_not_ready", outcome_unknown=True)


# ---------------------------------------------------------------------------
# Netlify Deploy
# ---------------------------------------------------------------------------

def _build_netlify_zip() -> tuple[bytes, str]:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("relay.js", NETLIFY_RELAY_CODE)
    zip_bytes = buf.getvalue()
    sha256 = hashlib.sha256(zip_bytes).hexdigest()
    return zip_bytes, sha256


def deploy_netlify(netlify_token: str, site_name: str = "", *,
                   control: DeployControl | None = None) -> dict[str, Any]:
    # The legacy ZIP is intentionally retained for inspection, not deployment.
    # A Request/Response export is not verified compatible with that package.
    # Reject before token validation, control callbacks, or any external call.
    raise RelayDeployError(DEPLOY_ERROR_MESSAGES["netlify_streaming_unsupported"],
                           code="netlify_streaming_unsupported")


# ---------------------------------------------------------------------------
# Test Relay Connectivity
# ---------------------------------------------------------------------------

PROBE_MAX_BYTES = 64 * 1024
_PROBE_ERRORS = {
    "invalid_relay_url": "Invalid relay URL: a credential-free HTTPS address is required.",
    "invalid_probe_timeout": "The relay probe timeout must be finite and positive.",
    "probe_timeout": "The relay did not answer in time within the forwarding probe limit.",
    "probe_tls_error": "The relay's TLS connection or security certificate could not be verified.",
    "probe_network_error": "The forwarding probe could not reach the relay securely.",
    "probe_redirect": "The relay redirected the forwarding probe; forwarding was not verified.",
    "probe_too_large": "The forwarding probe response exceeded 64 KiB.",
    "probe_invalid_json": "The relay did not return the expected JSON forwarding response.",
    "probe_mismatch": "The forwarding response did not match this probe's target and challenge.",
    "probe_encoded_body": "The forwarding probe returned an unsupported encoded response.",
    "probe_read_unsupported": "This HTTP transport cannot safely read a bounded forwarding probe.",
}


class _ProbeError(Exception):
    pass


def _probe_remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _ProbeError("probe_timeout")
    return remaining


def _probe_body(response, deadline: float) -> bytes:
    """Bound bytes *and* elapsed time, rather than buffered .text/.json().

    urllib3 read1 makes at most one underlying read for an identity-encoded body;
    resetting its socket timeout before each read stops slow-trickle responses
    from repeatedly gaining a fresh twelve seconds. Unsupported real transports
    fail closed instead of silently falling back to an unbounded buffered read.
    """
    raw = response.raw
    read1 = getattr(raw, "read1", None)
    if not callable(read1):
        raise _ProbeError("probe_read_unsupported")
    chunks = []
    size = 0
    while True:
        remaining = _probe_remaining(deadline)
        # requests/urllib3 keeps the socket either on the connection or on the
        # HTTPResponse's buffered file. The latter also covers Connection: close.
        sock = getattr(getattr(raw, "_connection", None), "sock", None)
        if sock is None:
            fp = getattr(getattr(raw, "_fp", None), "fp", None)
            sock = getattr(getattr(fp, "raw", None), "_sock", None)
        if sock is not None:
            sock.settimeout(remaining)
        elif isinstance(raw, requests.packages.urllib3.response.HTTPResponse) and not raw.closed:
            raise _ProbeError("probe_read_unsupported")
        chunk = read1(min(8192, PROBE_MAX_BYTES + 1 - size), decode_content=False)
        _probe_remaining(deadline)
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > PROBE_MAX_BYTES:
            raise _ProbeError("probe_too_large")
        chunks.append(chunk)


def ping_relay(relay_url: str, timeout: float = 12.0) -> dict[str, Any]:
    """Verify a single fresh forwarding echo, never model/streaming capability.

    This is a cooperative forwarding challenge, not a cryptographic attestation
    of arbitrary relay hosts. No inference is sent and no response text is leaked.
    """
    t0 = time.monotonic()
    response = None

    def result(ok: bool, status: int, code: str = "", error: str = "") -> dict[str, Any]:
        return {"ok": ok, "status": status, "error": error or _PROBE_ERRORS.get(code, ""),
                "elapsed_ms": max(0, int((time.monotonic() - t0) * 1000)),
                "probe_kind": "forwarding", "probe_verified": ok, "error_code": code,
                "model_capability": "not_tested", "streaming_capability": "not_tested"}

    relay_url = (relay_url or "").strip()
    try:
        parts = urlsplit(relay_url)
        if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
                or parts.fragment or any(ord(c) < 33 for c in relay_url)):
            return result(False, 400, "invalid_relay_url")
        parts.port  # invalid/out-of-range ports raise ValueError
    except ValueError:
        return result(False, 400, "invalid_relay_url")
    try:
        timeout = float(timeout)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError
    except (ValueError, TypeError):
        return result(False, 400, "invalid_probe_timeout")
    deadline = t0 + min(timeout, 12.0)
    nonce = secrets.token_urlsafe(24)
    headers = {
        "x-relay-target": "https://httpbin.org",
        "x-relay-path": f"/get?autozcoder_probe={nonce}",
        "User-Agent": UA,
        "Accept": "application/json",
        "Accept-Encoding": "identity",
        "Cache-Control": "no-cache, no-store",
    }
    try:
        remaining = _probe_remaining(deadline)
        response = requests.get(
            relay_url, headers=headers,
            timeout=Timeout(total=remaining, connect=min(5.0, remaining), read=remaining),
            verify=True, allow_redirects=False, stream=True,
        )
        _probe_remaining(deadline)
        status = response.status_code
        if 300 <= status < 400:
            return result(False, status, "probe_redirect")
        if status != 200:
            return result(False, status, "probe_http_error", _ping_status_text(status))
        if response.headers.get("content-encoding", "identity").lower() not in ("", "identity"):
            return result(False, status, "probe_encoded_body")
        if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            return result(False, status, "probe_invalid_json")
        length = response.headers.get("content-length")
        if length is not None:
            try:
                if int(length) > PROBE_MAX_BYTES:
                    return result(False, status, "probe_too_large")
            except (ValueError, TypeError):
                return result(False, status, "probe_invalid_json")
        body = _probe_body(response, deadline)
        try:
            data = json.loads(body)
        except (ValueError, UnicodeError):
            return result(False, status, "probe_invalid_json")
        if not isinstance(data, dict) or data.get("args") != {"autozcoder_probe": nonce}:
            return result(False, status, "probe_mismatch")
        echoed = data.get("url")
        if not isinstance(echoed, str):
            return result(False, status, "probe_mismatch")
        target = urlsplit(echoed)
        if (target.scheme != "https" or target.hostname != "httpbin.org"
                or target.port not in (None, 443) or target.username or target.password
                or target.path != "/get" or target.fragment
                or parse_qs(target.query, keep_blank_values=True) != {"autozcoder_probe": [nonce]}):
            return result(False, status, "probe_mismatch")
        _probe_remaining(deadline)
        return result(True, status)
    except _ProbeError as exc:
        return result(False, getattr(response, "status_code", 500), str(exc))
    except requests.exceptions.SSLError:
        return result(False, 500, "probe_tls_error")
    except (requests.exceptions.Timeout, TimeoutError):
        return result(False, 500, "probe_timeout")
    except Exception:
        return result(False, 500, "probe_network_error")
    finally:
        if response is not None:
            _close(response)


def _ping_status_text(status: int, body: str = "") -> str:
    """A status alone cannot tell host errors from forwarded provider errors.
    The legacy body argument is intentionally ignored (it can contain secrets).
    """
    if status in (401, 403):
        return f"The relay refused the request (HTTP {status}) - deployment protection or a wrong URL."
    if status == 404:
        return "Nothing is deployed at this relay URL (HTTP 404) - check the address."
    if status == 429:
        return "The forwarding probe was rate-limited (HTTP 429); its origin is not verified."
    return f"The forwarding probe returned HTTP {status}; forwarding was not verified."


# Backward-compatible alias
test_relay = ping_relay
