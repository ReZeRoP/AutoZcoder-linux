"""Fedora desktop isolation, locking and RPM updater safety."""
from pathlib import Path

import pytest
import yaml

from zcode_gateway import linux_desktop, updater


@pytest.fixture
def xdg(tmp_path, monkeypatch):
    for name, child in [("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                        ("XDG_CACHE_HOME", "cache")]:
        monkeypatch.setenv(name, str(tmp_path / child))
    return tmp_path


def test_bootstrap_is_private_and_preserves_config(xdg):
    path = linux_desktop.bootstrap_config()
    data = yaml.safe_load(path.read_text())
    assert path == xdg / "config/autozcoder/config.yaml"
    assert path.stat().st_mode & 0o777 == 0o600
    assert data["server"]["port"] == 8789
    assert data["accounts"]["dir"] == str(xdg / "data/autozcoder/accounts")
    assert data["update"]["enabled"] is False
    assert data["generator"]["enabled"] is False
    assert data["accounts"]["source"] == "accounts_dir"
    original = path.read_bytes()
    assert linux_desktop.bootstrap_config().read_bytes() == original


def test_distinct_install_keys(xdg, monkeypatch):
    first = yaml.safe_load(linux_desktop.bootstrap_config().read_text())
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg / "other"))
    second = yaml.safe_load(linux_desktop.bootstrap_config().read_text())
    assert first["server"]["admin_key"] != second["server"]["admin_key"]
    assert first["server"]["client_keys"] != second["server"]["client_keys"]


def test_lock_released_and_scoped_per_config(xdg):
    path = linux_desktop.bootstrap_config()
    first = linux_desktop.InstanceLock(path)
    second = linux_desktop.InstanceLock(path)
    other = linux_desktop.InstanceLock(xdg / "other.yaml")
    assert first.acquire()
    assert not second.acquire()
    assert other.acquire()
    first.close()
    assert second.acquire()
    second.close()
    other.close()


def test_managed_settings_cannot_break_loopback_startup(xdg, monkeypatch):
    from fastapi.testclient import TestClient
    from zcode_gateway.app import create_app
    from zcode_gateway.config import load_config

    monkeypatch.setenv("AUTOZCODER_PACKAGE_MANAGED", "1")
    cfg = load_config(linux_desktop.bootstrap_config())
    cfg.pool.balance_poll_seconds = 0
    headers = {"Authorization": "Bearer " + cfg.server.admin_key}
    with TestClient(create_app(cfg)) as client:
        settings = client.get("/admin/dash/settings", headers=headers).json()
        choices = settings["choices"]["server.host"]
        assert all(c["value"] in ("127.0.0.1", "localhost", "::1") for c in choices)
        for host in ("0.0.0.0", "::", "my-pc.lan"):
            response = client.put("/admin/dash/settings", headers=headers,
                                  json={"values": {"server.host": host}})
            assert response.status_code == 400
        assert client.put("/admin/dash/settings", headers=headers,
                          json={"values": {"server.host": "::1"}}).status_code == 200


async def test_rpm_updater_never_calls_network(monkeypatch):
    monkeypatch.setenv("AUTOZCODER_PACKAGE_MANAGED", "1")
    class NoNetwork:
        async def get(self, *args):
            pytest.fail("RPM updater must not use network")
        async def aclose(self):
            pass
    checker = updater.UpdateChecker("B3hnamR/AutoZcoder", "1.1.0", client=NoNetwork())
    assert (await checker.check())["package_managed"] is True
    with pytest.raises(RuntimeError, match="RPM-managed"):
        await updater.download_update("https://github.com/test", None)
    with pytest.raises(RuntimeError, match="RPM-managed"):
        updater.apply_update(Path("/does-not-exist"))
