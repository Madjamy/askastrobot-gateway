"""Prashna and birth charts built directly from the API (replaces the n8n webhooks).

The API calls and rules are those of the live n8n workflows; the blocks are
assembled in n8n's exact shape so `compact_chart` (tested against real n8n
output) formats them unchanged. Today's gochar and Shani status come from the
shared transit table (no API call).

API calls: birth chart 5 (kundali, D9, D10, dasha table, current dasha);
Prashna 2 (kundali, dasha at the moment). Calls run in parallel through the
client's rate limiter and retry on time-outs / blank data.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from app.logging_setup import get_logger
from app.mcp import astro_api, rules, shani, transit_table
from app.mcp.payload import GRAHAS, compact_chart, sign_name

log = get_logger(__name__)


def _now_at(offset: float) -> datetime:
    """Current wall-clock time at a place with this UTC offset (n8n 'current_transit')."""
    return (datetime.now(timezone.utc) + timedelta(hours=offset)).replace(tzinfo=None, second=0, microsecond=0)


async def _gather_all(*coros):
    """Like asyncio.gather, but if one fails the others are cancelled (no stray API calls)."""
    tasks = [asyncio.ensure_future(c) for c in coros]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def _saturn_sign_now(deadline: float | None = None) -> str | None:
    try:
        table = await transit_table.load()
        return transit_table.sign_of(table.position("Saturn", transit_table.now_utc())[0])
    except Exception as exc:   # table empty / DB issue: one live call instead
        log.warning("natal.transit_table_unavailable", error=type(exc).__name__)
        try:
            body = astro_api.birth_body(transit_table.now_utc(), 0.0, 0.0, 0.0, observation_point="geocentric")
            longs = await astro_api.planet_longitudes(body, deadline)
            return transit_table.sign_of(longs["Saturn"][0])
        except Exception:
            return None


def _shani_block(moon_sign_no: int, saturn_sign: str | None) -> dict[str, Any] | None:
    moon = sign_name(moon_sign_no)
    if not moon or not saturn_sign:
        return None
    st = shani.status(moon, saturn_sign)
    return {"Identifier": "Shani Sade Sathi, Dhaiya, Kantak Shani",
            "output": {"analysis_date": transit_table.now_utc().date().isoformat(), **st}}


async def birth_chart_raw(local: datetime, lat: float, lon: float, offset: float,
                          deadline: float | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """n8n-shaped blocks for a natal chart + the raw kundali (for gochar personalisation)."""
    kb = astro_api.kundali_body(local, lat, lon, offset)
    cb = astro_api.birth_body(local, lat, lon, offset)
    kundali, d9, d10, table, now_dasha, saturn = await _gather_all(
        astro_api.kundali(kb, deadline),
        astro_api.varga_raw("D9", cb, deadline),
        astro_api.varga_raw("D10", cb, deadline),
        astro_api.dasha_table(cb, deadline),
        astro_api.dasha_now(cb, _now_at(offset), lat, lon, offset, deadline),
        _saturn_sign_now(deadline),
    )
    blocks: list[dict[str, Any]] = [
        {"Identifier": "Janam Kundali", "output": kundali},
        rules.dignity_block(kundali),
        rules.aspects_block(kundali),
        rules.yogas_block(kundali),
    ]
    sb = _shani_block(kundali["Moon"]["current_sign"], saturn)
    if sb:
        blocks.append(sb)
    blocks += [
        rules.dasha_table_block(table),
        rules.current_dasha_block(now_dasha),
        {"Identifier": "Navamsa", "output": d9},
        {"Identifier": "D10", "output": d10},
    ]
    return blocks, kundali


async def prashna_raw(moment: datetime, lat: float, lon: float, offset: float,
                      deadline: float | None = None) -> list[dict[str, Any]]:
    kb = astro_api.kundali_body(moment, lat, lon, offset)
    cb = astro_api.birth_body(moment, lat, lon, offset)
    kundali, dasha = await _gather_all(
        astro_api.kundali(kb, deadline),
        astro_api.dasha_now(cb, moment, lat, lon, offset, deadline),
    )
    return [
        {"Kundali": "Kundali", "output": kundali},
        rules.dignity_block(kundali),
        rules.aspects_block(kundali),
        rules.current_dasha_block(dasha, label="Dasha"),
    ]


async def gochar_for(kundali: dict[str, Any]) -> dict[str, Any] | None:
    """Personalised current transits from the shared table; None if the table isn't available."""
    try:
        table = await transit_table.load()
        natal = {p: sign_name(kundali[p]["current_sign"]) for p in GRAHAS if isinstance(kundali.get(p), dict)}
        return transit_table.personal_gochar(
            table, lagna=sign_name(kundali["Ascendant"]["current_sign"]),
            moon=sign_name(kundali["Moon"]["current_sign"]), natal_planets=natal)
    except Exception as exc:
        log.warning("natal.gochar_unavailable", error=type(exc).__name__)
        return None


async def birth_chart(local: datetime, lat: float, lon: float, offset: float,
                      deadline: float | None = None) -> dict[str, Any]:
    blocks, kundali = await birth_chart_raw(local, lat, lon, offset, deadline)
    chart = compact_chart([{"status": "active", "data": blocks}], chart_type="natal")
    gochar = await gochar_for(kundali)
    if gochar:
        chart["Current Transit / Gochar"] = gochar
    else:
        chart.setdefault("unavailable_charts", []).append("Current Transit / Gochar")
    if "Shani Sade Sathi, Dhaiya, Kantak Shani" not in chart:
        chart.setdefault("unavailable_charts", []).append("Shani Sade Sathi, Dhaiya, Kantak Shani")
    return chart


async def prashna_chart(moment: datetime, lat: float, lon: float, offset: float,
                        deadline: float | None = None) -> dict[str, Any]:
    blocks = await prashna_raw(moment, lat, lon, offset, deadline)
    return compact_chart([{"status": "active", "data": blocks}], chart_type="prashna")


async def natal_reference(local: datetime, lat: float, lon: float, offset: float,
                          deadline: float | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """For get_transits: kundali + dasha table only (2 calls)."""
    kb = astro_api.kundali_body(local, lat, lon, offset)
    cb = astro_api.birth_body(local, lat, lon, offset)
    kundali, table = await _gather_all(astro_api.kundali(kb, deadline), astro_api.dasha_table(cb, deadline))
    return kundali, rules.dasha_table_block(table)["output"]
