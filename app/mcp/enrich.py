"""Topic-based additions to the birth chart (Owner's mapping in chart_topics.json).

get_birth_chart returns the n8n bundle (D1, aspects, yogas, Shani status, dasha,
gochar, D9, D10). This adds the divisional charts for the question's topic from
the API and, for a general reading, Shadbala (API) and the Moon chart.
Ashtakavarga is available as an extra but not switched on (Owner, 18 Oct).
Anything the API can't supply is listed under `unavailable_charts` instead of
failing the call.
"""
from __future__ import annotations

import asyncio
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.logging_setup import get_logger
from app.mcp import ashtakavarga, astro_api
from app.mcp.payload import moon_chart, varga_compact

log = get_logger(__name__)

_TOPICS_FILE = Path(__file__).with_name("chart_topics.json")


@lru_cache(maxsize=1)
def topics() -> dict[str, Any]:
    data = json.loads(_TOPICS_FILE.read_text())
    for charts in data["vargas"].values():
        unknown = [c for c in charts if c not in astro_api.VARGAS]
        if unknown:
            raise ValueError(f"chart_topics.json: unsupported charts {unknown}")
    return data


def varga_label(chart: str) -> str:
    return "Navamsa (D9)" if chart == "D9" else chart


async def fetch_vargas(
    charts: list[str], body: dict[str, Any], timeout: float | None = None,
) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Fetch several vargas in parallel. Returns ({label: compact}, [unavailable charts])."""
    gathered = asyncio.gather(*(astro_api.varga(c, body) for c in charts), return_exceptions=True)
    try:
        results = await (asyncio.wait_for(gathered, timeout) if timeout else gathered)
    except asyncio.TimeoutError:
        log.warning("enrich.timeout", charts=charts)
        return {}, list(charts)
    got: dict[str, dict[str, str]] = {}
    missing: list[str] = []
    for chart, res in zip(charts, results):
        compact = varga_compact(res) if not isinstance(res, BaseException) else {}
        if compact.get("Lagna") and len(compact) >= 10:      # Lagna + 9 grahas
            got[varga_label(chart)] = compact
        else:
            if isinstance(res, BaseException):
                log.warning("enrich.varga_failed", chart=chart, error=type(res).__name__)
            missing.append(chart)
    return got, missing


async def add_topic_charts(
    chart: dict[str, Any], query_type: str, body: dict[str, Any], timeout: float | None = None,
) -> None:
    """Optional additions: never fails the main chart (bounded by `timeout`)."""
    cfg = topics()
    extras = cfg.get("extras", {}).get(query_type, [])
    wanted = [c for c in cfg["vargas"].get(query_type, []) if varga_label(c) not in chart]
    missing: list[str] = []

    async def get_shadbala() -> dict[str, Any] | None:
        try:
            call = astro_api.shadbala(body)
            return await (asyncio.wait_for(call, timeout) if timeout else call)
        except Exception as exc:   # includes TimeoutError: optional block, never fails the chart
            log.warning("enrich.shadbala_failed", error=type(exc).__name__)
            return None

    # Each job has its own timeout, so a slow one never discards the other's results.
    vargas_task = fetch_vargas(wanted, body, timeout) if wanted else None
    shadbala_task = get_shadbala() if "shadbala" in extras else None
    jobs = [j for j in (vargas_task, shadbala_task) if j is not None]
    if jobs:
        results = await asyncio.gather(*jobs)
        it = iter(results)
        if vargas_task:
            got, missing = next(it)
            chart.update(got)
        if shadbala_task:
            sb = next(it)
            if sb:
                chart["Shadbala"] = {"unit": "rupas; strength_pct = % of the classical minimum (100+ = adequate)",
                                     "planets": sb}
            else:
                missing.append("Shadbala")
    if missing:
        merged = chart.get("unavailable_charts", []) + [m for m in missing if m not in chart.get("unavailable_charts", [])]
        chart["unavailable_charts"] = merged

    kundali = chart.get("Janam Kundali") or {}
    planets = kundali.get("planets") or {}
    lagna = (kundali.get("lagna") or {}).get("sign")
    if "ashtakavarga" in extras and lagna and all(p in planets for p in ashtakavarga.PLANETS):
        chart["Ashtakavarga"] = ashtakavarga.compute(lagna, {p: planets[p]["sign"] for p in ashtakavarga.PLANETS})
    if "moon_chart" in extras and "Moon" in planets:
        chart["Moon Chart (Chandra Lagna)"] = moon_chart(planets["Moon"]["sign"], planets)
