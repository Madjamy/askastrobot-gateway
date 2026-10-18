"""freeastrologyapi.com client: divisional charts, chart images and planet positions.

The n8n workflows keep providing the main kundali bundle; the gateway calls the
API directly for anything extra (topic vargas, any D-chart on request, transit
snapshots). Responses are parsed tolerantly (`output` may be a dict, a list or
a JSON string). Concurrency is limited so a long transit period can't flood
the API plan.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

import httpx

from app.logging_setup import get_logger
from app.mcp.payload import GRAHAS, sign_name
from app.settings import get_settings

log = get_logger(__name__)

BASE_URL = "https://json.freeastrologyapi.com"
VARGAS = ("D2", "D3", "D4", "D5", "D6", "D7", "D8", "D9", "D10", "D11", "D12",
          "D16", "D20", "D24", "D27", "D30", "D40", "D45", "D60")


def _info_endpoint(chart: str) -> str:
    return "navamsa-chart-info" if chart == "D9" else f"{chart.lower()}-chart-info"


def _image_endpoint(chart: str) -> str:
    return {"D1": "horoscope-chart-url", "D9": "navamsa-chart-url"}.get(chart, f"{chart.lower()}-chart-url")


class AstroApiError(Exception):
    pass


_semaphores: dict[int, asyncio.Semaphore] = {}


def _semaphore() -> asyncio.Semaphore:
    """One limiter per event loop (asyncio primitives must not cross loops)."""
    key = id(asyncio.get_running_loop())
    if key not in _semaphores:
        _semaphores[key] = asyncio.Semaphore(max(1, get_settings().astro_api_concurrency))
    return _semaphores[key]


def birth_body(
    local: datetime, latitude: float, longitude: float, utc_offset: float, *, observation_point: str | None = None,
) -> dict[str, Any]:
    return {
        "year": local.year, "month": local.month, "date": local.day,
        "hours": local.hour, "minutes": local.minute, "seconds": 0,
        "latitude": round(latitude, 4), "longitude": round(longitude, 4), "timezone": utc_offset,
        "config": {"observation_point": observation_point or get_settings().astro_api_observation_point,
                   "ayanamsha": "lahiri"},
    }


async def _post(endpoint: str, body: dict[str, Any]) -> Any:
    key = get_settings().freeastrology_api_key
    if not key:
        raise AstroApiError("API key not configured")
    async with _semaphore():
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(f"{BASE_URL}/{endpoint}", json=body,
                                         headers={"x-api-key": key, "Content-Type": "application/json"})
        except httpx.HTTPError as exc:
            log.error("astro_api.network", endpoint=endpoint, error=type(exc).__name__)
            raise AstroApiError(endpoint) from exc
    if resp.status_code != 200:
        log.error("astro_api.status", endpoint=endpoint, status=resp.status_code)
        raise AstroApiError(f"{endpoint}: {resp.status_code}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise AstroApiError(f"{endpoint}: not JSON") from exc
    out = data.get("output", data) if isinstance(data, dict) else data
    if isinstance(out, str):
        try:
            out = json.loads(out)
        except ValueError:
            pass  # e.g. an image URL
    return out


def _rows(obj: Any) -> list[dict[str, Any]]:
    """Collect planet rows from any nesting: dicts carrying `current_sign`."""
    found: list[dict[str, Any]] = []

    def walk(node: Any, key: str | None) -> None:
        if isinstance(node, dict):
            if "current_sign" in node:
                row = dict(node)
                row.setdefault("name", key)
                found.append(row)
                return
            for k, v in node.items():
                walk(v, k)
        elif isinstance(node, list):
            for v in node:
                walk(v, None)

    walk(obj, None)
    return found


async def varga(chart: str, body: dict[str, Any]) -> list[dict[str, Any]]:
    if chart not in VARGAS:
        raise AstroApiError(f"unsupported chart {chart}")
    rows = _rows(await _post(_info_endpoint(chart), body))
    if not rows:
        raise AstroApiError(f"{chart}: no rows")
    return rows


async def chart_image_url(chart: str, body: dict[str, Any]) -> str | None:
    out = await _post(_image_endpoint(chart), body)
    if isinstance(out, str) and out.startswith("https://"):
        return out
    if isinstance(out, dict):
        for v in out.values():
            if isinstance(v, str) and v.startswith("https://"):
                return v
    return None


async def planet_signs(body: dict[str, Any]) -> dict[str, tuple[str, bool]]:
    """{planet: (sign, retrograde)} for the 9 grahas at the given moment."""
    result: dict[str, tuple[str, bool]] = {}
    for row in _rows(await _post("planets", body)):
        name, sign = row.get("name"), sign_name(row.get("current_sign"))
        if name in GRAHAS and sign:
            retro = str(row.get("isRetro", "")).lower() == "true" and name not in ("Rahu", "Ketu")
            result[name] = (sign, retro)
    missing = [g for g in GRAHAS if g not in result]
    if missing:
        raise AstroApiError(f"planets: missing {missing}")
    return result
