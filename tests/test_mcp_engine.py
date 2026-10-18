"""Ashtakavarga, Shani status, varga parsing and transit helpers (pure functions)."""
from __future__ import annotations

import asyncio
from datetime import date, datetime

from app.mcp import ashtakavarga, shani, transits
from app.mcp.astro_api import _rows
from app.mcp.payload import SIGNS, moon_chart, varga_compact


def test_ashtakavarga_totals_any_chart() -> None:
    import random
    rng = random.Random(7)
    for _ in range(50):
        lagna = rng.choice(SIGNS)
        signs = {p: rng.choice(SIGNS) for p in ashtakavarga.PLANETS}
        r = ashtakavarga.compute(lagna, signs)
        assert r["SAV_total"] == 337 and sum(r["SAV_by_house"].values()) == 337
        assert r["BAV_totals"] == ashtakavarga.CLASSICAL_TOTALS
        assert all(0 <= v <= 8 for bav in r["BAV_by_house"].values() for v in bav)


def test_ashtakavarga_house_rotation() -> None:
    signs = {p: "Aries" for p in ashtakavarga.PLANETS}
    a = ashtakavarga.compute("Aries", signs)
    b = ashtakavarga.compute("Taurus", {p: "Taurus" for p in ashtakavarga.PLANETS})
    assert a["SAV_by_house"] == b["SAV_by_house"]           # everything shifted together


def test_shani_status() -> None:
    assert shani.status("Taurus", "Aries") == {"sade_sathi": "Yes – 1st Phase", "dhaiya": "No", "kantak_shani": "No"}
    assert shani.status("Taurus", "Taurus")["sade_sathi"] == "Yes – 2nd Phase"
    assert shani.status("Taurus", "Gemini")["sade_sathi"] == "Yes – 3rd Phase"
    assert shani.status("Taurus", "Leo") == {"sade_sathi": "No", "dhaiya": "Yes", "kantak_shani": "Yes"}   # 4th
    assert shani.status("Taurus", "Aquarius")["kantak_shani"] == "Yes"                                       # 10th
    assert shani.status("Taurus", "Pisces") == {"sade_sathi": "No", "dhaiya": "No", "kantak_shani": "No"}  # n8n sample


def test_api_row_parsing_formats() -> None:
    as_index = {"0": {"name": "Ascendant", "current_sign": 4, "house_number": 1},
                "1": {"name": "Sun", "current_sign": 12, "house_number": 9}}
    as_list = [{"0": {"name": "Ascendant", "current_sign": 4}}, {"debug": {}}]
    as_named = {"Sun": {"current_sign": 5, "isRetro": "false"}}
    assert [r["name"] for r in _rows(as_index)] == ["Ascendant", "Sun"]
    assert _rows(as_list)[0]["name"] == "Ascendant"
    assert _rows(as_named)[0]["name"] == "Sun"
    assert varga_compact(_rows(as_index)) == {"Lagna": "Cancer", "Sun": "Pisces (9)"}


def test_moon_chart() -> None:
    planets = {"Moon": {"sign": "Taurus"}, "Venus": {"sign": "Taurus"}, "Mars": {"sign": "Leo"}}
    mc = moon_chart("Taurus", planets)
    assert mc["houses"]["1"]["occupants"] == ["Moon", "Venus"] and mc["houses"]["4"]["occupants"] == ["Mars"]


def test_month_dates_and_dasha() -> None:
    assert len(transits.month_dates(date(2026, 10, 31), date(2028, 12, 31))) == 24          # capped
    assert transits.month_dates(date(2026, 10, 31), date(2026, 12, 1)) == [date(2026, 10, 31), date(2026, 11, 28)]
    table = {"Jupiter": [["Mercury", "2025-05-01", "2027-08-06"], ["Ketu", "2027-08-06", "2028-07-12"]]}
    assert transits.dasha_at(table, date(2027, 8, 6)) == "Jupiter / Ketu"
    assert transits.dasha_changes(table, date(2027, 1, 1), date(2027, 12, 31)) == [{"date": "2027-08-06", "to": "Jupiter / Ketu"}]


def test_rahu_ingress_mirrors_ketu() -> None:
    async def fetch(t):
        rahu, ketu = ("Aquarius", "Leo") if t < datetime(2027, 2, 15, 18, 20) else ("Capricorn", "Cancer")
        base = {g: ("Aries", False) for g in ("Sun", "Moon", "Mars", "Mercury", "Jupiter", "Venus", "Saturn")}
        return {**base, "Rahu": (rahu, False), "Ketu": (ketu, False)}
    out = asyncio.run(transits.build(fetch=fetch, lagna_sign="Libra", moon_sign="Taurus", dasha_table=None,
                                     start=date(2027, 1, 1), end=date(2027, 3, 1)))
    assert [(i["planet"], i["date"], i["enters"]) for i in out["ingress"]] == [
        ("Rahu", "2027-02-15", "Capricorn"), ("Ketu", "2027-02-15", "Cancer")]   # 18:20 UTC, not the next noon
    h, m = map(int, out["ingress"][0]["time_utc"].split(":"))
    assert abs((h * 60 + m) - (18 * 60 + 20)) <= 15             # within 15 minutes of the true change


def test_ingress_after_last_snapshot_is_found() -> None:
    async def fetch(t):
        sat = "Pisces" if t < datetime(2027, 1, 15, 3, 0) else "Aries"
        base = {g: ("Taurus", False) for g in ("Sun", "Moon", "Mars", "Mercury", "Jupiter", "Venus", "Rahu", "Ketu")}
        return {**base, "Saturn": (sat, False)}
    out = asyncio.run(transits.build(fetch=fetch, lagna_sign="Libra", moon_sign="Taurus", dasha_table=None,
                                     start=date(2027, 1, 1), end=date(2027, 1, 31)))
    assert len(out["snapshots"]) == 1
    assert out["ingress"][0]["date"] == "2027-01-15" and out["ingress"][0]["planet"] == "Saturn"
