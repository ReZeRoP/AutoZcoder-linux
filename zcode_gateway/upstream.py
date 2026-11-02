"""Upstream layer: fingerprint headers, body variants, HTTP client, error model.

All shapes here come from the Phase-1 capture. Do not "improve" the header set
or the body variants without re-capturing — the upstream fingerprints the client.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, AsyncIterator

import httpx

from .config import UpstreamConfig

log = logging.getLogger("zcode_gateway.upstream")

# --------------------------------------------------------------------------- #
# headers
# --------------------------------------------------------------------------- #

# The 16 static headers observed on every captured call. `authorization` and
# `x-task-id` are filled per request; `host` and `content-length` are set by httpx.
STATIC_HEADERS = {"accept": "*/*", "content-type": "application/json",
                  "http-referer": "https://zcode.z.ai",
                  "anthropic-version": "2023-06-01",
                  "anthropic-beta": "mid-conversation-system-2026-04-07"}
REQUIRED_HEADER_NAMES = set(STATIC_HEADERS) | {"authorization", "x-api-key", "x-device-mid",
    "user-agent", "x-zcode-app-version", "x-zcode-agent"}
VARIANTS = ("anthropic",)

def new_task_id():
    return str(uuid.uuid4())

def merge_anthropic_beta(base: str, extra: str | None) -> str:
    """Comma-merge the captured static beta with a client-supplied one.

    A client sending its own `anthropic-beta` used to be silently overridden
    by the static captured value; when Z.ai rotates the beta, clients that
    already know the new one had no way to pass it through. Order-stable,
    dedupes, keeps our captured beta first.
    """
    parts = [p.strip() for p in (base or '').split(',') if p.strip()]
    for p in (extra or '').split(','):
        p = p.strip()
        if p and p not in parts:
            parts.append(p)
    return ','.join(parts)


def build_headers(access_token, fp, task_id=None, *, device_mid=None, include_device_mid=True,
                  anthropic_beta: str | None = None):
    token = access_token.removeprefix("Bearer ").strip()
    h = dict(STATIC_HEADERS)
    if anthropic_beta:
        h["anthropic-beta"] = merge_anthropic_beta(
            STATIC_HEADERS.get("anthropic-beta", ""), anthropic_beta)
    h.update({"authorization": "Bearer " + token, "x-api-key": token,
        "user-agent": fp.user_agent, "x-device-mid": device_mid or fp.device_mid,
        "x-client-language": fp.language, "x-client-timezone": fp.timezone,
        "x-os-category": fp.os_category, "x-os-version": fp.os_version,
        "x-platform": fp.platform, "x-release-channel": "production",
        "x-title": "Z Code@electron", "x-zcode-agent": "glm",
        "x-zcode-app-version": fp.client_version, "x-zcode-session-type": "main",
        "x-query-id": new_task_id(), "x-request-id": new_task_id(),
        "x-zcode-trace-id": new_task_id(), "x-session-id": task_id or new_task_id()})
    # The model captures carry the device in metadata, while billing/OAuth
    # requests carry X-Device-Mid. Keep those two observed header sets distinct.
    if not include_device_mid:
        h.pop('x-device-mid')
    return h

def build_upstream_body(payload, variant="anthropic", default_max_tokens=128000,
                        anthropic_cache_control=True, model_caps=None,
                        default_effort='high'):
    from .zcode_protocol import build_body
    return build_body(payload, default_max_tokens, model_caps=model_caps,
                      default_effort=default_effort)


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #


class ErrorKind(str, Enum):
    OK = "ok"
    INSUFFICIENT_CREDITS = "insufficient_credits"
    UNAUTHORIZED = "unauthorized"
    ENTITLEMENT = "entitlement"     # 403: no plan/subscription for THIS model
    EDGE_BLOCK = "edge_block"       # 403 HTML from the CDN/edge, not the API
    RATE_LIMITED = "rate_limited"
    SERVER = "server"
    CLIENT = "client"
    TRANSPORT = "transport"


# Codes that mean "this account cannot use this model", not "this account is bad".
ENTITLEMENT_CODES = frozenset({
    "ENTITLEMENT_ERROR",
    "NOT_SUBSCRIBED",
    "PLAN_REQUIRED",
    "MODEL_NOT_IN_PLAN",
})


def is_entitlement_code(code: str) -> bool:
    """Single source of truth for entitlement classification.

    Three call sites used to classify independently (this frozenset plus two
    substring checks), and they had already drifted: NOT_SUBSCRIBED matched
    here but not the substring test. Accepts exact codes and any code
    containing ENTITLEMENT/SUBSCRIB/PLAN (covers future plan-gate codes).
    """
    c = (code or "").upper()
    if not c:
        return False
    if c in ENTITLEMENT_CODES:
        return True
    return ("ENTITLEMENT" in c) or ("SUBSCRIB" in c) or ("PLAN" in c)


# Abuse-heuristic rejection: HTTP 405 + code 3012, message
# "request has been blocked due to unusual activity." It is neither a quota nor
# an account defect, and it is scoped to one account AND one model at a time —
# the same account keeps serving other models (captured: GLM-5.3 blocked while
# GLM-5.3-Flash still returns 200). Treated like a per-model park: the account
# is skipped for that model and the request rotates to the next account.
BLOCKED_CODES = frozenset({"3012", "405", "HTTP_405"})


def is_block_code(code: str, status: int = 0) -> bool:
    if status == 405:
        return True
    c = str(code or "").upper()
    return c in BLOCKED_CODES or "3012" in c or "405" in c


def _error_code(body: str | bytes | None) -> str:
    """Best-effort extraction of error.code from a JSON error body."""
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else (body or "")
    try:
        parsed = json.loads(text)
    except Exception:
        return ""
    err = parsed.get("error", parsed)
    if isinstance(err, dict):
        return str(err.get("code") or err.get("statusCode") or "").upper()
    return ""


# Captured 2026-09-16: a global 403 served as HTML by Google's front end, for every
# model regardless of entitlement:
#   <html>...<title>403 Forbidden</title>...
#   <h2>Your client does not have permission to get URL
#       <code>/api/v1/chat/completions</code> from this server.</h2>
# This is infrastructure, not the API, and not the account's fault.
EDGE_BLOCK_MARKERS = (
    "does not have permission to get url",
    "403 forbidden",
    "error: forbidden",
)


def _looks_like_edge_block(body: str | bytes | None) -> bool:
    text = (body.decode("utf-8", "replace") if isinstance(body, bytes)
            else (body or "")).lower()
    if "<html" not in text:
        return False
    return any(marker in text for marker in EDGE_BLOCK_MARKERS)


def classify(status: int, body: str | bytes | None = None) -> ErrorKind:
    info = parse_upstream_error(status, body)
    if info["code"] == "11113":
        return ErrorKind.INSUFFICIENT_CREDITS
    if status == 200:
        return ErrorKind.OK
    if status == 401:
        return ErrorKind.UNAUTHORIZED
    if status == 403:
        if is_entitlement_code(_error_code(body)):
            return ErrorKind.ENTITLEMENT
        if _looks_like_edge_block(body):
            return ErrorKind.EDGE_BLOCK
        return ErrorKind.CLIENT
    if status == 402:
        return ErrorKind.INSUFFICIENT_CREDITS
    if status == 429:
        return ErrorKind.RATE_LIMITED
    if 500 <= status < 600:
        return ErrorKind.SERVER
    return ErrorKind.CLIENT


def edge_block_message(body: str | bytes | None) -> str:
    """Short, human-readable summary of an edge block (bodies are HTML)."""
    text = (body.decode("utf-8", "replace") if isinstance(body, bytes)
            else (body or ""))
    lowered = text.lower()
    if "does not have permission to get url" in lowered:
        return ("upstream edge rejected the request (403): the client has no "
                "permission for this URL")
    return "upstream edge rejected the request (403 Forbidden)"


def parse_upstream_error(status: int, body: str | bytes | None) -> dict[str, Any]:
    """Normalise an upstream error body (captured shape) into a dict."""
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else (body or "")
    try:
        parsed = json.loads(text)
        err = parsed.get("error", parsed)
        if isinstance(err, dict):
            return {
                "code": str(err.get("code") or err.get("statusCode") or err.get("type") or f"http_{status}"),
                "message": err.get("message") or err.get("msg") or text[:400],
                "raw": parsed,
            }
    except Exception:
        pass
    return {"code": f"http_{status}", "message": text[:400], "raw": None}


# Mapping for the OpenAI-compatible error envelope.
ERROR_TYPE_BY_STATUS = {
    400: "invalid_request_error",
    401: "authentication_error",
    402: "insufficient_quota",
    403: "permission_error",
    404: "not_found_error",
    413: "invalid_request_error",
    429: "rate_limit_error",
    500: "server_error",
    502: "server_error",
    503: "server_error",
    504: "server_error",
}


def openai_error(status: int, code: str, message: str,
                 extra: dict[str, Any] | None = None) -> dict[str, Any]:
    error = {
        "message": message,
        "type": ERROR_TYPE_BY_STATUS.get(status, "api_error"),
        "param": None,
        "code": code,
    }
    if extra:
        error.update(extra)
    return {"error": error}


def anthropic_error(status: int, code: str, message: str,
                    extra: dict[str, Any] | None = None) -> dict[str, Any]:
    # Anthropic error types: invalid_request_error, authentication_error,
    # permission_error, not_found_error, request_too_large, rate_limit_error,
    # api_error, overloaded_error
    mapping = {
        400: "invalid_request_error",
        401: "authentication_error",
        403: "permission_error",
        404: "not_found_error",
        413: "request_too_large",
        429: "rate_limit_error",
    }
    if status >= 500:
        etype = "overloaded_error" if status == 529 else "api_error"
    else:
        etype = mapping.get(status, "api_error")
    error: dict[str, Any] = {"type": etype, "message": message}
    # Anthropic's envelope has no standard code field, but clients doing
    # programmatic retry need the machine-readable code the OpenAI dialect
    # already exposes — carry it as an additive field (ignored by strict clients)
    if code:
        error["code"] = code
    if extra:
        error.update(extra)
    return {"type": "error", "error": error}


# --------------------------------------------------------------------------- #
# client
# --------------------------------------------------------------------------- #


@dataclass
class UpstreamResponse:
    status_code: int
    headers: dict[str, str]
    _resp: httpx.Response
    relay_url: str = ""        # which relay carried it ('' = direct); for failure logs

    async def aread(self) -> bytes:
        return await self._resp.aread()

    def aiter_bytes(self) -> AsyncIterator[bytes]:
        return self._resp.aiter_bytes()

    async def aclose(self) -> None:
        await self._resp.aclose()


class UpstreamClient:
    """Thin httpx wrapper. Adds nothing to the request the caller did not set.

    A GLOBAL randomized pacing gate spaces out upstream sends (config
    upstream.pacing): one egress IP hammering the CDN with zero gap is the
    traffic shape edge blocks are trained on. The gate is shared across all
    accounts and workers, randomized so parallel requests don't
    self-synchronize into lockstep.
    """

    _last_send_at: float = 0.0            # class-level: shared per process
    _pace_lock: asyncio.Lock | None = None

    def __init__(self, cfg: UpstreamConfig, relay_manager: Any = None,
                 pacing=None) -> None:
        self.cfg = cfg
        self.relay_manager = relay_manager
        self.pacing = pacing               # PacingConfig or None = off
        if UpstreamClient._pace_lock is None:
            UpstreamClient._pace_lock = asyncio.Lock()
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(cfg.timeout_read, connect=cfg.timeout_connect),
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
            headers={},          # never inject a default User-Agent
            follow_redirects=False,
            http2=False,
        )

    async def _pace(self) -> None:
        """Hold the shared lock for a randomized gap after the previous send.
        Waiting INSIDE the lock is what serializes the gap: concurrent callers
        queue, and each observes the gap of the one before it."""
        if self.pacing is None or not getattr(self.pacing, 'enabled', False):
            return
        import random as _random
        gap = _random.uniform(self.pacing.min_gap_ms / 1000.0,
                              self.pacing.max_gap_ms / 1000.0)
        async with UpstreamClient._pace_lock:
            elapsed = time.monotonic() - UpstreamClient._last_send_at
            wait = gap - elapsed
            if wait > 0:
                await asyncio.sleep(wait)
            UpstreamClient._last_send_at = time.monotonic()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def send(self, headers: dict[str, str], body: dict[str, Any],
                   stream: bool, control=None) -> UpstreamResponse:
        # Rotation: get_relay_for_model applies the strategy (single /
        # round_robin / random) and skips cooled-down relays.
        tried_relay_urls: list[str] = []
        max_relay_tries = 1
        if self.relay_manager and self.relay_manager.get_relay_count() > 1:
            max_relay_tries = min(3, self.relay_manager.get_relay_count())

        last_exc: Exception | None = None
        for attempt in range(max_relay_tries):
            if control is not None:
                control.phase = 'pacing'
                control.check()
            await self._pace()
            if control is not None:
                control.check()
                control.phase = 'opening'
            relay_url = None
            if self.relay_manager:
                req_model = str(body.get("model") or "")
                required = getattr(self.relay_manager, 'relay_required_for_model', None)
                if required is not None:
                    relay_url = self.relay_manager.get_relay_for_model(
                        req_model, exclude_urls=set(tried_relay_urls))
                else:
                    # Third-party injected managers retain their older interface.
                    relay_url = self.relay_manager.get_relay_for_model(req_model)
                if relay_url in tried_relay_urls:
                    break
                if not relay_url and (tried_relay_urls or (required is not None and required(req_model))):
                    if last_exc is not None:
                        raise last_exc
                    raise RelayRoutingError('relay_candidates_exhausted',
                        'No distinct relay candidate is available; direct routing was not attempted.',
                        _relay_diagnostics(control, attempt + 1, self.relay_manager, None,
                                           reason='candidates_exhausted'))

            try:
                if relay_url:
                    import urllib.parse
                    parsed = urllib.parse.urlparse(self.cfg.chat_url)
                    target = f"{parsed.scheme}://{parsed.netloc}"
                    relay_path = parsed.path + (f"?{parsed.query}" if parsed.query else "")

                    fwd_headers = dict(headers)
                    fwd_headers["x-relay-target"] = target
                    fwd_headers["x-relay-path"] = relay_path
                    req = self._client.build_request(
                        "POST", relay_url, headers=fwd_headers, json=body,
                    )
                else:
                    if attempt > 0:
                        break  # no relay path left — re-raise the last error
                    req = self._client.build_request(
                        "POST", self.cfg.chat_url, headers=headers, json=body,
                    )
                if control is not None:
                    control.before_send()
                resp = await self._client.send(req, stream=stream)
            except httpx.HTTPError as exc:
                # relay (or direct) transport failure: mark + rotate
                if relay_url and self.relay_manager:
                    self.relay_manager.mark_relay_failed(relay_url)
                    tried_relay_urls.append(relay_url)
                    last_exc = TransportError(str(exc))
                    continue
                raise TransportError(str(exc)) from exc

            # Until a live UpstreamResponse is returned, this layer owns resp.
            # In particular, a cancelled/failed relay-405 body read must close
            # here: the service's send await has not received the response yet.
            transferred = False
            try:
                if relay_url and resp.status_code == 429 and self.relay_manager:
                    # Only trusted relay markers or narrow native error shapes
                    # distinguish host quota from an upstream/account limit.
                    try:
                        raw = await _bounded_relay_error(resp)
                    except (ValueError, TimeoutError, httpx.HTTPError) as exc:
                        outcome = (exc.reason if isinstance(exc, RelayErrorReadFailure)
                                   else 'read_timeout' if isinstance(exc, TimeoutError)
                                   else 'read_failure')
                        evidence = _relay_rate_limit_evidence(resp.headers, None)
                        evidence.update(body_shape='unreadable', reason=outcome)
                        diagnostics = _relay_diagnostics(control, attempt + 1,
                            self.relay_manager, relay_url, resp.headers, evidence,
                            read_outcome=outcome, body_bytes=getattr(exc, 'body_bytes', None))
                        raise RelayRoutingError('relay_rate_limit_ambiguous',
                            'The relay rate-limit origin could not be verified; no account was penalized.',
                            diagnostics) from None
                    evidence = _relay_rate_limit_evidence(resp.headers, raw)
                    origin = evidence['origin']
                    if origin == 'relay':
                        self.relay_manager.mark_relay_failed(relay_url)
                        tried_relay_urls.append(relay_url)
                        last_exc = RelayRoutingError('relay_host_rate_limited',
                            'The relay host is rate-limiting requests; distinct relay attempts were exhausted.',
                            _relay_diagnostics(control, attempt + 1, self.relay_manager,
                                relay_url, resp.headers, evidence, body_bytes=len(raw)))
                        continue
                    if origin == 'unknown':
                        raise RelayRoutingError('relay_rate_limit_ambiguous',
                            'The relay returned an unclassified rate limit; check the relay host before retrying.',
                            _relay_diagnostics(control, attempt + 1, self.relay_manager,
                                relay_url, resp.headers, evidence, body_bytes=len(raw)))
                    busy = provider_busy_error(raw, resp.headers, control=control,
                        manager=self.relay_manager, relay_url=relay_url, attempt=attempt + 1)
                    if busy is not None:
                        raise busy
                    synthetic = _decoded_response(429, raw, resp.headers)
                    return UpstreamResponse(status_code=429,
                        headers=dict(synthetic.headers), _resp=synthetic, relay_url=relay_url)
                if relay_url and resp.status_code >= 500 and self.relay_manager:
                    # the relay itself is unhappy — cool it and try the next one
                    self.relay_manager.mark_relay_failed(relay_url)
                    tried_relay_urls.append(relay_url)
                    last_exc = TransportError(f"relay {relay_url} returned {resp.status_code}")
                    continue

                if relay_url and resp.status_code == 405 and self.relay_manager:
                    # 405/3012 through a relay = the RELAY'S exit IP is flagged
                    # by risk control ("unusual activity"). Rotating ACCOUNTS
                    # cannot help — every account goes through the same relay —
                    # so cool this relay and immediately try the next one before
                    # the error ever reaches the account layer. Read its JSON
                    # error under the same ownership guard as relay rotation.
                    body_peek = await resp.aread()
                    if b"3012" in body_peek or b"unusual activity" in body_peek:
                        self.relay_manager.mark_relay_failed(relay_url)
                        tried_relay_urls.append(relay_url)
                        last_exc = UpstreamBlockThroughRelay(
                            f"relay {relay_url} exit IP flagged (405/3012)")
                        continue
                    # plain 405 without 3012: a request-shape block, not the
                    # relay's IP — fall through to the caller (account layer)
                    synthetic = _decoded_response(405, body_peek, resp.headers)
                    return UpstreamResponse(status_code=405,
                                            headers=dict(synthetic.headers),
                                            _resp=synthetic, relay_url=relay_url or "")

                if relay_url and self.relay_manager:
                    self.relay_manager.mark_relay_ok(relay_url)
                result = UpstreamResponse(
                    status_code=resp.status_code,
                    headers={k.lower(): v for k, v in resp.headers.items()},
                    _resp=resp,
                    relay_url=relay_url or "",
                )
                transferred = True
                return result
            finally:
                if not transferred:
                    if control is None:
                        await resp.aclose()
                    else:
                        closing = control.spawn(resp.aclose(), 'verified-upstream-close')
                        async def wait_close():
                            done, _ = await asyncio.wait({closing}, timeout=control.cleanup_timeout)
                            if closing in done:
                                await closing
                            else:
                                # Resistant closes stay owned and retain admission.
                                control.cancel_once(closing)
                                if control.failure is None:
                                    control.failure = control.error(504, 'stream_cleanup_timeout',
                                        'The upstream response could not close before its cleanup deadline.')
                                raise control.failure
                        # The timeout must survive cancellation of this send, too.
                        waiter = control.spawn(wait_close(), 'verified-close-wait')
                        await asyncio.shield(waiter)

        if last_exc is not None:
            raise last_exc
        raise TransportError("no relay attempt succeeded")


def _decoded_response(status, body, headers):
    # aread/aiter_bytes already decoded Content-Encoding; do not decode twice.
    decoded_headers = {key: value for key, value in headers.items()
                       if key.lower() not in ('content-encoding', 'content-length', 'transfer-encoding')}
    return httpx.Response(status_code=status, content=body, headers=decoded_headers)


class RelayErrorReadFailure(ValueError):
    """Bounded, content-free read metadata; remains compatible with ValueError."""
    def __init__(self, reason, body_bytes):
        message = ('relay error body limit exceeded' if reason == 'body_limit'
                   else 'relay error body read timed out' if reason == 'read_timeout'
                   else 'relay error body read failed')
        super().__init__(message)
        self.reason = reason
        self.body_bytes = min(65537, max(0, body_bytes))


async def _bounded_relay_error(response, limit=65536):
    body = bytearray()
    try:
        async with asyncio.timeout(5):
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > limit:
                    raise RelayErrorReadFailure('body_limit', len(body) + len(chunk))
                body.extend(chunk)
    except RelayErrorReadFailure:
        raise
    except (TimeoutError, httpx.TimeoutException):
        raise RelayErrorReadFailure('read_timeout', len(body)) from None
    except (ValueError, httpx.HTTPError):
        raise RelayErrorReadFailure('read_failure', len(body)) from None
    return bytes(body)


_RELAY_QUOTA_CODES = frozenset(('11113', '1005'))
# ZCode 3.16.3 classifies 3009 as nonretryable ModelRateLimited. It is not
# evidence of spent quota, and must not enter account/model failover handling.
_PROVIDER_BUSY_CODES = frozenset(('3009',))
_RELAY_LIMIT_TYPES = frozenset(('rate_limit_error', 'quota_exceeded_error'))
_RELAY_DIAGNOSTIC_ENUMS = {
    'relay_provider': ('cloudflare', 'vercel', 'deno', 'netlify', 'custom', 'other'),
    'mime': ('json', 'html', 'text', 'event_stream', 'other', 'absent'),
    'body_shape': ('flat_error', 'nested_error', 'typed_error', 'nonobject', 'invalid_json', 'unreadable'),
    'provider_code': ('11113', '1005', '3009', 'rate_limit_error', 'quota_exceeded_error', 'other', 'absent'),
    'error_type': ('rate_limit_error', 'quota_exceeded_error', 'error', 'other', 'absent'),
    'origin': ('relay', 'upstream', 'unknown'),
    'evidence': ('versioned_header', 'platform_header', 'provider_code', 'typed_envelope', 'none'),
    'reason': ('versioned_origin', 'platform_rate_limit', 'provider_quota', 'provider_busy', 'typed_rate_limit',
               'code_conflict', 'envelope_conflict', 'invalid_envelope', 'unsupported_envelope',
               'invalid_json', 'nonobject', 'body_limit', 'read_timeout', 'read_failure',
               'candidates_exhausted'),
    'read_outcome': ('complete', 'body_limit', 'read_timeout', 'read_failure'),
}
_RELAY_DIAGNOSTIC_DEFAULTS = {
    'relay_provider': 'other', 'mime': 'absent', 'body_shape': 'unreadable',
    'provider_code': 'absent', 'error_type': 'absent', 'origin': 'unknown',
    'evidence': 'none', 'reason': 'unsupported_envelope', 'read_outcome': 'read_failure',
}
_RELAY_DIAGNOSTIC_FLAGS = ('marker_present', 'marker_valid', 'marker_conflict', 'retry_header_present')
_RELAY_JSON_FIELDS = ('code', 'statusCode', 'type', 'message', 'msg', 'error')
_RELAY_NUMERIC_LOCATIONS = tuple(prefix + key for prefix in ('root_', 'error_')
                                 for key in ('code', 'statusCode'))
_RELAY_STRUCTURE_LOCATIONS = ('root_kind',) + tuple(
    prefix + key for prefix in ('root_', 'error_') for key in _RELAY_JSON_FIELDS)
_RELAY_JSON_KINDS = ('null', 'boolean', 'number', 'string', 'array', 'object')
_RELAY_DIAGNOSTIC_KEYS = (tuple(_RELAY_DIAGNOSTIC_ENUMS) + _RELAY_DIAGNOSTIC_FLAGS +
    ('request_id', 'relay_attempt', 'upstream_status', 'body_bytes', 'numeric_codes', 'json_structure'))


def _relay_known_fields(value, keys):
    # Rebuild before lookup: even a builtin dict can contain hostile object keys.
    if type(value) is not dict:
        return {}
    return {key: item for key, item in value.items() if type(key) is str and key in keys}


def _relay_numeric_code(value):
    # Observations only, not origin evidence; no arbitrary strings or coercion.
    if type(value) is int:
        return value if 0 <= value <= 999999 else None
    if (type(value) is str and 1 <= len(value) <= 6 and
            all('0' <= char <= '9' for char in value) and
            (value == '0' or value[0] != '0')):
        return int(value)
    return None


def _relay_json_observations(value):
    kinds = {type(None): 'null', bool: 'boolean', int: 'number', float: 'number',
             str: 'string', list: 'array', dict: 'object'}
    structure = {'root_kind': kinds[type(value)]}
    codes = {}
    if type(value) is dict:
        nested = value.get('error')
        for prefix, fields in (('root_', value), ('error_', nested)):
            if type(fields) is not dict:
                continue
            for key in _RELAY_JSON_FIELDS:
                if key not in fields:
                    continue
                item = fields[key]
                location = prefix + key
                structure[location] = kinds[type(item)]
                if key in ('code', 'statusCode'):
                    code = _relay_numeric_code(item)
                    if code is not None:
                        codes[location] = code
    observations = {'json_structure': structure}
    if codes:
        observations['numeric_codes'] = codes
    return observations


def _relay_request_id(value):
    # RequestControl currently uses 16 hex digits; standalone calls use UUID hex.
    if (type(value) is str and len(value) in (16, 32)
            and all(c in '0123456789abcdefABCDEF' for c in value)):
        return value
    return uuid.uuid4().hex


def safe_relay_diagnostics(value) -> dict[str, Any]:
    """Project fixed scalars and bounded observation maps, never raw payloads.

    Also used by service delivery/logging, which may receive injected exceptions.
    Missing response status/byte count stays missing rather than inventing data.
    """
    value = _relay_known_fields(value, _RELAY_DIAGNOSTIC_KEYS)
    result = {'schema_version': 1, 'request_id': _relay_request_id(value.get('request_id')),
              'relay_attempt': 1}
    attempt = value.get('relay_attempt')
    if type(attempt) is int and 1 <= attempt <= 3:
        result['relay_attempt'] = attempt
    for key, choices in _RELAY_DIAGNOSTIC_ENUMS.items():
        item = value.get(key)
        result[key] = (item if type(item) is str and item in choices
                       else _RELAY_DIAGNOSTIC_DEFAULTS[key])
    for key in _RELAY_DIAGNOSTIC_FLAGS:
        item = value.get(key)
        result[key] = item if type(item) is bool else False
    if type(value.get('upstream_status')) is int and value['upstream_status'] in (200, 429):
        result['upstream_status'] = value['upstream_status']
    count = value.get('body_bytes')
    if type(count) is int:
        result['body_bytes'] = min(65537, max(0, count))
    codes = {}
    for key, item in _relay_known_fields(value.get('numeric_codes'), _RELAY_NUMERIC_LOCATIONS).items():
        code = _relay_numeric_code(item)
        if code is not None:
            codes[key] = code
    if codes:
        result['numeric_codes'] = codes
    structure = {key: item for key, item in
        _relay_known_fields(value.get('json_structure'), _RELAY_STRUCTURE_LOCATIONS).items()
        if type(item) is str and item in _RELAY_JSON_KINDS}
    if structure:
        result['json_structure'] = structure
    return result


def _relay_headers(headers):
    # Restrict keys at entry; case-normalize known names without serializing data.
    known = ('x-autozcoder-relay-version', 'x-autozcoder-response-origin',
             'x-vercel-error', 'content-type', 'retry-after', 'retry-after-ms')
    return {key.lower(): val for key, val in headers.items()
            if type(key) is str and key.lower() in known}


def _relay_code(value):
    # bool is an int subclass, and floats compare equal to ints: neither is evidence.
    if type(value) is int:
        return str(value) if value in (11113, 1005, 3009) else None
    if type(value) is str and value in _RELAY_QUOTA_CODES | _PROVIDER_BUSY_CODES | _RELAY_LIMIT_TYPES:
        return value
    return None


def _relay_error_fields(error):
    code_keys = [key for key in ('code', 'statusCode') if key in error]
    codes = [_relay_code(error[key]) for key in code_keys]
    code = codes[0] if codes else None
    if len(codes) > 1 and (None in codes or len(set(codes)) != 1):
        return code, None, 'code_conflict'
    if codes and code is None:
        return None, None, 'invalid_envelope'
    messages = [error[key] for key in ('message', 'msg') if key in error]
    if not messages or any(type(msg) is not str for msg in messages):
        return code, None, 'invalid_envelope'
    if len(messages) > 1 and messages[0] != messages[1]:
        return code, None, 'envelope_conflict'
    return code, messages[0], None


class _RelayEnvelopeConflict(ValueError):
    pass


def _relay_json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise _RelayEnvelopeConflict()
        value[key] = item
    return value


def _relay_json_constant(value):
    raise ValueError('nonfinite JSON constant')


def _relay_body_evidence(raw):
    result = {'body_shape': 'unreadable' if raw is None else 'invalid_json',
              'provider_code': 'absent', 'error_type': 'absent',
              'origin': 'unknown', 'evidence': 'none', 'reason': 'invalid_json',
              'marker_conflict': False}
    try:
        value = json.loads(raw, object_pairs_hook=_relay_json_object,
                           parse_constant=_relay_json_constant)
    except _RelayEnvelopeConflict:
        result.update(reason='envelope_conflict', marker_conflict=True)
        return result
    except (ValueError, TypeError, RecursionError):
        return result
    result.update(_relay_json_observations(value))
    if type(value) is not dict:
        result.update(body_shape='nonobject', reason='nonobject')
        return result
    nested = 'error' in value
    error = value.get('error') if nested else value
    result['body_shape'] = ('typed_error' if nested and value.get('type') == 'error'
                            else 'nested_error' if nested else 'flat_error')
    if type(error) is not dict:
        result['reason'] = 'invalid_envelope'
        return result
    type_value = error.get('type', value.get('type'))
    if type_value is not None:
        result['error_type'] = (type_value if type(type_value) is str and
            type_value in _RELAY_LIMIT_TYPES | {'error'} else 'other')
    raw_codes = [error[key] for key in ('code', 'statusCode') if key in error]
    code, message, problem = _relay_error_fields(error)
    if raw_codes:
        result['provider_code'] = code if code is not None else 'other'
    elif type(error.get('type')) is str and error['type'] in _RELAY_LIMIT_TYPES:
        result['provider_code'] = error['type']
    if problem:
        result.update(reason=problem, marker_conflict=problem.endswith('conflict'))
        return result
    if nested:
        # The provider parser chooses error over the root. Redundant root fields
        # must agree; otherwise body-only provenance would validate a different envelope.
        if 'type' in value and value['type'] != 'error':
            result.update(reason='envelope_conflict', marker_conflict=True)
            return result
        root_codes = [value[key] for key in ('code', 'statusCode') if key in value]
        if root_codes and (code is None or any(_relay_code(item) != code for item in root_codes)):
            result.update(reason='code_conflict', marker_conflict=True)
            return result
        if any(type(value[key]) is not str or value[key] != message
               for key in ('message', 'msg') if key in value):
            result.update(reason='envelope_conflict', marker_conflict=True)
            return result
        if 'type' in error and (type(error['type']) is not str or error['type'] not in _RELAY_LIMIT_TYPES):
            result['reason'] = 'invalid_envelope'
            return result
    elif 'type' in value and (type(value['type']) is not str or value['type'] not in _RELAY_LIMIT_TYPES | {'error'}):
        result['reason'] = 'invalid_envelope'
        return result
    if code in _RELAY_QUOTA_CODES:
        result.update(origin='upstream', evidence='provider_code', reason='provider_quota')
    elif code in _PROVIDER_BUSY_CODES:
        result.update(origin='upstream', evidence='provider_code', reason='provider_busy')
    elif (nested and value.get('type') == 'error' and type(error.get('type')) is str
          and error['type'] in _RELAY_LIMIT_TYPES and isinstance(error.get('message'), str)
          and (code is None or code in _RELAY_LIMIT_TYPES)):
        # Preserve the fully typed native path, not generic untyped rate-limit text.
        result.update(origin='upstream', evidence='typed_envelope', reason='typed_rate_limit')
    else:
        result['reason'] = 'unsupported_envelope'
    return result


def _relay_rate_limit_evidence(headers, raw):
    headers = _relay_headers(headers)
    result = _relay_body_evidence(raw)
    version = headers.get('x-autozcoder-relay-version')
    origin = headers.get('x-autozcoder-response-origin')
    result['marker_present'] = any(key in headers for key in
        ('x-autozcoder-relay-version', 'x-autozcoder-response-origin'))
    result['marker_valid'] = type(version) is str and version == '1' and type(origin) is str and origin in ('relay', 'upstream')
    vercel = headers.get('x-vercel-error')
    platform = type(vercel) is str and vercel.upper() in ('TOO_MANY_REQUESTS', 'FUNCTION_RATE_LIMIT')
    if result['marker_valid']:
        result['marker_conflict'] |= (platform and origin != 'relay') or (
            result['origin'] != 'unknown' and result['origin'] != origin)
        result.update(origin=origin, evidence='versioned_header', reason='versioned_origin')
    elif platform:
        result['marker_conflict'] |= result['origin'] == 'upstream'
        result.update(origin='relay', evidence='platform_header', reason='platform_rate_limit')
    return result


def _relay_rate_limit_origin(headers, raw):
    """Compatibility wrapper; the richer evidence never changes authoritative precedence."""
    return _relay_rate_limit_evidence(headers, raw)['origin']


def _relay_provider(manager, relay_url):
    if type(relay_url) is not str or not relay_url:
        return 'other'
    try:
        snapshot = manager.get_settings()
        relays = snapshot.get('relays') if type(snapshot) is dict else None
        if type(relays) is list:
            for relay in relays:
                if (type(relay) is dict and type(relay.get('url')) is str
                        and relay['url'].strip() == relay_url):
                    provider = relay.get('type')
                    return (provider if type(provider) is str and provider in
                            _RELAY_DIAGNOSTIC_ENUMS['relay_provider'] else 'other')
    except Exception:
        # Diagnostic collection must never mask the actual routing failure.
        pass
    return 'other'


def _relay_mime(headers):
    value = headers.get('content-type')
    if value is None:
        return 'absent'
    if type(value) is not str:
        return 'other'
    mime = value.partition(';')[0].strip().lower()
    if mime == 'application/json' or (mime.startswith('application/') and mime.endswith('+json')):
        return 'json'
    return {'text/html': 'html', 'text/plain': 'text', 'text/event-stream': 'event_stream'}.get(mime, 'other')


def _relay_diagnostics(control, attempt, manager, relay_url, headers=None, evidence=None,
                       *, reason=None, read_outcome='complete', body_bytes=None, upstream_status=None):
    headers = _relay_headers(headers or {})
    value = dict(evidence or {})
    try:
        request_id = getattr(control, 'request_id', None)
    except Exception:
        request_id = None
    value.update(request_id=_relay_request_id(request_id), relay_attempt=attempt,
                 relay_provider=_relay_provider(manager, relay_url), mime=_relay_mime(headers),
                 retry_header_present=any(key in headers for key in ('retry-after', 'retry-after-ms')),
                 read_outcome=read_outcome if relay_url or body_bytes is not None else 'read_failure')
    if upstream_status is not None:
        value['upstream_status'] = upstream_status
    elif relay_url:
        value['upstream_status'] = 429
    if body_bytes is not None:
        value['body_bytes'] = body_bytes
    if reason is not None:
        value['reason'] = reason
    return safe_relay_diagnostics(value)


class RelayRoutingError(Exception):
    """Relay-only terminal failure; never rotate or penalize upstream accounts."""
    def __init__(self, code, message, diagnostics=None):
        super().__init__(message)
        self.code, self.message = code, message
        self.diagnostics = safe_relay_diagnostics(diagnostics)


class ProviderBusyError(Exception):
    """Recognized terminal busy response, independent of account/relay quotas."""
    def __init__(self, diagnostics):
        super().__init__('ZCode returned a busy/rate-limit response (3009).')
        self.diagnostics = safe_relay_diagnostics(diagnostics)


def provider_busy_error(raw, headers, *, control=None, manager=None, relay_url=None,
                        attempt=1, status=429):
    """Recognize only complete, canonical 3009 JSON; never trust a code alone."""
    if status not in (200, 429) or type(raw) is not bytes or len(raw) > 65536:
        return None
    body = _relay_body_evidence(raw)
    if body['reason'] != 'provider_busy':
        return None
    evidence = _relay_rate_limit_evidence(headers, raw) if relay_url else body
    if evidence['origin'] != 'upstream':
        return None
    return ProviderBusyError(_relay_diagnostics(control, attempt, manager, relay_url,
        headers, evidence, body_bytes=len(raw), upstream_status=status))


class TransportError(Exception):
    """Network-level failure — safe to retry on another account."""


class UpstreamBlockThroughRelay(TransportError):
    """405/3012 'unusual activity' arrived THROUGH a relay: the relay's exit
    IP is flagged, not the account. The client layer must rotate the RELAY
    (already done in UpstreamClient.send) rather than park the account."""


# --------------------------------------------------------------------------- #
# SSE helpers
# --------------------------------------------------------------------------- #


def iter_sse_events(chunk_text, strict=False):
    events = []
    for frame in __import__('re').split(r'\r\n\r\n|\n\n|\r\r', chunk_text):
        lines = [line[5:].lstrip(' ') for line in frame.splitlines() if line.startswith('data:')]
        if not lines: continue
        data = '\n'.join(lines)
        if data == '[DONE]':
            if strict:
                raise ValueError('native stream ended without a message_stop event')
            events.append(data)
            continue
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError:
            if strict:
                raise ValueError('invalid upstream SSE JSON') from None
            continue
        if strict:
            if not isinstance(parsed, dict):
                raise ValueError('invalid upstream SSE event shape')
            names = [line[6:].strip() for line in frame.splitlines() if line.startswith('event:')]
            kind = parsed.get('type') or ('error' if isinstance(parsed.get('error'), dict) else None)
            if names and (len(names) != 1 or names[0] != kind):
                raise ValueError('upstream SSE event name mismatch')
        events.append(parsed)
    return events


def sse_frame(obj: dict[str, Any]) -> bytes:
    return ("data: " + json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
            + "\n\n").encode("utf-8")
