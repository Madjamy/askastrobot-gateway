"""Compact payload built from real n8n output supplied by the Owner (12 Oct 2026)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.mcp.payload import PayloadError, compact_chart, fmt_degree, house_from, manglik

FIX = Path(__file__).parent / "fixtures"


def _load(name: str):
    return json.loads((FIX / name).read_text())


def test_natal_core_blocks_and_houses() -> None:
    out = compact_chart(_load("n8n_horoscope_sample.json"), chart_type="natal")
    k = out["Janam Kundali"]
    assert k["lagna"] == {"sign": "Libra", "deg": "28°25'", "nak": "Visakha-3", "lord": "Venus"}  # API: 28°25'50"
    assert set(k["planets"]) == {"Sun", "Moon", "Mars", "Mercury", "Jupiter", "Venus", "Saturn", "Rahu", "Ketu"}
    h2 = k["houses"]["2"]
    assert h2["sign"] == "Scorpio" and h2["lord"] == "Mars" and h2["lord_in"] == 11
    assert h2["occupants"] == ["Rahu"]
    assert h2["aspected_by"] == ["Mars", "Moon", "Saturn", "Venus"]
    assert k["houses"]["8"]["occupants"] == ["Moon", "Venus", "Ketu"]
    assert k["houses"]["10"]["aspected_by"] == ["Rahu"]
    assert k["planets"]["Mercury"]["flags"] == ["R"]
    assert k["planets"]["Moon"]["flags"] == ["Ex"]          # Moon exalted in Taurus
    assert k["planets"]["Rahu"]["flags"] == []               # nodes: no R flag


def test_natal_other_blocks() -> None:
    out = compact_chart(_load("n8n_horoscope_sample.json"), chart_type="natal")
    assert out["Shani Sade Sathi, Dhaiya, Kantak Shani"]["sade_sathi"] == "No"
    assert out["Yoga Detection"] == ["Vipreet Raj Yoga: Jupiter is a dusthana lord placed in another dusthana house"]
    assert out["Current Dasha"]["md"].startswith("Jupiter (2020-08-30")
    assert out["Maha Dasha Antar Dasha"]["Jupiter"][0] == ["Mercury", "2025-05-01", "2027-08-06"]
    g = out["Current Transit / Gochar"]
    assert g["Saturn"] == {"sign": "Pisces", "from_lagna": 6, "from_moon": 11, "flags": ["R"]}
    assert g["Jupiter"] == {"sign": "Cancer", "from_lagna": 10, "from_moon": 3}
    assert out["Navamsa (D9)"]["Lagna"] == "Gemini" and out["Navamsa (D9)"]["Saturn"] == "Scorpio"
    assert out["D10"]["Rahu"] == "Sagittarius"
    assert "Uranus" not in json.dumps(out) and "Pluto" not in json.dumps(out)
    # Mars in Leo: 11th from Libra Lagna (no), 4th from Taurus Moon and Taurus Venus (yes).
    assert out["Manglik"] == {"from_lagna": False, "from_moon": True, "from_venus": True,
                              "summary": "Mild Manglik (from Moon and Venus only; not from Lagna)"}


def test_prashna_uses_extended_flags_and_no_manglik() -> None:
    out = compact_chart(_load("n8n_prashna_sample.json"), chart_type="prashna")
    k = out["Prashna Kundali"]
    assert k["lagna"]["sign"] == "Capricorn"
    assert k["planets"]["Mars"]["flags"] == ["Deb"]
    assert k["planets"]["Jupiter"]["flags"] == ["Ex"]
    assert k["planets"]["Venus"]["flags"] == ["R"]
    assert "Manglik" not in out
    assert out["Current Dasha"]["pd"].startswith("Venus")


def test_payload_is_much_smaller() -> None:
    for name, kind in (("n8n_prashna_sample.json", "prashna"), ("n8n_horoscope_sample.json", "natal")):
        raw = _load(name)
        compact = compact_chart(raw, chart_type=kind)
        ratio = len(json.dumps(compact, separators=(",", ":"))) / len(json.dumps(raw, separators=(",", ":")))
        assert ratio < 0.5, (name, ratio)


def test_inactive_status_rejected() -> None:
    with pytest.raises(PayloadError):
        compact_chart([{"status": "error", "data": []}], chart_type="natal")
    with pytest.raises(PayloadError):
        compact_chart([{"status": "active", "data": []}], chart_type="natal")


def test_helpers() -> None:
    assert house_from("Pisces", "Libra") == 6
    assert house_from("Libra", "Libra") == 1
    assert fmt_degree(28.43055) == "28°25'"     # truncated, not rounded
    assert fmt_degree(29.9999) == "29°59'"
    assert fmt_degree(30.5) is None
    assert fmt_degree(29.99999999999) == "29°59'"
    assert fmt_degree(12.1) == "12°06'"
    planets = {"Mars": {"sign": "Aries"}, "Moon": {"sign": "Cancer"}, "Venus": {"sign": "Leo"}}
    assert manglik("Aries", planets)["summary"] == "Manglik (from Lagna)"       # Mars in 1st
    m = manglik("Taurus", planets)                                              # 12th from Lagna
    assert m["from_lagna"] is True
    m = manglik("Gemini", planets)                                              # 11th from Lagna, 10th from Moon, 9th from Venus
    assert m["summary"] == "Not Manglik"
    m = manglik("Gemini", {"Mars": {"sign": "Aries"}, "Moon": {"sign": "Libra"}, "Venus": {"sign": "Leo"}})
    assert m["from_moon"] is True and m["summary"].startswith("Mild Manglik")


def test_invalid_house_or_missing_degree_rejected() -> None:
    raw = _load("n8n_horoscope_sample.json")
    raw[0]["data"][0]["output"]["Mars"]["house_number"] = 99
    with pytest.raises(PayloadError):
        compact_chart(raw, chart_type="natal")
    raw = _load("n8n_horoscope_sample.json")
    for key in ("normDegree", "degrees", "minutes"):
        raw[0]["data"][0]["output"]["Venus"][key] = None
    with pytest.raises(PayloadError):
        compact_chart(raw, chart_type="natal")
