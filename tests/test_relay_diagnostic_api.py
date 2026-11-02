"""JSON/SSE dialects share safe opening diagnostics and never fabricate success."""
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from test_verified_stream_api import config
from zcode_gateway.app import create_app
from zcode_gateway.pool import Account
from zcode_gateway.upstream import RelayRoutingError


@pytest.mark.parametrize('path,native', [('/v1/messages', True),
    ('/v1/chat/completions', False), ('/v1/completions', False)])
@pytest.mark.parametrize('stream', [False, True])
def test_diagnostic_error_parity_and_single_safe_record(tmp_path, path, native, stream, caplog):
    cfg = config(tmp_path)
    secret = 'request-response-secret-marker'
    calls = []
    captures = []
    diagnostic = {'schema_version': 1, 'request_id': 'a' * 16, 'relay_attempt': 1,
        'relay_provider': 'vercel', 'upstream_status': 429, 'origin': 'unknown',
        'mime': 'json', 'body_shape': 'nested_error', 'provider_code': 'other',
        'error_type': 'other', 'evidence': 'none', 'reason': 'unsupported_envelope',
        'marker_present': False, 'marker_valid': False, 'marker_conflict': False,
        'retry_header_present': True, 'read_outcome': 'complete', 'body_bytes': 40,
        'raw_body': secret, 'relay_url': secret,
        'numeric_codes': {'error_code': '123456', 'root_statusCode': 429, secret: secret,
                          'root_code': secret, 'error_statusCode': True},
        'json_structure': {'root_kind': 'object', 'root_error': 'object',
                          'error_code': 'string', 'error_message': 'string',
                          'error_type': secret, secret: secret}}
    async def send(*args, **kwargs):
        calls.append(True)
        raise RelayRoutingError('relay_rate_limit_ambiguous', 'safe message', diagnostic)
    with TestClient(create_app(cfg)) as client:
        state = client.app.state.app_state
        account = Account(id='synthetic', access_token=secret)
        client.portal.call(state.pool.upsert, account)
        state.service.client = SimpleNamespace(send=send)
        state.service.capture = SimpleNamespace(write=lambda **kw: captures.append(kw))
        payload = {'model': 'GLM-5.3', 'stream': stream,
                   'messages': [{'role': 'user', 'content': secret}]}
        if path == '/v1/completions':
            payload = {'model': 'GLM-5.3', 'stream': stream, 'prompt': secret}
        response = client.post(path, json=payload,
            headers={'Authorization': 'Bearer ' + cfg.server.admin_key})
        if stream:
            assert response.status_code == 200
            events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: {')]
            assert len(events) == (2 if native else 1)
            errors = [event['error'] for event in events if 'error' in event]
            assert len(errors) == 1
            error = errors[0]
            assert '[DONE]' not in response.text and 'event: message_stop' not in response.text
            assert 'event: message_start' not in response.text
        else:
            assert response.status_code == 503
            error = response.json()['error']
        assert error['code'] == 'relay_rate_limit_ambiguous'
        assert error['retryable'] is False and error['terminal'] is True
        result = error['diagnostic']
        assert result['request_id'] == 'a' * 16
        assert result['upstream_status'] == 429
        assert result['numeric_codes'] == {'error_code': 123456, 'root_statusCode': 429}
        assert result['json_structure'] == {'root_kind': 'object', 'root_error': 'object',
                                            'error_code': 'string', 'error_message': 'string'}
        assert "'error_code': 123456" in caplog.text and result['request_id'] in caplog.text
        assert 'raw_body' not in result and 'relay_url' not in result
        assert secret not in response.text and secret not in caplog.text
        assert not captures and len(calls) == 1
        assert account.in_flight == 0 and account.state.value == 'ready'
        rows = state.store._conn.execute('SELECT status,error_code,total_tokens FROM usage').fetchall()
        assert rows == [(503, 'relay_rate_limit_ambiguous', None)]
