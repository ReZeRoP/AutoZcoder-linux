"""Relay-host, upstream, and ambiguous limits preserve routing/account boundaries."""
import gzip
import json

import httpx
import pytest

from test_stream_retry import cfg, REQ, service
from test_stream_routing_cleanup import TrackedBody
from tests_fixtures import sample_sse
from zcode_gateway.relay_manager import RelayManager
from zcode_gateway.service import UpstreamFailure
from zcode_gateway.upstream import (
    UpstreamClient, RelayRoutingError, _relay_rate_limit_origin,
    _relay_rate_limit_evidence, parse_upstream_error,
)


@pytest.mark.parametrize('headers,body,expected', [
    ({'x-autozcoder-relay-version': '1', 'x-autozcoder-response-origin': 'relay'}, {}, 'relay'),
    ({'x-autozcoder-relay-version': '1', 'x-autozcoder-response-origin': 'upstream'}, {}, 'upstream'),
    ({'x-autozcoder-response-origin': 'upstream'}, {}, 'unknown'),
    ({'server': 'cloudflare', 'cf-ray': 'synthetic'}, {}, 'unknown'),
    ({'x-vercel-error': 'TOO_MANY_REQUESTS'}, {}, 'relay'),
    ({}, {'type': 'error', 'error': {'type': 'rate_limit_error', 'message': 'limit'}}, 'upstream'),
    ({}, {'error': {'code': 429, 'message': 'host quota'}}, 'unknown'),
    ({}, {'type': 'error', 'error': {'type': 'unknown', 'message': 'limit'}}, 'unknown'),
])
def test_rate_limit_provenance_is_narrow(headers, body, expected):
    assert _relay_rate_limit_origin(headers, json.dumps(body).encode()) == expected


@pytest.mark.parametrize('nested', [False, True])
@pytest.mark.parametrize('code_key', ['code', 'statusCode'])
@pytest.mark.parametrize('message_key', ['message', 'msg'])
@pytest.mark.parametrize('code', [11113, '11113', 1005, '1005'])
def test_provider_quota_envelopes_follow_existing_parser_conventions(nested, code_key, message_key, code):
    error = {code_key: code, message_key: 'provider quota'}
    raw = json.dumps({'error': error} if nested else error).encode()
    evidence = _relay_rate_limit_evidence({}, raw)
    assert evidence['origin'] == 'upstream'
    assert evidence['evidence'] == 'provider_code'
    assert evidence['provider_code'] == str(code)
    # statusCode is an existing parser alias, not a new HTTP-status inference.
    assert parse_upstream_error(429, raw)['code'] == str(code)
    assert parse_upstream_error(429, raw)['message'] == 'provider quota'


@pytest.mark.parametrize('body', [
    {'type': 'error', 'error': {'type': 'rate_limit_error', 'message': 'limit'}},
    {'type': 'error', 'error': {'type': 'quota_exceeded_error', 'message': 'limit'}},
    {'type': 'error', 'error': {'type': 'rate_limit_error', 'code': 'rate_limit_error', 'message': 'limit'}},
    {'type': 'error', 'error': {'type': 'rate_limit_error', 'code': 'quota_exceeded_error', 'message': 'limit'}},
    {'type': 'error', 'error': {'type': 'rate_limit_error', 'code': 11113, 'message': 'limit'}},
    {'type': 'error', 'error': {'type': 'quota_exceeded_error', 'statusCode': '1005', 'msg': 'limit'}},
    {'code': 11113, 'statusCode': '11113', 'message': 'limit', 'msg': 'limit'},
    {'code': '1005', 'msg': 'limit', 'error': {'statusCode': 1005, 'message': 'limit'}},
])
def test_native_and_consistent_redundant_envelopes_remain_accepted(body):
    assert _relay_rate_limit_origin({}, json.dumps(body).encode()) == 'upstream'


@pytest.mark.parametrize('code', [True, False, 11113.0, 1005.0, '011113', '+11113',
                                  '11113.0', ' 11113', '11113 ', '1e3', 429, '429',
                                  11114, '11114', None, [], {}])
@pytest.mark.parametrize('typed', [False, True])
def test_noncanonical_or_unrecognized_provider_codes_fail_closed(code, typed):
    error = {'code': code, 'message': 'quota'}
    body = {'type': 'error', 'error': {'type': 'rate_limit_error', **error}} if typed else error
    assert _relay_rate_limit_origin({}, json.dumps(body).encode()) == 'unknown'


@pytest.mark.parametrize('body', [
    {'error': {'code': 'rate_limit_error', 'message': 'limit'}},
    {'error': {'type': 'rate_limit_error', 'message': 'limit'}},
    {'type': 'rate_limit_error', 'message': 'limit'},
    {'code': 'quota_exceeded_error', 'message': 'limit'},
    {'type': 'error', 'error': {'type': 'rate_limit_error', 'msg': 'limit'}},
    {'code': 11113}, {'code': 1005, 'message': None}, {'code': 11113, 'msg': 11113},
    {'code': 1005, 'message': 'limit', 'msg': None},
    {'code': 11113, 'message': 'one', 'msg': 'two'},
    {'code': 11113, 'statusCode': 1005, 'message': 'limit'},
    {'code': 11113, 'statusCode': False, 'message': 'limit'},
    {'code': '11113', 'statusCode': 11113.0, 'message': 'limit'},
    {'type': 'error', 'error': {'type': 'rate_limit_error', 'code': 11113, 'statusCode': 1005, 'message': 'limit'}},
    {'code': 1005, 'error': {'code': 11113, 'message': 'limit'}},
    {'code': 11113, 'error': {'message': 'limit'}},
    {'message': 'root', 'error': {'code': 11113, 'message': 'nested'}},
    {'msg': [], 'error': {'code': 1005, 'message': 'limit'}},
    {'type': 'success', 'error': {'code': 11113, 'message': 'limit'}},
    {'type': 'error', 'error': {'type': 'authentication_error', 'code': 11113, 'message': 'limit'}},
    {'error': {'type': [], 'code': 1005, 'message': 'limit'}},
    {'code': 11113, 'type': {}, 'message': 'limit'},
    {'error': []}, {}, [], None, 'quota exceeded', 11113,
])
def test_malformed_generic_and_conflicting_envelopes_are_unknown(body):
    assert _relay_rate_limit_origin({}, json.dumps(body).encode()) == 'unknown'


@pytest.mark.parametrize('raw', [b'{"code":11113,"code":1005,"message":"quota"}',
                                b'{"code":11113,"message":"quota","other":NaN}',
                                b'\xff\xfe', b'{', b'<html>rate_limit_error 11113</html>',
                                b'[' * 2000 + b']' * 2000])
def test_invalid_or_ambiguous_json_never_raises_secondary_errors(raw):
    assert _relay_rate_limit_origin({}, raw) == 'unknown'


@pytest.mark.parametrize('headers,body,origin,evidence,conflict', [
    ({'x-autozcoder-relay-version': '1', 'x-autozcoder-response-origin': 'relay'},
     {'code': 11113, 'message': 'limit'}, 'relay', 'versioned_header', True),
    ({'x-autozcoder-relay-version': '1', 'x-autozcoder-response-origin': 'upstream', 'x-vercel-error': 'TOO_MANY_REQUESTS'},
     {}, 'upstream', 'versioned_header', True),
    ({'x-vercel-error': 'FUNCTION_RATE_LIMIT'},
     {'code': 1005, 'msg': 'limit'}, 'relay', 'platform_header', True),
    ({'x-autozcoder-relay-version': '1', 'x-autozcoder-response-origin': 'upstream'},
     {'code': 11113, 'statusCode': 1005, 'message': 'limit'}, 'upstream', 'versioned_header', True),
    ({'x-autozcoder-relay-version': '2', 'x-autozcoder-response-origin': 'relay'},
     {'code': 11113, 'message': 'limit'}, 'upstream', 'provider_code', False),
    ({'x-autozcoder-response-origin': 'relay'}, {}, 'unknown', 'none', False),
    ({'x-autozcoder-relay-version': '1', 'x-autozcoder-response-origin': 'other'}, {}, 'unknown', 'none', False),
    ({'x-autozcoder-relay-version': '1', 'x-autozcoder-response-origin': 'relay, upstream'}, {}, 'unknown', 'none', False),
    ({'X-AutoZcoder-Relay-Version': '1', 'X-AutoZcoder-Response-Origin': 'upstream'},
     {}, 'upstream', 'versioned_header', False),
    ({'x-vercel-error': 'too_many_requests'}, {}, 'relay', 'platform_header', False),
    ({'x-vercel-error': 'OTHER', 'server': 'vercel'}, {}, 'unknown', 'none', False),
    ({'x-vercel-error': ['TOO_MANY_REQUESTS'], 'x-autozcoder-relay-version': 1}, {}, 'unknown', 'none', False),
])
def test_authoritative_header_precedence_and_bounded_conflict_flags(headers, body, origin, evidence, conflict):
    result = _relay_rate_limit_evidence(headers, json.dumps(body).encode())
    assert result['origin'] == origin
    assert result['evidence'] == evidence
    assert result['marker_conflict'] is conflict
    assert _relay_rate_limit_origin(headers, json.dumps(body).encode()) == origin


async def setup_client(cfg, tmp_path, outcomes, *, strategy='round_robin'):
    relay = RelayManager(tmp_path)
    for label in ('a', 'b', 'c'):
        relay.add_relay(label, 'https://' + label + '.invalid', 'custom')
    relay.update_options(strategy=strategy)
    pending = list(outcomes)
    calls = []
    async def transport(request):
        calls.append(str(request.url))
        status, headers, stream = pending.pop(0)
        return httpx.Response(status, headers=headers, stream=stream)
    client = UpstreamClient(cfg.upstream, relay_manager=relay)
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    return client, relay, calls


async def test_known_relay_host_limit_rotates_and_closes(cfg, tmp_path):
    failed = TrackedBody(b'host quota')
    successful = TrackedBody(sample_sse())
    client, relay, calls = await setup_client(cfg, tmp_path, [
        (429, {'x-autozcoder-relay-version': '1', 'x-autozcoder-response-origin': 'relay'}, failed),
        (200, {'content-type': 'text/event-stream'}, successful)])
    try:
        response = await client.send({}, dict(REQ), True)
        assert response.status_code == 200
        assert calls == ['https://a.invalid', 'https://c.invalid'] or calls == ['https://a.invalid', 'https://b.invalid']
        assert failed.close_count == 1 and 'https://a.invalid' in relay._failed
        await response.aclose()
    finally:
        await client.aclose()


async def test_ambiguous_limit_neither_retries_nor_changes_account(service, tmp_path):
    service.cfg.models.auto_free_fallback.enabled = True
    body = TrackedBody(b'<html>quota exceeded</html>')
    client, relay, calls = await setup_client(service.cfg, tmp_path, [(429, {}, body)])
    old = service.client
    service.client = client
    try:
        with pytest.raises(UpstreamFailure) as failure:
            await service.verified_stream(dict(REQ), model='GLM-5.3')
        assert failure.value.code == 'relay_rate_limit_ambiguous'
        assert failure.value.details['retryable'] is False
        assert len(calls) == 1 and body.close_count == 1
        assert not relay._failed
        account = (await service.pool.all())[0]
        assert account.in_flight == 0 and account.state.value == 'ready'
        assert not account.model_caps
    finally:
        service.client = old
        await client.aclose()


async def test_recognized_upstream_limit_preserves_body_without_relay_rotation(cfg, tmp_path):
    raw = json.dumps({'type': 'error', 'error': {'type': 'rate_limit_error', 'message': 'provider limit'}}).encode()
    body = TrackedBody(raw)
    client, relay, calls = await setup_client(cfg, tmp_path, [(429, {}, body)])
    try:
        response = await client.send({}, dict(REQ), True)
        assert response.status_code == 429 and await response.aread() == raw
        assert len(calls) == 1 and not relay._failed and body.close_count == 1
        await response.aclose()
    finally:
        await client.aclose()


async def test_all_cooled_single_tries_distinct_relay(cfg, tmp_path):
    client, relay, calls = await setup_client(cfg, tmp_path, [
        (503, {}, TrackedBody()), (200, {'content-type': 'text/event-stream'}, TrackedBody(sample_sse()))], strategy='single')
    for url in ('https://a.invalid', 'https://b.invalid', 'https://c.invalid'):
        relay.mark_relay_failed(url)
    try:
        response = await client.send({}, dict(REQ), True)
        assert calls == ['https://a.invalid', 'https://b.invalid']
        await response.aclose()
    finally:
        await client.aclose()


@pytest.mark.parametrize('status', [405, 429])
async def test_decoded_error_response_drops_transfer_headers(cfg, tmp_path, status):
    raw = json.dumps({'type': 'error', 'error': {'type': 'rate_limit_error', 'message': 'limit'}}).encode()
    compressed = gzip.compress(raw)
    client, _, calls = await setup_client(cfg, tmp_path, [(status, {
        'content-encoding': 'gzip', 'content-length': str(len(compressed)),
        'content-type': 'application/json', 'retry-after': '17'}, TrackedBody(compressed))])
    try:
        response = await client.send({}, dict(REQ), True)
        assert await response.aread() == raw
        assert 'content-encoding' not in response.headers
        assert int(response.headers['content-length']) == len(raw)
        assert response.headers['retry-after'] == '17' and len(calls) == 1
        await response.aclose()
    finally:
        await client.aclose()


async def test_compressed_provider_limit_is_not_a_transport_failure(service, tmp_path):
    raw = json.dumps({'type': 'error', 'error': {'type': 'rate_limit_error',
                     'code': '11113', 'message': 'provider quota'}}).encode()
    compressed = gzip.compress(raw)
    body = TrackedBody(compressed)
    client, relay, calls = await setup_client(service.cfg, tmp_path, [(429, {
        'content-type': 'application/json', 'content-encoding': 'gzip',
        'content-length': str(len(compressed)), 'retry-after': '60',
        'x-autozcoder-relay-version': '1', 'x-autozcoder-response-origin': 'upstream'}, body)])
    service.cfg.upstream.max_attempts = 1
    old = service.client
    service.client = client
    try:
        with pytest.raises(UpstreamFailure) as failure:
            await service.verified_stream(dict(REQ), model='GLM-5.3')
        assert failure.value.code == '11113'
        assert failure.value.status == 429
        assert len(calls) == body.close_count == 1
        account = (await service.pool.all())[0]
        assert account.state.value == 'ready' and account.in_flight == 0
        assert account.is_model_capped('GLM-5.3')
        assert not relay._failed
    finally:
        service.client = old
        await client.aclose()


@pytest.mark.parametrize('code,code_key,message_key,nested,compressed', [
    (11113, 'code', 'msg', False, False),
    ('11113', 'statusCode', 'message', False, True),
    (11113, 'statusCode', 'msg', True, False),
    ('11113', 'code', 'message', True, True),
    (1005, 'code', 'msg', False, True),
    ('1005', 'statusCode', 'message', False, False),
    (1005, 'statusCode', 'msg', True, True),
    ('1005', 'code', 'message', True, False),
])
async def test_provider_quota_variants_reach_normal_account_handling_without_host_rotation(
        service, tmp_path, code, code_key, message_key, nested, compressed):
    error = {code_key: code, message_key: 'provider quota'}
    raw = json.dumps({'error': error} if nested else error).encode()
    encoded = gzip.compress(raw) if compressed else raw
    headers = {'content-type': 'application/json'}
    if compressed:
        headers.update({'content-encoding': 'gzip', 'content-length': str(len(encoded))})
    body = TrackedBody(encoded)
    client, relay, calls = await setup_client(service.cfg, tmp_path, [(429, headers, body)])
    service.cfg.upstream.max_attempts = 1
    old = service.client
    service.client = client
    try:
        with pytest.raises(UpstreamFailure) as failure:
            await service.verified_stream(dict(REQ), model='GLM-5.3')
        assert failure.value.code == str(code) and failure.value.status == 429
        assert len(calls) == body.close_count == 1 and not relay._failed
        account = (await service.pool.all())[0]
        assert account.in_flight == 0
        if str(code) == '11113':
            assert account.state.value == 'ready' and account.is_model_capped('GLM-5.3')
        else:
            assert account.state.value == 'exhausted' or account.is_model_capped('GLM-5.3')
    finally:
        service.client = old
        await client.aclose()


async def test_oversize_limit_fails_closed_and_closes(cfg, tmp_path):
    body = TrackedBody(b'x' * 65537)
    client, relay, calls = await setup_client(cfg, tmp_path, [(429, {}, body)])
    try:
        with pytest.raises(RelayRoutingError, match='origin could not be verified'):
            await client.send({}, dict(REQ), True)
        assert len(calls) == 1 and body.close_count == 1 and not relay._failed
    finally:
        await client.aclose()
