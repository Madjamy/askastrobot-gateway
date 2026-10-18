"""Gochar (transits) for a date or a period, relative to a natal chart.

- One snapshot per month (at most 24), planets at 12:00 UTC, geocentric.
- Sign changes of the slow planets (Saturn, Jupiter, Rahu; Ketu mirrors Rahu)
  between consecutive samples (the snapshots plus the requested end date) are
  narrowed by bisection to within 15 minutes (bounded number of extra API calls).
  A retrograde back-and-forth inside one month can be missed.
- Sade Sati / Dhaiya / Kantak per snapshot (app.mcp.shani) and the dasha running
  at each snapshot, plus MD/AD changes inside the period.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from typing import Any, Awaitable, Callable

from app.mcp import shani
from app.mcp.payload import GRAHAS, house_from

SLOW = ("Saturn", "Jupiter", "Rahu")
MAX_MONTHS = 24
MAX_BISECTION_CALLS = 48
_RESOLUTION = timedelta(minutes=15)

PlanetFetcher = Callable[[datetime], Awaitable[dict[str, tuple[str, bool]]]]


def _noon(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 12, 0)


def month_dates(start: date, end: date | None) -> list[date]:
    if end is None or end <= start:
        return [start]
    out, d = [], start
    while d <= end and len(out) < MAX_MONTHS:
        out.append(d)
        month = d.month % 12 + 1
        year = d.year + (1 if d.month == 12 else 0)
        d = date(year, month, min(d.day, 28))
    return out


def dasha_at(table: dict[str, list[list[Any]]] | None, day: date) -> str | None:
    if not table:
        return None
    iso = day.isoformat()
    for md, antars in table.items():
        for ad, start, end in antars:
            if start and end and start <= iso < end:
                return f"{md} / {ad}"
    return None


def dasha_changes(table: dict[str, list[list[Any]]] | None, start: date, end: date) -> list[dict[str, str]]:
    if not table:
        return []
    out = []
    for md, antars in table.items():
        for ad, s, _e in antars:
            if s and start.isoformat() < s <= end.isoformat():
                out.append({"date": s, "to": f"{md} / {ad}"})
    return sorted(out, key=lambda x: x["date"])


def _fmt(sign: str, retro: bool, lagna: str, moon: str) -> str:
    return f"{sign} ({house_from(sign, lagna)}/{house_from(sign, moon)}){' R' if retro else ''}"


async def build(
    *, fetch: PlanetFetcher, lagna_sign: str, moon_sign: str,
    dasha_table: dict[str, list[list[Any]]] | None, start: date, end: date | None,
) -> dict[str, Any]:
    dates = month_dates(start, end)
    samples = [_noon(d) for d in dates]
    if end is not None and end > dates[-1]:
        samples.append(_noon(end))            # search the tail of the period too
    sampled = await asyncio.gather(*(fetch(t) for t in samples))
    positions = sampled[:len(dates)]

    snapshots = []
    for d, pos in zip(dates, positions):
        snap: dict[str, Any] = {
            "date": d.isoformat(),
            "planets": {g: _fmt(*pos[g], lagna_sign, moon_sign) for g in GRAHAS},
            **shani.status(moon_sign, pos["Saturn"][0]),
        }
        running = dasha_at(dasha_table, d)
        if running:
            snap["dasha"] = running
        snapshots.append(snap)

    ingress: list[dict[str, Any]] = []
    budget = [MAX_BISECTION_CALLS]

    approximate = [False]

    async def transition(planet: str, lo: datetime, hi: datetime, old: str) -> tuple[datetime, str]:
        """Narrow the sign change to <= 15 min: `lo` still in `old`, `hi` already changed.
        Returns the midpoint of the final window."""
        new_sign = None
        while hi - lo > _RESOLUTION:
            if budget[0] <= 0:
                approximate[0] = True
                break
            mid = (lo + (hi - lo) / 2).replace(second=0, microsecond=0)
            budget[0] -= 1
            sign = (await fetch(mid))[planet][0]
            if sign == old:
                lo = mid
            else:
                hi, new_sign = mid, sign
        return (lo + (hi - lo) / 2).replace(second=0, microsecond=0), new_sign or ""

    for i in range(1, len(samples)):
        before, after = sampled[i - 1], sampled[i]
        for planet in SLOW:
            if before[planet][0] != after[planet][0]:
                when, sign = await transition(planet, samples[i - 1], samples[i], before[planet][0])
                sign = sign or after[planet][0]
                entry = {"date": when.date().isoformat(), "time_utc": when.strftime("%H:%M"),
                         "planet": planet, "enters": sign,
                         "from_lagna": house_from(sign, lagna_sign), "from_moon": house_from(sign, moon_sign)}
                ingress.append(entry)
                if planet == "Rahu":   # Ketu is always opposite Rahu
                    ketu = after["Ketu"][0]
                    ingress.append({**entry, "planet": "Ketu", "enters": ketu,
                                    "from_lagna": house_from(ketu, lagna_sign), "from_moon": house_from(ketu, moon_sign)})

    result: dict[str, Any] = {
        "status": "active",
        "natal_ref": {"lagna": lagna_sign, "moon": moon_sign},
        "legend": "Sign (house from Lagna / house from Moon); R = retrograde",
        "snapshots": snapshots,
    }
    if end and end > start:
        result["period"] = f"{start.isoformat()} to {end.isoformat()}"
        result["ingress"] = sorted(ingress, key=lambda x: x["date"])
        result["dasha_changes"] = dasha_changes(dasha_table, start, end)
        if approximate[0]:
            result["note"] = "Some ingress dates are approximate (to within a few days)."
    return result


def transit_fetcher(latitude: float, longitude: float) -> PlanetFetcher:
    from app.mcp import astro_api

    async def fetch(when: datetime) -> dict[str, tuple[str, bool]]:
        # UTC instant, geocentric: transit signs must not depend on the birthplace.
        body = astro_api.birth_body(when, latitude, longitude, 0.0, observation_point="geocentric")
        return await astro_api.planet_signs(body)

    return fetch
