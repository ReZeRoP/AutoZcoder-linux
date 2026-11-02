"""Code 3009 is terminal busy, not quota exhaustion or a failover signal."""
import asyncio
import gzip
import json
import threading
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from test_stream_retry import cfg, service, REQ
from test_relay_rate_limits import setup_client
from test_stream_routing_cleanup import TrackedBody
from test_verified_stream_api import config
from tests_fixtures import FakeClient, FakeResponse, sample_sse
from zcode_gateway.app import create_app
from zcode_gateway.pool import Account
from zcode_gateway.service import UpstreamFailure
from zcode_gateway.upstream import _relay_rate_limit_evidence, provider_busy_error


@pytest.mark.parametrize('nested', [False, True])
@pytest.mark.parametrize('code_key', ['code', 'statusCode'])
@pytest.mark.parametrize('message_key', ['message', 'msg'])
@pytest.mark.parametrize('code', [3009, '3009'])
def test_busy_envelopes_are_known_but_not_quota(nested, code_key, message_key, code):
    error = {code_key: code, message_key: 'synthetic busy'}
    raw = json.dumps({'error': error} if nested else error).encode()
    result = _relay_rate_limit_evidence({}, raw)
    assert result['origin'] == 'upstream'
    assert result['evidence'] == 'provider_code'
    assert result['provider_code'] == '3009'
    assert result['reason'] == 'provider_busy'


@pytest.mark.parametrize('body', [
    {'code': 3009, 'statusCode': '3009', 'message': 'busy', 'msg': 'busy'},
    {'code': '3009', 'msg': 'busy', 'error': {'statusCode': 3009, 'message': 'busy'}},
    {'type': 'error', 'error': {'type': 'rate_limit_error', 'code': 3009, 'message': 'busy'}},
])
def test_consistent_redundant_busy_envelopes(body):
    assert _relay_rate_limit_evidence({}, json.dumps(body).encode())['reason'] == 'provider_busy'


@pytest.mark.parametrize('body', [
    {'code': code, 'message': 'busy'} for code in
    [True, False, 3009.0, '03009', '+3009', ' 3009', '3009 ', '3009.0',
     3008, '3008', 3010, '3010', None, [], {}]
] + [
    {'code': 3009}, {'code': 3009, 'msg': None},
    {'code': 3009, 'message': 'busy', 'msg': 'other'},
    {'code': 3009, 'statusCode': 11113, 'message': 'busy'},
    {'code': 3009, 'statusCode': 3009.0, 'message': 'busy'},
    {'code': 11113, 'error': {'code': 3009, 'message': 'busy'}},
    {'message': 'other', 'error': {'code': 3009, 'message': 'busy'}},
    {'type': 'success', 'error': {'code': 3009, 'message': 'busy'}},
    {'error': {'type': 'authentication_error', 'code': 3009, 'message': 'busy'}},
])
def test_malformed_or_unknown_busy_codes_remain_unknown(body):
    assert _relay_rate_limit_evidence({}, json.dumps(body).encode())['origin'] == 'unknown'


@pytest.mark.parametrize('raw', [
    b'{"code":3009,"code":11113,"msg":"busy"}',
    b'{"code":3009,"msg":"busy","msg":"other"}',
    b'{"code":3009,"msg":"busy","other":NaN}', b'{', b'\xff',
])
def test_busy_classification_rejects_invalid_json(raw):
    assert _relay_rate_limit_evidence({}, raw)['origin'] == 'unknown'


@pytest.mark.parametrize('headers,origin,evidence,conflict', [
    ({'x-autozcoder-relay-version': '1', 'x-autozcoder-response-origin': 'relay'},
     'relay', 'versioned_header', True),
    ({'x-vercel-error': 'FUNCTION_RATE_LIMIT'}, 'relay', 'platform_header', True),
    ({'x-autozcoder-relay-version': '1', 'x-autozcoder-response-origin': 'upstream',
      'x-vercel-error': 'TOO_MANY_REQUESTS'}, 'upstream', 'versioned_header', True),
    ({'x-autozcoder-relay-version': '2', 'x-autozcoder-response-origin': 'relay'},
     'upstream', 'provider_code', False),
])
def test_busy_code_does_not_override_authoritative_headers(headers, origin, evidence, conflict):
    result = _relay_rate_limit_evidence(headers, b'{"code":3009,"msg":"busy"}')
    assert (result['origin'], result['evidence'], result['marker_conflict']) == (origin, evidence, conflict)


@pytest.mark.parametrize('verified', [False, True])
@pytest.mark.parametrize('headers', [
    {}, {'x-autozcoder-relay-version': '1', 'x-autozcoder-response-origin': 'upstream'},
    {'x-autozcoder-relay-version': '1', 'x-autozcoder-response-origin': 'upstream',
     'x-vercel-error': 'TOO_MANY_REQUESTS'},
])
async def test_relay_busy_is_one_send_safe_record_and_no_mutation(service, tmp_path, caplog, verified, headers):
    secret = 'synthetic-response-and-prompt-secret'
    raw = json.dumps({'code': 3009, 'msg': secret}).encode()
    body = TrackedBody(gzip.compress(raw))
    headers = {**headers, 'content-type': 'application/json; private=' + secret,
               'content-encoding': 'gzip', 'authorization': secret, 'retry-after': secret}
    client, relay, calls = await setup_client(service.cfg, tmp_path, [(429, headers, body)])
    original = service.client
    service.client = client
    service.cfg.models.auto_free_fallback.enabled = True
    captures = []
    service.capture = SimpleNamespace(write=lambda **kw: captures.append(kw))
    second = Account(id='two', access_token='fake')
    await service.pool.upsert(second)
    try:
        with pytest.raises(UpstreamFailure) as failure:
            payload = {**REQ, 'messages': [{'role': 'user', 'content': secret}]}
            if verified:
                await service.verified_stream(payload, model='GLM-5.3')
            else:
                await service.open_stream(payload, model='GLM-5.3')
        error = failure.value
        assert (error.status, error.code) == (429, '3009')
        assert error.details['retryable'] is False and error.details['terminal'] is True
        assert error.details['reason'] == 'provider_busy'
        diagnostic = error.details['diagnostic']
        assert diagnostic['provider_code'] == '3009'
        assert diagnostic['numeric_codes'] == {'root_code': 3009}
        assert diagnostic['read_outcome'] == 'complete'
        assert diagnostic['upstream_status'] == 429 and diagnostic['body_bytes'] == len(raw)
        assert diagnostic['request_id'] in caplog.text
        assert secret not in caplog.text and secret not in json.dumps(error.extra())
        assert secret not in str(error) and not captures
        assert len(calls) == 1 and body.close_count == 1 and not relay._failed
        for account in await service.pool.all():
            assert account.in_flight == 0 and account.state.value == 'ready'
            assert not account.model_caps and not account.error_history
            assert getattr(account, 'quota_strikes', 0) == 0
        rows = service.store._conn.execute(
            'SELECT status,error_code,prompt_tokens,completion_tokens,total_tokens FROM usage').fetchall()
        assert rows == [(429, '3009', None, None, None)]
    finally:
        service.client = original
        await client.aclose()


@pytest.mark.parametrize('headers', [
    {'x-autozcoder-relay-version': '1', 'x-autozcoder-response-origin': 'relay'},
    {'x-vercel-error': 'TOO_MANY_REQUESTS'},
])
async def test_authoritative_relay_busy_rotates_relays_not_accounts(cfg, tmp_path, headers):
    body = TrackedBody(b'{"code":3009,"msg":"busy"}')
    client, relay, calls = await setup_client(cfg, tmp_path, [
        (429, headers, body),
        (200, {'content-type': 'text/event-stream'}, TrackedBody(sample_sse())),
    ])
    try:
        response = await client.send({}, dict(REQ), True)
        assert response.status_code == 200 and len(calls) == 2
        assert 'https://a.invalid' in relay._failed and body.close_count == 1
        await response.aclose()
    finally:
        await client.aclose()


@pytest.mark.parametrize('raw', [
    b'{"code":3009,"statusCode":11113,"msg":"busy"}',
    b'{"code":3009,"code":3009,"msg":"busy"}',
    b'{"code":3009.0,"msg":"busy"}',
])
async def test_invalid_busy_relay_response_stays_terminal_ambiguous(service, tmp_path, raw):
    client, relay, calls = await setup_client(service.cfg, tmp_path, [(429, {}, TrackedBody(raw))])
    original = service.client
    service.client = client
    try:
        with pytest.raises(UpstreamFailure) as failure:
            await service.verified_stream(dict(REQ), model='GLM-5.3')
        assert failure.value.code == 'relay_rate_limit_ambiguous'
        assert failure.value.details['retryable'] is False
        assert len(calls) == 1 and not relay._failed
        account = (await service.pool.all())[0]
        assert account.in_flight == 0 and not account.model_caps
    finally:
        service.client = original
        await client.aclose()


async def test_busy_after_host_rotation_retains_actual_attempt_and_send_budget(service, tmp_path):
    client, relay, calls = await setup_client(service.cfg, tmp_path, [
        (503, {}, TrackedBody()),
        (429, {}, TrackedBody(b'{"code":3009,"msg":"busy"}')),
    ])
    service.cfg.upstream.stream_max_sends = 2
    original = service.client
    service.client = client
    try:
        with pytest.raises(UpstreamFailure) as failure:
            await service.verified_stream(dict(REQ), model='GLM-5.3')
        assert failure.value.code == '3009'
        assert failure.value.details['diagnostic']['relay_attempt'] == 2
        assert len(calls) == 2 and set(relay._failed) == {'https://a.invalid'}
        account = (await service.pool.all())[0]
        assert account.in_flight == 0 and not account.model_caps
    finally:
        service.client = original
        await client.aclose()


@pytest.mark.parametrize('status', [200, 429])
async def test_direct_json_busy_has_real_status_and_no_retries(service, status):
    response = FakeResponse(b'{"code":"3009","msg":"synthetic busy"}', status=status)
    response.headers = {'content-type': 'application/json'}
    upstream = FakeClient(responses=[response])
    service.client = upstream
    with pytest.raises(UpstreamFailure) as failure:
        await service.verified_stream(dict(REQ), model='GLM-5.3')
    assert (failure.value.status, failure.value.code) == (429, '3009')
    diagnostic = failure.value.details['diagnostic']
    assert diagnostic['upstream_status'] == status
    assert diagnostic['read_outcome'] == 'complete'
    assert len(upstream.calls) == 1 and response.closed
    account = (await service.pool.all())[0]
    assert account.in_flight == 0 and not account.model_caps


async def test_busy_after_authorized_token_refresh_does_not_retry_again(service):
    unauthorized = FakeResponse(b'{"error":{"type":"authentication_error","message":"expired"}}', status=401)
    busy = FakeResponse(b'{"code":3009,"msg":"busy"}', status=429)
    unauthorized.headers = busy.headers = {'content-type': 'application/json'}
    upstream = FakeClient(responses=[unauthorized, busy])
    service.client = upstream
    refreshes = []
    async def refresh(account, force=False):
        refreshes.append(force)
        return True
    service.tokens = SimpleNamespace(refresh=refresh)
    with pytest.raises(UpstreamFailure) as failure:
        await service.verified_stream(dict(REQ), model='GLM-5.3')
    assert failure.value.code == '3009'
    assert refreshes == [True] and len(upstream.calls) == 2
    assert unauthorized.closed and busy.closed
    account = (await service.pool.all())[0]
    assert account.in_flight == 0 and not account.model_caps and not account.error_history


@pytest.mark.parametrize('raw,status', [
    (b'{"code":3009,"msg":"busy"}', 401),
    (b'{"code":3009,"msg":"busy"}', 502),
    (b'{"code":3009,"statusCode":11113,"msg":"busy"}', 429),
    (b'{"code":3009,"msg":"busy","msg":"other"}', 429),
    (b'{"code":3009,"msg":"busy"}' + b' ' * 65536, 429),
])
def test_busy_helper_requires_bounded_consistent_body_even_with_upstream_marker(raw, status):
    assert provider_busy_error(raw, {
        'x-autozcoder-relay-version': '1', 'x-autozcoder-response-origin': 'upstream',
    }, relay_url='https://fixture.invalid', status=status) is None


@pytest.mark.parametrize('outcome', ['failed', 'saturated'])
async def test_busy_recording_problems_preserve_original_terminal_error(service, caplog, outcome):
    secret = 'synthetic-disk-error-secret'
    response = FakeResponse(b'{"code":3009,"msg":"busy"}', status=429)
    service.client = FakeClient(responses=[response])
    held = 0
    if outcome == 'failed':
        def failed(**kw):
            raise OSError(secret)
        service.store.record = failed
    else:
        for _ in range(4):
            assert service._relay_record_slots.acquire(blocking=False)
            held += 1
    try:
        with pytest.raises(UpstreamFailure) as failure:
            await service.verified_stream(dict(REQ), model='GLM-5.3')
        assert failure.value.code == '3009' and failure.value.details['retryable'] is False
        assert 'recording=' + outcome in caplog.text and secret not in caplog.text
        assert response.closed and (await service.pool.all())[0].in_flight == 0
        assert not service._relay_record_futures
    finally:
        for _ in range(held):
            service._relay_record_slots.release()


@pytest.mark.parametrize('cancel', [False, True])
async def test_busy_record_writer_retains_slot_after_timeout_or_cancellation(service, cancel):
    entered, release = threading.Event(), threading.Event()
    writes = []
    def write(rec, cap):
        assert cap is None
        entered.set()
        assert release.wait(5)
        writes.append(rec)
        return True
    service._write_record = write
    account = (await service.pool.all())[0]
    task = asyncio.create_task(service._record_relay_failure(
        account, 'fixture', 'anthropic', 'GLM-5.3', '3009', 4, status=429))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert await task == 'pending'
        assert len(service._relay_record_futures) == 1
        acquired = 0
        for _ in range(4):
            if service._relay_record_slots.acquire(blocking=False):
                acquired += 1
        for _ in range(acquired):
            service._relay_record_slots.release()
        assert acquired == 3
    finally:
        release.set()
        for _ in range(100):
            if not service._relay_record_futures:
                break
            await asyncio.sleep(.01)
    assert len(writes) == 1 and not service._relay_record_futures
    assert writes[0]['status'] == 429 and writes[0]['error_code'] == '3009'
    assert writes[0]['total_tokens'] is None


@pytest.mark.parametrize('path', ['/v1/messages', '/v1/chat/completions', '/v1/completions'])
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('delivery', ['realtime', 'verified'])
def test_busy_api_parity_with_real_mock_transport(tmp_path, path, stream, delivery, caplog):
    cfg = config(tmp_path)
    cfg.upstream.stream_delivery = delivery
    secret = 'synthetic-api-request-response-secret'
    calls, captures = [], []
    raw = json.dumps({'error': {'code': 3009, 'msg': secret}}).encode()
    bodies = []
    async def transport(request):
        calls.append(True)
        body = TrackedBody(raw)
        bodies.append(body)
        return httpx.Response(429, headers={'content-type': 'application/json'}, stream=body)
    with TestClient(create_app(cfg)) as client:
        state = client.app.state.app_state
        account = Account(id='synthetic', access_token='fake')
        client.portal.call(state.pool.upsert, account)
        upstream = state.service.client
        client.portal.call(upstream._client.aclose)
        upstream._client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
        state.service.capture = SimpleNamespace(write=lambda **kw: captures.append(kw))
        payload = {'model': 'GLM-5.3', 'stream': stream,
                   'messages': [{'role': 'user', 'content': secret}]}
        if path == '/v1/completions':
            payload = {'model': 'GLM-5.3', 'stream': stream, 'prompt': secret}
        response = client.post(path, json=payload,
            headers={'Authorization': 'Bearer ' + cfg.server.admin_key})
        if response.status_code == 200:
            assert stream
            events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: {')]
            errors = [event['error'] for event in events if 'error' in event]
            assert len(errors) == 1
            error = errors[0]
        else:
            assert response.status_code == 429
            error = response.json()['error']
        assert error['code'] == '3009' and error['type'] == 'rate_limit_error'
        assert error['retryable'] is False and error['terminal'] is True
        assert error['reason'] == 'provider_busy'
        assert error['diagnostic']['provider_code'] == '3009'
        assert error['diagnostic']['request_id'] in caplog.text
        assert '[DONE]' not in response.text and 'event: message_stop' not in response.text
        assert 'event: message_start' not in response.text
        assert secret not in response.text and secret not in caplog.text
        assert len(calls) == 1 and bodies[0].close_count == 1 and not captures
        assert account.in_flight == 0 and account.state.value == 'ready'
        assert not account.model_caps and not account.error_history
        rows = state.store._conn.execute(
            'SELECT status,error_code,prompt_tokens,completion_tokens,total_tokens FROM usage').fetchall()
        assert rows == [(429, '3009', None, None, None)]
