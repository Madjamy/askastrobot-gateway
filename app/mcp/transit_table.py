"""Transit (gochar) table: one API call per day, shared by every user.

Storage: public.gw_transit_daily(day, planet, longitude, retro): the 9 grahas'
sidereal (Lahiri, geocentric) longitudes at 00:00 UTC each day, from about a
month back to 24 months ahead. A daily job adds missing days (~1 call/day).

Everything else is computed without API calls:
- position at any moment by interpolating between the two daily points
  (Moon error ~0.05 deg, i.e. sign/nakshatra change times within minutes);
- sign periods, nakshatra changes, retrograde periods;
- a personalised gochar: house from Lagna (H) and from Moon (M), natal planets
  the transit planet is over, houses/natal planets it aspects (n8n drishti rules),
  what comes next, Moon's sign and nakshatra, Shani status (n8n rule).
"""
from __future__ import annotations

import asyncio
import time
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.db import get_pool
from app.logging_setup import get_logger
from app.mcp import shani
from app.mcp.payload import GRAHAS, SIGNS, house_from
from app.mcp.rules import ASPECT_MAP

log = get_logger(__name__)

NAKSHATRAS = (
    "Ashwini", "Bharani", "Krittika", "Rohini", "Mrigashira", "Ardra", "Punarvasu", "Pushya", "Ashlesha",
    "Magha", "Purva Phalguni", "Uttara Phalguni", "Hasta", "Chitra", "Swati", "Vishakha", "Anuradha",
    "Jyeshtha", "Mula", "Purva Ashadha", "Uttara Ashadha", "Shravana", "Dhanishta", "Shatabhisha",
    "Purva Bhadrapada", "Uttara Bhadrapada", "Revati",
)
NAK_SPAN = 360.0 / 27
SLOW = ("Saturn", "Jupiter", "Rahu", "Ketu")
HORIZON_DAYS = {"Saturn": 730, "Jupiter": 730, "Rahu": 730, "Ketu": 730, "Mars": 180,
                "Sun": 92, "Mercury": 92, "Venus": 92}
MAX_NEXT = {"Saturn": 2, "Jupiter": 3, "Rahu": 2, "Ketu": 2, "Mars": 4, "Sun": 3, "Mercury": 4, "Venus": 3}
_CACHE_TTL = 3600


class TransitDataMissing(Exception):
    pass


def sign_of(lon: float) -> str:
    return SIGNS[int((lon % 360) // 30)]


def nakshatra_of(lon: float) -> str:
    lon %= 360
    idx = int(lon // NAK_SPAN)
    pada = int((lon - idx * NAK_SPAN) // (NAK_SPAN / 4)) + 1
    return f"{NAKSHATRAS[idx]}-{pada}"


def _delta(a: float, b: float) -> float:
    """Signed shortest move from a to b in degrees (-180, 180]."""
    d = (b - a) % 360
    return d - 360 if d > 180 else d


@dataclass
class Table:
    days: list[date]                                  # sorted
    rows: dict[date, dict[str, tuple[float, bool]]]   # day -> planet -> (lon, retro)

    # ------------------------------------------------------------ positions
    def _bracket(self, when: datetime) -> tuple[date, date, float]:
        d0 = when.date()
        d1 = d0 + timedelta(days=1)
        if d0 not in self.rows or d1 not in self.rows:
            raise TransitDataMissing(f"no transit data around {d0}")
        frac = (when - datetime(d0.year, d0.month, d0.day)).total_seconds() / 86400
        return d0, d1, frac

    def covers(self, start: datetime, end: datetime) -> bool:
        """True if every day from start to the day after end is present (no gaps)."""
        d, last = start.date(), end.date() + timedelta(days=1)
        while d <= last:
            if d not in self.rows:
                return False
            d += timedelta(days=1)
        return True

    def require(self, start: datetime, end: datetime) -> None:
        if not self.covers(start, end):
            first = self.days[0].isoformat() if self.days else "none"
            last = (self.days[-1] - timedelta(days=1)).isoformat() if self.days else "none"
            raise TransitDataMissing(f"transit data covers {first} to {last}")

    def position(self, planet: str, when: datetime) -> tuple[float, bool]:
        d0, d1, frac = self._bracket(when)
        lon0, retro0 = self.rows[d0][planet]
        lon1, _ = self.rows[d1][planet]
        return (lon0 + _delta(lon0, lon1) * frac) % 360, retro0

    # ------------------------------------------------------------ crossings
    def crossings(self, planet: str, start: datetime, end: datetime, span: float) -> list[tuple[datetime, int]]:
        """Times the planet crosses a multiple of `span` degrees, in order: [(time, new_index)].
        Handles several crossings in one day (Moon vs 13°20' nakshatras) and retrograde motion."""
        n = int(round(360 / span))
        out: list[tuple[datetime, int]] = []
        day = start.date()
        last = end.date()
        while day <= last:
            nxt = day + timedelta(days=1)
            if day not in self.rows or nxt not in self.rows:
                break
            lon0 = self.rows[day][planet][0]
            move = _delta(lon0, self.rows[nxt][planet][0])
            k0, k1 = int(lon0 // span), int((lon0 + move) // span)
            base = datetime(day.year, day.month, day.day)
            if move > 0:
                hits = [(j * span, j % n) for j in range(k0 + 1, k1 + 1)]
            elif move < 0:
                hits = [(j * span, (j - 1) % n) for j in range(k0, k1, -1)]
            else:
                hits = []
            for boundary, new_idx in hits:
                t = base + timedelta(days=(boundary - lon0) / move)
                if start <= t <= end:
                    out.append((t.replace(microsecond=0), new_idx))
            day = nxt
        return out

    def sign_changes(self, planet: str, start: datetime, end: datetime) -> list[tuple[datetime, str]]:
        return [(t, SIGNS[i % 12]) for t, i in self.crossings(planet, start, end, 30.0)]

    def nakshatra_changes(self, planet: str, start: datetime, end: datetime) -> list[tuple[datetime, str]]:
        return [(t, NAKSHATRAS[i % 27]) for t, i in self.crossings(planet, start, end, NAK_SPAN)]

    def last_sign_change(self, planet: str, before: datetime, lookback_days: int = 400) -> datetime | None:
        start = max(before - timedelta(days=lookback_days), datetime.combine(self.days[0], datetime.min.time()))
        changes = self.sign_changes(planet, start, before)
        return changes[-1][0] if changes else None

    def retro_periods(self, planet: str, start: date, end: date) -> list[tuple[str, str]]:
        """Retrograde runs overlapping [start, end] as ("YYYY-MM-DD" | "before …", "YYYY-MM-DD" | "after …").
        Scans the whole table so runs crossing the window keep their real station dates."""
        if planet in ("Rahu", "Ketu", "Sun", "Moon") or not self.days:
            return []
        runs, cur, prev = [], None, None
        for d in self.days:
            if cur is not None and prev is not None and (d - prev).days > 1:
                runs.append((cur, None))          # gap in the data: don't bridge it
                cur = None
            prev = d
            retro = self.rows[d][planet][1]
            if retro and cur is None:
                cur = d
            elif not retro and cur is not None:
                runs.append((cur, d))
                cur = None
        if cur is not None:
            runs.append((cur, None))
        out = []
        for a, b in runs:
            if (b is not None and b < start) or a > end:
                continue
            a_txt = f"before {a.isoformat()}" if a == self.days[0] else a.isoformat()
            b_txt = f"after {self.days[-1].isoformat()}" if b is None else b.isoformat()
            out.append((a_txt, b_txt))
        return out


# ------------------------------------------------------------------ loading --

_cache: dict[str, Any] = {"table": None, "at": 0.0}


async def load(force: bool = False) -> Table:
    if not force and _cache["table"] is not None and time.monotonic() - _cache["at"] < _CACHE_TTL:
        return _cache["table"]
    pool = get_pool()
    async with pool.acquire() as conn:
        records = await conn.fetch(
            "SELECT day, planet, longitude, retro FROM public.gw_transit_daily "
            "WHERE day >= CURRENT_DATE - 400 ORDER BY day")
    rows: dict[date, dict[str, tuple[float, bool]]] = {}
    for r in records:
        rows.setdefault(r["day"], {})[r["planet"]] = (float(r["longitude"]), bool(r["retro"]))
    rows = {d: v for d, v in rows.items() if all(g in v for g in GRAHAS)}
    table = Table(days=sorted(rows), rows=rows)
    _cache.update(table=table, at=time.monotonic())
    return table


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


# --------------------------------------------------------- personalisation --

def _fmt_date(t: datetime | date | None) -> str | None:
    if t is None:
        return None
    return (t.date() if isinstance(t, datetime) else t).isoformat()


def _natal_by_sign(natal_planets: dict[str, str]) -> dict[str, list[str]]:
    by: dict[str, list[str]] = {}
    for p, s in natal_planets.items():
        by.setdefault(s, []).append(p)
    return by


def _placement(planet: str, sign: str, lagna: str, moon: str, natal_by_sign: dict[str, list[str]]) -> dict[str, Any]:
    h = house_from(sign, lagna)
    aspects = []
    for off in ASPECT_MAP.get(planet, []):
        target = SIGNS[(SIGNS.index(sign) + off - 1) % 12]
        natal = natal_by_sign.get(target, [])
        aspects.append(f"H{house_from(target, lagna)} {target}" + (f" (natal {', '.join(natal)})" if natal else ""))
    return {
        "where": f"{sign}, H{h} / M{house_from(sign, moon)}",
        "over_natal": natal_by_sign.get(sign, []),
        "aspects": aspects,
    }


def personal_gochar(table: Table, *, lagna: str, moon: str, natal_planets: dict[str, str],
                    at: datetime | None = None) -> dict[str, Any]:
    """Current transits for one person + what comes next (no API calls)."""
    now = at or now_utc()
    table.require(now, now)
    by_sign = _natal_by_sign(natal_planets)
    out: dict[str, Any] = {
        "data_until": table.days[-1].isoformat() if table.days else None,
        "as_of_utc": now.strftime("%Y-%m-%d %H:%M"),
        "houses": f"H = house from Lagna ({lagna}), M = house from Moon ({moon})",
        "aspect_rules": "All planets aspect the 7th; Mars also 4th/8th; Jupiter and Rahu also 5th/9th; Saturn also 3rd/10th",
    }
    for planet in GRAHAS:
        lon, retro = table.position(planet, now)
        sign = sign_of(lon)
        entry = _placement(planet, sign, lagna, moon, by_sign)
        if retro and planet not in ("Rahu", "Ketu"):
            entry["where"] += ", retrograde"
        if planet == "Moon":
            entry["nakshatra"] = nakshatra_of(lon)
            soon = now + timedelta(seconds=1)       # strictly after now (#8)
            nxt_sign = table.sign_changes("Moon", soon, now + timedelta(days=3))
            nxt_nak = table.nakshatra_changes("Moon", soon, now + timedelta(days=2))
            if nxt_sign:
                entry["until"] = nxt_sign[0][0].strftime("%Y-%m-%d %H:%M UTC")
                entry["next"] = f"{nxt_sign[0][1]}, H{house_from(nxt_sign[0][1], lagna)} / M{house_from(nxt_sign[0][1], moon)}"
            if nxt_nak:
                entry["nakshatra_until"] = nxt_nak[0][0].strftime("%Y-%m-%d %H:%M UTC")
            out[planet] = entry
            continue
        entry["nakshatra"] = nakshatra_of(lon)
        since = table.last_sign_change(planet, now)
        if since:
            entry["since"] = _fmt_date(since)
        horizon = now + timedelta(days=HORIZON_DAYS[planet])
        changes = table.sign_changes(planet, now + timedelta(seconds=1),
                                     min(horizon, datetime.combine(table.days[-1], datetime.min.time())))
        if changes:
            entry["until"] = _fmt_date(changes[0][0])
            nxt = []
            for t, s in changes[:MAX_NEXT[planet]]:
                note = " (retrograde)" if table.retro_periods(planet, t.date(), t.date()) else ""
                nxt.append(f"{s}, H{house_from(s, lagna)} / M{house_from(s, moon)} from {_fmt_date(t)}{note}")
            entry["next"] = nxt
        retros = table.retro_periods(planet, now.date(), horizon.date())
        if retros:
            entry["retrograde_periods"] = [f"{a} to {b}" for a, b in retros[:3]]
        out[planet] = entry

    # Shani status now and when it changes (n8n rule, Moon-relative)
    sat_now = sign_of(table.position("Saturn", now)[0])
    status = {"now": shani.status(moon, sat_now)}
    changes = table.sign_changes("Saturn", now + timedelta(seconds=1),
                                 min(now + timedelta(days=730), datetime.combine(table.days[-1], datetime.min.time())))
    if changes:
        status["changes"] = [{"from": _fmt_date(t), **shani.status(moon, s)} for t, s in changes[:2]]
    out["Shani Sade Sathi, Dhaiya, Kantak Shani"] = status
    return out


def transits_window(table: Table, *, lagna: str, moon: str, natal_planets: dict[str, str],
                    start: date, end: date) -> dict[str, Any]:
    """Sign periods of each planet overlapping [start, end], personalised (no API calls)."""
    s_dt = datetime(start.year, start.month, start.day)
    e_dt = datetime(end.year, end.month, end.day, 23, 59)
    table.require(s_dt, e_dt)                  # never extend a sign beyond the data (#1)
    by_sign = _natal_by_sign(natal_planets)
    result: dict[str, Any] = {
        "status": "active",
        "period": f"{start.isoformat()} to {end.isoformat()}",
        "houses": f"H = house from Lagna ({lagna}), M = house from Moon ({moon})",
        "aspect_rules": "All planets aspect the 7th; Mars also 4th/8th; Jupiter and Rahu also 5th/9th; Saturn also 3rd/10th",
    }
    planets = list(GRAHAS) if (end - start).days <= 10 else [p for p in GRAHAS if p != "Moon"]
    for planet in planets:
        sign0 = sign_of(table.position(planet, s_dt)[0])
        changes = table.sign_changes(planet, s_dt, e_dt)
        bounds = [(s_dt, sign0)] + changes
        periods = []
        for i, (t, sign) in enumerate(bounds):
            t_end = bounds[i + 1][0] if i + 1 < len(bounds) else e_dt
            p = _placement(planet, sign, lagna, moon, by_sign)
            periods.append({"from": _fmt_date(t), "to": _fmt_date(t_end), **p})
        entry: dict[str, Any] = {"periods": periods}
        retros = table.retro_periods(planet, start, end)
        if retros:
            entry["retrograde_periods"] = [f"{a} to {b}" for a, b in retros]
        result[planet] = entry
    sat = [(s_dt, sign_of(table.position("Saturn", s_dt)[0]))] + table.sign_changes("Saturn", s_dt, e_dt)
    result["Shani Sade Sathi, Dhaiya, Kantak Shani"] = [{"from": _fmt_date(t), **shani.status(moon, s)} for t, s in sat]
    return result


# -------------------------------------------------------------- maintenance --

async def missing_days(start: date, end: date) -> list[date]:
    pool = get_pool()
    async with pool.acquire() as conn:
        have = await conn.fetch(
            "SELECT day FROM public.gw_transit_daily WHERE day BETWEEN $1 AND $2 "
            "GROUP BY day HAVING count(*) >= 9", start, end)
    present = {r["day"] for r in have}
    out, d = [], start
    while d <= end:
        if d not in present:
            out.append(d)
        d += timedelta(days=1)
    return out


async def store_day(day: date, positions: dict[str, tuple[float, bool]]) -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO public.gw_transit_daily (day, planet, longitude, retro)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (day, planet) DO UPDATE SET longitude = EXCLUDED.longitude, retro = EXCLUDED.retro
            """,
            [(day, p, lon, retro) for p, (lon, retro) in positions.items()],
        )
