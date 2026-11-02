"""GTK dashboard client. The user service, never this window, owns the gateway."""
from __future__ import annotations

import json
import logging
import os  # compatibility: older bootstrap tests patch linux_desktop.os.link
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger("zcode_gateway.desktop")
DASH_DIR = Path(__file__).parent / "dashboard"

# Application-owned widget names keep the studio palette out of other GTK apps.
STUDIO_CSS = b"""
#autozcoder-studio-window, #autozcoder-studio-dialog,
#autozcoder-studio-popover {
    background-color: #000000;
    color: #FAFAFA;
    font-family: Cantarell, sans-serif;
    font-size: 13px;
}
#autozcoder-studio-strip {
    background-color: #000000;
    border-bottom: 1px solid #262626;
    padding: 12px 20px;
}
#autozcoder-studio-window label, #autozcoder-studio-dialog label,
#autozcoder-studio-popover label {
    color: #FAFAFA;
    font-size: 14px;
}
#autozcoder-studio-window #autozcoder-studio-wordmark {
    font-weight: 700;
    padding-right: 12px;
}
#autozcoder-studio-window #autozcoder-studio-status,
#autozcoder-studio-popover #autozcoder-studio-autostart-caption {
    color: #A3A3A3;
    font-size: 13px;
}
#autozcoder-studio-window #autozcoder-studio-indicator {
    min-width: 8px;
    min-height: 8px;
    border-radius: 8px;
    background-color: #A3A3A3;
}
#autozcoder-studio-window #autozcoder-studio-indicator.connected {
    background-color: #FAFAFA;
}
#autozcoder-studio-window #autozcoder-studio-indicator.attention {
    background-color: #E6B879;
}
#autozcoder-studio-window button, #autozcoder-studio-dialog button,
#autozcoder-studio-popover button {
    background-image: none;
    background-color: transparent;
    color: #FAFAFA;
    border: 1px solid #333333;
    border-radius: 8px;
    box-shadow: none;
    text-shadow: none;
    padding: 8px 14px;
    font-size: 13px;
}
#autozcoder-studio-window button:hover, #autozcoder-studio-dialog button:hover,
#autozcoder-studio-popover button:hover {
    background-color: #171717;
    border-color: #FAFAFA;
}
#autozcoder-studio-window button:focus, #autozcoder-studio-dialog button:focus,
#autozcoder-studio-popover button:focus {
    outline-color: #FAFAFA;
    outline-style: solid;
    outline-width: 2px;
    outline-offset: 2px;
}
#autozcoder-studio-window button:disabled, #autozcoder-studio-popover button:disabled {
    color: #A3A3A3;
    opacity: 0.5;
}
#autozcoder-studio-window #autozcoder-studio-start {
    background-color: #FAFAFA;
    border-color: #FAFAFA;
    color: #000000;
    font-weight: 700;
}
#autozcoder-studio-window #autozcoder-studio-start label {
    color: #000000;
    font-size: 13px;
    font-weight: 700;
}
#autozcoder-studio-window #autozcoder-studio-start:hover {
    background-color: #D4D4D4;
    border-color: #D4D4D4;
}
#autozcoder-studio-popover {
    border: 1px solid #333333;
    border-radius: 12px;
}
#autozcoder-studio-popover separator {
    background-color: #262626;
    min-height: 1px;
    margin: 8px 0;
}
"""


def bootstrap_config() -> Path:
    """Compatibility export; runtime initialization belongs to linux_service."""
    from .linux_service import bootstrap_config as bootstrap
    return bootstrap()


def __getattr__(name):
    if name == "InstanceLock":
        from . import linux_service
        return getattr(linux_service, name)
    raise AttributeError(name)


def trusted_dashboard_url(url: str, origin: str) -> bool:
    """Only the exact configured dashboard can replace the local waiting page."""
    try:
        u, base = urlsplit(url), urlsplit(origin)
        return (u.scheme == base.scheme == "http" and u.netloc == base.netloc
                and not u.username and not u.password and u.path == "/dash"
                and not u.query and base.path == "" and not base.query
                and base.hostname in ("localhost", "127.0.0.1", "::1"))
    except ValueError:
        return False


def _local_get(origin: str, path: str, key: str | None = None, limit=8192):
    # Reuse the numeric-loopback transport's total deadline, including slow headers.
    from .linux_config import origin_for
    from .linux_control import _LoopbackConnection
    parsed = urlsplit(origin)
    if origin_for(parsed.hostname, parsed.port) != origin or path not in ("/dash", "/admin/dash/lifecycle"):
        raise ValueError("invalid dashboard origin")
    deadline = time.monotonic() + 2
    connection = _LoopbackConnection(parsed.hostname, parsed.port, timeout=2)
    headers = {"Host": parsed.netloc, "Cache-Control": "no-cache", "Connection": "close"}
    if key is not None:
        headers["Authorization"] = "Bearer " + key
    try:
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        if response.status != 200 or response.getheader("Content-Encoding", "identity") != "identity":
            raise ValueError("invalid dashboard response")
        length = response.getheader("Content-Length")
        if length is not None and (not length.isdigit() or int(length) > limit):
            raise ValueError("dashboard response too large")
        body = bytearray()
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("dashboard response timed out")
            chunk = response.read1(min(4096, limit + 1 - len(body)))
            if not chunk:
                break
            body.extend(chunk)
            if len(body) > limit:
                raise ValueError("dashboard response too large")
        return body.decode("utf-8")
    finally:
        connection.close()


def dashboard_handshake(origin: str, instance: dict) -> bool:
    """Identity is not auth: verify trusted bootstrap and authenticated lifecycle."""
    try:
        html = _local_get(origin, "/dash", limit=2 * 1024 * 1024)
        match = re.search(r'<script id="autozcoder-bootstrap" type="application/json">(.*?)</script>',
                          html, flags=re.S)
        if not match or len(match[1]) > 8192:
            return False
        bootstrap = json.loads(match[1])
        if not isinstance(bootstrap.get("key"), str) or bootstrap.get("instance") != instance:
            return False
        authenticated = json.loads(_local_get(origin, "/admin/dash/lifecycle", bootstrap["key"]))
        return authenticated == instance and not authenticated.get("stopping")
    except Exception:
        return False


class DesktopMonitor:
    """One bounded poll owner; actions are explicit and closing only cancels monitoring."""
    def __init__(self, config_path=None, *, controller=None, probe=None, inspect=None,
                 handshake=None):
        from .linux_config import (default_config_path, config_identity, inspect_config,
                                   validate_service_environment)
        from .linux_control import ServiceController, probe_instance
        self.managed = config_path is None
        self.env_error = None
        if self.managed:
            try:
                validate_service_environment()
            except ValueError as exc:
                self.env_error = str(exc)
        self.path = Path(config_path) if config_path is not None else default_config_path()
        self.controller = controller or ServiceController()
        self.probe = probe or probe_instance
        self.inspect = inspect or inspect_config
        self.handshake = handshake or dashboard_handshake
        self.identity = config_identity
        self.origin = None
        self.instance = None
        self.closed = threading.Event()
        self.started_once = False
        self.attached = False
        self.intentional_stop = False
        self._action_lock = threading.Lock()
        self._verified_instance = None

    def _context(self):
        info = self.inspect(self.path)
        self.origin = info.get("server", {}).get("origin") if info.get("ok") else None
        return info

    def snapshot(self):
        if self.env_error:
            return {"state": "foreign", "message": self.env_error, "managed": True,
                    "origin": None, "attach": False}
        info = self._context()
        result = {"state": "offline", "message": "Waiting for the gateway…",
                  "managed": self.managed, "origin": self.origin, "attach": False}
        status = self.controller.status() if self.managed else {}
        result["properties"] = status.get("properties", {})
        result["enabled"] = result["properties"].get("UnitFileState") == "enabled"
        if not self.origin:
            result["message"] = info.get("message", "Configuration is not available yet.")
            return result
        probe = self.probe(self.origin, self.identity(self.path, self.origin))
        if not probe.get("instance") or probe.get("state") not in ("healthy", "degraded"):
            result["state"] = "foreign" if probe.get("state") == "foreign" else "offline"
            result["message"] = probe.get("message", "Gateway is unavailable.")
            if self.intentional_stop and result["state"] == "offline":
                result.update(state="stopped", message="Service stopped. API clients are disconnected.")
            elif self.managed and not status.get("ok"):
                result["message"] = status.get("message", "User service unavailable. Run autozcoder serve explicitly.")
            return result
        instance = probe.get("instance", {})
        if self.managed and (instance.get("mode") != "user-service"
                             or result["properties"].get("ActiveState") != "active"):
            result.update(state="foreign", message="Another instance owns this port. Close the old app or use a custom attach-only configuration.")
            return result
        if instance.get("stopping"):
            result.update(state="stopped" if self.intentional_stop else "offline",
                          message="Service is stopping. Your dashboard edits are retained.")
            return result
        if self._verified_instance != instance.get("instance_id"):
            if not self.handshake(self.origin, instance):
                result.update(state="unauthorized", message="Dashboard authentication failed. No listener was adopted.")
                return result
            self._verified_instance = instance.get("instance_id")
        self.instance = instance
        result.update(state=probe.get("state", "healthy"),
                      message="Connected. Closing this window leaves the gateway running.",
                      attach=not self.attached)
        return result

    def deliberate_start(self):
        if self.started_once or not self.managed or self.closed.is_set() or self.env_error:
            return
        self.started_once = True
        self._context()
        if self.origin:
            probe = self.probe(self.origin, self.identity(self.path, self.origin))
            if probe.get("state") == "foreign" or (probe.get("instance") and
                    probe["instance"].get("mode") != "user-service"):
                return  # Never adopt/kill an old embedded or unrelated listener.
        if self.controller.status().get("properties", {}).get("ActiveState") != "active":
            self.controller.action("start")  # exactly once per deliberate UI launch

    def load_failed(self):
        self.attached = False
        self._verified_instance = None

    def action(self, verb):
        if self.env_error:
            return {"ok": False, "message": self.env_error}
        if not self.managed or verb not in ("start", "stop", "restart", "enable", "disable"):
            return {"ok": False, "message": "Custom configurations are attach-only."}
        if self.closed.is_set() or not self._action_lock.acquire(blocking=False):
            return {"ok": False, "message": "A service action is already in progress."}
        try:
            if verb in ("start", "restart") and self.snapshot()["state"] == "foreign":
                return {"ok": False, "message": "Another listener owns this port. No service action was taken."}
            result = self.controller.action(verb)
            if result.get("ok"):
                if verb == "stop":
                    self.intentional_stop = True
                elif verb in ("start", "restart"):
                    self.intentional_stop = False
            return result
        finally:
            self._action_lock.release()

    def close(self):
        self.closed.set()  # no shutdown callback, signal, lock, or service action


def open_dashboard(config_path=None) -> int:
    """Launch GTK only; custom --config is strictly attach-only."""
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        gi.require_version("WebKit2", "4.1")
        from gi.repository import GLib, Gtk, WebKit2
        monitor = DesktopMonitor(config_path)
        if Path("/sys/module/nvidia").exists():
            os.environ.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")
        window = Gtk.Window(title="AutoZcoder")
        window.set_name("autozcoder-studio-window")
        window.set_default_size(1280, 800)
        window.set_size_request(980, 620)
        provider = Gtk.CssProvider()
        provider.load_from_data(STUDIO_CSS)
        Gtk.StyleContext.add_provider_for_screen(
            window.get_screen(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        controls = Gtk.Box(spacing=12)
        controls.set_name("autozcoder-studio-strip")
        wordmark = Gtk.Label(label="AutoZcoder")
        wordmark.set_name("autozcoder-studio-wordmark")
        controls.pack_start(wordmark, False, False, 0)
        indicator = Gtk.Box()
        indicator.set_name("autozcoder-studio-indicator")
        indicator.set_size_request(8, 8)
        indicator.set_valign(Gtk.Align.CENTER)
        controls.pack_start(indicator, False, False, 0)
        status = Gtk.Label(label="Waiting for the gateway…")
        status.set_name("autozcoder-studio-status")
        status.set_xalign(0)
        status.set_line_wrap(True)
        controls.pack_start(status, True, True, 0)
        actions_available = monitor.managed and not monitor.env_error
        view = WebKit2.WebView()
        view.load_html((DASH_DIR / "offline.html").read_text(encoding="utf-8"), "about:blank")
        buttons = []

        def update(data):
            if monitor.closed.is_set():
                return False
            status.set_text(data["message"])
            status.set_tooltip_text(data["message"])
            indicator_style = indicator.get_style_context()
            for name in ("connected", "attention"):
                indicator_style.remove_class(name)
            if data["state"] == "healthy":
                indicator_style.add_class("connected")
            elif data["state"] in ("degraded", "foreign", "unauthorized"):
                indicator_style.add_class("attention")
            indicator.set_tooltip_text(data["state"].capitalize())
            start_button.set_visible(actions_available and data["state"] in ("offline", "stopped"))
            service_menu.set_sensitive(data["state"] != "foreign")
            for button in buttons:
                button.set_sensitive(data["state"] != "foreign")
            autostart.set_label("Disable login autostart" if data.get("enabled") else "Enable login autostart")
            autostart._verb = "disable" if data.get("enabled") else "enable"
            if data.get("attach") and monitor.origin:
                monitor.attached = True
                view.load_uri(monitor.origin + "/dash")
            return False

        def clicked(button, verb=None):
            verb = verb or button._verb
            service_popover.popdown()
            if verb in ("stop", "restart"):
                dialog = Gtk.MessageDialog(transient_for=window, modal=True,
                    message_type=Gtk.MessageType.WARNING, buttons=Gtk.ButtonsType.OK_CANCEL,
                    text="Stop service?" if verb == "stop" else "Restart service?")
                dialog.set_name("autozcoder-studio-dialog")
                dialog.format_secondary_text("API clients will disconnect. Closing the dashboard does not stop the service.")
                accepted = dialog.run() == Gtk.ResponseType.OK
                dialog.destroy()
                if not accepted:
                    return
            def perform():
                result = monitor.action(verb)
                GLib.idle_add(status.set_text, result.get("message", "Service action completed."))
            threading.Thread(target=perform, name="dashboard-control", daemon=True).start()

        start_button = Gtk.Button(label="Start service")
        start_button.set_name("autozcoder-studio-start")
        start_button.connect("clicked", clicked, "start")
        start_button.set_no_show_all(True)  # Only the monitor's offline/stopped state reveals it.
        controls.pack_start(start_button, False, False, 0)
        buttons.append(start_button)

        service_menu = Gtk.MenuButton(label="Service")
        service_menu.set_tooltip_text("Service controls and login autostart")
        service_menu.set_no_show_all(not actions_available)
        service_popover = Gtk.Popover.new(service_menu)
        service_popover.set_name("autozcoder-studio-popover")
        service_menu.set_popover(service_popover)
        menu_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        menu_box.set_border_width(12)
        for title, verb in (("Stop service", "stop"), ("Restart service", "restart")):
            button = Gtk.Button(label=title)
            button.connect("clicked", clicked, verb)
            menu_box.pack_start(button, False, False, 0)
            buttons.append(button)
        menu_box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)
        autostart_caption = Gtk.Label(label="Login autostart")
        autostart_caption.set_name("autozcoder-studio-autostart-caption")
        autostart_caption.set_xalign(0)
        menu_box.pack_start(autostart_caption, False, False, 4)
        autostart = Gtk.Button(label="Enable login autostart")
        autostart._verb = "enable"
        autostart.connect("clicked", clicked)
        menu_box.pack_start(autostart, False, False, 0)
        buttons.append(autostart)
        service_popover.add(menu_box)
        menu_box.show_all()
        controls.pack_start(service_menu, False, False, 0)
        box.pack_start(controls, False, False, 0)
        box.pack_start(view, True, True, 0)
        window.add(box)

        def navigation(_view, decision, decision_type):
            if decision_type not in (WebKit2.PolicyDecisionType.NAVIGATION_ACTION,
                                     WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION):
                return False
            uri = decision.get_navigation_action().get_request().get_uri()
            if uri == "about:blank" and not monitor.attached:
                return False
            if monitor.origin and trusted_dashboard_url(uri, monitor.origin):
                if decision_type != WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION:
                    return False
            decision.ignore()
            # No native bridge exists. External HTTP(S) links leave the WebView.
            if urlsplit(uri).scheme in ("http", "https"):
                import webbrowser
                webbrowser.open(uri)
            return True

        view.connect("decide-policy", navigation)
        def load_failed(_view, _event, _uri, _error):
            monitor.load_failed()
            view.load_html((DASH_DIR / "offline.html").read_text(encoding="utf-8"), "about:blank")
            return True
        view.connect("load-failed", load_failed)
        def closed(*_):
            monitor.close()
            Gtk.main_quit()
        window.connect("destroy", closed)
        window.show_all()
        def poll():
            monitor.deliberate_start()
            retries = (1, 2, 4, 8, 15)
            retry_index = 0
            while not monitor.closed.is_set():
                try:
                    snapshot = monitor.snapshot()
                except Exception:
                    snapshot = {"state": "offline", "message": "User service unavailable. Run autozcoder serve explicitly."}
                GLib.idle_add(update, snapshot)
                if snapshot["state"] in ("healthy", "degraded"):
                    delay = 10
                    retry_index = 0
                else:
                    delay = retries[retry_index]
                    retry_index = min(retry_index + 1, len(retries) - 1)
                monitor.closed.wait(delay)
        threading.Thread(target=poll, name="dashboard-monitor", daemon=True).start()
        Gtk.main()
        monitor.close()
        return 0
    except Exception:
        log.error("AutoZcoder dashboard could not open. GTK and WebKit2GTK 4.1 are required.")
        return 1


def main(argv=None) -> int:
    from .linux_cli import main as dispatch
    return dispatch(argv)


if __name__ == "__main__":
    raise SystemExit(main())
