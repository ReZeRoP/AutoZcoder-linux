"""Synthetic, bounded provenance diagnostics never contain upstream/config data."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from test_stream_retry import cfg, REQ
from test_stream_routing_cleanup import TrackedBody, RelaySet
from zcode_gateway import upstream
from zcode_gateway.stream_delivery import RequestControl
from zcode_gateway.upstream import (
    UpstreamClient, RelayRoutingError, RelayErrorReadFailure, safe_relay_diagnostics,
    _relay_rate_limit_evidence, _relay_diagnostics, _bounded_relay_error,
)


SECRET = 'PRIVATE_TOKEN_device_session_model_account_url_name'
KEYS = {
    'schema_version', 'request_id', 'relay_attempt', 'relay_provider', 'upstream_status',
    'mime', 'body_shape', 'provider_code', 'error_type', 'origin', 'evidence', 'reason',
    'marker_present', 'marker_valid', 'marker_conflict', 'retry_header_present',
    'read_outcome', 'body_bytes', 'numeric_codes', 'json_structure',
}
FLAGS = ('marker_present', 'marker_valid', 'marker_conflict', 'retry_header_present')


def assert_safe(diagnostics):
    assert set(diagnostics) <= KEYS
    assert type(diagnostics['schema_version']) is int and diagnostics['schema_version'] == 1
    assert type(diagnostics['relay_attempt']) is int and 1 <= diagnostics['relay_attempt'] <= 3
    assert len(diagnostics['request_id']) in (16, 32)
    assert all(c in '0123456789abcdefABCDEF' for c in diagnostics['request_id'])
    assert all(type(v) in (str, int, bool) for k, v in diagnostics.items()
               if k not in ('numeric_codes', 'json_structure'))
    if 'numeric_codes' in diagnostics:
        codes = diagnostics['numeric_codes']
        assert type(codes) is dict and set(codes) <= set(upstream._RELAY_NUMERIC_LOCATIONS)
        assert len(codes) <= 4 and all(type(v) is int and 0 <= v <= 999999 for v in codes.values())
    if 'json_structure' in diagnostics:
        structure = diagnostics['json_structure']
        assert type(structure) is dict and set(structure) <= set(upstream._RELAY_STRUCTURE_LOCATIONS)
        assert len(structure) <= 13
        assert all(type(v) is str and v in upstream._RELAY_JSON_KINDS for v in structure.values())
    assert all(type(diagnostics[key]) is bool for key in FLAGS)
    assert SECRET not in json.dumps(diagnostics)
    assert len(json.dumps(diagnostics)) < 1600
    if 'body_bytes' in diagnostics:
        assert type(diagnostics['body_bytes']) is int and 0 <= diagnostics['body_bytes'] <= 65537


@pytest.mark.parametrize('value', [None, SECRET, [], True, 11113, {'request_id': SECRET}])
def test_exception_default_and_malformed_diagnostics_have_safe_identity(value):
    error = RelayRoutingError('relay_rate_limit_ambiguous', 'safe message', value)
    assert error.code == 'relay_rate_limit_ambiguous' and str(error) == 'safe message'
    assert_safe(error.diagnostics)
    assert 'upstream_status' not in error.diagnostics and 'body_bytes' not in error.diagnostics


@pytest.mark.parametrize('request_id', ['a' * 16, 'b' * 32, 'AB' * 8])
def test_sanitizer_preserves_valid_gateway_identity_and_is_idempotent(request_id):
    diagnostic = safe_relay_diagnostics({'request_id': request_id, 'relay_attempt': 3,
                                        'upstream_status': 429, 'body_bytes': 65537})
    assert diagnostic['request_id'] == request_id
    assert diagnostic['relay_attempt'] == 3 and diagnostic['upstream_status'] == 429
    assert safe_relay_diagnostics(diagnostic) == diagnostic
    assert_safe(diagnostic)


@pytest.mark.parametrize('request_id', ['', 'a' * 15, 'a' * 17, 'a' * 64, 'g' * 16,
                                       'a' * 16 + '\n', 11113, True, {}])
def test_sanitizer_replaces_invalid_or_unbounded_correlation(request_id):
    diagnostic = safe_relay_diagnostics({'request_id': request_id})
    assert diagnostic['request_id'] != request_id and len(diagnostic['request_id']) == 32
    assert_safe(diagnostic)


def test_sanitizer_rejects_arbitrary_keys_strings_containers_and_wrong_scalar_types():
    diagnostic = {key: SECRET for key in KEYS}
    diagnostic.update({SECRET: SECRET, 'url': SECRET, 'headers': {SECRET: SECRET},
                       'raw': SECRET, 'message': SECRET, 'schema_version': 999,
                       'relay_attempt': True, 'body_bytes': True, 'upstream_status': 429.0,
                       'marker_present': 1, 'marker_valid': 'true', 'marker_conflict': [],
                       'retry_header_present': None, 'mime': {}, 'reason': []})
    projected = safe_relay_diagnostics(diagnostic)
    assert projected['relay_attempt'] == 1 and projected['schema_version'] == 1
    assert 'body_bytes' not in projected and 'upstream_status' not in projected
    assert not any(projected[key] for key in FLAGS)
    assert_safe(projected)


@pytest.mark.parametrize('count,expected', [(-1, 0), (0, 0), (65536, 65536), (65537, 65537), (10**100, 65537)])
def test_sanitizer_bounds_numeric_size_without_serializing_other_fields(count, expected):
    projected = safe_relay_diagnostics({'body_bytes': count, 'relay_attempt': 99})
    assert projected['body_bytes'] == expected and projected['relay_attempt'] == 1
    assert_safe(projected)


@pytest.mark.parametrize('mime,expected', [
    (None, 'absent'), ('application/json; token=' + SECRET, 'json'),
    ('Application/Problem+JSON;charset=UTF-8', 'json'), ('text/html; secret=' + SECRET, 'html'),
    ('text/plain', 'text'), ('text/event-stream', 'event_stream'),
    ('application/' + SECRET, 'other'), (SECRET, 'other'),
])
def test_diagnostics_map_mime_and_headers_to_enums_only(mime, expected):
    headers = {'X-Request-ID': SECRET, SECRET: SECRET, 'retry-after-ms': SECRET}
    if mime is not None:
        headers['Content-Type'] = mime
    result = _relay_diagnostics(None, 1, None, 'https://private.invalid/' + SECRET,
                                headers, _relay_rate_limit_evidence(headers, b'{}'), body_bytes=2)
    assert result['mime'] == expected and result['retry_header_present'] is True
    assert result['relay_provider'] == 'other' and result['upstream_status'] == 429
    assert_safe(result)


@pytest.mark.parametrize('provider,expected', [('cloudflare', 'cloudflare'), ('vercel', 'vercel'),
    ('deno', 'deno'), ('netlify', 'netlify'), ('custom', 'custom'), (SECRET, 'other'), ([], 'other')])
def test_provider_identity_uses_only_private_snapshot_url_comparison(provider, expected):
    url = 'https://user:password@private.invalid/' + SECRET
    manager = SimpleNamespace(get_settings=lambda: {'relays': [
        {'id': SECRET, 'name': SECRET, 'url': url, 'type': provider, SECRET: SECRET}]})
    result = _relay_diagnostics(None, 2, manager, url)
    assert result['relay_provider'] == expected and result['relay_attempt'] == 2
    assert_safe(result)


def test_broken_or_missing_snapshot_does_not_mask_routing_failure():
    def broken():
        raise ValueError(SECRET)
    for manager in (None, SimpleNamespace(get_settings=broken),
                    SimpleNamespace(get_settings=lambda: {SECRET: SECRET})):
        result = _relay_diagnostics(None, 1, manager, 'https://private.invalid/' + SECRET)
        assert result['relay_provider'] == 'other'
        assert_safe(result)


@pytest.mark.parametrize('body,shape,code,error_type,reason', [
    ({'code': SECRET, 'msg': SECRET, SECRET: SECRET}, 'flat_error', 'other', 'absent', 'invalid_envelope'),
    ({'error': {'type': SECRET, 'message': SECRET}}, 'nested_error', 'absent', 'other', 'invalid_envelope'),
    ({'type': 'error', 'error': {'type': 'error', 'message': SECRET}}, 'typed_error', 'absent', 'error', 'invalid_envelope'),
    ([SECRET], 'nonobject', 'absent', 'absent', 'nonobject'),
])
def test_arbitrary_provider_codes_types_and_json_keys_are_never_diagnostics(body, shape, code, error_type, reason):
    raw = json.dumps(body).encode()
    result = _relay_diagnostics(None, 1, None, 'https://private.invalid', {},
                                _relay_rate_limit_evidence({}, raw), body_bytes=len(raw))
    assert result['body_shape'] == shape and result['provider_code'] == code
    assert result['error_type'] == error_type and result['reason'] == reason
    assert_safe(result)


async def make_client(cfg, outcomes, relay=None):
    relay = relay or RelaySet()
    pending, calls = list(outcomes), []
    async def transport(request):
        calls.append(request)
        status, headers, body = pending.pop(0)
        return httpx.Response(status, headers=headers, stream=body)
    client = UpstreamClient(cfg.upstream, relay_manager=relay)
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    return client, relay, calls


async def test_unknown_limit_correlates_requestcontrol_without_echoing_any_upstream_data(cfg):
    raw = json.dumps({'error': {'code': SECRET, 'type': SECRET, 'message': SECRET, SECRET: SECRET}}).encode()
    body = TrackedBody(raw)
    headers = {'content-type': 'application/json; secret=' + SECRET,
               'retry-after': SECRET, 'x-request-id': SECRET,
               'x-autozcoder-relay-version': SECRET, 'x-autozcoder-response-origin': SECRET,
               'x-vercel-error': SECRET, SECRET: SECRET}
    relay = RelaySet()
    relay.urls = ['https://private.invalid/' + SECRET]
    relay.get_settings = lambda: {'relays': [{'url': relay.urls[0], 'id': SECRET, 'name': SECRET, 'type': SECRET}]}
    client, relay, calls = await make_client(cfg, [(429, headers, body)], relay)
    control = RequestControl(cfg.upstream)
    try:
        with pytest.raises(RelayRoutingError) as caught:
            await client.send({'authorization': SECRET}, {**REQ, 'session': SECRET}, True, control)
        result = caught.value.diagnostics
        assert result['request_id'] == control.request_id
        assert result['upstream_status'] == 429 and result['read_outcome'] == 'complete'
        assert result['body_bytes'] == len(raw) and result['marker_present'] is True
        assert result['marker_valid'] is False and result['origin'] == 'unknown'
        assert result['evidence'] == 'none' and result['provider_code'] == result['error_type'] == 'other'
        assert len(calls) == control.sends == body.close_count == 1 and not relay.failed
        assert_safe(result)
    finally:
        await client.aclose()


async def test_known_host_exhaustion_keeps_last_safe_attempt_evidence(cfg):
    bodies = [TrackedBody(json.dumps({'code': 11113, 'msg': SECRET}).encode()) for _ in range(3)]
    headers = {'x-autozcoder-relay-version': '1', 'x-autozcoder-response-origin': 'relay'}
    client, relay, calls = await make_client(cfg, [(429, headers, body) for body in bodies], RelaySet(3))
    try:
        with pytest.raises(RelayRoutingError) as caught:
            await client.send({}, dict(REQ), True)
        result = caught.value.diagnostics
        assert caught.value.code == 'relay_host_rate_limited'
        assert result['relay_attempt'] == 3 and result['origin'] == 'relay'
        assert result['marker_valid'] is result['marker_conflict'] is True
        assert result['evidence'] == 'versioned_header' and result['upstream_status'] == 429
        assert len(calls) == len(relay.failed) == 3 and all(body.close_count == 1 for body in bodies)
        assert_safe(result)
    finally:
        await client.aclose()


async def test_no_candidate_has_correlation_but_no_fabricated_response_metadata(cfg):
    relay = RelaySet()
    relay.get_relay_for_model = lambda model, exclude_urls=None: None
    relay.relay_required_for_model = lambda model: True
    client, relay, calls = await make_client(cfg, [], relay)
    control = RequestControl(cfg.upstream)
    try:
        with pytest.raises(RelayRoutingError) as caught:
            await client.send({}, dict(REQ), True, control)
        result = caught.value.diagnostics
        assert caught.value.code == 'relay_candidates_exhausted'
        assert result['reason'] == 'candidates_exhausted' and result['request_id'] == control.request_id
        assert result['relay_provider'] == 'other' and result['relay_attempt'] == 1
        assert 'upstream_status' not in result and 'body_bytes' not in result
        assert not calls and not relay.failed
        assert_safe(result)
    finally:
        await client.aclose()


@pytest.mark.parametrize('error,outcome', [(ValueError(SECRET), 'read_failure'),
    (httpx.ReadError(SECRET), 'read_failure'), (httpx.ReadTimeout(SECRET), 'read_timeout'),
    (TimeoutError(SECRET), 'read_timeout')])
async def test_failed_reads_emit_fixed_reason_and_close_once(cfg, error, outcome):
    body = TrackedBody(error=error)
    client, relay, calls = await make_client(cfg, [(429, {}, body)])
    try:
        with pytest.raises(RelayRoutingError) as caught:
            await client.send({}, dict(REQ), True)
        result = caught.value.diagnostics
        assert result['read_outcome'] == result['reason'] == outcome
        assert result['body_shape'] == 'unreadable' and result['body_bytes'] == 0
        assert len(calls) == body.close_count == 1 and not relay.failed
        assert_safe(result)
    finally:
        await client.aclose()


async def test_oversize_decoded_body_count_is_capped_and_not_retained(cfg):
    body = TrackedBody(SECRET.encode() * 5000)
    client, relay, calls = await make_client(cfg, [(429, {}, body)])
    try:
        with pytest.raises(RelayRoutingError) as caught:
            await client.send({}, dict(REQ), True)
        result = caught.value.diagnostics
        assert result['body_bytes'] == 65537 and result['body_shape'] == 'unreadable'
        assert result['read_outcome'] == result['reason'] == 'body_limit'
        assert len(calls) == body.close_count == 1 and not relay.failed
        assert_safe(result)
    finally:
        await client.aclose()


async def test_reader_keeps_five_second_timeout_and_partial_byte_count(monkeypatch):
    observed = []
    real_timeout = asyncio.timeout
    def quick_timeout(delay):
        observed.append(delay)
        return real_timeout(.01)
    monkeypatch.setattr(upstream.asyncio, 'timeout', quick_timeout)
    class PartialBody:
        async def aiter_bytes(self):
            yield b'abc'
            await asyncio.Event().wait()
    with pytest.raises(RelayErrorReadFailure) as caught:
        await _bounded_relay_error(PartialBody())
    assert observed == [5]
    assert caught.value.reason == 'read_timeout' and caught.value.body_bytes == 3
    assert SECRET not in str(caught.value)


async def test_429_cancellation_propagates_without_rotation_and_closes_once(cfg):
    body = TrackedBody(hung=True)
    client, relay, calls = await make_client(cfg, [(429, {}, body)])
    control = RequestControl(cfg.upstream)
    task = asyncio.create_task(client.send({}, dict(REQ), True, control))
    try:
        await asyncio.wait_for(body.entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert body.close_count == 1 and len(calls) == control.sends == 1
        assert not relay.failed and body.read_finished.is_set()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await client.aclose()
