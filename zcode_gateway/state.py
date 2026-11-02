"""Shared application state container."""

from __future__ import annotations

import asyncio
import secrets
import threading
from dataclasses import dataclass, field
from typing import Any

from .config import Config
from .notify import NotificationCenter
from .pool import PoolManager
from .ratelimit import RateLimiter
from .registry import Registry
from .service import ChatService
from .store import JsonlCapture, Store
from .tokens import TokenManager
from .upstream import UpstreamClient


@dataclass
class AppState:
    cfg: Config
    pool: PoolManager
    tokens: TokenManager
    client: UpstreamClient
    registry: Registry
    store: Store
    capture: JsonlCapture
    service: ChatService
    limiter: RateLimiter
    updater: Any = None          # updater.UpdateChecker, wired in lifespan
    catalog: Any = None          # model_catalog.ModelCatalog, wired in lifespan
    generator: Any = None        # generator.GeneratorManager, lazily created
    relay_manager: Any = None    # relay_manager.RelayManager, wired in app setup
    # notification center: cross-thread event bus for the dashboard bell
    notifications: NotificationCenter = field(default_factory=NotificationCenter)
    # Per-run internal token for loopback service-to-service calls (generator
    # children -> gateway admin). Random on every process start, exposed to
    # local children via env; the static legacy value only works from loopback.
    internal_token: str = field(default_factory=lambda: f"internal-{secrets.token_hex(16)}")
    stopping: bool = False
    stop_event: threading.Event = field(default_factory=threading.Event)
    background_tasks: set = field(default_factory=set)
    async_tasks: set = field(default_factory=set)
    _background_lock: Any = field(default_factory=threading.RLock, repr=False)

    def begin_stopping(self) -> None:
        """Idempotent cross-thread producer fence; never create lazy managers."""
        self.stopping = True
        self.stop_event.set()
        self.service.begin_stopping()
        relay = self.relay_manager
        if relay is not None:
            request_stop = getattr(relay, "request_shutdown", None)
            if request_stop is not None:
                request_stop()
            else:
                event = getattr(relay, "_health_stop", None)
                if event is not None:
                    event.set()
        generator = self.generator
        if generator is not None:
            request_stop = getattr(generator, "request_shutdown", None)
            if request_stop is not None:
                request_stop()
        def cancel_once(task):
            if task.get_name() in ('verified-upstream-close', 'verified-close-wait', 'verified-pool-release', 'verified-owned-cleanup'):
                return  # Their bounded owners handle cleanup, not a second cancellation.
            if not task.done() and not task.cancelling():
                task.cancel()
        for task in tuple(self.async_tasks):
            task.get_loop().call_soon_threadsafe(cancel_once, task)

    def schedule_background(self, coroutine, loop):
        """Own thread-to-loop callbacks until completion, or reject on stop."""
        with self._background_lock:
            if self.stopping:
                coroutine.close()
                return None
            async def owned():
                if self.stopping:
                    coroutine.close()
                    return
                task = asyncio.current_task()
                self.async_tasks.add(task)
                try:
                    return await coroutine
                finally:
                    self.async_tasks.discard(task)
            future = asyncio.run_coroutine_threadsafe(owned(), loop)
            self.background_tasks.add(future)
        def completed(future):
            with self._background_lock:
                self.background_tasks.discard(future)
        future.add_done_callback(completed)
        return future
