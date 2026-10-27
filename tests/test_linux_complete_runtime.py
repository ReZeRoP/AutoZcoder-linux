"""Offline regressions for the complete Fedora runtime integration."""
from pathlib import Path
import os
import subprocess
import sys
import time

import pytest
import yaml
from fastapi.testclient import TestClient

from zcode_gateway.app import create_app
from zcode_gateway.config import load_config
from zcode_gateway.generator import EnvSetup, GeneratorManager, resolve_python
from zcode_gateway.linux_desktop import bootstrap_config
from zcode_gateway.linux_runtime import configure_runtime, stop_process


def test_private_runtime_selection(tmp_path, monkeypatch):
    wrapper = tmp_path / 'generator-python'
    wrapper.write_text('#!/bin/sh\n')
    browsers = tmp_path / 'browsers/chromium-1248/chrome-linux64'
    browsers.mkdir(parents=True)
    (browsers / 'chrome').touch()
    monkeypatch.setenv('AUTOZCODER_PACKAGE_MANAGED', '1')
    monkeypatch.setenv('AUTOZCODER_RUNTIME_ROOT', str(tmp_path))
    configure_runtime()
    assert resolve_python() == str(wrapper)
    assert os.environ['PLAYWRIGHT_BROWSERS_PATH'] == str(tmp_path / 'browsers')
    assert os.environ['AUTOZCODER_CHROMIUM_EXECUTABLE'] == str(browsers / 'chrome')


@pytest.mark.parametrize('valid', [True, False])
def test_setup_checks_explicit_worker_interpreter(tmp_path, monkeypatch, valid):
    from zcode_gateway import linux_runtime
    explicit = str(tmp_path / 'custom-python')
    monkeypatch.setattr(linux_runtime, 'generator_python', lambda: '/packaged/python')
    setup = EnvSetup()
    setup.python_exe = explicit
    checked = []
    def check(candidate):
        checked.append(candidate)
        return valid
    monkeypatch.setattr(setup, '_python_real', check)
    assert setup._find_python() == (explicit if valid else '')
    assert checked == [explicit]


def test_worker_export_preserves_edits(tmp_path):
    gen = Path(__file__).resolve().parents[1] / 'generator'
    mgr = GeneratorManager(gen, data_dir=tmp_path / 'data')
    destination = tmp_path / 'worker'
    first = mgr.export_worker(destination)
    assert first['ok']
    config = destination / 'wrangler.toml'
    config.write_text('user edits and credentials must remain\n')
    assert mgr.export_worker(destination)['ok']
    assert config.read_text() == 'user edits and credentials must remain\n'
    assert mgr.export_worker(gen.parent / 'mail-worker')['ok'] is False


def test_passive_deploy_status_no_cloud_calls(tmp_path, monkeypatch):
    mgr = GeneratorManager(tmp_path, data_dir=tmp_path / 'data')
    mgr._worker_dir().mkdir(parents=True)
    monkeypatch.setattr(mgr, '_npx', lambda: '/fake/wrangler')
    monkeypatch.setattr(mgr, '_node_version_ok', lambda: True)
    def forbidden(*args, **kwargs):
        pytest.fail('passive status must not run a cloud command')
    monkeypatch.setattr(subprocess, 'run', forbidden)
    assert mgr.worker_deploy_status()['wrangler_authed'] is None


def test_managed_setup_never_installs(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOZCODER_PACKAGE_MANAGED', '1')
    setup = EnvSetup()
    monkeypatch.setattr(setup, 'check', lambda: {'all_ok': False})
    assert setup.start_install() is False
    assert setup.state == 'failed'
    assert 'reinstall' in setup.error.lower()


@pytest.mark.parametrize('ignore_term', [False, True])
def test_process_group_shutdown(tmp_path, ignore_term):
    marker = tmp_path / 'child.pid'
    ready = tmp_path / 'ready'
    child_code = ('import os,signal,time,pathlib; '
                  + ('signal.signal(signal.SIGTERM, signal.SIG_IGN); ' if ignore_term else '')
                  + 'pathlib.Path(' + repr(str(ready)) + ').touch(); time.sleep(60)')
    code = ('import subprocess,time,pathlib; '
            'p=subprocess.Popen([' + repr(sys.executable) + ',"-c",' + repr(child_code) + ']); '
            'pathlib.Path(' + repr(str(marker)) + ').write_text(str(p.pid));time.sleep(60)')
    proc = subprocess.Popen([sys.executable, '-c', code], start_new_session=True)
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        assert ready.exists() and marker.exists()
        child = int(marker.read_text())
        stop_process(proc)
        assert proc.poll() is not None
        time.sleep(.1)
        status = Path(f'/proc/{child}/stat')
        assert not status.exists() or status.read_text().split()[2] == 'Z'
    finally:
        if proc.poll() is None:
            stop_process(proc)


def test_key_rotation_is_confirmed_and_restart_scoped(tmp_path, monkeypatch):
    for var, child in [('XDG_CONFIG_HOME','config'),('XDG_DATA_HOME','data')]:
        monkeypatch.setenv(var, str(tmp_path / child))
    path = bootstrap_config()
    cfg = load_config(path)
    cfg.pool.balance_poll_seconds = 0
    old_key = cfg.server.admin_key
    with TestClient(create_app(cfg)) as client:
        headers = {'Authorization':'Bearer '+old_key}
        route = '/admin/dash/settings/keys/rotate'
        assert client.post(route, json={'confirmed':True}).status_code in (401, 403)
        assert client.post(route, json={}, headers=headers).status_code == 400
        response = client.post(route, json={'confirmed':True}, headers=headers)
        assert response.status_code == 200
        assert response.json()['restart_required']
        assert old_key not in response.text
        assert client.get('/admin/dash/settings',headers=headers).status_code == 200
    saved = yaml.safe_load(path.read_text())
    assert saved['server']['admin_key'] != old_key
    assert saved['server']['client_keys'][0]['key'] != cfg.server.client_keys[0].key
    assert path.stat().st_mode & 0o777 == 0o600


def test_packaged_generator_uses_user_state_and_preserves_options(tmp_path, monkeypatch):
    import json
    import shutil
    from zcode_gateway import api_dash
    for var, child in [('XDG_CONFIG_HOME', 'config'), ('XDG_DATA_HOME', 'data')]:
        monkeypatch.setenv(var, str(tmp_path / child))
    monkeypatch.setenv('AUTOZCODER_PACKAGE_MANAGED', '1')
    path = bootstrap_config()
    cfg = load_config(path)
    cfg.pool.balance_poll_seconds = 0
    # Model a read-only installed generator directory and migrated user options.
    installed = tmp_path / 'installed/generator'
    installed.mkdir(parents=True)
    for name in ['main.py', 'config.py']:
        (installed / name).touch()
    cfg.generator.zcodeauto_dir = str(installed)
    state_dir = Path(cfg.accounts.dir).parent / 'generator-data'
    state_dir.mkdir(parents=True)
    saved = {'captcha': 'manual', 'claim': False, 'ip_source': 'pin',
             'pin_proxy': 'socks5://127.0.0.1:9999', 'invite_code': 'CUSTOM'}
    (state_dir / 'generator_options.json').write_text(json.dumps(saved))
    def no_finisher(*args):
        pytest.fail('disabled generator must not start referral automation')
    monkeypatch.setattr(api_dash, '_start_referral_finisher', no_finisher)
    with TestClient(create_app(cfg)) as client:
        headers = {'Authorization': 'Bearer ' + cfg.server.admin_key}
        result = client.get('/admin/dash/generator/status', headers=headers)
        assert result.status_code == 200
        manager = client.app.state.app_state.generator
        assert manager.data_dir == state_dir
        assert manager.options()['claim'] is False
        assert manager.options()['ip_source'] == 'pin'
        assert client.get('/admin/dash/generator/mail', headers=headers).status_code == 200
    assert not (installed / 'output').exists()
