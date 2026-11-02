"""Presentation-only Dark studio contracts; no display, network or service calls."""
import ast
from html.parser import HTMLParser
from pathlib import Path
import re

import pytest

from zcode_gateway import linux_desktop


SOURCE = Path(linux_desktop.__file__).read_text(encoding="utf-8")
OFFLINE = (linux_desktop.DASH_DIR / "offline.html").read_text(encoding="utf-8")
PALETTE = ("#000000", "#FAFAFA", "#A3A3A3", "#262626")


def test_native_css_has_shared_studio_palette_and_readable_type():
    css = linux_desktop.STUDIO_CSS.decode("utf-8")
    for color in PALETTE:
        assert color in css
        assert color in OFFLINE
    assert "font-family: Cantarell, sans-serif" in css
    assert "font-size: 13px" in css
    assert "font-size: 14px" in css
    assert "Cantarell,system-ui,sans-serif" in OFFLINE
    assert "background-image: none" in css  # Overrides stock GTK button gradients.
    assert "button:focus" in css
    assert "button:disabled" in css


def test_every_native_css_selector_is_application_scoped():
    css = linux_desktop.STUDIO_CSS.decode("utf-8")
    selectors = re.findall(r"([^{}]+)\{[^{}]*\}", css)
    assert selectors
    for group in selectors:
        for selector in group.split(","):
            assert selector.strip().startswith("#autozcoder-studio-")
    assert "provider.load_from_data(STUDIO_CSS)" in SOURCE
    assert "Gtk.StyleContext.add_provider_for_screen(" in SOURCE
    assert "Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION" in SOURCE
    for name in ("window", "strip", "wordmark", "status", "indicator", "start", "popover", "dialog"):
        assert f'set_name("autozcoder-studio-{name}")' in SOURCE


def test_controls_are_compact_and_follow_monitor_state():
    assert 'Gtk.MenuButton(label="Service")' in SOURCE
    assert 'Gtk.Popover.new(service_menu)' in SOURCE
    assert '(("Stop service", "stop"), ("Restart service", "restart"))' in SOURCE
    assert 'start_button.connect("clicked", clicked, "start")' in SOURCE
    assert 'actions_available = monitor.managed and not monitor.env_error' in SOURCE
    assert 'start_button.set_no_show_all(True)' in SOURCE
    assert 'start_button.set_visible(actions_available and data["state"] in ("offline", "stopped"))' in SOURCE
    assert 'service_menu.set_no_show_all(not actions_available)' in SOURCE
    assert 'button.set_sensitive(data["state"] != "foreign")' in SOURCE
    assert 'indicator_style.add_class("connected")' in SOURCE
    assert 'indicator_style.add_class("attention")' in SOURCE
    assert 'Gtk.Label(label="Login autostart")' in SOURCE
    assert 'autostart._verb = "disable" if data.get("enabled") else "enable"' in SOURCE
    assert 'menu_box.pack_start(autostart, False, False, 0)' in SOURCE
    assert 'controls.pack_start(autostart' not in SOURCE


def test_confirmation_and_client_only_lifecycle_are_retained():
    assert 'if verb in ("stop", "restart"):' in SOURCE
    assert 'buttons=Gtk.ButtonsType.OK_CANCEL' in SOURCE
    assert 'accepted = dialog.run() == Gtk.ResponseType.OK' in SOURCE
    assert 'API clients will disconnect. Closing the dashboard does not stop the service.' in SOURCE
    assert 'result = monitor.action(verb)' in SOURCE
    assert 'window.set_size_request(980, 620)' in SOURCE
    assert 'window.set_default_size(1280, 800)' in SOURCE
    tree = ast.parse(SOURCE)
    monitor = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "DesktopMonitor")
    close = next(node for node in monitor.body if isinstance(node, ast.FunctionDef) and node.name == "close")
    assert ast.unparse(close.body[0]) == "self.closed.set()"
    for forbidden in ("register_script_message_handler", "js_api=", "import uvicorn", "from .app import"):
        assert forbidden not in SOURCE


@pytest.mark.parametrize("uri,allowed", [
    ("http://127.0.0.1:18789/dash", True),
    ("http://127.0.0.1:18789/dash#settings", True),
    ("http://127.0.0.1:18789/dash?redirect=evil", False),
    ("http://127.0.0.1:18789/other", False),
    ("http://127.0.0.1:8789/dash", False),
    ("https://127.0.0.1:18789/dash", False),
    ("file:///tmp/dash", False),
    ("javascript:alert(1)", False),
])
def test_studio_does_not_expand_trusted_navigation(uri, allowed):
    assert linux_desktop.trusted_dashboard_url(uri, "http://127.0.0.1:18789") is allowed
    assert 'view.load_uri(monitor.origin + "/dash")' in SOURCE
    assert 'decision.ignore()' in SOURCE
    assert 'webbrowser.open(uri)' in SOURCE
    assert '"about:blank"' in SOURCE


class OfflineMarkup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.attributes = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attributes.append(dict(attrs))


def test_waiting_page_is_inert_self_contained_and_accessible():
    page = OfflineMarkup()
    page.feed(OFFLINE)
    assert "svg" in page.tags
    assert "script" not in page.tags
    assert "link" not in page.tags
    assert not set(page.tags) & {"button", "input", "form", "iframe", "object"}
    assert page.tags.count("li") == 3
    assert page.tags.count("h1") == 1
    assert 'role="status" aria-live="polite"' in OFFLINE
    assert "Waiting for the gateway" in OFFLINE
    assert "Closing this window leaves the gateway running" in OFFLINE
    assert "Custom configurations are attach-only" in OFFLINE
    assert "autozcoder serve" in OFFLINE
    assert "autozcoder ui --config PATH" in OFFLINE
    assert "@media(max-width:620px)" in OFFLINE
    assert "url(" not in OFFLINE
    for attrs in page.attributes:
        assert not set(attrs) & {"src", "href"}
        assert not any(name.startswith("on") for name in attrs)
