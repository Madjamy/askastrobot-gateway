"""freeastrologyapi.com client: the only chart data source (n8n is no longer used).

Request settings mirror the live n8n workflows exactly (docs/n8n-logic-extract.txt):
  planets/extended   "settings": topocentric, lahiri, en      (D1 / Prashna kundali)
  *-chart-info       "config":   geocentric, lahiri           (vargas)
  vimsottari/*       "config":   geocentric, lahiri           (dashas)
  planets            "config":   geocentric, lahiri           (transit table, UTC)

Reliability (Owner: retry on time-out or blank chart data):
- up to `retries` extra attempts after ~1 s and ~3 s, only while the caller's
  deadline allows, on: network error/time-out, HTTP 429/5xx, the API's
  "errorType" (e.g. Sandbox.Timedout) bodies, or output that fails validation
  (blank / incomplete);
- a process-wide limiter: at most `astro_api_concurrency` calls in flight and at
  least `astro_api_min_interval` seconds between call starts (plan rate limit);
- every call is logged (`astro_api.call`) for quota monitoring.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from typing import Any, Callable

import httpx

from app.logging_setup import get_logger
from app.mcp.payload import GRAHAS, SIGNS, sign_name
from app.settings import get_settings

log = get_logger(__name__)

BASE_URL = "https://json.freeastrologyapi.com"
VARGAS = ("D2", "D3", "D4", "D5", "D6", "D7", "D8", "D9", "D10", "D11", "D12",
          "D16", "D20", "D24", "D27", "D30", "D40", "D45", "D60")
_BACKOFF = (1.0, 3.0)


class AstroApiError(Exception):
    pass


def _info_endpoint(chart: str) -> str:
    return "navamsa-chart-info" if chart == "D9" else f"{chart.lower()}-chart-info"


def _image_endpoint(chart: str) -> str:
    return {"D1": "horoscope-chart-url", "D9": "navamsa-chart-url"}.get(chart, f"{chart.lower()}-chart-url")


# ------------------------------------------------------------------ bodies ---

def _moment(local: datetime, latitude: float, longitude: float, utc_offset: float) -> dict[str, Any]:
    return {"year": local.year, "month": local.month, "date": local.day,
            "hours": local.hour, "minutes": local.minute, "seconds": 0,
            "latitude": round(latitude, 4), "longitude": round(longitude, 4), "timezone": utc_offset}


def kundali_body(local: datetime, latitude: float, longitude: float, utc_offset: float) -> dict[str, Any]:
    """planets/extended, as n8n sends it (topocentric, 'settings' key)."""
    return {**_moment(local, latitude, longitude, utc_offset),
            "settings": {"observation_point": "topocentric", "ayanamsha": "lahiri", "language": "en"}}


def birth_body(
    local: datetime, latitude: float, longitude: float, utc_offset: float, *, observation_point: str | None = None,
) -> dict[str, Any]:
    """Vargas, dashas, shadbala, planets: 'config' key, geocentric unless overridden (as n8n)."""
    return {**_moment(local, latitude, longitude, utc_offset),
            "config": {"observation_point": observation_point or get_settings().astro_api_observation_point,
                       "ayanamsha": "lahiri"}}


# --------------------------------------------------------------- transport ---

_state: dict[int, tuple[asyncio.Semaphore, asyncio.Lock, list[float]]] = {}


def _limiter() -> tuple[asyncio.Semaphore, asyncio.Lock, list[float]]:
    """Per event loop: (in-flight semaphore, start-spacing lock, [last start time])."""
    key = id(asyncio.get_running_loop())
    if key not in _state:
        _state[key] = (asyncio.Semaphore(max(1, get_settings().astro_api_concurrency)), asyncio.Lock(), [0.0])
    return _state[key]


async def _spaced_start() -> None:
    _, lock, last = _limiter()
    gap = get_settings().astro_api_min_interval
    async with lock:
        wait = last[0] + gap - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        last[0] = time.monotonic()


def _unwrap(data: Any) -> Any:
    out = data.get("output", data) if isinstance(data, dict) else data
    if isinstance(out, str):
        try:
            out = json.loads(out)
        except ValueError:
            pass  # e.g. an image URL
    return out


async def _post(
    endpoint: str, body: dict[str, Any], *,
    validate: Callable[[Any], bool] | None = None, deadline: float | None = None, retries: int = 2,
) -> Any:
    key = get_settings().freeastrology_api_key
    if not key:
        raise AstroApiError("API key not configured")
    sem, _, _ = _limiter()
    last_error = "unknown"
    for attempt in range(retries + 1):
        status: int | None = None
        text, retry_after = "", 0.0
        per_try = 15.0
        if deadline is not None:
            per_try = min(per_try, deadline - time.monotonic() - 0.5)
            if per_try < 1.0:
                last_error = last_error if attempt else "no time left"
                break
        async with sem:
            await _spaced_start()
            t0 = time.perf_counter()
            try:
                async with httpx.AsyncClient(timeout=per_try) as client:
                    resp = await client.post(f"{BASE_URL}/{endpoint}", json=body,
                                             headers={"x-api-key": key, "Content-Type": "application/json"})
                status, text = resp.status_code, resp.text
                if status == 429:
                    try:
                        retry_after = min(float(resp.headers.get("retry-after", 0)), 10.0)
                    except ValueError:
                        retry_after = 0.0
            except httpx.HTTPError as exc:
                last_error = type(exc).__name__
        log.info("astro_api.call", endpoint=endpoint, status=status, attempt=attempt,
                 ms=int((time.perf_counter() - t0) * 1000))

        if status == 200 and '"errorType"' not in text:
            try:
                out = _unwrap(json.loads(text))
            except ValueError:
                last_error = "not JSON"
            else:
                if validate is None or validate(out):
                    return out
                last_error = "blank or incomplete data"
        elif status is not None:
            last_error = "API error body (e.g. Sandbox.Timedout)" if status == 200 else f"HTTP {status}"
            if status != 200 and status != 429 and status < 500:
                break                                   # other 4xx: retrying won't help

        if attempt < retries:
            pause = max(_BACKOFF[min(attempt, len(_BACKOFF) - 1)], retry_after)
            if deadline is not None and time.monotonic() + pause + 2 > deadline:
                break                                   # not enough time left for another try
            log.warning("astro_api.retry", endpoint=endpoint, reason=last_error, pause=pause)
            await asyncio.sleep(pause)
    raise AstroApiError(f"{endpoint}: {last_error}")


# ------------------------------------------------------------- validators ---

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


def _complete_rows(obj: Any) -> bool:
    names = {r.get("name") for r in _rows(obj) if sign_name(r.get("current_sign"))}
    return "Ascendant" in names and all(g in names for g in GRAHAS)


def _complete_kundali(obj: Any) -> bool:
    if not isinstance(obj, dict):
        return False
    needed = ("Ascendant",) + tuple(GRAHAS)
    return all(isinstance(obj.get(p), dict) and sign_name(obj[p].get("current_sign"))
               and isinstance(obj[p].get("house_number"), int) and 1 <= obj[p]["house_number"] <= 12
               and isinstance(obj[p].get("fullDegree"), (int, float))
               and isinstance(obj[p].get("normDegree"), (int, float))
               for p in needed)


def _is_ts(v: Any) -> bool:
    try:
        datetime.fromisoformat(str(v)[:19])
        return True
    except ValueError:
        return False


def _has_dasha(obj: Any) -> bool:
    return isinstance(obj, dict) and all(
        isinstance(obj.get(k), dict) and obj[k].get("Lord") in GRAHAS
        and _is_ts(obj[k].get("start_time")) and _is_ts(obj[k].get("end_time"))
        for k in ("maha_dasa", "antar_dasa", "pratyantar_dasa"))


def _has_dasha_table(obj: Any) -> bool:
    if not isinstance(obj, dict) or set(obj) != set(GRAHAS):
        return False
    return all(isinstance(ads, dict) and set(ads) == set(GRAHAS) and all(
        isinstance(p, dict) and _is_ts(p.get("start_time")) and _is_ts(p.get("end_time")) for p in ads.values())
        for ads in obj.values())


def _flat_rows(obj: Any) -> dict[str, dict[str, Any]]:
    """Any validated varga shape -> n8n's flat indexed dict {"0": {...}, "1": {...}}."""
    return {str(i): r for i, r in enumerate(_rows(obj))}


# -------------------------------------------------------------- endpoints ---

async def kundali(body: dict[str, Any], deadline: float | None = None) -> dict[str, Any]:
    return await _post("planets/extended", body, validate=_complete_kundali, deadline=deadline)


async def varga_raw(chart: str, body: dict[str, Any], deadline: float | None = None) -> dict[str, Any]:
    """Varga output normalised to n8n's flat indexed shape (as its Navamsa / D10 blocks)."""
    if chart not in VARGAS:
        raise AstroApiError(f"unsupported chart {chart}")
    return _flat_rows(await _post(_info_endpoint(chart), body, validate=_complete_rows, deadline=deadline))


async def varga(chart: str, body: dict[str, Any], deadline: float | None = None) -> list[dict[str, Any]]:
    return _rows(await varga_raw(chart, body, deadline))


async def dasha_now(body: dict[str, Any], event: datetime, latitude: float, longitude: float,
                    utc_offset: float, deadline: float | None = None) -> dict[str, Any]:
    """vimsottari/dasa-information for the dasha running at `event` (local time at the place)."""
    payload = {**body, "event_data": _moment(event, latitude, longitude, utc_offset)}
    return await _post("vimsottari/dasa-information", payload, validate=_has_dasha, deadline=deadline)


async def dasha_table(body: dict[str, Any], deadline: float | None = None) -> dict[str, Any]:
    return await _post("vimsottari/maha-dasas-and-antar-dasas", body, validate=_has_dasha_table, deadline=deadline)


async def chart_image_url(chart: str, body: dict[str, Any]) -> str | None:
    out = await _post(_image_endpoint(chart), body, retries=0)
    if isinstance(out, str) and out.startswith("https://"):
        return out
    if isinstance(out, dict):
        for v in out.values():
            if isinstance(v, str) and v.startswith("https://"):
                return v
    return None


async def shadbala(body: dict[str, Any], deadline: float | None = None) -> dict[str, dict[str, float]]:
    """Shadbala summary for the 7 planets: rupas, strength % of the classical minimum, Ishta/Kashta phala."""
    def ok(out: Any) -> bool:
        return isinstance(out, dict) and sum(
            1 for p in GRAHAS if isinstance(out.get(p), dict) and isinstance(out[p].get("rupas"), (int, float))) >= 7

    out = await _post("shadbala/summary", body, validate=ok, deadline=deadline)
    result: dict[str, dict[str, float]] = {}
    for planet, v in out.items():
        if planet in GRAHAS and isinstance(v, dict) and isinstance(v.get("rupas"), (int, float)):
            result[planet] = {
                "rupas": round(float(v["rupas"]), 2),
                "strength_pct": round(float(v.get("percentage_strength", 0)), 1),
                "ishta": round(float(v.get("ishta_phala", 0)), 1),
                "kashta": round(float(v.get("kashta_phala", 0)), 1),
            }
    if len(result) < 7:
        raise AstroApiError("shadbala: incomplete")
    return result


async def planet_longitudes(body: dict[str, Any], deadline: float | None = None) -> dict[str, tuple[float, bool]]:
    """{planet: (sidereal longitude 0-360, retrograde)} for the 9 grahas (transit table)."""
    def ok(out: Any) -> bool:
        rows = {r.get("name"): r for r in _rows(out)}
        return all(g in rows and isinstance(rows[g].get("fullDegree"), (int, float)) for g in GRAHAS)

    rows = _rows(await _post("planets", body, validate=ok, deadline=deadline))
    result: dict[str, tuple[float, bool]] = {}
    for r in rows:
        name = r.get("name")
        if name in GRAHAS and r.get("fullDegree") is not None:
            retro = str(r.get("isRetro", "")).lower() == "true"
            result[name] = (float(r["fullDegree"]) % 360.0, retro)
    missing = [g for g in GRAHAS if g not in result]
    if missing:
        raise AstroApiError(f"planets: missing {missing}")
    return result


async def planet_signs(body: dict[str, Any]) -> dict[str, tuple[str, bool]]:
    """{planet: (sign, retrograde)} for the 9 grahas at the given moment."""
    longs = await planet_longitudes(body)
    return {p: (SIGNS[int(lon // 30) % 12], retro and p not in ("Rahu", "Ketu")) for p, (lon, retro) in longs.items()}
