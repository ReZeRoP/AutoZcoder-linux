"""Request-wide budgets count real chat POSTs, not abstract retry layers."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest

from test_stream_retry import cfg, REQ
from test_stream_routing_cleanup import RelaySet, TrackedBody
from tests_fixtures import frame, sample_events, sample_sse
from zcode_gateway.model_retry import wait_for_retry
from zcode_gateway.pool import Account, PoolManager
from zcode_gateway.registry import Registry
from zcode_gateway.service import ChatService, UpstreamFailure
from zcode_gateway.stream_delivery import RequestControl, StreamPolicyError
from zcode_gateway.upstream import UpstreamClient


class Budget:
    """Transport-only control spy, independent of RequestControl's clock."""
    def __init__(self, limit=1):
        self.sends = 0
        self.limit = limit
        self.phase = ''
        self.close_tasks = set()

    def check(self):
        pass

    def before_send(self):
        if self.sends >= self.limit:
            raise StreamPolicyError(502, 'stream_send_budget_exhausted', 'Budget exhausted',
                                    {'retryable': False})
        self.sends += 1

    def remaining(self):
        return 10

    cleanup_timeout = 1
    failure = None

    def spawn(self, coroutine, name):
        return asyncio.create_task(coroutine, name=name)

    def error(self, status, code, message):
        return StreamPolicyError(status, code, message, {})

    @staticmethod
    def cancel_once(task):
        if not task.done() and not task.cancelling():
            task.cancel()


async def client_for(cfg, outcomes, relay=None):
    pending = list(outcomes)
    calls = []
    async def transport(request):
        calls.append(request)
        outcome = pending.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        status, stream = outcome
        return httpx.Response(status, headers={'content-type': 'text/event-stream'}, stream=stream)
    client = UpstreamClient(cfg.upstream, relay_manager=relay)
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    return client, calls


async def test_actual_relay_posts_share_one_budget_and_close_failed_response(cfg):
    first = TrackedBody()
    second = TrackedBody(sample_sse())
    relay = RelaySet(3)
    client, calls = await client_for(cfg, [(503, first), (200, second)], relay)
    budget = Budget(1)
    try:
        with pytest.raises(StreamPolicyError) as failure:
            await client.send({}, dict(REQ), True, control=budget)
        assert failure.value.code == 'stream_send_budget_exhausted'
        assert len(calls) == budget.sends == 1
        assert first.close_count == 1
        assert second.close_count == 0
    finally:
        await client.aclose()


async def test_timed_out_cooperative_pretransfer_close_is_cancelled_once(cfg):
    class HungClose(TrackedBody):
        def __init__(self):
            super().__init__()
            self.cancelled = 0
        async def aclose(self):
            self.close_count += 1
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled += 1
                raise
    body = HungClose()
    client, calls = await client_for(cfg, [(503, body)], RelaySet())
    budget = Budget()
    budget.cleanup_timeout = .01
    try:
        with pytest.raises(StreamPolicyError) as error:
            await client.send({}, dict(REQ), True, control=budget)
        await asyncio.sleep(0)
        assert error.value.code == 'stream_cleanup_timeout'
        assert body.close_count == body.cancelled == 1
        assert len(calls) == 1
        assert all(task.done() for task in budget.close_tasks)
    finally:
        await client.aclose()


async def test_failed_transport_post_counts_before_relay_rotation(cfg):
    relay = RelaySet(3)
    client, calls = await client_for(cfg, [httpx.ConnectError('offline synthetic'),
                                         (200, TrackedBody(sample_sse()))], relay)
    budget = Budget(1)
    try:
        with pytest.raises(StreamPolicyError):
            await client.send({}, dict(REQ), True, control=budget)
        assert len(calls) == budget.sends == 1
    finally:
        await client.aclose()


async def test_budget_checked_after_pacing_before_any_post(cfg):
    client, calls = await client_for(cfg, [(200, TrackedBody(sample_sse()))])
    budget = Budget()
    async def pace():
        budget.check = lambda: (_ for _ in ()).throw(
            StreamPolicyError(504, 'stream_buffer_timeout', 'Expired while pacing', {}))
    client._pace = pace
    try:
        with pytest.raises(StreamPolicyError):
            await client.send({}, dict(REQ), True, control=budget)
        assert not calls and budget.sends == 0
    finally:
        await client.aclose()


async def test_retry_wait_checks_policy_without_sending_or_resetting_it():
    budget = Budget()
    checks = 0
    def check():
        nonlocal checks
        checks += 1
        if checks > 1:
            raise StreamPolicyError(504, 'stream_progress_timeout', 'No progress', {})
    budget.check = check
    with pytest.raises(StreamPolicyError):
        await wait_for_retry(1, control=budget)
    assert budget.phase == 'backoff' and budget.sends == 0


async def make_service(cfg, client):
    cfg.upstream.max_attempts = 1
    cfg.upstream.overload_max_attempts = 11
    cfg.upstream.overload_base_delay_seconds = 0
    cfg.upstream.stream_max_sends = 2
    cfg.upstream.stream_verify_timeout_seconds = 5
    cfg.pool.acquire_wait_seconds = 0
    pool = PoolManager(cfg.pool, [Account(id='synthetic', access_token='not-real')])
    async def refresh(account, force=False):
        return False
    service = ChatService(cfg, pool, SimpleNamespace(refresh=refresh), client,
                          Registry(), None, SimpleNamespace(enabled=False))
    service.records = []
    service._record = lambda *args, **kwargs: service.records.append((args, kwargs))
    return service


async def test_nested_overload_attempts_stop_at_actual_post_budget(cfg):
    bodies = [TrackedBody(frame({'type': 'error', 'error': {'code': '1305', 'message': 'busy'}}))
              for _ in range(11)]
    client, calls = await client_for(cfg, [(200, body) for body in bodies])
    service = await make_service(cfg, client)
    try:
        with pytest.raises(UpstreamFailure) as failure:
            await service.verified_stream(dict(REQ), model='GLM-5.3')
        assert failure.value.code == 'stream_send_budget_exhausted'
        assert len(calls) == 2
        assert all(body.close_count == 1 for body in bodies[:2])
        assert all(body.close_count == 0 for body in bodies[2:])
        assert (await service.pool.all())[0].in_flight == 0
    finally:
        await client.aclose()


async def test_refresh_resend_uses_same_budget(cfg):
    bodies = [TrackedBody(b'{"error":{"code":"authentication_error","message":"expired"}}'),
              TrackedBody(sample_sse())]
    client, calls = await client_for(cfg, [(401, bodies[0]), (200, bodies[1])])
    service = await make_service(cfg, client)
    cfg.upstream.stream_max_sends = 1
    refreshed = []
    async def refresh(account, force=False):
        refreshed.append(force)
        account.access_token = 'refreshed-not-real'
        return True
    service.tokens.refresh = refresh
    try:
        with pytest.raises(UpstreamFailure) as failure:
            await service.verified_stream(dict(REQ), model='GLM-5.3')
        assert failure.value.code == 'stream_send_budget_exhausted'
        assert refreshed == [True]
        assert len(calls) == 1
        assert bodies[0].close_count == 1
        assert (await service.pool.all())[0].in_flight == 0
    finally:
        await client.aclose()


async def test_policy_exhaustion_is_not_masked_by_previous_interruption(cfg):
    cut = TrackedBody(frame(sample_events()[0]))
    complete = TrackedBody(sample_sse())
    client, calls = await client_for(cfg, [(200, cut), (200, complete)])
    service = await make_service(cfg, client)
    cfg.upstream.stream_max_sends = 1
    try:
        with pytest.raises(UpstreamFailure) as failure:
            await service.verified_stream(dict(REQ), model='GLM-5.3')
        assert failure.value.code == 'stream_send_budget_exhausted'
        assert len(calls) == 1
        assert cut.close_count == 1
        assert (await service.pool.all())[0].in_flight == 0
    finally:
        await client.aclose()
