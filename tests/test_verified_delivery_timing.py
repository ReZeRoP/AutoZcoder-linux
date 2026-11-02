"""Owned ASGI timing, admission and teardown without real upstream networking."""
import asyncio
import logging
import time

import pytest

from test_stream_retry import cfg, service, REQ
from tests_fixtures import FakeClient, FakeResponse, frame, sample_events, sample_sse
from zcode_gateway.api_openai import _legacy_completion_stream
from zcode_gateway.service import UpstreamFailure


class Timeline:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.messages = []
        self.readers = self.max_readers = 0

    async def receive(self):
        self.readers += 1
        self.max_readers = max(self.max_readers, self.readers)
        try:
            return await self.queue.get()
        finally:
            self.readers -= 1

    async def send(self, message):
        self.messages.append((time.monotonic(), dict(message)))

    def body(self):
        return b''.join(message.get('body', b'') for _, message in self.messages)


class Gated(FakeResponse):
    def __init__(self, gate, opened=None):
        super().__init__(b'')
        self.gate = gate
        self.opened = opened or asyncio.Event()

    async def aiter_bytes(self):
        yield frame(sample_events()[0])
        self.opened.set()
        await self.gate.wait()
        yield b''.join(frame(e) for e in sample_events()[1:])


def response(service, native=False):
    return service.verified_response(dict(REQ), model='GLM-5.3',
        dialect='anthropic' if native else 'openai', anthropic=native,
        headers={'cache-control': 'no-cache'})


async def wait_until(predicate):
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(.005)
    assert predicate(), 'condition did not become true'


@pytest.mark.parametrize('native', [False, True])
async def test_headers_and_keepalives_precede_private_complete_result(service, native):
    gate, opened = asyncio.Event(), asyncio.Event()
    service.client = FakeClient(responses=[Gated(gate, opened)])
    service.cfg.upstream.stream_heartbeat_seconds = .02
    wire = Timeline()
    task = asyncio.create_task(response(service, native)({}, wire.receive, wire.send))
    await opened.wait()
    await wait_until(lambda: len(wire.messages) >= 4)
    assert wire.messages[0][1]['type'] == 'http.response.start'
    assert wire.messages[0][1]['status'] == 200
    assert wire.max_readers == 1
    before = wire.body()
    assert b'hello' not in before and b'message_start' not in before
    assert b'[DONE]' not in before and b'message_stop' not in before
    assert b'event: ping' in before if native else b': keepalive' in before
    gate.set()
    await task
    assert b'hello' in wire.body()
    assert wire.body().count(b'event: message_stop' if native else b'data: [DONE]') == 1
    assert not service._verified_controls
    assert not service._owned_tasks
    assert service.client.responses[0].closed
    assert (await service.pool.all())[0].in_flight == 0


@pytest.mark.parametrize('native', [False, True])
@pytest.mark.parametrize('during_send', [False, True])
async def test_normal_disconnect_after_final_body_is_success_not499(service, native, during_send, caplog):
    service.client = FakeClient(responses=[FakeResponse(sample_sse())])
    wire = Timeline()
    async def final_disconnect(message):
        await wire.send(message)
        if message['type'] == 'http.response.body' and not message.get('more_body', False):
            await wire.queue.put({'type': 'http.disconnect'})
            if during_send:
                # Force receive to observe the normal completion notification
                # while ASGI's final send acknowledgement is returning.
                await asyncio.sleep(0)
    with caplog.at_level(logging.INFO):
        await response(service, native)({}, wire.receive, final_disconnect)
    assert wire.body().count(b'event: message_stop' if native else b'data: [DONE]') == 1
    assert 'status=200' in caplog.text and 'status=499' not in caplog.text
    assert not service._verified_controls and not service._owned_tasks
    assert (await service.pool.all())[0].in_flight == 0


async def test_never_consumed_response_does_not_admit_or_start_collector(service):
    unopened = response(service)
    assert unopened.consumed is False
    assert not service.client.calls
    assert not service._verified_controls and not service._owned_tasks


@pytest.mark.parametrize('mode', ['silence', 'ping', 'empty_delta', 'partial_bytes'])
async def test_unproductive_upstream_fails_progress_without_false_success(service, mode, caplog):
    class Noise(FakeResponse):
        async def aiter_bytes(self):
            yield frame(sample_events()[0])
            if mode == 'empty_delta':
                yield frame(sample_events()[1])
            while True:
                await asyncio.sleep(.005)
                if mode == 'ping':
                    yield frame({'type': 'ping'})
                elif mode == 'empty_delta':
                    yield frame({'type': 'content_block_delta', 'index': 0,
                                 'delta': {'type': 'thinking_delta', 'thinking': ''}})
                elif mode == 'partial_bytes':
                    yield b' '
    service.cfg.upstream.stream_progress_timeout_seconds = .04
    service.cfg.upstream.stream_heartbeat_seconds = .01
    service.client = FakeClient(responses=[Noise(b'')])
    wire = Timeline()
    with caplog.at_level(logging.INFO):
        await response(service)({}, wire.receive, wire.send)
    assert b'stream_progress_timeout' in wire.body()
    assert b'"retryable":false' in wire.body()
    assert b'[DONE]' not in wire.body() and b'hello' not in wire.body()
    assert 'status=504' in caplog.text and 'status=499' not in caplog.text
    assert service.client.responses[0].closed
    assert not service._verified_controls
    assert (await service.pool.all())[0].in_flight == 0


@pytest.mark.parametrize('native', [False, True])
async def test_total_deadline_has_writable_terminal_sse_grace_and_true504(service, native, caplog):
    service.cfg.upstream.stream_verify_timeout_seconds = .035
    service.cfg.upstream.stream_progress_timeout_seconds = 10
    service.client = FakeClient(responses=[Gated(asyncio.Event())])
    wire = Timeline()
    service.records = []
    service._record = lambda *args: service.records.append(args)
    with caplog.at_level(logging.INFO):
        await response(service, native)({}, wire.receive, wire.send)
    assert b'stream_buffer_timeout' in wire.body()
    assert b'[DONE]' not in wire.body() and b'message_stop' not in wire.body()
    assert 'status=504' in caplog.text and 'status=499' not in caplog.text
    assert service.records[-1][3] == 504
    assert not service._verified_controls and not service._owned_tasks


async def test_disconnect_has_one_receiver_no_reservation_or_admission_leak(service, caplog):
    opened = asyncio.Event()
    service.client = FakeClient(responses=[Gated(asyncio.Event(), opened)])
    wire = Timeline()
    task = asyncio.create_task(response(service)({}, wire.receive, wire.send))
    await opened.wait()
    with caplog.at_level(logging.INFO):
        await wire.queue.put({'type': 'http.disconnect'})
        with pytest.raises(asyncio.CancelledError):
            await task
    assert wire.max_readers == 1
    assert 'status=499' in caplog.text
    assert b'[DONE]' not in wire.body()
    assert service.client.responses[0].closed
    assert not service._verified_controls and not service._owned_tasks
    assert (await service.pool.all())[0].in_flight == 0


async def test_admission_is_held_through_buffer_delivery(service):
    service.cfg.upstream.stream_max_concurrent = 1
    service.client = FakeClient(responses=[FakeResponse(sample_sse())])
    wire, delivering, release = Timeline(), asyncio.Event(), asyncio.Event()
    async def slow_send(message):
        await wire.send(message)
        if b'"role":"assistant"' in message.get('body', b''):
            delivering.set()
            await release.wait()
    task = asyncio.create_task(response(service)({}, wire.receive, slow_send))
    await delivering.wait()
    assert len(service._verified_controls) == 1
    rejected = Timeline()
    await response(service)({'type': 'http', 'method': 'POST', 'path': '/v1/chat/completions'}, rejected.receive, rejected.send)
    assert rejected.messages[0][1]['status'] == 503
    assert b'stream_admission_rejected' in rejected.body()
    assert len(service.client.calls) == 1
    release.set()
    await task
    assert not service._verified_controls


async def test_blocked_downstream_write_is_bounded_without_false_done(service, caplog):
    service.cfg.upstream.stream_send_timeout_seconds = .025
    service.cfg.upstream.stream_cleanup_timeout_seconds = .025
    service.client = FakeClient(responses=[FakeResponse(sample_sse())])
    wire = Timeline()
    async def blocked(message):
        if b'"role":"assistant"' in message.get('body', b''):
            await asyncio.Event().wait()
        await wire.send(message)
    start = time.monotonic()
    with caplog.at_level(logging.INFO):
        await response(service)({}, wire.receive, blocked)
    assert time.monotonic() - start < .3
    assert b'[DONE]' not in wire.body()
    assert 'stream_delivery_timeout' in caplog.text
    assert not service._verified_controls and not service._owned_tasks
    assert (await service.pool.all())[0].in_flight == 0


async def test_slow_owned_cleanup_stays_visible_and_admitted_until_finished(service):
    cleanup_started, release_cleanup = asyncio.Event(), asyncio.Event()
    class SlowClose(Gated):
        async def aclose(self):
            cleanup_started.set()
            try:
                await release_cleanup.wait()
            except asyncio.CancelledError:
                await release_cleanup.wait()
            await super().aclose()
    service.cfg.upstream.stream_cleanup_timeout_seconds = .025
    service.cfg.upstream.stream_verify_timeout_seconds = .02
    service.cfg.upstream.stream_max_concurrent = 1
    service.client = FakeClient(responses=[SlowClose(asyncio.Event())])
    wire = Timeline()
    start = time.monotonic()
    await response(service)({}, wire.receive, wire.send)
    assert time.monotonic() - start < .2
    await cleanup_started.wait()
    assert len(service._verified_controls) == 1
    assert service._owned_tasks
    assert (await service.pool.all())[0].in_flight == 1
    with pytest.raises(UpstreamFailure) as err:
        service._admit_verified()
    assert err.value.code == 'stream_admission_rejected'
    release_cleanup.set()
    await wait_until(lambda: not service._verified_controls)
    assert not service._owned_tasks
    assert (await service.pool.all())[0].in_flight == 0


async def test_legacy_adapter_preserves_comments_errors_and_inner_close():
    closed = False
    async def source():
        nonlocal closed
        try:
            yield b': keepalive\n\n'
            yield b'data: {"error":{"code":"stream_buffer_timeout"}}\n\n'
        finally:
            closed = True
    result = b''.join([chunk async for chunk in _legacy_completion_stream(source(), 'GLM-5.3')])
    assert b': keepalive' in result and b'stream_buffer_timeout' in result
    assert b'[DONE]' not in result and closed


@pytest.mark.parametrize('resist_cancel', [False, True])
async def test_total_deadline_during_send_never_starts_competing_terminal_write(service, resist_cancel, caplog):
    service.cfg.upstream.stream_verify_timeout_seconds = .035
    service.cfg.upstream.stream_send_timeout_seconds = .2
    service.cfg.upstream.stream_cleanup_timeout_seconds = .02
    service.client = FakeClient(responses=[FakeResponse(sample_sse())])
    wire, blocked, release = Timeline(), asyncio.Event(), asyncio.Event()
    send_count = 0
    active = peak = 0
    async def transport(message):
        nonlocal send_count, active, peak
        send_count += 1
        active += 1
        peak = max(peak, active)
        try:
            if b'"role":"assistant"' in message.get('body', b''):
                blocked.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    if not resist_cancel:
                        raise
                    await release.wait()
            await wire.send(message)
        finally:
            active -= 1
    with caplog.at_level(logging.INFO):
        task = asyncio.create_task(response(service)({}, wire.receive, transport))
        await blocked.wait()
        await task
    assert peak == 1
    assert send_count == 3  # headers, initial ping, blocked content; no error write
    assert b'[DONE]' not in wire.body() and b'error' not in wire.body()
    assert 'status=504' in caplog.text and 'stream_buffer_timeout' in caplog.text
    if resist_cancel:
        assert service._verified_controls and service._owned_tasks
        release.set()
        await wait_until(lambda: not service._verified_controls)
    assert not service._verified_controls and not service._owned_tasks
    assert not any(not msg.get('more_body', True) for _, msg in wire.messages)


async def test_actual_socket_oserror_is499_and_does_not_retry_write(service, caplog):
    service.client = FakeClient(responses=[FakeResponse(sample_sse())])
    wire = Timeline()
    attempts = 0
    async def broken_socket(message):
        nonlocal attempts
        attempts += 1
        if attempts == 3:
            raise BrokenPipeError('synthetic downstream closed')
        await wire.send(message)
    with caplog.at_level(logging.INFO):
        await response(service)({}, wire.receive, broken_socket)
    assert attempts == 3
    assert 'status=499' in caplog.text and 'status=502' not in caplog.text
    assert b'error' not in wire.body() and b'[DONE]' not in wire.body()
    assert not service._verified_controls and not service._owned_tasks


def mounted_app(service):
    from types import SimpleNamespace
    from zcode_gateway.app import create_app
    from zcode_gateway.ratelimit import RateLimiter
    service.cfg.upstream.stream_delivery = 'verified'
    app = create_app(service.cfg)
    app.state.app_state = SimpleNamespace(service=service, cfg=service.cfg,
        registry=service.registry, limiter=RateLimiter(), stopping=False)
    return app


def request_scope(service, path='/v1/chat/completions'):
    return {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.4'},
            'http_version': '1.1', 'method': 'POST', 'scheme': 'http',
            'path': path, 'raw_path': path.encode(), 'query_string': b'',
            'headers': [(b'content-type', b'application/json'),
                        (b'authorization', ('Bearer ' + service.cfg.server.admin_key).encode())],
            'client': ('127.0.0.1', 12345), 'server': ('127.0.0.1', 9999)}


async def test_full_app_bounds_actual_socket_send_and_holds_admission(service):
    import json
    service.cfg.upstream.stream_send_timeout_seconds = .025
    service.cfg.upstream.stream_cleanup_timeout_seconds = .025
    service.cfg.upstream.stream_max_concurrent = 1
    service.client = FakeClient(responses=[FakeResponse(sample_sse())])
    app = mounted_app(service)
    wire, blocked, release = Timeline(), asyncio.Event(), asyncio.Event()
    await wire.queue.put({'type': 'http.request', 'body': json.dumps({**REQ, 'stream': True}).encode(),
                          'more_body': False})
    async def actual_socket_send(message):
        if b'"role":"assistant"' in message.get('body', b''):
            blocked.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
        await wire.send(message)
    started = time.monotonic()
    task = asyncio.create_task(app(request_scope(service), wire.receive, actual_socket_send))
    await blocked.wait()
    assert len(service._verified_controls) == 1
    await task
    assert time.monotonic() - started < .3
    assert service._verified_controls and service._owned_tasks
    assert b'[DONE]' not in wire.body() and b'error' not in wire.body()
    with pytest.raises(UpstreamFailure):
        service._admit_verified()
    release.set()
    await wait_until(lambda: not service._verified_controls)
    assert not service._owned_tasks


@pytest.mark.parametrize('path,native', [('/v1/chat/completions', False), ('/v1/messages', True)])
async def test_pure_asgi_stopping_guard_keeps_dialect503_and_safe_health(service, path, native):
    app = mounted_app(service)
    app.state.stopping = True
    wire = Timeline()
    await app(request_scope(service, path), wire.receive, wire.send)
    assert wire.messages[0][1]['status'] == 503
    assert b'gateway is stopping' in wire.body()
    assert b'"type":"error"' in wire.body() if native else b'"error":' in wire.body()
    headers = dict(wire.messages[0][1]['headers'])
    assert headers[b'retry-after'] == b'1'
    assert not service.client.calls
    health = Timeline()
    health_scope = request_scope(service, '/')
    health_scope['method'] = 'GET'
    await app(health_scope, health.receive, health.send)
    assert health.messages[0][1]['status'] == 200


@pytest.mark.parametrize('rejected', [False, True])
async def test_verified_background_callback_runs_once_after_cleanup(service, rejected):
    from starlette.background import BackgroundTask
    calls = []
    async def callback():
        calls.append((await service.pool.all())[0].in_flight)
    service.client = FakeClient(responses=[FakeResponse(sample_sse())])
    owned = response(service)
    owned.background = BackgroundTask(callback)
    lease = service._admit_verified() if rejected else None
    if rejected:
        service.cfg.upstream.stream_max_concurrent = 1
    wire = Timeline()
    await owned(request_scope(service), wire.receive, wire.send)
    assert calls == [0]
    assert wire.messages[0][1]['status'] == (503 if rejected else 200)
    if lease is not None:
        service._release_verified(lease)
    assert not service._verified_controls and not service._owned_tasks


async def test_slow_background_callback_is_bounded_owned_and_holds_admission(service):
    from starlette.background import BackgroundTask
    service.cfg.upstream.stream_cleanup_timeout_seconds = .025
    service.client = FakeClient(responses=[FakeResponse(sample_sse())])
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0
    async def callback():
        nonlocal calls
        calls += 1
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
    owned = response(service)
    owned.background = BackgroundTask(callback)
    wire = Timeline()
    await owned(request_scope(service), wire.receive, wire.send)
    assert started.is_set() and calls == 1
    assert service._verified_controls and service._owned_tasks
    release.set()
    await wait_until(lambda: not service._verified_controls)
    assert not service._owned_tasks


async def test_shutdown_cancels_owned_collector_and_fences_admission(service):
    opened = asyncio.Event()
    service.client = FakeClient(responses=[Gated(asyncio.Event(), opened)])
    wire = Timeline()
    task = asyncio.create_task(response(service)({}, wire.receive, wire.send))
    await opened.wait()
    service.begin_stopping()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not service._verified_controls and not service._owned_tasks
    assert (await service.pool.all())[0].in_flight == 0
    with pytest.raises(UpstreamFailure):
        service._admit_verified()
