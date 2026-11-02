"""Dark studio presentation changes retain gateway and action contracts."""
from pathlib import Path

from fastapi.testclient import TestClient

from zcode_gateway.app import create_app
from zcode_gateway.config import Config

ROOT = Path(__file__).resolve().parents[1]
DASH = ROOT / 'zcode_gateway/dashboard'


def test_fixed_studio_assets_and_safe_bootstrap(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = Config(accounts={'source': 'accounts_dir', 'dir': str(tmp_path / 'accounts')},
                 store={'sqlite_path': str(tmp_path / 'usage.db')},
                 logging={'capture_dir': str(tmp_path / 'logs')}, update={'enabled': False})
    cfg.pool.balance_poll_seconds = 0
    with TestClient(create_app(cfg), base_url='http://127.0.0.1:8789', client=('127.0.0.1', 111)) as client:
        page = client.get('/dash')
        assert page.status_code == 200
        assert '/dash/studio.css' in page.text and '/dash/overview.js' in page.text
        assert 'id="autozcoder-bootstrap"' in page.text
        for asset, mime in [('studio.css', 'text/css'), ('overview.js', 'application/javascript'),
                            ('preferences.js', 'application/javascript'), ('usability.js', 'application/javascript')]:
            response = client.get('/dash/' + asset)
            assert response.status_code == 200
            assert response.headers['content-type'].startswith(mime)
            assert response.headers['cache-control'] == 'no-store'
        assert client.get('/dash/../../config.yaml').status_code == 404


def test_navigation_retains_all_destinations_and_adds_overview():
    source = (DASH / 'index.html').read_text()
    for name in ('overview', 'accounts', 'models', 'usage', 'analytics', 'stats',
                 'logs', 'generator', 'proxy', 'server', 'settings', 'guide'):
        assert f'data-tab="{name}"' in source
    assert 'sessionStorage.getItem("gw-selected-tab")' in source
    assert 'PAGE_INFO' in source
    assert 'Advanced tools' not in source
    assert 'class="nav-tools"' not in source
    positions = [source.index(f'data-tab="{name}"')
                 for name in ('accounts', 'generator', 'proxy', 'models')]
    assert positions == sorted(positions)
    assert 'aria-label="Main navigation"' in source
    assert 'aria-expanded="false"' in source
    assert 'function toggleNavigation()' in source
    assert 'e.key === "/" && !editing' in source
    assert 'search.focus()' in source


def test_accounts_have_progressive_details_and_preserved_actions():
    source = (DASH / 'index.html').read_text()
    start = source.index('  async function renderAccounts(')
    end = source.index('  async function doImport()', start)
    account = source[start:end]
    assert 'class="account-table"' in account
    assert '<th>Token</th>' not in account
    assert 'class="account-details-link"' in account
    assert 'data-act="acct-details"' in account
    assert 'id="account-drawer-overlay"' in source
    assert 'Token validity' in source and 'Recent request errors' in source
    assert 'colspan="6"' in account
    for operation in ('App.loginStart()', 'App.doImport()', 'App.doReload()',
                      'App.purgeDrained()', 'App.claimAllPending()'):
        assert operation in account
    assert 'document.querySelector(".dd.open")' in account
    assert 'setSelectionRange' in account


def test_styles_are_local_legible_and_reduced_motion_safe():
    css = (DASH / 'studio.css').read_text()
    assert '--bg:#000000' in css
    assert '--panel:#000000' in css
    assert '--card:#0A0A0A' in css
    assert 'color-scheme:dark' in css
    assert 'width:236px' in css
    assert 'prefers-reduced-motion' in css
    assert ':focus-visible' in css
    assert 'table-sort' in css and 'overview-grid' in css
    assert '@import' not in css
    assert 'fonts.googleapis' not in css
