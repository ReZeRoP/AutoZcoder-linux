"""Render both fork workspaces using intercepted requests and disposable state."""
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from test_v2_fork_contract import fixture_app


def test_v2_navigation_and_studio_handoff(tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    app = fixture_app(tmp_path)
    app.state.linux_identity = {"version": "2.0.0", "instance_id": "a" * 32,
                                "config_id": "b" * 64, "mode": "foreground"}
    errors, unexpected, writes = [], [], []
    with TestClient(app, base_url="http://127.0.0.1:19087",
                    client=("127.0.0.1", 123)) as client, playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch()
        except playwright.Error as exc:
            pytest.skip("Installed Chromium unavailable: " + str(exc))
        context = browser.new_context(viewport={"width": 1440, "height": 1000},
                                      service_workers="block")
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))

        def handle(route):
            request = route.request
            url = urlsplit(request.url)
            if url.hostname != "127.0.0.1":
                unexpected.append(request.url)
                route.abort()
                return
            if request.method != "GET":
                writes.append((request.method, url.path))
                route.abort()
                return
            if url.path == "/admin/dash/zcode-status":
                route.fulfill(json={"chip_kind": "off", "detail": ""})
                return
            response = client.get(url.path + ("?" + url.query if url.query else ""),
                                  headers={"Authorization": "Bearer fixture-only"})
            headers = {key: value for key, value in response.headers.items()
                       if key not in ("content-length", "content-encoding")}
            route.fulfill(status=response.status_code, headers=headers, body=response.content)

        page.route("**/*", handle)
        page.goto("http://127.0.0.1:19087/dash/v2#server")
        page.get_by_role("heading", name="Server", exact=True).wait_for()
        page.get_by_text("Endpoints", exact=True).wait_for()
        assert page.locator("#login-overlay").is_hidden()
        assert "2.0.0" in page.locator("#foot-version").inner_text()
        for section in ("Analytics", "Guide", "Server"):
            page.get_by_role("button", name=section, exact=True).click()
            page.locator("#view-title").filter(has_text=section).wait_for()
            page.wait_for_function("!document.querySelector('#view-inner .view-error')")
        assert page.get_by_role("link", name="Return to Studio").get_attribute("href") == "/dash"
        page.get_by_text("Endpoints", exact=True).wait_for()
        page.screenshot(path=str(tmp_path / "v2-server.png"))
        page.get_by_role("link", name="Return to Studio").click()
        page.get_by_role("link", name="V2 workspace").wait_for()
        assert page.url.endswith("/dash")
        page.screenshot(path=str(tmp_path / "studio.png"))
        print("Workspace screenshots:", tmp_path)
        assert not errors and not unexpected and not writes
        context.close()
        browser.close()
