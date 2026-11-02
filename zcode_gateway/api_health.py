"""Health, readiness and metrics."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from .peer import peer_is_local

router = APIRouter(tags=["ops"])


def _ops_exposed(request: Request) -> bool:
    """Ops endpoints leak pool/usage state; they answer only to local peers
    (see peer.py: loopback connections AND loopback-bound same-box proxies
    with a local visitor pass; an internet visitor behind Caddy does not)
    unless the operator explicitly opts in (server.expose_ops)."""
    state = request.app.state.app_state
    cfg = state.cfg.server
    if cfg.expose_ops:
        return True
    return peer_is_local(request)


@router.get("/health")
async def health(request: Request) -> dict:
    if not _ops_exposed(request):
        return JSONResponse(status_code=404, content={"detail": "Not Found"})
    state = request.app.state.app_state
    out = {"status": "ok"}
    try:
        # surface the IP-wide edge-block breaker so clients/ops can see WHY
        # everything is 503-ing instead of guessing account problems
        out["edge_block"] = state.service.breaker.status()
        if out["edge_block"]["blocked"]:
            out["status"] = "degraded"
    except AttributeError:
        pass
    return out


def instance_payload(request: Request) -> dict:
    """Explicit allowlist; instance identity is context, never authentication."""
    identity = getattr(request.app.state, "linux_identity", None)
    if not isinstance(identity, dict):
        raise HTTPException(status_code=404, detail="Linux instance identity unavailable")
    state = getattr(request.app.state, "app_state", None)
    return {"schema_version": 1, "product": "autozcoder-linux",
            "version": identity["version"], "instance_id": identity["instance_id"],
            "config_id": identity["config_id"], "mode": identity["mode"],
            "stopping": bool(getattr(request.app.state, "stopping", False)
                             or getattr(state, "stopping", False))}


@router.get("/health/instance")
async def health_instance(request: Request):
    # A reverse proxy is not a local TCP peer. Uvicorn Linux mode disables
    # proxy-header interpretation; retain all three key-injection checks here.
    from .api_dash import _LOOPBACK_HOSTS, _request_is_loopback, _peer_is_loopback
    cfg = request.app.state.cfg
    if not ((cfg.server.host or "").strip() in _LOOPBACK_HOSTS
            and _request_is_loopback(request) and _peer_is_loopback(request)):
        raise HTTPException(status_code=403, detail="local instance information only")
    return JSONResponse(instance_payload(request), headers={"Cache-Control": "no-store"})


@router.get("/ready")
async def ready(request: Request):
    if not _ops_exposed(request):
        return JSONResponse(status_code=404, content={"detail": "Not Found"})
    state = request.app.state.app_state
    ready_count = await state.pool.ready_count()
    if ready_count == 0:
        # orchestrators decide readiness from the status code; a 200 with
        # ready=false kept them routing to a dead gateway
        return JSONResponse(
            status_code=503,
            content={"ready": False, "accounts_ready": 0})
    return {"ready": True, "accounts_ready": ready_count}


@router.get("/metrics")
async def metrics(request: Request, since: int | None = None) -> dict:
    """JSON metrics (Prometheus text would need a client library).

    `since` bounds the usage window in seconds; default is the last 24h so a
    long-lived DB does not turn every scrape into a full-table scan. Pass
    since=0 for all-time.
    """
    if not _ops_exposed(request):
        return JSONResponse(status_code=404, content={"detail": "Not Found"})
    state = request.app.state.app_state
    snapshot = await state.pool.snapshot()
    window = 86_400 if since is None else (since or None)
    summary = await asyncio.to_thread(state.store.summary, since_seconds=window)
    return {
        "pool": {
            "accounts": snapshot["accounts"],
            "ready": snapshot["ready"],
            "by_state": snapshot["by_state"],
        },
        "usage": summary,
        "usage_window_s": window,
    }
