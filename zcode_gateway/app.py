"""FastAPI application factory and lifespan wiring."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .api_admin import router as admin_router
from .api_anthropic import router as anthropic_router
from .api_dash import router as dash_router
from .api_health import router as health_router
from .api_openai import router as openai_router
from .config import Config, load_config
from .logbuffer import LogBuffer
from .model_catalog import ModelCatalog
from .pool import PoolManager, load_accounts
from .ratelimit import RateLimiter
from .registry import Registry
from .service import ChatService
from .state import AppState
from .store import JsonlCapture, Store
from .tokens import TokenManager
from .upstream import UpstreamClient, anthropic_error, openai_error

log = logging.getLogger("zcode_gateway")


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg: Config = app.state.cfg
    # Rollback covers acquisition as well as yield: a failed constructor must
    # not strand SQLite, a relay thread or an already-open upstream client.
    resources = []
    tasks = []
    relay_manager = None
    pkg_log = logging.getLogger("zcode_gateway")
    log_buffer = None
    app.state.cleanup_failed = False
    try:
        cfg.ensure_dirs()

        store = Store(cfg.store.sqlite_path)
        resources.append(("store", store.close))
        capture = JsonlCapture(cfg.logging.capture_dir, enabled=cfg.logging.capture)
        registry = Registry(
            aliases=cfg.models.aliases,
            default=cfg.models.default,
            default_anthropic=cfg.models.default_anthropic,
            # probe_unknown intentionally not forwarded: probing unknown ids
            # against a plan-gated upstream burns quota for no benefit; the
            # registry no longer implements it and the config field is inert.
        )
        catalog = ModelCatalog(cfg.upstream.base_url, registry)
        resources.append(("catalog", catalog.aclose))

        try:
            accounts = load_accounts(cfg.accounts)
        except Exception as exc:
            log.warning("could not load accounts from %s: %s", cfg.accounts.source, exc)
            accounts = []

        from .relay_manager import RelayManager
        relays_data_dir = (Path(cfg.accounts.dir).resolve().parent / "generator-data") if cfg.accounts.dir else Path("./generator-data")
        relay_manager = RelayManager(data_dir=relays_data_dir)
        # relay health loop: background ping every 5 minutes; failures feed the
        # rotation cooldown and the dashboard health column
        try:
            relay_manager.start_health_loop(300)
        except Exception:
            log.warning("relay health loop failed to start", exc_info=True)

        pool = PoolManager(cfg.pool, accounts)
        # persist per-model parks (UNUSUAL_ACTIVITY / GEO_BLOCKED / caps) next to
        # the sqlite store so a restart doesn't forget and re-burn every account
        try:
            caps_file = Path(cfg.store.sqlite_path).resolve().parent / "model-caps.json"
            pool.set_caps_path(caps_file)
            pool.load_caps()
        except Exception:
            log.warning("model-cap persistence unavailable", exc_info=True)
        tokens = TokenManager(cfg, pool)
        resources.append(("tokens", tokens.aclose))
        client = UpstreamClient(cfg.upstream, relay_manager=relay_manager,
                                pacing=cfg.models.pacing)
        resources.append(("client", client.aclose))
        service = ChatService(cfg, pool, tokens, client, registry, store, capture)

        # feed the dashboard's log view: one ring-buffer handler on the package log
        log_buffer = LogBuffer(capacity=1000)
        pkg_log = logging.getLogger("zcode_gateway")
        pkg_log.addHandler(log_buffer)

        app.state.app_state = AppState(
            cfg=cfg, pool=pool, tokens=tokens, client=client,
            registry=registry, store=store, capture=capture, service=service,
            limiter=RateLimiter(),
            catalog=catalog,
            relay_manager=relay_manager,
        )
        app.state.app_state.log_buffer = log_buffer
        service._lifecycle_tasks = app.state.app_state.async_tasks
        # pool + relay events (drained, DEAD, expiring, cooled) feed the bell
        pool.attach_notifications(app.state.app_state.notifications)
        relay_manager._notify = app.state.app_state.notifications

        # release feed: one GitHub call on startup, then every interval; the
        # dashboard polls this cache, never GitHub directly
        from .updater import UpdateChecker
        update_repo = cfg.update.repo
        if update_repo in getattr(cfg.update, "legacy_repos", ()):
            # config written before the repo rename: follow the current feed so
            # existing installs receive updates without a manual config edit
            update_repo = "B3hnamR/AutoZcoder"
        updater = UpdateChecker(update_repo, __version__,
                                enabled=cfg.update.enabled,
                                interval_hours=cfg.update.interval_hours)
        app.state.app_state.updater = updater
        resources.append(("updater", updater.aclose))
        if getattr(app.state, "stopping", False):
            app.state.app_state.begin_stopping()

        async def update_loop() -> None:
            # first check is deferred: startup must never block on GitHub, and
            # short-lived apps (tests, CLI runs) should not phone home at all
            await asyncio.sleep(min(20.0, updater.interval_s))
            while True:
                try:
                    await updater.check()
                except Exception:
                    log.exception("update check iteration failed")
                await asyncio.sleep(updater.interval_s)

        if not app.state.app_state.stopping:
            tasks.append(asyncio.create_task(tokens.refresh_loop(), name="token-refresh"))
            if cfg.update.enabled:
                tasks.append(asyncio.create_task(update_loop(), name="update-check"))
            if cfg.pool.balance_poll_seconds > 0:
                tasks.append(asyncio.create_task(tokens.balance_loop(), name="balance-poll"))
            for task in tasks:
                app.state.app_state.async_tasks.add(task)
                task.add_done_callback(app.state.app_state.async_tasks.discard)

        log.info("zcode gateway %s up — %d account(s), strategy=%s — Coded by @B3hnamR",
                 __version__, len(accounts), cfg.pool.strategy)

        yield
    finally:
        from .linux_service import CLEANUP_SECONDS
        deadline = time.monotonic() + CLEANUP_SECONDS
        app.state.stopping = True
        state = getattr(app.state, "app_state", None)
        if state is not None:
            try:
                state.begin_stopping()
            except Exception:
                app.state.cleanup_failed = True
                log.warning("producer fence failed")
            tasks.extend(state.async_tasks)
            login = getattr(state, "device_login", None)
            if login is not None and getattr(login, "_task", None) is not None:
                try:
                    login.cancel()
                except Exception:
                    app.state.cleanup_failed = True
                tasks.append(login._task)
        if log_buffer is not None:
            pkg_log.removeHandler(log_buffer)
        tasks = list(set(tasks))
        for task in tasks:
            if task.get_name() in ('verified-upstream-close', 'verified-close-wait', 'verified-pool-release', 'verified-owned-cleanup'):
                continue
            if not task.done() and not task.cancelling():
                task.cancel()
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=max(0, deadline - time.monotonic()))
            if pending:
                app.state.cleanup_failed = True
            for task in done:
                try:
                    task.result()
                except (asyncio.CancelledError, Exception):
                    pass
        # A dedicated daemon cleanup thread, not the loop's default executor:
        # cancelling to_thread cannot stop it and asyncio.run would wait for it.
        # The manager cooperates via request_shutdown and a shared deadline;
        # owned child groups are terminated even if another cleanup fails.
        generator = getattr(state, "generator", None) if state is not None else None
        workers = []
        worker_errors = []
        def launch_cleanup(name, callback):
            def run():
                try:
                    callback(deadline=deadline)
                except Exception:
                    worker_errors.append(name)
            thread = threading.Thread(target=run, name=f"shutdown-{name}", daemon=True)
            workers.append(thread)
            thread.start()
        if generator is not None:
            shutdown = getattr(generator, "shutdown", None)
            if shutdown is not None:
                launch_cleanup("generator", shutdown)
            else:
                # Backward-compatible injected test/third-party managers.
                launch_cleanup("generator", lambda **kw: generator.stop_all())
        if relay_manager is not None:
            def close_relay(*, deadline):
                shutdown = getattr(relay_manager, 'shutdown', None)
                if shutdown is not None:
                    if shutdown(deadline=deadline) is False:
                        raise TimeoutError('relay workers are still stopping')
                else:
                    relay_manager.stop_health_loop(deadline=deadline)
            launch_cleanup("relay", close_relay)
        while any(thread.is_alive() for thread in workers) and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        if worker_errors or any(thread.is_alive() for thread in workers):
            app.state.cleanup_failed = True
            log.warning("background shutdown deadline exceeded; process containment remains required")
        # Each acquired client and store closes independently even after failure.
        # Await actual cancellable async I/O, not a pretend thread cancellation.
        for name, close in reversed(resources):
            try:
                if name == "store" and hasattr(store, "close_before"):
                    # Interrupt queries and bound its lock wait; never wait on the
                    # event loop for a worker thread that ignores cancellation.
                    result = store.close_before(deadline)
                else:
                    result = close()
                if hasattr(result, "__await__"):
                    # Allow every resource a cleanup attempt even near deadline.
                    task = asyncio.create_task(result)
                    done, pending = await asyncio.wait({task}, timeout=max(0.05, deadline - time.monotonic()))
                    if pending:
                        task.cancel()
                        app.state.cleanup_failed = True
                    else:
                        task.result()
            except (asyncio.CancelledError, Exception):
                app.state.cleanup_failed = True
                log.warning("resource cleanup failed: %s", name)


class StoppingGuardMiddleware:
    """Producer fence without buffering or proxying the ASGI send callable.

    BaseHTTPMiddleware puts a memory channel and an outer streaming response
    between the delivery owner and the socket. Passing send through is required
    for the verified owner's write deadline and admission lease to cover the
    actual downstream transport.
    """
    SAFE_PATHS = frozenset({"/", "/health", "/health/instance", "/ready", "/dash", "/dash/v2", "/dash/next",
                           "/dash/connection.js", "/dash/studio.css", "/dash/overview.js",
                           "/dash/preferences.js", "/dash/usability.js", "/admin/dash/lifecycle"})

    def __init__(self, app, state):
        self.app, self.state = app, state

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'http':
            state = getattr(self.state, 'app_state', None)
            stopping = self.state.stopping or getattr(state, 'stopping', False)
            path = scope.get('path', '')
            if stopping and path not in self.SAFE_PATHS and not path.startswith("/dash/assets/"):
                message = 'gateway is stopping'
                if path.startswith('/v1/messages'):
                    content = anthropic_error(503, 'overloaded_error', message)
                elif path.startswith('/v1/'):
                    content = openai_error(503, 'service_unavailable', message)
                else:
                    content = {'detail': message}
                await JSONResponse(content, status_code=503,
                                   headers={'Retry-After': '1'})(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _norm_netloc(netloc: str, scheme: str) -> str:
    """Lower-cased host[:port] with the scheme's default port dropped, so
    `https://h` and `h:443` compare equal."""
    netloc = (netloc or "").strip().lower()
    for sch, port in (("https", ":443"), ("http", ":80")):
        if (scheme or "").lower() == sch and netloc.endswith(port):
            return netloc[: -len(port)]
    return netloc


class BrowserSecurityGuardMiddleware:
    """Header-only guard; never wraps/buffers an owned streaming response."""

    def __init__(self, app, cfg):
        self.app, self.cfg = app, cfg

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope)
        path = request.url.path
        privileged = (path in ("/dash", "/dash/v2", "/dash/next", "/health", "/ready", "/metrics")
                      or path.startswith("/admin/"))
        response = None
        if privileged and (request.url.hostname or "").lower() not in self.cfg.server.trusted_hosts():
            response = JSONResponse(status_code=400, content={"detail": "untrusted Host header"})
        elif request.method in ("POST", "PUT", "PATCH", "DELETE") and not path.startswith("/v1/"):
            site = request.headers.get("sec-fetch-site", "").lower()
            origin = request.headers.get("origin", "")
            if site in ("cross-site", "same-site"):
                response = JSONResponse(status_code=403, content={"detail": "cross-site mutation blocked"})
            elif origin:
                parsed = urlparse(origin)
                origin_netloc = _norm_netloc(parsed.netloc, parsed.scheme)
                allowed = {
                    _norm_netloc(request.headers.get(h) or "",
                                 request.headers.get("x-forwarded-proto") or request.url.scheme)
                    for h in ("host", "x-forwarded-host")
                } - {""}
                if not origin_netloc or origin_netloc == "null" or origin_netloc not in allowed:
                    response = JSONResponse(status_code=403, content={"detail": "cross-origin mutation blocked"})
        if response is not None:
            return await response(scope, receive, send)
        await self.app(scope, receive, send)


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or load_config()
    setup_logging(cfg.logging.level)

    # --- bind security (VPS / remote deployments) --------------------------
    # The admin key is local root-equivalent: it can point the generator at
    # arbitrary code, swap the binary, and rewrite config. A non-loopback
    # bind is only allowed with real credentials in place.
    if not cfg.server.is_loopback_bind():
        if not cfg.server.require_client_key:
            raise RuntimeError(
                f"server.host={cfg.server.host} is a public bind but "
                "server.require_client_key is false — an unauthenticated "
                "proxy on a public interface is an open relay. Set "
                "require_client_key: true (and configure client_keys).")
        if not cfg.server.client_keys:
            raise RuntimeError(
                "server.host is a public bind but no client_keys are "
                "configured — nothing would authenticate. Configure "
                "client_keys.")
        if cfg.server.has_placeholder_client_key():
            raise RuntimeError(
                "server.host is a public bind but a client key is still the "
                "example placeholder from config.example.yaml — set real "
                "long random keys (see docs/guides/VPS-DEPLOY.md).")
        if cfg.server.admin_key_is_placeholder():
            raise RuntimeError(
                "server.host is a public bind but server.admin_key is still "
                "a template placeholder — set a long random admin key first.")
        if not cfg.server.tls_cert:
            log.warning("serving PLAIN HTTP on a public bind: set server.tls_cert / "
                        "tls_key (built-in HTTPS) or put a TLS proxy in front")
        log.warning(
            "NON-LOCAL BIND (%s): the admin key is equivalent to code "
            "execution on this machine — keep it secret, prefer a reverse "
            "proxy with TLS, and leave allow_remote_admin=false unless the "
            "admin API must be reachable from non-loopback peers",
            cfg.server.host)

    app = FastAPI(title="Zcode Gateway", version=__version__, lifespan=lifespan)
    app.state.cfg = cfg
    app.state.stopping = False
    # Factory/legacy launchers retain a dashboard connection contract without
    # claiming ownership by the managed Linux user service. Its runner replaces
    # this identity before startup with the stronger config/context identity.
    import hashlib
    context = f"{getattr(cfg, '_config_file', None)}|{cfg.server.host}|{cfg.server.port}"
    app.state.linux_identity = {
        "schema_version": 1, "product": "autozcoder-linux", "version": __version__,
        "instance_id": uuid4().hex, "config_id": hashlib.sha256(context.encode()).hexdigest(),
        "mode": "embedded",
    }

    app.add_middleware(StoppingGuardMiddleware, state=app.state)

    # No CORS middleware on purpose: the dashboard is served same-origin and
    # every API client is a non-browser tool. `allow_origins=["*"]` let any
    # visited website read /dash (which embeds the admin key on loopback) and
    # then drive the admin API — a remote origin controlling a local tool.
    # No BaseHTTPMiddleware: it would replace the verified owner's actual
    # socket send with an intermediate buffer and change cancellation timing.
    app.add_middleware(BrowserSecurityGuardMiddleware, cfg=cfg)

    # dual-dialect surfaces
    app.include_router(openai_router)        # /v1/chat/completions, /v1/completions, /v1/models
    app.include_router(anthropic_router)     # /v1/messages, /v1/messages/count_tokens
    app.include_router(admin_router)         # /admin/*
    app.include_router(dash_router)          # /dash + /admin/dash/*
    app.include_router(health_router)        # /health, /ready, /metrics

    # Dashboard CSS/JS modules. Static assets only — no secrets are served from
    # here (the admin key is injected into the HTML shell, not the modules), so
    # these stay unauthenticated like the shell itself.
    from .api_dash import DASH_DIR
    _assets = DASH_DIR / "assets"
    if _assets.is_dir():
        from fastapi.staticfiles import StaticFiles
        app.mount("/dash/assets", StaticFiles(directory=str(_assets)), name="dash-assets")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, exc):
        # Auth/rate-limit failures raised in deps.py must reach /v1 clients in
        # their own dialect, not FastAPI's {"detail": ...} shape. Admin and
        # dashboard routes keep the plain detail (the dashboard reads it).
        status = exc.status_code
        detail = str(exc.detail)
        headers = getattr(exc, "headers", None)
        if request.url.path.startswith("/v1/messages"):
            code = {401: "invalid_api_key", 403: "permission_denied",
                    429: "rate_limit_exceeded"}.get(status, "invalid_request_error")
            return JSONResponse(status_code=status, headers=headers,
                                content=anthropic_error(status, code, detail))
        if request.url.path.startswith("/v1/"):
            code = {401: "invalid_api_key", 403: "permission_denied",
                    429: "rate_limit_exceeded"}.get(status, "invalid_request_error")
            return JSONResponse(status_code=status, headers=headers,
                                content=openai_error(status, code, detail))
        return JSONResponse(status_code=status, headers=headers,
                            content={"detail": exc.detail})

    @app.exception_handler(Exception)
    async def unhandled(request, exc):  # pragma: no cover
        log.exception("unhandled error on %s", request.url.path)
        # generic message + correlation id server-side: str(exc) can leak
        # paths, SQL errors and upstream URLs; and Anthropic-dialect callers
        # must not receive an OpenAI-shaped error envelope
        rid = uuid4().hex[:12]
        message = f"internal error (request id {rid})"
        if request.url.path.startswith("/v1/messages"):
            return JSONResponse(
                status_code=500,
                content=anthropic_error(500, "api_error", message))
        return JSONResponse(status_code=500,
                            content=openai_error(500, "internal_error", message))

    @app.get("/")
    async def root() -> dict:
        return {
            "name": "zcode-gateway",
            "version": __version__,
            "dialects": {
                "openai": ["POST /v1/chat/completions", "POST /v1/completions",
                           "GET /v1/models"],
                "anthropic": ["POST /v1/messages",
                              "POST /v1/messages/count_tokens"],
            },
            "admin": ["/admin/pool/state", "/admin/accounts", "/admin/stats"],
            "ops": ["/health", "/ready", "/metrics"],
        }

    return app
