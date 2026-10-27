"""Credential imports honor configuration without importing legacy Tk GUI."""
from pathlib import Path

from fastapi.testclient import TestClient

from zcode_gateway.app import create_app
from zcode_gateway.config import load_config
from zcode_gateway import linux_desktop, zcode_accounts


def test_import_uses_configured_credentials_without_tk(tmp_path, monkeypatch):
    for name, child in [("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data")]:
        monkeypatch.setenv(name, str(tmp_path / child))
    cfg = load_config(linux_desktop.bootstrap_config())
    cfg.pool.balance_poll_seconds = 0
    cfg.accounts.providers_json = str(tmp_path / "fake-credentials.json")
    captured = {}

    def fake_import(source, destination):
        captured["source"] = source
        captured["destination"] = destination
        return {"imported": 0}

    monkeypatch.setattr(zcode_accounts, "import_credentials", fake_import)
    with TestClient(create_app(cfg)) as client:
        result = client.post("/admin/dash/import",
                             headers={"Authorization": "Bearer " + cfg.server.admin_key})
    assert result.status_code == 200
    assert captured["source"] == Path(cfg.accounts.providers_json)
    assert captured["destination"] == Path(cfg.accounts.dir)
