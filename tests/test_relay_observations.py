"""Finite code/field observations do not add provenance or expose raw payloads."""
import gzip
import json
import logging

import pytest

from test_relay_diagnostics import SECRET, assert_safe, make_client
from test_stream_retry import cfg, service, REQ
from test_stream_routing_cleanup import TrackedBody
from zcode_gateway.service import UpstreamFailure
from zcode_gateway.upstream import _relay_rate_limit_evidence, safe_relay_diagnostics


def observed(body):
    return safe_relay_diagnostics(_relay_rate_limit_evidence({}, json.dumps(body).encode()))


@pytest.mark.parametrize('value,expected', [
    (0, 0), (999999, 999999), (123456, 123456), ('0', 0), ('999999', 999999), ('123456', 123456),
    (True, None), (False, None), (123456.0, None), (-1, None), (1000000, None), (10**1000, None),
    ('', None), ('00', None), ('012345', None), ('-0', None), ('-1', None), ('+1', None),
    (' 1', None), ('1 ', None), ('1\n', None), ('1.0', None), ('1e3', None),
    ('１２３', None), ('١٢٣', None), ('1000000', None), ('9' * 10000, None),
    (SECRET, None), (None, None), ({'code': 123456}, None), ([123456], None),
])
def test_only_bounded_canonical_numeric_values_are_observations(value, expected):
    result = observed({'code': value, 'msg': SECRET})
    assert result.get('numeric_codes', {}) == ({} if expected is None else {'root_code': expected})
    assert result['origin'] == 'unknown' and result['evidence'] == 'none'
    assert result['provider_code'] == 'other'
    assert result['json_structure']['root_msg'] == 'string'
    assert safe_relay_diagnostics(result) == result
    assert_safe(result)


def test_conflicting_root_nested_aliases_are_retained_without_becoming_evidence():
    result = observed({'code': 123456, 'statusCode': '654321', 'message': SECRET,
                       'error': {'code': '234567', 'statusCode': 345678, 'msg': SECRET,
                                 'error': {'code': 456789}, SECRET: SECRET}, SECRET: SECRET})
    assert result['numeric_codes'] == {'root_code': 123456, 'root_statusCode': 654321,
                                      'error_code': 234567, 'error_statusCode': 345678}
    assert result['json_structure'] == {'root_kind': 'object', 'root_code': 'number',
        'root_statusCode': 'string', 'root_message': 'string', 'root_error': 'object',
        'error_code': 'string', 'error_statusCode': 'number', 'error_msg': 'string', 'error_error': 'object'}
    assert result['origin'] == 'unknown' and result['reason'] == 'code_conflict'
    assert result['marker_conflict'] is True
    assert_safe(result)


@pytest.mark.parametrize('value,kind', [(None, 'null'), (True, 'boolean'), (12, 'number'),
    (1.5, 'number'), (SECRET, 'string'), ([SECRET], 'array'), ({SECRET: SECRET}, 'object')])
def test_fixed_field_types_distinguish_null_from_absence_without_values(value, kind):
    result = observed({'error': value})
    assert result['json_structure'] == {'root_kind': 'object', 'root_error': kind}
    assert 'numeric_codes' not in result
    assert_safe(result)


@pytest.mark.parametrize('value,kind', [(None, 'null'), (True, 'boolean'), (12, 'number'),
    (1.5, 'number'), (SECRET, 'string'), ([SECRET], 'array')])
def test_nonobject_root_has_only_root_kind(value, kind):
    result = observed(value)
    assert result['json_structure'] == {'root_kind': kind}
    assert result['origin'] == 'unknown' and 'numeric_codes' not in result
    assert_safe(result)


@pytest.mark.parametrize('raw', [None, b'{', b'{"code":123456,"code":654321}',
    b'{"code":123456,"error":{"x":1,"x":2}}', b'{"code":123456,"msg":NaN}',
    b'{"code":123456,"msg":Infinity}', b'{"code":123456} trailing', b'\xff'])
def test_failed_strict_parse_has_no_partial_observations(raw):
    result = safe_relay_diagnostics(_relay_rate_limit_evidence({}, raw))
    assert 'numeric_codes' not in result and 'json_structure' not in result
    assert result['origin'] == 'unknown'
    assert_safe(result)


def test_recursion_failure_omits_observations(monkeypatch):
    def recursion_failure(*args, **kwargs):
        raise RecursionError()
    with monkeypatch.context() as patch:
        patch.setattr(json, 'loads', recursion_failure)
        result = safe_relay_diagnostics(_relay_rate_limit_evidence({}, b'{"code":123456}'))
    assert 'numeric_codes' not in result and 'json_structure' not in result
    assert_safe(result)


class Poison:
    def __init__(self, key='root_code'):
        self.key = key
    def __hash__(self):
        return hash(self.key)
    def __eq__(self, other):
        raise AssertionError('untrusted equality invoked')
    def __str__(self):
        raise AssertionError('untrusted string conversion invoked')
    def __int__(self):
        raise AssertionError('untrusted integer conversion invoked')


class StringSubclass(str):
    pass


class IntSubclass(int):
    pass


class DictSubclass(dict):
    def items(self):
        raise AssertionError('untrusted mapping invoked')


def test_projection_rebuilds_maps_before_lookup_and_never_converts_objects():
    poisoned_code = {Poison('root_code'): SECRET}
    poisoned_structure = {Poison('root_kind'): SECRET}
    for result in [safe_relay_diagnostics({Poison('request_id'): SECRET}),
                   safe_relay_diagnostics({'numeric_codes': poisoned_code,
                                           'json_structure': poisoned_structure}),
                   safe_relay_diagnostics(DictSubclass(request_id=SECRET)),
                   safe_relay_diagnostics({'numeric_codes': DictSubclass(root_code=123456),
                                           'json_structure': DictSubclass(root_kind='object')})]:
        assert 'numeric_codes' not in result and 'json_structure' not in result
        assert_safe(result)
    for value in [Poison(), StringSubclass('123456'), IntSubclass(123456)]:
        result = safe_relay_diagnostics({'numeric_codes': {'root_code': value},
                                        'json_structure': {'root_kind': value}})
        assert 'numeric_codes' not in result and 'json_structure' not in result
        assert_safe(result)


def test_optional_map_output_is_fresh_finite_and_idempotent():
    diagnostic = observed({'code': 123456, 'statusCode': 654321, 'type': SECRET,
        'message': SECRET, 'msg': SECRET, 'error': {'code': 234567, 'statusCode': 345678,
        'type': SECRET, 'message': SECRET, 'msg': SECRET, 'error': SECRET}})
    assert_safe(diagnostic)
    assert len(diagnostic['numeric_codes']) == 4 and len(diagnostic['json_structure']) == 13
    projected = safe_relay_diagnostics(diagnostic)
    assert projected == diagnostic
    assert projected['numeric_codes'] is not diagnostic['numeric_codes']
    assert projected['json_structure'] is not diagnostic['json_structure']
    diagnostic['numeric_codes']['root_code'] = SECRET
    diagnostic['json_structure']['root_kind'] = SECRET
    assert_safe(projected)


@pytest.mark.parametrize('compressed', [False, True])
async def test_unknown_numeric_code_reaches_log_and_error_with_no_capture_or_retry(service, compressed, caplog):
    raw = json.dumps({'code': 123456, 'msg': SECRET, SECRET: SECRET}).encode()
    body = TrackedBody(gzip.compress(raw) if compressed else raw)
    headers = {'content-type': 'application/json'}
    if compressed:
        headers['content-encoding'] = 'gzip'
    client, relay, calls = await make_client(service.cfg, [(429, headers, body)])
    original = service.client
    service.client = client
    try:
        with caplog.at_level(logging.WARNING):
            with pytest.raises(UpstreamFailure) as caught:
                await service.verified_stream(dict(REQ), model='GLM-5.3')
        result = caught.value.extra()['diagnostic']
        assert result['numeric_codes'] == {'root_code': 123456}
        assert result['json_structure'] == {'root_kind': 'object', 'root_code': 'number', 'root_msg': 'string'}
        assert result['origin'] == 'unknown' and result['read_outcome'] == 'complete'
        assert caught.value.code == 'relay_rate_limit_ambiguous' and caught.value.extra()['retryable'] is False
        assert "'root_code': 123456" in caplog.text and result['request_id'] in caplog.text
        assert SECRET not in caplog.text
        assert len(calls) == body.close_count == 1 and not relay.failed
        assert (await service.pool.all())[0].in_flight == 0
        rows = service.store._conn.execute('SELECT status,error_code,total_tokens FROM usage').fetchall()
        assert rows == [(503, 'relay_rate_limit_ambiguous', None)]
        assert_safe(result)
    finally:
        service.client = original
        await client.aclose()
