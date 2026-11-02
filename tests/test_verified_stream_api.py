"""Verify SSE route wiring using synthetic upstream responses, no real accounts."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from zcode_gateway.app import create_app
from zcode_gateway.config import Config, UpstreamConfig
from zcode_gateway.pool import Account
from zcode_gateway.upstream import iter_sse_events
from tests_fixtures import FakeClient, FakeResponse, frame, sample_events, sample_sse


class Broken(FakeResponse):
    async def aiter_bytes(self):
        async for part in super().aiter_bytes():
            yield part
        raise httpx.RemoteProtocolError('private-upstream-address must not be exposed')


def config(tmp_path):
    cfg = Config(accounts={'source': 'accounts_dir', 'dir': str(tmp_path / 'accounts')},
                 store={'sqlite_path': str(tmp_path / 'usage.db')},
                 logging={'capture_dir': str(tmp_path / 'logs'), 'capture': False},
                 update={'enabled': False}, upstream={'stream_delivery': 'verified'})
    cfg.pool.balance_poll_seconds = 0
    return cfg


@pytest.mark.parametrize('path,native', [('/v1/chat/completions', False), ('/v1/messages', True), ('/v1/completions', False)])
def test_verified_routes_deliver_only_one_complete_attempt(tmp_path, path, native):
    cfg = config(tmp_path)
    with TestClient(create_app(cfg)) as client:
        state = client.app.state.app_state
        account = Account(id='synthetic', access_token='not-real')
        client.portal.call(state.pool.upsert, account)
        upstream = FakeClient(responses=[Broken(b''.join(frame(e) for e in sample_events()[:7])),
                                         FakeResponse(sample_sse())])
        state.service.client = upstream
        body = {'model': 'GLM-5.3', 'stream': True, 'messages': [{'role': 'user', 'content': 'synthetic'}]}
        if path == '/v1/completions':
            body = {'model': 'GLM-5.3', 'stream': True, 'prompt': 'synthetic'}
        response = client.post(path, json=body, headers={'Authorization': 'Bearer ' + cfg.server.admin_key})
        assert response.status_code == 200
        assert response.headers['content-type'].startswith('text/event-stream')
        assert len(upstream.calls) == 2
        assert 'private-upstream-address' not in response.text
        if native:
            assert response.text.count('event: message_start') == 1
            assert response.text.count('event: message_stop') == 1
        else:
            assert response.text.count('data: [DONE]') == 1
        assert all(r.closed for r in upstream.responses)
        assert account.in_flight == 0
        assert 'stream_delivery' not in upstream.calls[0][1]
        assert 'stream_buffer_max_bytes' not in upstream.calls[0][1]


@pytest.mark.parametrize('path', ['/v1/chat/completions', '/v1/messages'])
def test_exhausted_verified_cut_is_sse_error_after_keepalive_headers(tmp_path, path):
    cfg = config(tmp_path)
    with TestClient(create_app(cfg)) as client:
        state = client.app.state.app_state
        account = Account(id='synthetic', access_token='not-real')
        client.portal.call(state.pool.upsert, account)
        upstream = FakeClient(responses=[Broken(frame(sample_events()[0])) for _ in range(3)])
        state.service.client = upstream
        response = client.post(path, json={'model': 'GLM-5.3', 'stream': True,
            'messages': [{'role': 'user', 'content': 'synthetic'}]},
            headers={'Authorization': 'Bearer ' + cfg.server.admin_key})
        assert response.status_code == 200
        assert response.headers['content-type'].startswith('text/event-stream')
        assert '[DONE]' not in response.text
        assert 'event: message_stop' not in response.text
        assert 'event: message_start' not in response.text
        if path == '/v1/messages':
            assert response.text.startswith('event: ping\n')
            assert 'event: error' in response.text
        else:
            assert response.text.startswith(': keepalive\n\n')
        assert 'upstream_stream_interrupted' in response.text
        assert 'private-upstream-address' not in response.text
        assert account.in_flight == 0


@pytest.mark.parametrize('settings', [
    {'stream_delivery': 'unknown'}, {'stream_buffer_max_bytes': 0},
    {'stream_buffer_max_bytes': 100000000}, {'stream_verify_timeout_seconds': float('nan')},
    {'stream_verify_timeout_seconds': 0}, {'stream_progress_timeout_seconds': float('inf')},
    {'stream_heartbeat_seconds': 0}, {'stream_max_sends': 0}, {'stream_max_concurrent': 0},
    {'stream_cleanup_timeout_seconds': 6}, {'stream_send_timeout_seconds': 31},
])
def test_verified_configuration_rejects_invalid_limits(settings):
    with pytest.raises(ValidationError):
        UpstreamConfig(**settings)


def test_realtime_default_remains_compatible():
    assert UpstreamConfig().stream_delivery == 'realtime'
    cfg = UpstreamConfig()
    assert (cfg.stream_verify_timeout_seconds, cfg.stream_progress_timeout_seconds,
            cfg.stream_heartbeat_seconds, cfg.stream_max_sends, cfg.stream_max_concurrent,
            cfg.stream_cleanup_timeout_seconds, cfg.stream_send_timeout_seconds) == (300, 600, 10, 12, 4, 5, 30)


@pytest.mark.parametrize('path', ['/v1/chat/completions', '/v1/messages', '/v1/completions'])
@pytest.mark.parametrize('invalid', ['auth', 'model', 'tokens', 'messages'])
def test_verified_preflight_stays_json_without_opening_upstream(tmp_path, path, invalid):
    cfg = config(tmp_path)
    with TestClient(create_app(cfg)) as client:
        state = client.app.state.app_state
        upstream = FakeClient()
        state.service.client = upstream
        body = {'model': 'GLM-5.3', 'stream': True,
                'messages': [{'role': 'user', 'content': 'synthetic'}]}
        if path == '/v1/completions':
            body = {'model': 'GLM-5.3', 'stream': True, 'prompt': 'synthetic'}
        headers = {'Authorization': 'Bearer ' + cfg.server.admin_key}
        if invalid == 'auth':
            headers = {'Authorization': 'Bearer incorrect-key'}
        elif invalid == 'model':
            body['model'] = 'not-a-real-model'
        elif invalid == 'tokens':
            body['max_tokens'] = -1
        else:
            if path == '/v1/completions':
                body['model'] = 42
            else:
                body['messages'] = []
        result = client.post(path, json=body, headers=headers)
        assert result.status_code == (401 if invalid == 'auth' else 400)
        assert result.headers['content-type'].startswith('application/json')
        assert 'error' in result.json()
        assert not upstream.calls and not state.service._verified_controls
