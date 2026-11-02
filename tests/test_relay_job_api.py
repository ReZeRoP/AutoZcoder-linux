"""Deployment HTTP calls observe one owned operation; all providers are synthetic."""
import threading
import time

import pytest
from fastapi.testclient import TestClient

from zcode_gateway.app import create_app
from zcode_gateway.config import Config

ADMIN = {'Authorization': 'Bearer synthetic-admin'}


def app_for(tmp_path):
    cfg = Config(server={'admin_key': 'synthetic-admin'},
                 accounts={'dir': str(tmp_path / 'accounts')},
                 store={'sqlite_path': str(tmp_path / 'gateway.db')},
                 logging={'capture': False, 'capture_dir': str(tmp_path / 'logs')},
                 update={'enabled': False})
    cfg.pool.balance_poll_seconds = 0
    return create_app(cfg)


def test_start_returns_before_provider_completion_and_duplicate_is_observation(tmp_path, monkeypatch):
    entered, finish = threading.Event(), threading.Event()
    calls = []
    def deploy(vercel_token, project_name='', control=None, secret=''):
        calls.append(project_name)
        entered.set()
        assert finish.wait(3)
        return {'ok': True, 'name': project_name, 'url': 'https://synthetic.invalid', 'type': 'vercel'}
    monkeypatch.setattr('zcode_gateway.relay_manager.deploy_vercel', deploy)
    with TestClient(app_for(tmp_path)) as client:
        base = '/admin/dash/relays/deploy/jobs'
        assert client.get(base).status_code == 403
        state = client.get(base, headers=ADMIN).json()
        op = state['generation'] + ':00000000-0000-4000-8000-000000000001'
        payload = {'type': 'vercel', 'token': 'synthetic-secret-token', 'projectName': 'fixture', 'operation_id': op}
        try:
            start = time.monotonic()
            response = client.post(base, json=payload, headers=ADMIN)
            assert response.status_code == 202 and time.monotonic() - start < 1
            assert entered.wait(1)
            assert response.json()['job']['id'] == op
            assert response.json()['job']['status'] == 'running'
            assert 'synthetic-secret-token' not in response.text
            duplicate = client.post(base, json=payload, headers=ADMIN)
            assert duplicate.status_code == 202 and calls == ['fixture']
            changed = client.post(base, json={**payload, 'projectName': 'different'}, headers=ADMIN)
            assert changed.status_code == 409
            other = client.post(base, json={**payload, 'operation_id': state['generation'] + ':00000000-0000-4000-8000-000000000002'}, headers=ADMIN)
            assert other.status_code == 409
            assert 'synthetic-secret-token' not in other.text
        finally:
            finish.set()
        for _ in range(100):
            result = client.get(base, params={'id': op}, headers=ADMIN)
            if result.json()['job']['status'] != 'running':
                break
            time.sleep(.01)
        assert result.json()['job']['status'] == 'succeeded'
        assert len(calls) == 1
        assert client.post(base, json=payload, headers=ADMIN).json()['job']['status'] == 'succeeded'
        assert len(calls) == 1


def test_unknown_previous_instance_is_not_started_and_netlify_is_gated(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Unsupported/old request started a provider')
    monkeypatch.setattr('zcode_gateway.relay_manager.deploy_netlify', forbidden)
    monkeypatch.setattr('zcode_gateway.relay_manager.deploy_vercel', forbidden)
    with TestClient(app_for(tmp_path)) as client:
        base = '/admin/dash/relays/deploy/jobs'
        state = client.get(base, headers=ADMIN).json()
        old = '0' * 32 + ':00000000-0000-4000-8000-000000000001'
        response = client.post(base, json={'type': 'vercel', 'token': 'synthetic', 'operation_id': old}, headers=ADMIN)
        assert response.status_code == 410
        assert client.get(base, params={'id': old}, headers=ADMIN).status_code == 410
        response = client.post(base, json={'type': 'netlify', 'netlifyToken': 'synthetic',
            'operation_id': state['generation'] + ':00000000-0000-4000-8000-000000000001'}, headers=ADMIN)
        assert response.status_code == 400
        assert 'stream' in response.json()['detail'].lower()


def test_added_provider_metadata_is_validated(tmp_path):
    with TestClient(app_for(tmp_path)) as client:
        response = client.post('/admin/dash/relays/add', headers=ADMIN,
            json={'name': 'synthetic', 'url': 'https://synthetic.invalid', 'type': '<b>unsafe</b>'})
        assert response.status_code == 400
        assert not client.get('/admin/dash/relays', headers=ADMIN).json()['relays']
