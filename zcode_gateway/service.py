"""Shared pool, persistence and dashboard with a native ZCode transport."""
from __future__ import annotations
import asyncio
import json
import logging
import re
import threading
import time
import urllib.parse
from collections import deque
from dataclasses import dataclass

import httpx
from .aggregate import aggregate_openai_stream
from .pool import Account, AccountState
from .breaker import EdgeBlockBreaker
from .model_retry import is_model_overload, overload_delay, retry_after_seconds, wait_for_retry
from .zcode_protocol import MessageAccumulator, OpenAITranslator, usage_openai, validate_stream_event
from .upstream import (build_headers, build_upstream_body, iter_sse_events, sse_frame,
                       parse_upstream_error, anthropic_error, openai_error, TransportError,
                       is_block_code, RelayRoutingError, safe_relay_diagnostics,
                       ProviderBusyError, provider_busy_error)
from .registry import model_caps
from .stream_delivery import RequestControl, StreamPolicyError, OwnedVerifiedResponse

log = logging.getLogger('zcode_gateway.service')

class NoAccountsAvailable(Exception): pass

class UpstreamFailure(Exception):
    def __init__(self, status, code, message, release_at=None, release_in_s=None, details=None, retry_after=None):
        super().__init__(message)
        self.status, self.code, self.message = status, str(code), message
        self.release_at, self.release_in_s, self.details = release_at, release_in_s, details or {}
        self.retry_after = retry_after
    def extra(self):
        out = dict(self.details)
        if self.release_at: out['release_at'] = self.release_at
        if self.release_in_s is not None: out['release_in_s'] = self.release_in_s
        return out

@dataclass
class StreamHandle:
    account: Account
    headers: dict
    body: dict
    resp: object
    variant: str
    started: float
    events: object = None
    first: dict | None = None
    closed: bool = False
    # re-opens the same request on a fresh upstream stream (set by open_stream);
    # used to retry a stream that died before any content reached the client
    reopen: object = None

@dataclass
class CallResult:
    account: Account
    status: int
    raw: bytes
    duration_ms: float
    upstream_headers: dict


@dataclass
class VerifiedResult:
    frames: tuple[bytes, ...]
    result: CallResult


def _stream_failure(exc, *, content_sent=False):
    if isinstance(exc, StreamPolicyError):
        return UpstreamFailure(exc.status, exc.code, exc.message, details=exc.details)
    if isinstance(exc, UpstreamFailure):
        return exc
    if isinstance(exc, httpx.HTTPError):
        return UpstreamFailure(502, 'upstream_stream_interrupted',
            'The upstream connection ended before a complete response was received.',
            details={'reason': 'transport_interruption', 'error_class': type(exc).__name__,
                     'content_sent': content_sent})
    return UpstreamFailure(502, 'invalid_stream',
        'The upstream returned an invalid stream event or response.',
        details={'reason': 'invalid_stream', 'content_sent': content_sent})


def _stream_error_info(event):
    info = event.get('error')
    if not isinstance(info, dict):
        raise ValueError('invalid upstream error event')
    code = str(info.get('code') or info.get('type') or 'api_error')[:128]
    message = info.get('message')
    message = message if isinstance(message, str) else 'The upstream stream failed.'
    return code, message


def _snapshot_payload(payload):
    """Snapshot request routing for private retries without sharing its dicts."""
    snap = dict(payload)
    if isinstance(snap.get('_anthropic'), dict):
        snap['_anthropic'] = dict(snap['_anthropic'])
    return snap


def _split_sse_frames(pending):
    frames = []
    while True:
        match = re.search(rb'\r\n\r\n|\n\n|\r\r', pending)
        if not match: return frames, pending
        frames.append(pending[:match.start()])
        pending = pending[match.end():]


def _sse_event(name, data):
    return (f'event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n').encode()


async def upstream_events(resp, control=None):
    pending = b''
    async for chunk in resp.aiter_bytes():
        if control is not None:
            control.check()
        pending += chunk
        frames, pending = _split_sse_frames(pending)
        if len(pending) > 4 * 1024 * 1024:
            raise UpstreamFailure(502, 'invalid_stream', 'upstream SSE frame exceeded 4 MB')
        for frame in frames:
            if len(frame) > 4 * 1024 * 1024:
                raise UpstreamFailure(502, 'invalid_stream', 'upstream SSE frame exceeded 4 MB')
            for event in iter_sse_events(frame.decode('utf-8'), strict=True):
                validate_stream_event(event)
                yield event
    if pending.strip():
        for event in iter_sse_events(pending.decode('utf-8'), strict=True):
            validate_stream_event(event)
            yield event


class ChatService:
    def __init__(self, cfg, pool, tokens, client, registry, store, capture):
        self.cfg, self.pool, self.tokens, self.client = cfg, pool, tokens, client
        self.registry, self.store, self.capture = registry, store, capture
        # IP-wide CDN edge-block breaker: shared by every account (one egress
        # IP, one block). Replaces the old behaviour where an HTML 403 was
        # mislabelled ENTITLEMENT_ERROR and parked accounts one by one until
        # the pool drained for a condition no account caused.
        self.breaker = EdgeBlockBreaker()
        self._verified_controls = set()
        self._owned_tasks = set()
        self._delivery_tasks = set()
        self._stopping = False
        self._lifecycle_tasks = None
        self._relay_record_slots = threading.BoundedSemaphore(4)
        self._relay_record_futures = set()

        def _on_edge_block(count: int) -> None:
            try:
                self.pool.notify('edge_block',
                                 f"upstream edge block #{count} — requests paused, "
                                 f"probing to resume automatically",
                                 level='error')
            except Exception:
                pass
        self.breaker.on_block = _on_edge_block

    def _build(self, payload, variant='anthropic', model=None):
        caps = model_caps(self.registry.resolve(model or payload.get('model') or '')) \
            if hasattr(self, 'registry') and self.registry is not None else None
        return build_upstream_body(payload, variant, self.cfg.models.default_max_tokens,
                                   model_caps=caps,
                                   default_effort=getattr(self.cfg.models, 'default_effort', 'high'))

    async def _edge_probe(self):
        """Cheap GET against the upstream host for the breaker's prober.

        Any HTTP answer that is NOT the CDN's HTML block page means the edge
        is no longer intercepting (a 404/401 JSON from the API is fine — the
        point is the request reached the API at all). Returns (status, body).
        """
        base = self.cfg.upstream.base_url.rstrip('/')
        resp = await self.client._client.get(base + '/zcode-plan/billing/balance',
                                             timeout=10)
        return resp.status_code, resp.text
    async def _close(self, handle, control=None):
        if handle.closed: return
        handle.closed = True
        async def close():
            try:
                if handle.events is not None: await handle.events.aclose()
            finally:
                try:
                    await handle.resp.aclose()
                finally:
                    await self.pool.release(handle.account)
        if control is None:
            # Legacy realtime callers retain their shielded release behavior.
            try:
                if handle.events is not None: await handle.events.aclose()
            finally:
                try:
                    await handle.resp.aclose()
                finally:
                    await asyncio.shield(self.pool.release(handle.account))
            return
        task = control.spawn(close(), 'verified-upstream-close')
        control.close_tasks.add(task)
        # Keep actual cleanup owned even if the collector is cancelled again.
        waiter = control.spawn(self._wait_close(task, control), 'verified-close-wait')
        control.close_tasks.add(waiter)
        await asyncio.shield(waiter)

    @staticmethod
    async def _wait_close(task, control):
        done, _ = await asyncio.wait({task}, timeout=control.cleanup_timeout)
        if task in done:
            await task
        else:
            RequestControl.cancel_once(task)
            if control.failure is None:
                control.failure = control.error(504, 'stream_cleanup_timeout',
                    'The upstream did not release the verified response before its cleanup deadline.')
            raise control.failure
    async def _apply_error(self, account, model, status, code, message, headers=None):
        if is_model_overload(status, code):
            # A busy model is not evidence of a bad account or spent quota.
            # Keep this account available for other models and delayed retries.
            return
        # autopsy trail: keep the last errors for the dashboard "why" view
        try:
            account.error_history.append(
                {"ts": time.time(), "status": status, "code": str(code),
                 "message": str(message)[:200], "model": model})
        except Exception:
            pass
        if status == 401 or code == 'authentication_error':
            await self.pool.kill_after_strikes(account, 'ZCode token revoked; sign in again')
        elif status == 405 or is_block_code(code, status):
            # 405/3012 "unusual activity": not a quota and not a bad account.
            # Park only this model on this account and let the caller rotate to
            # the next account; the account keeps serving every other model.
            await self.pool.cap_model(account, model,
                                      self.cfg.pool.blocked_model_cooldown_seconds,
                                      reason='UNUSUAL_ACTIVITY', message=message)
        elif code in ('11113', '1005') or status == 402:
            # 1005 is the SAME "exceed quota limit" rejection under an HTTP
            # 502 (captured live 2026-10-21: 10x/10min on a plan-less
            # account) — treating it as a generic 5xx only cooled the
            # account 30s and the router picked it again, forever.
            # Duration depends on what the quota data says:
            #   remaining <= 0 + resets_at -> genuinely spent bucket: park
            #     until the reset (correct, could be hours)
            #   remaining > 0 -> the upstream's answer CONTRADICTS the ledger:
            #     either in-flight usage drained it since the last 60s poll,
            #     or (seen live across whole clusters 2026-10-22) the exit IP
            #     is risk-shaped and z.ai mislabels the rejection. Parking
            #     until resets_at here would sideline a 105M-token account
            #     for a day on ONE transient error — short park instead, the
            #     next balance poll auto-clears it when the ledger is right.
            #   no bucket data -> strike toward EXHAUSTED (plan-less churner)
            quota = account.notes.get('quotas', {}).get(model, {})
            remaining = int(quota.get('remaining', 0) or 0)
            if remaining > 0:
                await self.pool.cap_model(account, model, 600,
                                          reason='11113' if code == '11113' else '1005',
                                          message=message)
                return
            seconds = max(1, quota.get('resets_at', 0) - time.time()) if quota else 0
            if seconds <= 1:
                # no bucket data to schedule around: if the account HAS no
                # entitlement (plans empty, quotas empty), parking one model
                # still leaves it churning on the other — strike toward
                # EXHAUSTED; the balance loop restores it the moment real
                # quota appears.
                account.quota_strikes = getattr(account, 'quota_strikes', 0) + 1
                if account.quota_strikes >= 3:
                    account.state = AccountState.EXHAUSTED
                    account.last_error = 'repeated quota rejection with no visible buckets'
                    try:
                        self.pool.notify('quota',
                                         f"account {account.id} EXHAUSTED — repeated "
                                         f"quota rejections, no visible plan/buckets",
                                         level='warning')
                    except Exception:
                        pass
                    return
                seconds = 900      # park the model past the churn window
            await self.pool.cap_model(account, model, seconds,
                                      reason='11113' if code == '11113' else '1005',
                                      message=message)
        elif status == 429 or code == 'rate_limit_error':
            try: seconds = min(3600, max(1, float((headers or {}).get('retry-after', self.cfg.pool.cooldown_seconds))))
            except ValueError: seconds = self.cfg.pool.cooldown_seconds
            await self.pool.cap_model(account, model, seconds, reason='MODEL_CAPPED', message=message)
        elif status == 403:
            await self.pool.cap_model(account, model, 300, reason='ENTITLEMENT_ERROR', message=message)
        elif status == 451:
            # Geo/legal block tied to this account's egress context: park the
            # model on THIS account only and let the caller rotate to another
            # account (different IP) — the account itself stays healthy.
            await self.pool.cap_model(account, model, 600,
                                      reason='GEO_BLOCKED', message=message or 'HTTP 451')
        elif status >= 500:
            await self.pool.cool(account, reason='upstream_error')

    # A stream that dies BEFORE any content reached the client (only
    # message_start/ping so far) is re-opened this many times, transparently:
    # the client cannot tell, and nothing is ever replayed to it.
    STREAM_RETRIES = 2

    def _relay_label(self, resp) -> str:
        url = getattr(resp, 'relay_url', '') or ''
        if not url:
            return 'direct'
        name = ''
        try:
            rm = getattr(self.client, 'relay_manager', None)
            if rm is not None:
                for r in rm.get_settings().get('relays') or []:
                    if str(r.get('url', '')).rstrip('/') == url.rstrip('/'):
                        name = str(r.get('name') or '')
                        break
        except Exception:
            pass
        host = urllib.parse.urlparse(url).netloc or url
        return f'{name} ({host})' if name else host

    @staticmethod
    def _describe_failure(exc) -> str:
        if isinstance(exc, UpstreamFailure):
            if exc.code == 'truncated_stream':
                return 'connection closed cleanly before message_stop'
            return f'{exc.code}: {exc.message}'
        text = f'{type(exc).__name__}: {exc}'.rstrip(': ')
        inner = exc.__cause__ or exc.__context__
        if inner is not None and inner is not exc:
            text += f' (caused by {type(inner).__name__}: {inner})'.replace(': )', ')')
        return text

    @staticmethod
    def _retryable(exc) -> bool:
        # transport-level only: a dead/cut connection, or a clean EOF with no
        # message_stop. Upstream error events and parse errors are not retried.
        if isinstance(exc, StreamPolicyError) or (isinstance(exc, UpstreamFailure)
                and exc.details.get('retryable') is False):
            return False
        return isinstance(exc, httpx.HTTPError) or (
            isinstance(exc, UpstreamFailure) and exc.code == 'truncated_stream')

    def _log_stream_failure(self, handle, exc, *, committed, events, retry):
        acct = handle.account
        who = str(acct.id)[:12] + (f' <{acct.email}>' if getattr(acct, 'email', '') else '')
        log.warning('ZCode stream failed mid-flight: model=%s account=%s relay=%s after %.1fs, '
                    '%d event(s), content_sent=%s - %s%s',
                    handle.body.get('model'), who, self._relay_label(handle.resp),
                    time.time() - handle.started, events, committed,
                    self._describe_failure(exc),
                    ' - retrying transparently' if retry else '')

    async def _after_stream_failure(self, handle, exc, *, committed, events, retries_left):
        """Log a mid-stream failure; return a freshly opened handle when the
        failure is retryable and nothing has been sent to the client."""
        will_retry = (not committed) and retries_left > 0 and self._retryable(exc)
        self._log_stream_failure(handle, exc, committed=committed, events=events, retry=will_retry)
        if not will_retry:
            return None
        reopen = getattr(handle, 'reopen', None)
        if reopen is None:
            return None
        # free this attempt's in-flight slot BEFORE re-opening: with a low
        # per-account cap the retry would otherwise wait on the slot we hold
        await self._close(handle)
        await asyncio.sleep(0.15)
        try:
            return await reopen()
        except asyncio.CancelledError:
            raise
        except Exception as exc2:
            log.warning('ZCode stream retry could not open a new upstream stream (%s) - '
                        'reporting the original failure', self._describe_failure(exc2))
            return None

    async def open_stream(self, payload, *, model, variant='anthropic', dialect='openai', client_key='anonymous', is_disconnected=None, control=None):
        if control is not None: control.check()
        snapshot = _snapshot_payload(payload)
        handle = await self._open_stream(payload, model=model, variant=variant, dialect=dialect,
                                         client_key=client_key, is_disconnected=is_disconnected, control=control)

        async def reopen():
            return await self.open_stream(_snapshot_payload(snapshot), model=model, variant=variant,
                                          dialect=dialect, client_key=client_key,
                                          is_disconnected=is_disconnected, control=control)
        handle.reopen = reopen
        return handle

    async def _open_stream(self, payload, *, model, variant='anthropic', dialect='openai', client_key='anonymous', is_disconnected=None, control=None):
        fallback = self.cfg.models.auto_free_fallback
        chain = list(dict.fromkeys(self.registry.resolve(m, dialect) for m in fallback.chain))
        candidates = chain[chain.index(model):] if fallback.enabled and model in chain else [model]
        attempted = []
        for candidate in candidates:
            if control is not None: control.check()
            attempted.append(candidate)
            try:
                return await self._open_model_stream({**payload, 'model': candidate}, model=candidate,
                    variant=variant, dialect=dialect, client_key=client_key, is_disconnected=is_disconnected, control=control)
            except StreamPolicyError as exc:
                raise _stream_failure(exc) from exc
            except UpstreamFailure as exc:
                if exc.details.get('retryable') is False: raise
                # Try the next model in the fallback chain for every error the
                # account-parking path already handled (its own codes) AND the
                # upstream's real rate-limit code — `rate_limit_error` was
                # missing from this list, so a 429 on the paid model never
                # fell back to the free one (measured: 0 retries on 429).
                if exc.code not in ('11113', '1005', 'rate_limit_error', 'MODEL_CAPPED', 'MODEL_PARKED',
                                    'INFERENCE_CAP_ERROR', 'ENTITLEMENT_ERROR',
                                    'UNUSUAL_ACTIVITY', '3012', '405', 'HTTP_405',
                                    'HTTP_451', 'http_451'):
                    raise
                if candidate == candidates[-1]:
                    exc.details['attempted_models'] = attempted
                    raise

    async def _open_model_stream(self, payload, *, model, variant='anthropic', dialect='openai', client_key='anonymous', is_disconnected=None, control=None):
        # Only retry before a StreamHandle is returned. Never replay output or tools.
        attempts = self.cfg.upstream.overload_max_attempts
        for attempt in range(attempts):
            if control is not None: control.check()
            if is_disconnected is not None and await is_disconnected():
                raise asyncio.CancelledError()
            try:
                return await self._open_model_stream_once(payload, model=model, variant=variant,
                    dialect=dialect, client_key=client_key, is_disconnected=is_disconnected, control=control)
            except StreamPolicyError as exc:
                raise _stream_failure(exc) from exc
            except UpstreamFailure as exc:
                if exc.details.get('retryable') is False: raise
                if not is_model_overload(exc.status, exc.code):
                    raise
                delay = overload_delay(self.cfg.upstream, attempt + 1, exc.retry_after)
                if attempt + 1 == attempts:
                    exc.release_in_s = max(1, int(delay + .999))
                    raise
                log.info('ZCode model %s overloaded (%s); retry %s/%s in %.2fs',
                         model, exc.code, attempt + 1, attempts - 1, delay)
                await wait_for_retry(delay, is_disconnected, control=control)

    async def _open_model_stream_once(self, payload, *, model, variant='anthropic', dialect='openai', client_key='anonymous', is_disconnected=None, control=None):
        if not self.registry.knows(model):
            raise UpstreamFailure(400, 'unknown_model', f'unknown ZCode model: {model}')
        # fail fast while the egress IP is edge-blocked: every account would
        # return the same HTML 403, and trying parks them one by one
        blocked = self.breaker.check()
        if blocked is not None:
            raise UpstreamFailure(blocked['status'], blocked['code'],
                                  blocked['message'],
                                  retry_after=blocked['retry_in_s'])
        try: body = self._build(payload, variant, model=model)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise UpstreamFailure(400, 'invalid_request_error', str(exc)) from exc
        if not body.get('messages'):
            raise UpstreamFailure(400, 'invalid_request_error', 'at least one user or assistant message is required')
        supplied_metadata = 'metadata' in body
        # Routing belongs to the request, not an individual attempt. Reading
        # the shared native payload must not consume its pin on overload retry
        # or model fallback. Only remove routing extensions from the built copy.
        target_account = (
            payload.get("_anthropic", {}).get("account")
            or payload.get("_anthropic", {}).get("account_id")
            or payload.get("account")
            or payload.get("account_id")
        )
        body.pop('account', None)
        body.pop('account_id', None)
        tried, last = set(), None
        for attempt in range(max(1, self.cfg.upstream.max_attempts)):
            if control is not None:
                control.phase = 'acquiring'
                control.check()
            account = await self.pool.acquire(tried, model=model, only_id=target_account)
            if account is None:
                if last: raise last
                reason = await self.pool.unavailable_reason(model)
                if reason:
                    raise UpstreamFailure(reason['status'], reason['code'], reason['message'],
                                          reason.get('release_at'), reason.get('release_in_s'))
                raise NoAccountsAvailable()
            headers = build_headers(account.access_token, self.cfg.upstream.fingerprint,
                                    device_mid=account.notes.get('device_mid'), include_device_mid=False)
            if not supplied_metadata:
                device_mid = account.notes.get('device_mid') or self.cfg.upstream.fingerprint.device_mid
                body['metadata'] = {'user_id': json.dumps({'device_id': device_mid,
                    'account_uuid': '', 'session_id': headers['x-session-id']}, separators=(',', ':'))}
            elif isinstance(body.get('metadata'), dict) and 'user_id' not in body['metadata']:
                # A client-supplied Anthropic `metadata` field used to skip the
                # device binding entirely — every such request went upstream
                # with no device id (an "unusual activity" signal). Merge it.
                device_mid = account.notes.get('device_mid') or self.cfg.upstream.fingerprint.device_mid
                body['metadata'] = {**body['metadata'],
                    'user_id': json.dumps({'device_id': device_mid,
                        'account_uuid': '', 'session_id': headers['x-session-id']}, separators=(',', ':'))}
            resp, handle, transferred = None, None, False
            opening_started = time.monotonic()
            try:
                if account.expires_in() <= 0 and not await self.tokens.refresh(account):
                    # Expired token: strike-and-cool (like upstream 401s), not
                    # an instant permanent kill — an expired token whose file
                    # simply hasn't rotated yet used to DEAD the account with
                    # no recovery path (enable() -> next request kills again).
                    await self.pool.kill_after_strikes(account, 'ZCode token expired; sign in again')
                    last = UpstreamFailure(401, 'authentication_error', 'ZCode token expired; sign in again')
                    tried.add(account.id)
                    continue
                headers = build_headers(account.access_token, self.cfg.upstream.fingerprint,
                                        task_id=headers['x-session-id'], device_mid=account.notes.get('device_mid'),
                                        include_device_mid=False)
                resp = await self.client.send(headers, body, stream=True, **({'control': control} if control is not None else {}))
                if resp.status_code != 200 or 'application/json' in resp.headers.get('content-type', ''):
                    raw = await resp.aread()
                    busy = provider_busy_error(raw, resp.headers, control=control,
                        manager=getattr(self.client, 'relay_manager', None),
                        relay_url=getattr(resp, 'relay_url', None), status=resp.status_code)
                    if busy is not None:
                        raise busy
                    info = parse_upstream_error(resp.status_code, raw)
                    status, code, message = resp.status_code, str(info['code']), info['message']
                    if status == 200:
                        status = 429 if code in ('11113', '1005') else 502
                    if status == 401 and await self.tokens.refresh(account, force=True):
                        # Retry a newly imported token once, without a second pool reservation.
                        await resp.aclose()
                        headers = build_headers(account.access_token, self.cfg.upstream.fingerprint,
                                                task_id=headers['x-session-id'], device_mid=account.notes.get('device_mid'),
                                                include_device_mid=False)
                        resp = await self.client.send(headers, body, stream=True, **({'control': control} if control is not None else {}))
                        if resp.status_code == 200 and 'application/json' not in resp.headers.get('content-type', ''):
                            status = 200
                        else:
                            raw = await resp.aread()
                            busy = provider_busy_error(raw, resp.headers, control=control,
                                manager=getattr(self.client, 'relay_manager', None),
                                relay_url=getattr(resp, 'relay_url', None), status=resp.status_code)
                            if busy is not None:
                                raise busy
                            info = parse_upstream_error(resp.status_code, raw)
                            status, code, message = resp.status_code, str(info['code']), info['message']
                            if status == 200:
                                status = 429 if code in ('11113', '1005') else 502
                    if status != 200:
                        # IP-wide CDN edge block (HTML body, not API JSON)?
                        # Detect BEFORE _apply_error — this is not an account
                        # problem, and parking the account for it is the bug
                        # the breaker exists to prevent.
                        if self.breaker.note_edge_block(raw):
                            await resp.aclose()
                            self.breaker.ensure_prober(self._edge_probe)
                            raise UpstreamFailure(
                                503, 'waf_block',
                                'upstream edge block (IP-wide): requests '
                                'rejected for every account; retry shortly',
                                retry_after=35)
                        await self._apply_error(account, model, status, code, message, resp.headers)
                        last = UpstreamFailure(429 if code == '11113' else status, code, message,
                                               retry_after=retry_after_seconds(resp.headers))
                        self._record(account, headers, body, last.status, resp.headers,
                                     raw.decode('utf-8', 'replace'), 0, client_key, dialect, model, {})
                        if is_model_overload(status, code): raise last
                        if (status < 500 and status not in (401, 402, 403, 405, 429, 451)
                                and code != '11113' and not is_block_code(code, status)):
                            raise last
                        tried.add(account.id)
                        continue
                if control is not None:
                    control.phase = 'opening'
                    control.check()
                handle = StreamHandle(account, headers, body, resp, variant, time.time())
                handle.events = upstream_events(resp, control=control)
                # a clean stream through the CDN is evidence any prior edge
                # block has lifted
                self.breaker.note_success()
                # An immediate SSE error must reach the client before HTTP 200 is committed.
                while True:
                    first = await anext(handle.events, None)
                    if control is not None:
                        control.check()
                        if first is not None and first.get('type') == 'ping':
                            control.accept(first, 0)
                    if first is None:
                        raise UpstreamFailure(502, 'empty_stream', 'ZCode returned an empty event stream')
                    if first.get('type') == 'ping': continue
                    # Two error shapes reach this loop:
                    #   1) Anthropic event form:  {"type":"error","error":{...}}
                    #   2) OpenAI-shaped frame (captured 2026-09-16):
                    #      {"error":{"code":"stream_initialization_failed", ...}}
                    # The second has no "type" key — checking only the first
                    # form masked the real code/message into a generic 502
                    # invalid_stream and never cooled/parked the account.
                    if first.get('type') == 'error' or (first.get('type') is None and 'error' in first):
                        info = first.get('error') or first
                        code, message = str(info.get('code') or info.get('type') or 'api_error'), info.get('message', 'ZCode stream failed')
                        status = 529 if is_model_overload(502, code) else (429 if code in ('11113', 'rate_limit_error') else (405 if is_block_code(code) else 502))
                        await self._apply_error(account, model, status, code, message)
                        last = UpstreamFailure(status, code, message)
                        if is_model_overload(status, code): raise last
                        tried.add(account.id)
                        break
                    if first.get('type') != 'message_start':
                        raise UpstreamFailure(502, 'invalid_stream', 'expected ZCode message_start event')
                    handle.first = first
                    result = handle
                    transferred = True
                    handle, resp = None, None
                    return result
            except StreamPolicyError as exc:
                raise _stream_failure(exc) from exc
            except (RelayRoutingError, ProviderBusyError) as exc:
                diagnostic = safe_relay_diagnostics(exc.diagnostics)
                busy = isinstance(exc, ProviderBusyError)
                code = ('3009' if busy else exc.code if exc.code in ('relay_rate_limit_ambiguous',
                    'relay_host_rate_limited', 'relay_candidates_exhausted') else 'relay_rate_limit_ambiguous')
                status = 429 if busy else 503
                recording = 'interrupted'
                try:
                    recording = await self._record_relay_failure(account, client_key, dialect, model,
                        code, (time.monotonic() - opening_started) * 1000, status=status)
                finally:
                    log.warning('%s opening failed: code=%s recording=%s diagnostic=%s',
                                'provider' if busy else 'relay', code, recording, diagnostic)
                messages = {
                    '3009': 'ZCode returned a busy/rate-limit response (3009). Retry manually later.',
                    'relay_rate_limit_ambiguous': 'The relay rate-limit origin could not be verified; check the diagnostic before retrying.',
                    'relay_host_rate_limited': 'The relay host is rate-limiting requests; distinct relay attempts were exhausted.',
                    'relay_candidates_exhausted': 'No distinct relay candidate is available; direct routing was not attempted.',
                }
                details = {'retryable': False, 'terminal': True, 'diagnostic': diagnostic}
                if busy:
                    details['reason'] = 'provider_busy'
                raise UpstreamFailure(status, code, messages[code], details=details) from exc
            except (TransportError, httpx.HTTPError) as exc:
                # A block that arrived THROUGH a relay is the relay's exit-IP
                # problem: UpstreamClient already rotated+cooled every relay
                # it could. Do NOT cool the account (the same request through
                # the next relay on the next attempt is fine) and do NOT burn
                # attempts rotating accounts — every account uses the same
                # relay set, so the next attempt would go DIRECT (all relays
                # are cooling) and burn the home IP. Fail the request now
                # with the real code; relays uncool in 60s.
                from .upstream import UpstreamBlockThroughRelay
                if isinstance(exc, UpstreamBlockThroughRelay):
                    raise UpstreamFailure(
                        405, '3012',
                        'relay exit IP(s) flagged (unusual activity): all relays '
                        'cooled ~60s — retry shortly; a different relay '
                        'rotation is not needed, it happens automatically'
                    ) from exc
                await self.pool.cool(account, reason='transport_error')
                last = UpstreamFailure(502, 'transport_error', 'could not reach ZCode')
                tried.add(account.id)
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                raise _stream_failure(exc) from exc
            finally:
                if handle is not None:
                    await self._close(handle, control=control)
                elif resp is not None:
                    if control is not None:
                        cleanup_handle = StreamHandle(account, headers, body, resp, variant, time.time())
                        await self._close(cleanup_handle, control=control)
                    else:
                        try: await resp.aclose()
                        finally: await self.pool.release(account)
                elif not transferred:
                    if control is not None:
                        release = control.spawn(self.pool.release(account), 'verified-pool-release')
                        await asyncio.shield(release)
                    else:
                        await self.pool.release(account)
            if attempt + 1 < self.cfg.upstream.max_attempts:
                await wait_for_retry(0.1 * (attempt + 1), is_disconnected, control=control)
        raise last or NoAccountsAvailable()

    async def stream(self, payload, *, model, variant='anthropic', dialect='openai', client_key='anonymous', anthropic=False):
        handle = await self.open_stream(payload, model=model, variant=variant, dialect=dialect, client_key=client_key)
        async for frame in self.stream_from(handle, model=model, dialect=dialect,
                                            client_key=client_key, anthropic=anthropic):
            yield frame

    async def stream_from(self, handle, *, model, dialect='openai', client_key='anonymous', anthropic=False):
        model = handle.body['model']
        translator = OpenAITranslator(model)
        usage, stopped, status = {}, False, 200
        captured, capture_size = deque(), 0
        committed, prelude, seen = False, [], 0
        retries_left = self.STREAM_RETRIES

        def emit(event):
            validate_stream_event(event)
            kind = event.get('type')
            if kind == 'message_start': usage.update(event['message'].get('usage') or {})
            if kind == 'message_delta': usage.update(event.get('usage') or {})
            if anthropic: return [_sse_event(kind, event)]
            return [sse_frame(chunk) for chunk in translator.feed(event)]

        async def events():
            yield handle.first
            async for event in handle.events: yield event
        try:
            while True:
                try:
                    async for event in events():
                        validate_stream_event(event)
                        kind = event.get('type')
                        seen += 1
                        if self.capture.enabled and capture_size < 200000:
                            text = json.dumps(event, ensure_ascii=False)
                            captured.append(text)
                            capture_size += len(text)
                        if kind == 'error' or (kind is None and 'error' in event):
                            code, message = _stream_error_info(event)
                            status = 529 if is_model_overload(502, code) else (429 if code in ('11113', 'rate_limit_error') else 502)
                            await self._apply_error(handle.account, model, status, code, message)
                            raise UpstreamFailure(status, code, message)
                        # Prelude carries no user output. Discard keepalive pings
                        # while waiting rather than buffering an unbounded list.
                        if kind == 'ping':
                            if committed and anthropic:
                                yield _sse_event('ping', event)
                            continue
                        frames = emit(event)
                        if not committed and kind == 'message_start':
                            if prelude:
                                raise ValueError('duplicate upstream message start')
                            prelude = frames
                            continue
                        if not frames:
                            continue  # Empty OpenAI block setup is not delivery.
                        if not committed:
                            committed = True
                            for out in prelude: yield out
                            prelude.clear()
                        for out in frames: yield out
                        if kind == 'message_stop':
                            stopped = True
                            if not anthropic: yield b'data: [DONE]\n\n'
                            await self.pool.on_success(handle.account)
                            break
                    if not stopped:
                        raise UpstreamFailure(502, 'truncated_stream', 'ZCode stream ended before message_stop')
                    break
                except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError, UpstreamFailure) as exc:
                    fresh = await self._after_stream_failure(
                        handle, exc, committed=committed, events=seen, retries_left=retries_left)
                    if fresh is None:
                        raise
                    retries_left -= 1
                    self._record(handle.account, handle.headers, handle.body, 502, handle.resp.headers,
                                 '\n'.join(captured), (time.time() - handle.started) * 1000,
                                 client_key, dialect, model, usage)
                    old, handle = handle, fresh
                    await self._close(old)
                    model = handle.body['model']
                    translator = OpenAITranslator(model)
                    usage, captured, capture_size, prelude, seen = {}, deque(), 0, [], 0
        except asyncio.CancelledError:
            status = 499
            raise
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError, UpstreamFailure) as exc:
            failure = _stream_failure(exc, content_sent=committed)
            status = failure.status
            error = anthropic_error(status, failure.code, failure.message, failure.extra()) if anthropic else openai_error(status, failure.code, failure.message, failure.extra())
            yield _sse_event('error', error) if anthropic else sse_frame(error)
        finally:
            await self._close(handle)
            self._record(handle.account, handle.headers, handle.body, status, handle.resp.headers,
                         '\n'.join(captured), (time.time() - handle.started) * 1000,
                         client_key, dialect, model, usage)

    async def _collect_verified(self, payload, *, model, variant, dialect, client_key, anthropic, is_disconnected, control=None):
        snapshot = _snapshot_payload(payload)
        limit = self.cfg.upstream.stream_buffer_max_bytes
        previous_failure = None
        for attempt in range(self.STREAM_RETRIES + 1):
            if control is not None:
                control.check()
                control.attempts += 1
            if is_disconnected is not None and await is_disconnected():
                raise asyncio.CancelledError()
            try:
                handle = await self.open_stream(_snapshot_payload(snapshot), model=model, variant=variant,
                                               dialect=dialect, client_key=client_key,
                                               is_disconnected=is_disconnected, control=control)
            except asyncio.CancelledError:
                raise
            except (StreamPolicyError, UpstreamFailure) as exc:
                if isinstance(exc, StreamPolicyError) or exc.details.get('retryable') is False:
                    raise _stream_failure(exc) from exc
                if previous_failure is not None:
                    raise previous_failure from None
                raise
            except Exception:
                if previous_failure is not None:
                    # A failed reopen must not hide the original interrupted turn.
                    raise previous_failure from None
                raise
            if control is not None: control.phase = 'collecting'
            actual_model = handle.body['model']
            accumulator = MessageAccumulator(strict=True)
            translator, frames, size, seen = OpenAITranslator(actual_model), [], 0, 0
            status, failure = 200, None
            recorded_message = ''
            async def events():
                yield handle.first
                async for event in handle.events: yield event
            try:
                async for event in events():
                    if is_disconnected is not None and await is_disconnected():
                        raise asyncio.CancelledError()
                    if control is not None: control.check()
                    validate_stream_event(event)
                    seen += 1
                    kind = event.get('type')
                    if kind == 'error' or (kind is None and 'error' in event):
                        code, message = _stream_error_info(event)
                        status = 529 if is_model_overload(502, code) else (429 if code in ('11113', 'rate_limit_error') else (405 if is_block_code(code) else 502))
                        await self._apply_error(handle.account, actual_model, status, code, message)
                        raise UpstreamFailure(status, code, message)
                    if kind == 'ping':
                        if control is not None: control.accept(event, 0)
                        continue  # Not content; never retain an endless ping stream.
                    # Account for both preserved output and accumulator input.
                    # Fail closed before extending either representation.
                    serialized = json.dumps(event, ensure_ascii=False).encode('utf-8')
                    size += len(serialized)
                    if size > limit:
                        raise UpstreamFailure(502, 'stream_buffer_limit_exceeded',
                                              'The verified response exceeded its configured buffer limit.',
                                              details={'retryable': False, 'terminal': True})
                    accumulator.feed(event)
                    if control is not None: control.accept(event, len(serialized))
                    output = [_sse_event(kind, event)] if anthropic else [sse_frame(c) for c in translator.feed(event)]
                    size += sum(map(len, output))
                    if size > limit:
                        raise UpstreamFailure(502, 'stream_buffer_limit_exceeded',
                                              'The verified response exceeded its configured buffer limit.',
                                              details={'retryable': False, 'terminal': True})
                    frames.extend(output)
                    if kind == 'message_stop':
                        break
                if not accumulator.stopped:
                    raise UpstreamFailure(502, 'truncated_stream', 'ZCode stream ended before message_stop')
                message = accumulator.finish()
                if control is not None: control.verified = True
                if not anthropic:
                    terminal = b'data: [DONE]\n\n'
                    if size + len(terminal) > limit:
                        raise UpstreamFailure(502, 'stream_buffer_limit_exceeded',
                                              'The verified response exceeded its configured buffer limit.',
                                              details={'retryable': False, 'terminal': True})
                    frames.append(terminal)
                recorded_message = json.dumps(message, ensure_ascii=False)
                raw = recorded_message.encode('utf-8') if dialect == 'anthropic' else b''.join(frames[:-1] if not anthropic else frames)
                await self.pool.on_success(handle.account)
                result = CallResult(handle.account, 200, raw,
                                    (time.time() - handle.started) * 1000, handle.resp.headers)
                return VerifiedResult(tuple(frames), result)
            except asyncio.CancelledError:
                normalized = _stream_failure(control.failure) if control is not None and control.failure is not None else None
                status = normalized.status if normalized else 499
                recorded_message = json.dumps({'error': normalized.code if normalized else 'request_cancelled'})
                raise
            except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError, UpstreamFailure, StreamPolicyError) as exc:
                failure = exc
                normalized = _stream_failure(exc)
                status = normalized.status
                recorded_message = json.dumps({'error': normalized.code})
                will_retry = attempt < self.STREAM_RETRIES and self._retryable(exc)
                if control is not None:
                    log.warning('verified attempt failed: code=%s diagnostics=%s%s',
                                normalized.code, control.diagnostics(),
                                ' - retrying transparently' if will_retry else '')
                else:
                    self._log_stream_failure(handle, exc, committed=False, events=seen, retry=will_retry)
                if not will_retry:
                    if normalized is exc:
                        raise
                    raise normalized from exc
                previous_failure = normalized
            finally:
                try:
                    await self._close(handle, control=control)
                finally:
                    self._record(handle.account, handle.headers, handle.body, status, handle.resp.headers,
                                 recorded_message, (time.time() - handle.started) * 1000,
                                 client_key, dialect, actual_model, accumulator.message.get('usage') or {})
            if failure is not None:
                await wait_for_retry(0.15, is_disconnected, control=control)
        raise UpstreamFailure(502, 'upstream_stream_interrupted', 'The upstream could not complete the response.')

    def preflight(self, payload, *, model, variant='anthropic'):
        if not self.registry.knows(model):
            raise UpstreamFailure(400, 'unknown_model', f'unknown ZCode model: {model}')
        try:
            body = self._build({**payload, 'model': model}, variant, model=model)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise UpstreamFailure(400, 'invalid_request_error', str(exc)) from exc
        if not body.get('messages'):
            raise UpstreamFailure(400, 'invalid_request_error', 'at least one user or assistant message is required')

    def verified_response(self, payload, *, model, variant='anthropic', dialect='openai',
                          client_key='anonymous', anthropic=False, headers=None):
        self.preflight(payload, model=model, variant=variant)
        return OwnedVerifiedResponse(self, _snapshot_payload(payload), model=model,
            variant=variant, dialect=dialect, client_key=client_key,
            anthropic=anthropic, headers=headers)

    def _admit_verified(self):
        if self._stopping or len(self._verified_controls) >= self.cfg.upstream.stream_max_concurrent:
            raise UpstreamFailure(503, 'stream_admission_rejected',
                'The gateway has no free verified delivery slot.',
                details={'retryable': False, 'terminal': True})
        control = RequestControl(self.cfg.upstream)
        # Share the existing lifecycle task set once wired by AppState.
        control.owner = self._lifecycle_tasks if self._lifecycle_tasks is not None else self._owned_tasks
        self._verified_controls.add(control)
        return control

    def _release_verified(self, control):
        # Pending cancellation-resistant cleanup keeps admission, rather than
        # creating an unlimited queue of abandoned buffers/reservations.
        if any(not task.done() for task in control.tasks):
            control.on_idle = lambda: self._verified_controls.discard(control)
        else:
            self._verified_controls.discard(control)

    def _start_verified(self, payload, *, control, **options):
        return control.spawn(self._collect_verified(payload, is_disconnected=None,
                                                     control=control, **options),
                             'verified-collector')

    def begin_stopping(self):
        self._stopping = True
        for control in tuple(self._verified_controls):
            for task in tuple(control.tasks):
                if task not in control.close_tasks:
                    task.get_loop().call_soon_threadsafe(RequestControl.cancel_once, task)
        for task in tuple(self._delivery_tasks):
            task.get_loop().call_soon_threadsafe(RequestControl.cancel_once, task)

    async def verified_stream(self, payload, *, model, variant='anthropic', dialect='openai', client_key='anonymous', anthropic=False, is_disconnected=None):
        # Non-streaming compatibility entry point. Only this watcher consumes
        # request.is_disconnected; the collector never competes for receive().
        self.preflight(payload, model=model, variant=variant)
        control = self._admit_verified()
        work = self._start_verified(payload, control=control, model=model, variant=variant,
                                    dialect=dialect, client_key=client_key, anthropic=anthropic)
        async def disconnect():
            while True:
                if await is_disconnected():
                    control.disconnected = True
                    return
                await asyncio.sleep(.1)
        watcher = control.spawn(disconnect(), 'verified-disconnect') if is_disconnected else None
        try:
            while not work.done():
                control.check()
                done, _ = await asyncio.wait({work} | ({watcher} if watcher else set()),
                    timeout=control.until_check(), return_when=asyncio.FIRST_COMPLETED)
                if watcher is not None and watcher in done:
                    raise asyncio.CancelledError()
                if work in done:
                    break
                control.check()
            return await work
        except StreamPolicyError as exc:
            control.failure = exc
            raise _stream_failure(exc) from exc
        finally:
            control.phase = 'cleanup'
            cleanup = control.spawn(control.cleanup(tuple(control.tasks)), 'verified-owned-cleanup')
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                pass
            self._release_verified(control)

    async def complete(self, payload, *, model, variant='anthropic', dialect='openai', client_key='anonymous', is_disconnected=None):
        verified = await self.verified_stream(payload, model=model, variant=variant, dialect=dialect,
                                              client_key=client_key, anthropic=dialect == 'anthropic',
                                              is_disconnected=is_disconnected)
        return verified.result

    def _record(self, account, headers, body, status, response_headers, response, duration, key, dialect, model, usage):
        converted = usage_openai(usage)
        error_code = None if status == 200 else f'http_{status}'
        if status != 200:
            try:
                recorded = json.loads(response)
                code = recorded.get('error') if isinstance(recorded, dict) else None
                if isinstance(code, str) and code.startswith('stream_'):
                    error_code = code[:128]
            except (ValueError, TypeError):
                pass
        rec = dict(account_id=account.id, client_key=key, dialect=dialect, model=model,
                   variant='anthropic', stream=int(body.get('stream', True)), status=status,
                   duration_ms=duration, prompt_tokens=converted['prompt_tokens'],
                   completion_tokens=converted['completion_tokens'], total_tokens=converted['total_tokens'],
                   error_code=error_code)
        cap = dict(request_headers=headers, request_body=body, status=status,
                   response_headers=dict(response_headers), response_body=response)
        # Disk I/O off the event loop: SQLite/JSONL writes (and AV scans on
        # those files) used to stall every concurrent stream while they ran.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            loop.run_in_executor(None, self._write_record, rec, cap)
        else:
            self._write_record(rec, cap)

    async def _record_relay_failure(self, account, key, dialect, model, code, duration, *, status=503):
        """Bound safe ledger writes independently of prompt-bearing capture work."""
        if not self._relay_record_slots.acquire(blocking=False):
            return 'saturated'
        rec = dict(account_id=account.id, client_key=key, dialect=dialect, model=model,
                   variant='anthropic', stream=1, status=status, duration_ms=duration,
                   prompt_tokens=None, completion_tokens=None, total_tokens=None, error_code=code)
        def write():
            try:
                return self._write_record(rec, None)
            finally:
                # An asyncio cancellation cannot stop the executor's disk I/O.
                self._relay_record_slots.release()
        try:
            future = asyncio.get_running_loop().run_in_executor(None, write)
        except Exception:
            self._relay_record_slots.release()
            return 'failed'
        self._relay_record_futures.add(future)
        future.add_done_callback(self._relay_record_futures.discard)
        done, _ = await asyncio.wait({future}, timeout=1)
        if future not in done:
            return 'pending'
        try:
            return 'stored' if future.result() else 'failed'
        except Exception:
            return 'failed'

    def _write_record(self, rec, cap):
        try:
            self.store.record(**rec)
            if cap is not None:
                self.capture.write(**cap)
            return True
        except Exception:
            if cap is not None:
                log.debug("store/capture write failed", exc_info=True)
            return False
