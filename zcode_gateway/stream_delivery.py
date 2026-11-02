"""Request-wide verified delivery budgets and a single ASGI stream owner.

No inference work or admission is acquired until the response is consumed.  Tasks
which exceed bounded cleanup remain owned and keep their admission slot until
actually finished; a cancellation-resistant transport cannot grow an orphan queue.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable

from starlette.responses import JSONResponse, StreamingResponse

log = logging.getLogger('zcode_gateway.delivery')


class StreamPolicyError(Exception):
    def __init__(self, status, code, message, details=None):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.details = {'retryable': False, 'terminal': True, **(details or {})}


class RequestControl:
    def __init__(self, cfg, *, clock: Callable = time.monotonic):
        self.clock = clock
        self.started = self.last_progress = clock()
        self.deadline = self.started + cfg.stream_verify_timeout_seconds
        self.progress_timeout = cfg.stream_progress_timeout_seconds
        self.max_sends = cfg.stream_max_sends
        self.cleanup_timeout = cfg.stream_cleanup_timeout_seconds
        self.phase = 'admission'
        self.request_id = uuid.uuid4().hex[:16]
        self.sends = self.attempts = self.events = self.bytes = 0
        self.failure = None
        self.disconnected = False
        self.verified = False
        self.tasks = set()
        self.close_tasks = set()
        self.owner = None
        self.on_idle = None

    def remaining(self):
        return max(0.0, self.deadline - self.clock())

    def until_check(self):
        total = self.remaining()
        if self.verified:
            return total
        return max(0.0, min(total, self.last_progress + self.progress_timeout - self.clock()))

    def diagnostics(self):
        now = self.clock()
        return {'request_id': self.request_id, 'phase': self.phase,
                'elapsed_seconds': round(max(0, now - self.started), 3),
                'attempts': self.attempts, 'sends': self.sends,
                'events': self.events, 'bytes': self.bytes,
                'last_progress_age_seconds': round(max(0, now - self.last_progress), 3)}

    def error(self, status, code, message):
        return StreamPolicyError(status, code, message, self.diagnostics())

    def check(self):
        if self.failure is not None:
            raise self.failure
        if self.disconnected:
            raise asyncio.CancelledError()
        if self.remaining() <= 0:
            self.failure = self.error(504, 'stream_buffer_timeout',
                'The verified response exceeded its total deadline.')
            raise self.failure
        if not self.verified and self.clock() - self.last_progress >= self.progress_timeout:
            self.failure = self.error(504, 'stream_progress_timeout',
                'The upstream made no meaningful progress before its deadline.')
            raise self.failure

    def before_send(self):
        self.check()
        if self.sends >= self.max_sends:
            self.failure = self.error(504, 'stream_send_budget_exhausted',
                'The verified request exhausted its upstream send budget.')
            raise self.failure
        self.sends += 1

    def accept(self, event, size):
        """Call only after the strict accumulator accepts the event."""
        self.events += 1
        self.bytes += size
        kind = event.get('type')
        payload = None
        if kind == 'content_block_delta':
            delta = event['delta']
            key = {'text_delta': 'text', 'thinking_delta': 'thinking',
                   'input_json_delta': 'partial_json', 'signature_delta': 'signature'}.get(delta['type'])
            payload = delta.get(key)
        elif kind == 'content_block_start':
            block = event['content_block']
            # A tool announcement (including its initial input) is not progress.
            if block['type'] != 'tool_use':
                payload = next((block.get(k) for k in ('text', 'thinking', 'signature', 'data')
                                if block.get(k)), None)
        if isinstance(payload, str) and payload:
            self.last_progress = self.clock()

    def track(self, task):
        self.tasks.add(task)
        if self.owner is not None:
            self.owner.add(task)
        def finished(task):
            self.tasks.discard(task)
            self.close_tasks.discard(task)
            if self.owner is not None:
                self.owner.discard(task)
            try:
                task.exception()
            except (asyncio.CancelledError, Exception):
                pass
            if not self.tasks and self.on_idle is not None:
                self.on_idle()
        task.add_done_callback(finished)
        return task

    def spawn(self, coroutine, name):
        task = self.track(asyncio.create_task(coroutine, name=name))
        if name in ('verified-upstream-close', 'verified-close-wait', 'verified-pool-release'):
            self.close_tasks.add(task)
        return task

    @staticmethod
    def cancel_once(task):
        if not task.done() and not task.cancelling():
            task.cancel()

    async def cleanup(self, tasks):
        tasks = {task for task in tasks if task is not asyncio.current_task() and not task.done()}
        for task in tasks:
            if task not in self.close_tasks:
                self.cancel_once(task)
        if tasks:
            # asyncio.wait does not wait indefinitely for cancellation-resistant I/O.
            await asyncio.wait(tasks, timeout=self.cleanup_timeout)


class OwnedVerifiedResponse(StreamingResponse):
    """Immediate private keepalives, then one validated turn, with owned teardown."""
    def __init__(self, service, payload, *, model, variant, dialect, client_key, anthropic, headers):
        super().__init__(content=iter(()), media_type='text/event-stream', headers=headers)
        self.service, self.payload = service, payload
        self.options = dict(model=model, variant=variant, dialect=dialect,
                            client_key=client_key, anthropic=anthropic)
        self.native = anthropic
        self.transform = None
        self.consumed = False

    def map_frames(self, transform):
        self.transform = transform
        return self

    async def __call__(self, scope, receive, send):
        from .service import NoAccountsAvailable, UpstreamFailure, _stream_failure
        from .upstream import anthropic_error, openai_error, sse_frame
        from .service import _sse_event

        if self.consumed:
            raise RuntimeError('verified response already consumed')
        self.consumed = True
        service = self.service
        try:
            control = service._admit_verified()
        except UpstreamFailure as exc:
            error = anthropic_error if self.native else openai_error
            await JSONResponse(error(exc.status, exc.code, exc.message, exc.extra()),
                               status_code=exc.status, background=self.background)(scope, receive, send)
            return
        task = asyncio.current_task()
        service._delivery_tasks.add(task)
        if service._lifecycle_tasks is not None:
            service._lifecycle_tasks.add(task)
        headers_sent = False
        response_finished = False
        writable = True
        pending_send = None
        watcher = work = None
        stream = None
        cfg = service.cfg.upstream

        async def disconnect():
            while True:
                event = await receive()
                if event['type'] == 'http.disconnect':
                    # ASGI reports disconnect after a normally completed body,
                    # too. That is not an abandoned verified request.
                    if not response_finished:
                        control.disconnected = True
                    return

        async def send_bounded(message, *, terminal_error=False):
            nonlocal writable, pending_send
            if not writable or pending_send is not None:
                raise RuntimeError('verified downstream is not writable')
            final_body = message['type'] == 'http.response.body' and not message.get('more_body', False)
            async def deliver():
                nonlocal response_finished
                await send(message)
                if final_body:
                    # Mark acknowledgement here, before the receive watcher or
                    # owner resumes; normal final-send/disconnect ties succeed.
                    response_finished = True
                    control.disconnected = False
            sending = control.spawn(deliver(), 'verified-downstream-send')
            pending_send = sending
            try:
                waiters = {sending}
                if watcher is not None:
                    waiters.add(watcher)
                timeout = (cfg.stream_send_timeout_seconds if terminal_error else
                           min(cfg.stream_send_timeout_seconds, control.remaining()))
                done, _ = await asyncio.wait(waiters, timeout=timeout,
                                             return_when=asyncio.FIRST_COMPLETED)
                if sending in done and final_body:
                    await sending
                    pending_send = None
                    return
                if watcher is not None and watcher in done:
                    raise asyncio.CancelledError()
                if sending in done:
                    await sending
                    pending_send = None
                    return
                if not terminal_error:
                    control.check()
                control.failure = control.error(504, 'stream_delivery_timeout',
                    'The downstream could not receive the verified response before its send deadline.')
                raise control.failure
            except BaseException:
                # Any failed write poisons this response, including a total
                # deadline shorter than the write budget. Never start an error
                # write concurrently with a still-running content/header write.
                writable = False
                RequestControl.cancel_once(sending)
                raise

        async def source(result):
            for frame in result.frames:
                yield frame

        try:
            watcher = control.spawn(disconnect(), 'verified-disconnect')
            await send_bounded({'type': 'http.response.start', 'status': 200,
                                'headers': self.raw_headers})
            headers_sent = True
            heartbeat = _sse_event('ping', {'type': 'ping'}) if self.native else b': keepalive\n\n'
            await send_bounded({'type': 'http.response.body', 'body': heartbeat, 'more_body': True})
            # The collector is never started at route construction, nor before
            # the downstream accepts headers and its initial harmless heartbeat.
            work = service._start_verified(self.payload, control=control, **self.options)
            next_heartbeat = time.monotonic() + cfg.stream_heartbeat_seconds
            while not work.done():
                control.check()
                wait = min(control.until_check(), max(0, next_heartbeat - time.monotonic()))
                done, _ = await asyncio.wait({work, watcher}, timeout=wait,
                                             return_when=asyncio.FIRST_COMPLETED)
                if watcher in done:
                    raise asyncio.CancelledError()
                if work in done:
                    break  # Completion wins a simultaneous timer boundary.
                control.check()
                if time.monotonic() >= next_heartbeat:
                    await send_bounded({'type': 'http.response.body', 'body': heartbeat, 'more_body': True})
                    next_heartbeat = time.monotonic() + cfg.stream_heartbeat_seconds
            result = await work
            control.verified = True
            control.phase = 'delivery'
            stream = source(result)
            if self.transform is not None:
                stream = self.transform(stream)
            async for frame in stream:
                control.check()
                await send_bounded({'type': 'http.response.body', 'body': frame, 'more_body': True})
            await send_bounded({'type': 'http.response.body', 'body': b'', 'more_body': False})
        except asyncio.CancelledError:
            control.disconnected = control.failure is None and not response_finished
            raise
        except OSError:
            # BrokenPipe/ConnectionReset from the actual ASGI socket is a
            # downstream disconnect, not an invalid upstream response.
            control.disconnected = not response_finished
        except Exception as exc:
            failure = (UpstreamFailure(503, 'no_accounts_available', 'no usable Zcode account in the pool')
                       if isinstance(exc, NoAccountsAvailable) else _stream_failure(exc))
            control.failure = exc if isinstance(exc, StreamPolicyError) else failure
            if headers_sent and not control.disconnected and writable and pending_send is None:
                error = anthropic_error if self.native else openai_error
                frame = (_sse_event('error', error(failure.status, failure.code, failure.message, failure.extra()))
                         if self.native else sse_frame(error(failure.status, failure.code, failure.message, failure.extra())))
                # A blocked send cannot safely accept an additional error send.
                if failure.code != 'stream_delivery_timeout':
                    try:
                        await send_bounded({'type': 'http.response.body', 'body': frame, 'more_body': False}, terminal_error=True)
                    except (asyncio.CancelledError, Exception):
                        pass
        finally:
            if stream is not None:
                # This is our in-memory iterator (and optional legacy adapter),
                # not an arbitrary upstream iterator requiring unbounded close.
                await stream.aclose()
            control.phase = 'cleanup'
            cleanup = control.spawn(control.cleanup(tuple(control.tasks)), 'verified-owned-cleanup')
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                pass  # Cleanup stays registered and keeps admission until done.
            if self.background is not None and not control.disconnected:
                background = control.spawn(self.background(), 'verified-background')
                done, _ = await asyncio.wait({background}, timeout=control.cleanup_timeout)
                if background not in done:
                    RequestControl.cancel_once(background)
            service._delivery_tasks.discard(task)
            if service._lifecycle_tasks is not None:
                service._lifecycle_tasks.discard(task)
            service._release_verified(control)
            log.info('verified delivery ended: status=%s code=%s diagnostics=%s',
                     499 if control.disconnected else (control.failure.status if control.failure else 200),
                     control.failure.code if control.failure else '', control.diagnostics())
            # Do not let an unconsumed response or a retained response object
            # hold prompts after its request has ended.
            self.payload = None
