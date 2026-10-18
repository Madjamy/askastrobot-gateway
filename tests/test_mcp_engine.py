"""Ashtakavarga, Shani status, varga parsing and transit helpers (pure functions)."""
from __future__ import annotations

import asyncio
from datetime import date, datetime

from app.mcp import ashtakavarga, shani
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
    assert shani.status("Taurus", "Aries") == {"sade_sathi": "Yes - 1st Phase", "dhaiya": "No", "kantak_shani": "No"}
    assert shani.status("Taurus", "Taurus")["sade_sathi"] == "Yes - 2nd Phase"
    assert shani.status("Taurus", "Gemini")["sade_sathi"] == "Yes - 3rd Phase"
    assert shani.status("Taurus", "Leo") == {"sade_sathi": "No", "dhaiya": "Yes - 4th from Moon (Dhaiya)", "kantak_shani": "No"}    # 4th
    assert shani.status("Taurus", "Sagittarius") == {"sade_sathi": "No", "dhaiya": "No", "kantak_shani": "Yes - 8th from Moon (Kantak Shani)"}  # 8th
    assert shani.status("Taurus", "Aquarius")["kantak_shani"] == "No"                                        # 10th: not Kantak
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


def test_shadbala_parsing(monkeypatch) -> None:
    from app.mcp import astro_api

    async def fake_post(endpoint, body, **kw):
        assert endpoint == "shadbala/summary"
        return {"Sun": {"Shadbala": 559.5, "rupas": 9.325, "percentage_strength": 186.501, "ishta_phala": 50.597,
                        "kashta_phala": 9.378},
                **{p: {"rupas": 6.0, "percentage_strength": 100.0, "ishta_phala": 1, "kashta_phala": 1}
                   for p in ("Moon", "Mars", "Mercury", "Jupiter", "Venus", "Saturn")}}
    monkeypatch.setattr(astro_api, "_post", fake_post)
    sb = asyncio.run(astro_api.shadbala({}))
    assert sb["Sun"] == {"rupas": 9.32, "strength_pct": 186.5, "ishta": 50.6, "kashta": 9.4}


def test_slow_shadbala_does_not_drop_vargas(monkeypatch) -> None:
    from app.mcp import astro_api, enrich

    async def slow(body):
        await asyncio.sleep(5)
    async def d7(chart, body):
        return [{"name": n, "current_sign": 1, "house_number": 1} for n in
                ["Ascendant", "Sun", "Moon", "Mars", "Mercury", "Jupiter", "Venus", "Saturn", "Rahu", "Ketu"]]
    monkeypatch.setattr(astro_api, "shadbala", slow)
    monkeypatch.setattr(astro_api, "varga", d7)
    monkeypatch.setattr(enrich, "topics", lambda: {"vargas": {"general": ["D7"]}, "extras": {"general": ["shadbala"]}})
    chart = {"Janam Kundali": {"lagna": {"sign": "Aries"}, "planets": {}}}
    asyncio.run(enrich.add_topic_charts(chart, "general", {}, timeout=0.2))
    assert chart["D7"]["Lagna"] == "Aries" and chart["unavailable_charts"] == ["Shadbala"]
