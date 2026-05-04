"""Liveness + deep readiness probes."""
from __future__ import annotations

import os
import time

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.db import get_pool
from app.settings import get_settings

router = APIRouter()


@router.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({
        "status": "ok",
        "version": os.getenv("GATEWAY_BUILD_VERSION", "dev"),
    })


@router.get("/health/deep")
async def health_deep() -> JSONResponse:
    """Used by external uptime monitor. Checks DB + n8n reachability."""
    settings = get_settings()
    checks: dict[str, dict[str, object]] = {}
    overall_ok = True

    # DB check
    db_start = time.perf_counter()
    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        checks["db"] = {"ok": True, "ms": int((time.perf_counter() - db_start) * 1000)}
    except Exception as exc:  # noqa: BLE001
        overall_ok = False
        checks["db"] = {"ok": False, "error": str(exc)[:200]}

    # n8n reachability (one quick HEAD per bot)
    async with httpx.AsyncClient(timeout=3) as client:
        for slug, url in settings.n8n_url_for_bot.items():
            n_start = time.perf_counter()
            try:
                resp = await client.head(url)
                checks[f"n8n_{slug}"] = {
                    "ok": resp.status_code < 500,
                    "status": resp.status_code,
                    "ms": int((time.perf_counter() - n_start) * 1000),
                }
            except Exception as exc:  # noqa: BLE001
                overall_ok = False
                checks[f"n8n_{slug}"] = {"ok": False, "error": str(exc)[:200]}

    return JSONResponse(
        {"status": "ok" if overall_ok else "degraded", "checks": checks},
        status_code=200 if overall_ok else 503,
    )
