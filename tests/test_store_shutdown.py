"""A busy SQLite worker cannot make service cleanup wait on an unbounded lock."""
import threading
import time

import pytest
from fastapi.testclient import TestClient

from zcode_gateway.store import Store
from zcode_gateway.app import create_app
from zcode_gateway.config import Config


def test_close_before_is_bounded_by_busy_worker(tmp_path):
    store = Store(str(tmp_path / 'usage.db'))
    entered = threading.Event()
    release = threading.Event()
    def worker():
        with store._lock:
            entered.set()
            release.wait(2)
    thread = threading.Thread(target=worker)
    thread.start()
    assert entered.wait(1)
    start = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            store.close_before(time.monotonic() + .05)
        assert time.monotonic() - start < .5
    finally:
        release.set()
        thread.join(1)
        store.close()


def test_close_before_keeps_overridden_close_hook(tmp_path):
    called = []
    class ObservedStore(Store):
        def close(self):
            called.append('closed')
            super().close()
    store = ObservedStore(str(tmp_path / 'usage.db'))
    store.close_before(time.monotonic() + 1)
    assert called == ['closed']


def test_factory_dashboard_gets_embedded_boot_contract(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = Config(accounts={'source': 'accounts_dir', 'dir': str(tmp_path / 'accounts')},
                 store={'sqlite_path': str(tmp_path / 'usage.db')},
                 logging={'capture_dir': str(tmp_path / 'logs')},
                 update={'enabled': False})
    cfg.pool.balance_poll_seconds = 0
    app = create_app(cfg)
    with TestClient(app, base_url='http://127.0.0.1:8788', client=('127.0.0.1', 123)) as client:
        identity = client.get('/health/instance').json()
        assert identity['mode'] == 'embedded'
        assert identity['product'] == 'autozcoder-linux'
        page = client.get('/dash')
        assert 'id="autozcoder-bootstrap"' in page.text
        auth = {'Authorization': 'Bearer ' + cfg.server.admin_key}
        assert client.get('/admin/dash/lifecycle', headers=auth).json() == identity
