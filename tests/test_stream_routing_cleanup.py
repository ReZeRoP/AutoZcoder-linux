"""Offline regressions for immutable routing pins and pre-transfer relay cleanup."""
import asyncio
import copy
import json
from types import SimpleNamespace

import httpx
import pytest

from test_stream_retry import cfg, REQ
from tests_fixtures import FakeClient, FakeResponse, frame, sample_sse
from zcode_gateway.pool import Account, PoolManager
from zcode_gateway.registry import Registry
from zcode_gateway.service import ChatService, UpstreamFailure
from zcode_gateway.upstream import UpstreamClient


class TrackingPool(PoolManager):
    def __init__(self, config):
        # Without only_id the real router deterministically chooses "other".
        super().__init__(config, [Account(id='other', access_token='other-token'),
                                  Account(id='pinned', access_token='pinned-token')])
        self.acquisitions = []
        self.releases = []

    async def acquire(self, exclude=None, **kwargs):
        self.acquisitions.append((kwargs.get('model'), kwargs.get('only_id')))
        return await super().acquire(exclude, **kwargs)

    async def release(self, account):
        self.releases.append(account.id)
        await super().release(account)


@pytest.fixture
def routing_service(cfg):
    cfg.upstream.max_attempts = 1
    cfg.upstream.overload_max_attempts = 3
    cfg.upstream.overload_base_delay_seconds = 0
    cfg.pool.max_in_flight_per_account = 1

    async def refresh(account, force=False):
        return False

    svc = ChatService(cfg, TrackingPool(cfg.pool), SimpleNamespace(refresh=refresh),
                      FakeClient(), Registry(),
                      None, SimpleNamespace(enabled=False))
    svc.records = []
    svc._record = lambda *args: svc.records.append(args)
    return svc


PIN_LAYOUTS = ['nested-account', 'nested-id', 'nested-both',
               'top-account', 'top-id', 'nested-over-top']


def pinned_payload(layout):
    payload = copy.deepcopy(REQ)
    if layout.startswith('nested'):
        native = payload.pop('messages')
        payload['_anthropic'] = {'messages': native}
        if layout == 'nested-id':
            payload['_anthropic']['account_id'] = 'pinned'
        else:
            payload['_anthropic']['account'] = 'pinned'
        if layout == 'nested-both':
            payload['_anthropic']['account_id'] = 'other'
        if layout == 'nested-over-top':
            payload['account'] = 'other'
    else:
        payload['account' if layout == 'top-account' else 'account_id'] = 'pinned'
    return payload


@pytest.mark.parametrize('layout', PIN_LAYOUTS)
async def test_first_attempt_preserves_request_and_does_not_forward_routing(routing_service, layout):
    svc = routing_service
    payload = pinned_payload(layout)
    before = copy.deepcopy(payload)
    handle = await svc.open_stream(payload, model='GLM-5.3')
    try:
        assert handle.account.id == 'pinned'
        assert payload == before
        assert not {'account', 'account_id'} & handle.body.keys()
        assert svc.pool.acquisitions == [('GLM-5.3', 'pinned')]
    finally:
        await svc._close(handle)
    assert svc.pool.releases == ['pinned']
    assert all(a.in_flight == 0 for a in await svc.pool.all())


@pytest.mark.parametrize('layout', PIN_LAYOUTS)
@pytest.mark.parametrize('error_kind', ['http', 'sse'])
async def test_overload_retries_keep_pin_and_strip_routing_fields(routing_service, layout, error_kind):
    svc = routing_service
    error = {'type': 'error', 'error': {'type': 'overloaded_error', 'code': '1305', 'message': 'busy'}}
    failed = (lambda: FakeResponse(json.dumps(error).encode(), status=529)) if error_kind == 'http' else (lambda: FakeResponse(frame(error)))
    svc.client = FakeClient(responses=[failed(), failed(), FakeResponse(sample_sse())])
    payload = pinned_payload(layout)
    before = copy.deepcopy(payload)

    verified = await svc.verified_stream(payload, model='GLM-5.3')

    assert verified.result.account.id == 'pinned'
    assert svc.pool.acquisitions == [('GLM-5.3', 'pinned')] * 3
    assert payload == before
    assert len(svc.client.calls) == 3
    assert all(headers['authorization'] == 'Bearer pinned-token' for headers, _ in svc.client.calls)
    assert all(not {'account', 'account_id'} & body.keys() for _, body in svc.client.calls)
    assert svc.pool.releases == ['pinned'] * 3
    assert all(r.closed for r in svc.client.responses)
    assert all(a.in_flight == 0 and a.state.value == 'ready' for a in await svc.pool.all())
    assert b''.join(verified.frames).count(b'data: [DONE]') == 1


@pytest.mark.parametrize('layout', PIN_LAYOUTS)
@pytest.mark.parametrize('code,status', [('rate_limit_error', 429), ('3012', 405)])
async def test_model_fallback_keeps_pin_and_strip_routing_fields(routing_service, layout, code, status):
    svc = routing_service
    svc.cfg.models.auto_free_fallback.enabled = True
    failure = json.dumps({'error': {'code': code, 'message': 'try next model'}}).encode()
    svc.client = FakeClient(responses=[FakeResponse(failure, status=status), FakeResponse(sample_sse())])
    payload = pinned_payload(layout)
    before = copy.deepcopy(payload)

    verified = await svc.verified_stream(payload, model='GLM-5.3')

    assert verified.result.account.id == 'pinned'
    assert svc.pool.acquisitions == [('GLM-5.3', 'pinned'), ('GLM-5.3-Flash', 'pinned')]
    assert payload == before
    assert [body['model'] for _, body in svc.client.calls] == ['GLM-5.3', 'GLM-5.3-Flash']
    assert all(headers['authorization'] == 'Bearer pinned-token' for headers, _ in svc.client.calls)
    assert all(not {'account', 'account_id'} & body.keys() for _, body in svc.client.calls)
    assert svc.pool.releases == ['pinned', 'pinned']
    assert all(r.closed for r in svc.client.responses)
    assert all(a.in_flight == 0 for a in await svc.pool.all())
    assert (await svc.pool.find('pinned')).is_model_capped('GLM-5.3')
    assert not (await svc.pool.find('other')).model_caps
    assert b''.join(verified.frames).count(b'data: [DONE]') == 1


class RelaySet:
    def __init__(self, count=1):
        self.urls = [f'https://relay-{i}.invalid' for i in range(count)]
        self.index = 0
        self.failed = []
        self.ok = []

    def get_relay_count(self):
        return len(self.urls)

    def get_relay_for_model(self, model):
        url = self.urls[self.index % len(self.urls)]
        self.index += 1
        return url

    def mark_relay_failed(self, url):
        self.failed.append(url)

    def mark_relay_ok(self, url):
        self.ok.append(url)


class TrackedBody(httpx.AsyncByteStream):
    def __init__(self, body=b'', *, hung=False, error=None):
        self.body, self.hung, self.error = body, hung, error
        self.entered = asyncio.Event()
        self.read_finished = asyncio.Event()
        self.close_count = 0

    async def __aiter__(self):
        self.entered.set()
        try:
            if self.hung:
                await asyncio.Event().wait()
            if self.error:
                raise self.error
            yield self.body
        finally:
            self.read_finished.set()

    async def aclose(self):
        self.close_count += 1
        # Exercise an actual await in cleanup, not only a synchronous flag.
        await asyncio.sleep(0)


@pytest.fixture
async def synthetic_client(cfg):
    clients = []

    async def make(responses, relay=None):
        relay = relay or RelaySet()
        pending = list(responses)
        calls = []

        async def transport(request):
            calls.append(request)
            status, stream = pending.pop(0)
            return httpx.Response(status, headers={'content-type': 'application/json' if status != 200 else 'text/event-stream',
                                                  'x-test-header': 'preserved'}, stream=stream)

        client = UpstreamClient(cfg.upstream, relay_manager=relay)
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
        clients.append(client)
        return client, relay, calls

    yield make
    for client in clients:
        await client.aclose()


async def test_relay_405_cancel_during_read_closes_before_ownership_transfer(synthetic_client):
    body = TrackedBody(hung=True)
    client, relay, calls = await synthetic_client([(405, body)])
    task = asyncio.create_task(client.send({}, dict(REQ), stream=True))
    try:
        await asyncio.wait_for(body.entered.wait(), 1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert body.read_finished.is_set()
    assert body.close_count == 1
    assert len(calls) == 1
    assert relay.failed == relay.ok == []


@pytest.mark.parametrize('error', [httpx.ReadError('synthetic read failure'), ValueError('synthetic decoder failure')])
async def test_relay_405_read_error_closes_untransferred_response(synthetic_client, error):
    body = TrackedBody(error=error)
    client, relay, calls = await synthetic_client([(405, body)])
    with pytest.raises(type(error)) as raised:
        await client.send({}, dict(REQ), stream=True)
    assert raised.value is error
    assert body.close_count == 1
    assert body.read_finished.is_set()
    assert len(calls) == 1 and not relay.failed


@pytest.mark.parametrize('stop', ['cancel', 'deadline', 'disconnect'])
async def test_verified_relay_405_hung_read_cleanup(routing_service, synthetic_client, stop):
    svc = routing_service
    body = TrackedBody(hung=True)
    svc.client, relay, calls = await synthetic_client([(405, body)])
    svc.cfg.upstream.stream_verify_timeout_seconds = .05 if stop == 'deadline' else 5
    disconnected = asyncio.Event()

    async def is_disconnected():
        return disconnected.is_set()

    task = asyncio.create_task(svc.verified_stream(pinned_payload('nested-account'), model='GLM-5.3',
        is_disconnected=is_disconnected if stop == 'disconnect' else None))
    try:
        await asyncio.wait_for(body.entered.wait(), 1)
        if stop == 'cancel':
            task.cancel()
        elif stop == 'disconnect':
            disconnected.set()
        if stop == 'deadline':
            with pytest.raises(UpstreamFailure) as raised:
                await asyncio.wait_for(task, 1)
            assert raised.value.status == 504
            assert raised.value.code == 'stream_buffer_timeout'
        else:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert body.read_finished.is_set() and body.close_count == 1
    assert svc.pool.acquisitions == [('GLM-5.3', 'pinned')]
    assert svc.pool.releases == ['pinned']
    assert all(a.in_flight == 0 and a.state.value == 'ready' for a in await svc.pool.all())
    assert len(calls) == 1
    assert relay.failed == relay.ok == []
    assert not svc.records  # No completed/partially delivered turn was recorded.


@pytest.mark.parametrize('stop', ['cancel', 'deadline', 'disconnect'])
@pytest.mark.parametrize('resistant', [False, True])
async def test_pretransfer_close_keeps_timeout_after_request_cancellation(
        routing_service, synthetic_client, stop, resistant):
    svc = routing_service
    closing, cancelled, finished, release = (asyncio.Event() for _ in range(4))

    class SlowClose(TrackedBody):
        async def aclose(self):
            self.close_count += 1
            closing.set()
            try:
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    if not resistant:
                        raise
                    await release.wait()
            finally:
                finished.set()

    body = SlowClose()
    svc.client, _, calls = await synthetic_client([(503, body)])
    svc.cfg.upstream.stream_verify_timeout_seconds = .03 if stop == 'deadline' else 5
    svc.cfg.upstream.stream_cleanup_timeout_seconds = .2
    disconnected = asyncio.Event()

    async def is_disconnected():
        return disconnected.is_set()

    task = asyncio.create_task(svc.verified_stream(pinned_payload('nested-account'), model='GLM-5.3',
        is_disconnected=is_disconnected if stop == 'disconnect' else None))
    try:
        await asyncio.wait_for(closing.wait(), 1)
        if stop == 'cancel':
            task.cancel()
        elif stop == 'disconnect':
            disconnected.set()
        if stop == 'deadline':
            with pytest.raises(UpstreamFailure) as raised:
                await asyncio.wait_for(task, 1)
            assert raised.value.code == 'stream_buffer_timeout'
        else:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
        await asyncio.wait_for(cancelled.wait(), .5)
        assert len(calls) == body.close_count == 1
        assert all(a.in_flight == 0 for a in await svc.pool.all())
        if resistant:
            assert not finished.is_set()
            assert svc._verified_controls
        else:
            await asyncio.wait_for(finished.wait(), .5)
            await asyncio.sleep(0)
            assert not svc._verified_controls
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.wait_for(finished.wait(), 1)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    assert not svc._verified_controls


async def test_plain_relay_405_closes_original_and_preserves_synthetic_error(synthetic_client):
    raw = b'{"error":{"code":"request_shape","message":"method not allowed"}}'
    body = TrackedBody(raw)
    client, relay, calls = await synthetic_client([(405, body)])
    resp = await client.send({}, dict(REQ), stream=True)
    try:
        assert body.close_count == 1
        assert resp.status_code == 405 and await resp.aread() == raw
        assert resp.headers['x-test-header'] == 'preserved'
        assert resp.relay_url == relay.urls[0]
        assert not relay.failed and len(calls) == 1
    finally:
        await resp.aclose()


@pytest.mark.parametrize('status,raw', [(405, b'{"error":{"code":"3012"}}'), (503, b'relay unavailable')])
async def test_relay_rotation_closes_rejected_response_but_transfers_success(synthetic_client, status, raw):
    rejected, successful = TrackedBody(raw), TrackedBody(sample_sse())
    client, relay, calls = await synthetic_client([(status, rejected), (200, successful)], RelaySet(2))
    resp = await client.send({}, dict(REQ), stream=True)
    try:
        assert rejected.close_count == 1
        assert successful.close_count == 0
        assert relay.failed == [relay.urls[0]] and relay.ok == [relay.urls[1]]
        assert resp.status_code == 200 and resp.relay_url == relay.urls[1]
        assert [str(r.url) for r in calls] == relay.urls
    finally:
        await resp.aclose()
    assert successful.close_count == 1


async def test_relay_post_send_error_closes_before_transfer(synthetic_client):
    body = TrackedBody(sample_sse())
    relay = RelaySet()

    def mark_relay_ok(url):
        raise RuntimeError('synthetic relay bookkeeping failure')

    relay.mark_relay_ok = mark_relay_ok
    client, _, _ = await synthetic_client([(200, body)], relay)
    with pytest.raises(RuntimeError, match='bookkeeping failure'):
        await client.send({}, dict(REQ), stream=True)
    assert body.close_count == 1
