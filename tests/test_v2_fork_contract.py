"""Offline contracts for the Linux v2 integration."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import yaml
from fastapi.testclient import TestClient

from zcode_gateway import __version__
from zcode_gateway.app import create_app
from zcode_gateway.config import Config

ROOT = Path(__file__).resolve().parents[1]


def fixture_app(tmp_path):
    cfg = Config(server={"admin_key": "fixture-only", "host": "127.0.0.1"},
                 accounts={"source": "accounts_dir", "dir": str(tmp_path / "accounts")},
                 store={"sqlite_path": str(tmp_path / "fixture.db")})
    cfg.update.enabled = False
    cfg.pool.balance_poll_seconds = 0
    app = create_app(cfg)
    app.state.linux_mode = "foreground"
    return app


def test_version_and_linux_release_are_package_managed():
    assert __version__ == "2.0.0"
    spec = (ROOT / "packaging/fedora/autozcoder.spec").read_text()
    assert "Version:        2.0.0" in spec
    assert "Requires:       xorg-x11-server-Xvfb" in spec
    workflow = yaml.load((ROOT / ".github/workflows/release.yml").read_text(), Loader=yaml.BaseLoader)
    assert workflow["on"]["push"]["tags"] == ["v*-linux.*"]
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["jobs"]["tests"]["uses"] == "./.github/workflows/ci.yml"
    assert all("publish" not in name and "windows" not in name for name in workflow["jobs"])


def test_linux_ci_requires_browser_coverage_on_both_python_versions():
    workflow = yaml.load((ROOT / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)
    job = workflow["jobs"]["test"]
    assert job["strategy"]["fail-fast"] == "false"
    assert job["strategy"]["matrix"]["python-version"] == ["3.12", "3.14"]
    steps = job["steps"]
    assert any("--junitxml=/tmp/autozcoder-pytest.xml" in step.get("run", "") for step in steps)
    gate = next(step for step in steps if "check_browser_coverage.py" in step.get("run", ""))
    assert gate["if"] == "always()"


def test_both_workspaces_are_local_no_store_and_share_backend(tmp_path):
    with TestClient(fixture_app(tmp_path), base_url="http://127.0.0.1",
                    client=("127.0.0.1", 123)) as client:
        studio, v2 = client.get("/dash"), client.get("/dash/v2")
        assert studio.status_code == v2.status_code == 200
        assert "/dash/v2" in studio.text and "boot.js" in v2.text
        assert studio.headers["cache-control"] == v2.headers["cache-control"] == "no-store"
        assert client.get("/dash/next").text == v2.text
        assert "fixture-only" in v2.text
        assets = ROOT / "zcode_gateway/dashboard/assets"
        for asset in assets.rglob("*"):
            if asset.is_file():
                assert client.get("/dash/assets/" + asset.relative_to(assets).as_posix()).status_code == 200
        auth = {"Authorization": "Bearer fixture-only"}
        assert client.get("/admin/dash/server", headers=auth).status_code == 200
        assert client.get("/admin/dash/analytics/overview?range=24h", headers=auth).status_code == 200
        assert client.post("/admin/dash/update/download", headers=auth).status_code == 409
        assert client.post("/admin/dash/update/apply", headers=auth).status_code == 409


def test_remote_workspace_does_not_inject_admin_key(tmp_path):
    with TestClient(fixture_app(tmp_path), client=("192.0.2.9", 123)) as client:
        for path in ("/dash", "/dash/v2", "/dash/next"):
            response = client.get(path)
            assert "fixture-only" not in response.text
        assert client.get("/admin/dash/server", headers={"Authorization": "Bearer fixture-only"}).status_code == 403


def test_workspace_host_guard_and_streaming_middleware_contract(tmp_path):
    from starlette.middleware.base import BaseHTTPMiddleware
    app = fixture_app(tmp_path)
    assert all(item.cls is not BaseHTTPMiddleware for item in app.user_middleware)
    with TestClient(app, base_url="http://untrusted.invalid",
                    client=("127.0.0.1", 123)) as client:
        for path in ("/dash", "/dash/v2", "/dash/next"):
            response = client.get(path)
            assert response.status_code == 400 and "fixture-only" not in response.text


def test_failed_manager_shutdown_does_not_replace_owner(tmp_path):
    app = fixture_app(tmp_path)
    gen = tmp_path / "generator"
    gen.mkdir()
    (gen / "main.py").touch()
    (gen / "config.py").touch()
    with TestClient(app) as client:
        state = app.state.app_state
        owner = SimpleNamespace(shutdown=Mock(side_effect=TimeoutError("fixture")),
                                request_shutdown=Mock())
        state.generator = owner
        previous = state.cfg.generator.zcodeauto_dir
        response = client.post("/admin/dash/generator/locate", json={"path": str(gen)},
                               headers={"Authorization": "Bearer fixture-only"})
        assert response.status_code == 409
        assert state.generator is owner and state.cfg.generator.zcodeauto_dir == previous
        owner.shutdown.assert_called_once()
        state.generator = None
