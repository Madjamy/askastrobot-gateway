"""Historical timezone offsets and place disambiguation (no network)."""
from __future__ import annotations

from datetime import datetime

import pytest

from app.mcp.geo import GeoError, NonexistentLocalTime, checked_offset_hours, pick_place, tz_name_for, utc_offset_hours


def test_historical_offsets() -> None:
    assert utc_offset_hours("Asia/Kolkata", datetime(1982, 5, 21, 12, 38)) == 5.5
    assert utc_offset_hours("Asia/Kolkata", datetime(1943, 6, 1, 12, 0)) == 6.5      # war time
    assert utc_offset_hours("Australia/Melbourne", datetime(2026, 1, 15, 9, 0)) == 11  # DST
    assert utc_offset_hours("Australia/Melbourne", datetime(2026, 7, 15, 9, 0)) == 10
    assert utc_offset_hours("America/New_York", datetime(1990, 7, 4, 10, 0)) == -4


def test_tz_lookup() -> None:
    assert tz_name_for(28.61, 77.21) == "Asia/Kolkata"
    assert tz_name_for(-37.85, 144.70) == "Australia/Melbourne"


def _r(lat, lon, cc, city):
    return {"lat": str(lat), "lon": str(lon), "address": {"city": city, "country_code": cc, "country": cc.upper()}}


def test_ambiguous_without_country() -> None:
    results = [_r(43.65, -79.38, "ca", "London"), _r(51.5, -0.12, "gb", "London")]
    with pytest.raises(GeoError) as e:
        pick_place("London", results)
    assert e.value.kind == "ambiguous" and len(e.value.candidates) == 2


def test_country_given_picks_first() -> None:
    results = [_r(51.5, -0.12, "gb", "London"), _r(43.65, -79.38, "ca", "London")]
    p = pick_place("London, UK", results)
    assert p.tz_name == "Europe/London"


def test_not_found() -> None:
    with pytest.raises(GeoError) as e:
        pick_place("Nowhere", [])
    assert e.value.kind == "not_found"


def test_dst_gap_and_overlap() -> None:
    with pytest.raises(NonexistentLocalTime):
        checked_offset_hours("Australia/Melbourne", datetime(2026, 10, 4, 2, 30))
    offset, ambiguous = checked_offset_hours("Australia/Melbourne", datetime(2026, 4, 5, 2, 30))
    assert ambiguous is True and offset == 11            # first occurrence (daylight time)
    assert checked_offset_hours("Asia/Kolkata", datetime(1982, 5, 21, 12, 38)) == (5.5, False)
