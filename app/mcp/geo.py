"""Place name -> latitude / longitude / UTC offset (historical, DST-aware).

The n8n workflows need numeric latitude, longitude and timezone offset. The
Custom GPTs guessed these themselves; the MCP server resolves them so that
historical offsets (e.g. India's 1942-45 war time, DST countries) are correct.

- Geocoding: OpenStreetMap Nominatim (no key; max 1 request/second, so calls are
  serialised and results cached in memory).
- Timezone: timezonefinder (offline polygon lookup) + zoneinfo (IANA history).
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

import httpx
from timezonefinder import TimezoneFinder

from app.logging_setup import get_logger
from app.settings import get_settings

log = get_logger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_MIN_INTERVAL_SECONDS = 1.1
_CACHE_MAX = 2048


class GeoError(Exception):
    """`kind` is 'not_found', 'ambiguous' or 'unavailable'."""

    def __init__(self, message: str, *, kind: str, candidates: list[str] | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.candidates = candidates or []


@dataclass(frozen=True)
class Place:
    name: str
    latitude: float
    longitude: float
    tz_name: str


_lock = asyncio.Lock()
_last_call = 0.0
_cache: dict[str, list[dict]] = {}


@lru_cache(maxsize=1)
def _tf() -> TimezoneFinder:
    return TimezoneFinder()


def tz_name_for(latitude: float, longitude: float) -> str:
    name = _tf().timezone_at(lat=latitude, lng=longitude)
    if not name:
        raise GeoError("no timezone for coordinates", kind="not_found")
    return name


def utc_offset_hours(tz_name: str, local: datetime) -> float:
    """UTC offset in hours for a naive local datetime in `tz_name` (historical rules)."""
    aware = local.replace(tzinfo=ZoneInfo(tz_name))
    offset = aware.utcoffset()
    assert offset is not None
    return round(offset.total_seconds() / 3600, 4)


class NonexistentLocalTime(ValueError):
    """The wall-clock time was skipped by a DST change in that zone."""


def checked_offset_hours(tz_name: str, local: datetime) -> tuple[float, bool]:
    """(offset, ambiguous) for a naive local time; raises NonexistentLocalTime for DST gaps.

    Gap: the time never happened, detected by a UTC round trip.
    Overlap: the time happened twice; the first occurrence (fold=0) is used.
    """
    zone = ZoneInfo(tz_name)
    first = local.replace(tzinfo=zone, fold=0)
    second = local.replace(tzinfo=zone, fold=1)
    round_trip = first.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
    if round_trip != local:
        raise NonexistentLocalTime(f"{local} does not exist in {tz_name}")
    offset = first.utcoffset()
    assert offset is not None
    return round(offset.total_seconds() / 3600, 4), first.utcoffset() != second.utcoffset()


def now_in(tz_name: str) -> datetime:
    """Current local wall-clock time in `tz_name`, returned naive."""
    return datetime.now(ZoneInfo(tz_name)).replace(tzinfo=None, microsecond=0)


async def _nominatim(query: str) -> list[dict]:
    key = query.strip().lower()
    if key in _cache:
        return _cache[key]
    global _last_call
    async with _lock:
        wait = _MIN_INTERVAL_SECONDS - (time.monotonic() - _last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.get(
                    NOMINATIM_URL,
                    params={"q": query, "format": "jsonv2", "limit": 5, "addressdetails": 1,
                            "accept-language": "en"},
                    headers={"User-Agent": get_settings().geocoder_user_agent},
                )
        except httpx.HTTPError as exc:
            log.error("geo.nominatim_error", error=str(exc))
            raise GeoError("geocoder unreachable", kind="unavailable") from exc
        finally:
            _last_call = time.monotonic()
    if resp.status_code != 200:
        log.error("geo.nominatim_status", status=resp.status_code)
        raise GeoError("geocoder error", kind="unavailable")
    results = [r for r in resp.json() if isinstance(r, dict)]
    if len(_cache) >= _CACHE_MAX:
        _cache.clear()
    _cache[key] = results
    return results


def _label(r: dict) -> str:
    addr = r.get("address") or {}
    city = addr.get("city") or addr.get("town") or addr.get("village") or addr.get("municipality") or r.get("name")
    parts = [p for p in (city, addr.get("state"), addr.get("country")) if p]
    return ", ".join(dict.fromkeys(parts)) or r.get("display_name", "")


def pick_place(query: str, results: list[dict]) -> Place:
    """Choose one result, or raise 'ambiguous' when the query has no country and
    the top results are in different countries."""
    usable = [r for r in results if r.get("lat") and r.get("lon")]
    if not usable:
        raise GeoError(f"place not found: {query}", kind="not_found")
    if "," not in query:
        countries = {(r.get("address") or {}).get("country_code") for r in usable[:3]}
        if len(countries) > 1:
            raise GeoError(
                f"'{query}' matches places in several countries",
                kind="ambiguous",
                candidates=list(dict.fromkeys(_label(r) for r in usable[:5])),
            )
    best = usable[0]
    lat, lon = float(best["lat"]), float(best["lon"])
    return Place(name=_label(best), latitude=lat, longitude=lon, tz_name=tz_name_for(lat, lon))


async def resolve_place(query: str) -> Place:
    return pick_place(query, await _nominatim(query))
