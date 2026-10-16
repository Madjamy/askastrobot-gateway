"""Compact chart payload for the MCP tools.

Turns the n8n workflow output (the same JSON the Custom GPTs receive) into a
smaller, unambiguous structure:

- one planet list (9 grahas only; Uranus/Neptune/Pluto dropped)
- degrees as D°M', flags R/C/Ex/Deb
- a ready-made 12-house table (sign, lord, lord placement, occupants, aspects received)
- divisional-chart signs as names, not numbers
- gochar with house counted from Lagna and from Moon
- Manglik counted from Lagna, Moon and Venus

Pure functions only, no I/O. The original `Identifier` block names are kept so
the skill rule "only interpret blocks that exist" still works.
"""
from __future__ import annotations

from typing import Any

SIGNS = [
    "Aries", "Taurus", "Gemini", "Cancer", "Leo", "Virgo",
    "Libra", "Scorpio", "Sagittarius", "Capricorn", "Aquarius", "Pisces",
]
SIGN_LORDS = {
    "Aries": "Mars", "Taurus": "Venus", "Gemini": "Mercury", "Cancer": "Moon",
    "Leo": "Sun", "Virgo": "Mercury", "Libra": "Venus", "Scorpio": "Mars",
    "Sagittarius": "Jupiter", "Capricorn": "Saturn", "Aquarius": "Saturn", "Pisces": "Jupiter",
}
GRAHAS = ["Sun", "Moon", "Mars", "Mercury", "Jupiter", "Venus", "Saturn", "Rahu", "Ketu"]

# Classical exaltation / debilitation signs (nodes excluded: conventions differ).
_EXALTED = {"Sun": "Aries", "Moon": "Taurus", "Mars": "Capricorn", "Mercury": "Virgo",
            "Jupiter": "Cancer", "Venus": "Pisces", "Saturn": "Libra"}
_DEBILITATED = {"Sun": "Libra", "Moon": "Scorpio", "Mars": "Cancer", "Mercury": "Pisces",
                "Jupiter": "Capricorn", "Venus": "Virgo", "Saturn": "Aries"}

MANGLIK_HOUSES = {1, 2, 4, 7, 8, 12}

_KUNDALI_KEYS = ("Janam Kundali", "Kundali")
_ASPECTS_ID = "Planetary Drishti (Aspects)"
_YOGA_ID = "Yoga Detection"
_SHANI_ID = "Shani Sade Sathi, Dhaiya, Kantak Shani"
_DASHA_TABLE_ID = "Maha Dasha Antar Dasha"
_GOCHAR_KEY = "Current Transit / Gochar - Sign & Motion"
_VARGA_IDS = {"Navamsa": "Navamsa (D9)", "D10": "D10"}


class PayloadError(ValueError):
    """Raised when the n8n output is not usable (wrong status or no chart block)."""


# ----------------------------------------------------------------- helpers ---

def sign_name(value: Any) -> str | None:
    """Accept a sign number (1-12) or a sign name; return the canonical name."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 1 <= value <= 12:
        return SIGNS[value - 1]
    if isinstance(value, str):
        for s in SIGNS:
            if s.lower() == value.strip().lower():
                return s
    return None


def house_from(sign: str, reference_sign: str) -> int:
    """House number of `sign` counted from `reference_sign` (whole-sign, 1-12)."""
    return (SIGNS.index(sign) - SIGNS.index(reference_sign)) % 12 + 1


def fmt_degree(norm_degree: Any) -> str | None:
    try:
        d = float(norm_degree)
    except (TypeError, ValueError):
        return None
    deg = int(d)
    minutes = int(round((d - deg) * 60))
    if minutes == 60:
        deg, minutes = deg + 1, 0
    return f"{deg}°{minutes:02d}'"


def _truthy(value: Any) -> bool:
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


def _date_only(ts: Any) -> str | None:
    return str(ts)[:10] if ts else None


def _blocks(n8n_output: Any) -> list[dict[str, Any]]:
    """Unwrap `[{"status": "active", "data": [...]}]` and check the status."""
    root = n8n_output[0] if isinstance(n8n_output, list) and n8n_output else n8n_output
    if not isinstance(root, dict):
        raise PayloadError("unexpected n8n output shape")
    if root.get("status") != "active":
        raise PayloadError(f"n8n status is {root.get('status')!r}, not 'active'")
    data = root.get("data")
    if not isinstance(data, list):
        raise PayloadError("n8n output has no data list")
    return [b for b in data if isinstance(b, dict)]


def _find_kundali(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    for b in blocks:
        if b.get("Identifier") in _KUNDALI_KEYS or any(k in b for k in _KUNDALI_KEYS):
            out = b.get("output")
            if isinstance(out, dict) and "Ascendant" in out:
                return out
    raise PayloadError("no Kundali block in n8n output")


def _extended_flags(blocks: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Prashna output carries a `planets` list with combust/exalted/debilitated flags."""
    for b in blocks:
        if isinstance(b.get("planets"), list):
            return {p.get("planet"): p for p in b["planets"] if isinstance(p, dict)}
    return {}


# ------------------------------------------------------------ main pieces ---

def build_planets(kundali: dict[str, Any], extended: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    planets: dict[str, dict[str, Any]] = {}
    for name in GRAHAS:
        raw = kundali.get(name)
        if not isinstance(raw, dict):
            continue
        sign = sign_name(raw.get("zodiac_sign_name")) or sign_name(raw.get("current_sign"))
        if sign is None:
            continue
        flags: list[str] = []
        if name not in ("Rahu", "Ketu") and _truthy(raw.get("isRetro")):
            flags.append("R")
        ext = extended.get(name, {})
        if ext.get("isCombust") is True:
            flags.append("C")
        if _EXALTED.get(name) == sign or ext.get("isExalted") is True:
            flags.append("Ex")
        if _DEBILITATED.get(name) == sign or ext.get("isDebilitated") is True:
            flags.append("Deb")
        nak = raw.get("nakshatra_name")
        pada = raw.get("nakshatra_pada")
        planets[name] = {
            "sign": sign,
            "deg": fmt_degree(raw.get("normDegree")),
            "house": raw.get("house_number"),
            "nak": f"{nak}-{pada}" if nak and pada else nak,
            "flags": flags,
        }
    return planets


def build_lagna(kundali: dict[str, Any]) -> dict[str, Any]:
    asc = kundali["Ascendant"]
    sign = sign_name(asc.get("zodiac_sign_name")) or sign_name(asc.get("current_sign"))
    if sign is None:
        raise PayloadError("Ascendant sign missing")
    nak, pada = asc.get("nakshatra_name"), asc.get("nakshatra_pada")
    return {
        "sign": sign,
        "deg": fmt_degree(asc.get("normDegree")),
        "nak": f"{nak}-{pada}" if nak and pada else nak,
        "lord": SIGN_LORDS[sign],
    }


def parse_aspects(blocks: list[dict[str, Any]]) -> dict[str, list[int]] | None:
    for b in blocks:
        if b.get("Identifier") == _ASPECTS_ID and isinstance(b.get("output"), dict):
            result: dict[str, list[int]] = {}
            for key, val in b["output"].items():
                planet = str(key).split(" (", 1)[0].strip()
                houses = val.get("aspects_on_houses") if isinstance(val, dict) else None
                if planet in GRAHAS and isinstance(houses, list):
                    result[planet] = [h for h in houses if isinstance(h, int)]
            return result
    return None


def build_houses(
    lagna_sign: str,
    planets: dict[str, dict[str, Any]],
    aspects: dict[str, list[int]] | None,
) -> dict[str, dict[str, Any]]:
    """Whole-sign house table. Occupants come from the planets' house numbers."""
    houses: dict[str, dict[str, Any]] = {}
    start = SIGNS.index(lagna_sign)
    for h in range(1, 13):
        sign = SIGNS[(start + h - 1) % 12]
        lord = SIGN_LORDS[sign]
        houses[str(h)] = {
            "sign": sign,
            "lord": lord,
            "lord_in": planets.get(lord, {}).get("house"),
            "occupants": [p for p, d in planets.items() if d.get("house") == h],
            "aspected_by": sorted(p for p, hs in (aspects or {}).items() if h in hs),
        }
    return houses


def manglik(lagna_sign: str, planets: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """Mars in 1/2/4/7/8/12 counted from Lagna, Moon and Venus (Lagna weighted most)."""
    mars, moon, venus = planets.get("Mars"), planets.get("Moon"), planets.get("Venus")
    if not mars:
        return None
    from_lagna = house_from(mars["sign"], lagna_sign) in MANGLIK_HOUSES
    from_moon = bool(moon) and house_from(mars["sign"], moon["sign"]) in MANGLIK_HOUSES
    from_venus = bool(venus) and house_from(mars["sign"], venus["sign"]) in MANGLIK_HOUSES
    if from_lagna:
        summary = "Manglik (from Lagna)"
    elif from_moon or from_venus:
        refs = " and ".join(r for r, ok in (("Moon", from_moon), ("Venus", from_venus)) if ok)
        summary = f"Mild Manglik (from {refs} only; not from Lagna)"
    else:
        summary = "Not Manglik"
    return {"from_lagna": from_lagna, "from_moon": from_moon, "from_venus": from_venus, "summary": summary}


def _current_dasha(blocks: list[dict[str, Any]]) -> dict[str, str] | None:
    for b in blocks:
        if "maha_dasa" in b:
            out: dict[str, str] = {}
            for key, label in (("maha_dasa", "md"), ("antar_dasa", "ad"), ("pratyantar_dasa", "pd")):
                d = b.get(key)
                if isinstance(d, dict) and d.get("Lord"):
                    out[label] = f"{d['Lord']} ({_date_only(d.get('start_time'))} to {_date_only(d.get('end_time'))})"
            return out or None
    return None


def _dasha_table(blocks: list[dict[str, Any]]) -> dict[str, list[list[str | None]]] | None:
    for b in blocks:
        if b.get("Identifier") == _DASHA_TABLE_ID and isinstance(b.get("output"), dict):
            table: dict[str, list[list[str | None]]] = {}
            for md, antars in b["output"].items():
                if not isinstance(antars, dict):
                    continue
                table[md] = [
                    [ad, _date_only(p.get("start_time")), _date_only(p.get("end_time"))]
                    for ad, p in antars.items() if isinstance(p, dict)
                ]
            return table
    return None


def _gochar(blocks: list[dict[str, Any]], lagna_sign: str, moon_sign: str | None) -> dict[str, Any] | None:
    for b in blocks:
        rows = b.get(_GOCHAR_KEY)
        if isinstance(rows, list):
            result: dict[str, Any] = {}
            for r in rows:
                name, sign = r.get("Planet"), sign_name(r.get("Sign"))
                if name not in GRAHAS or sign is None:
                    continue
                entry: dict[str, Any] = {"sign": sign, "from_lagna": house_from(sign, lagna_sign)}
                if moon_sign:
                    entry["from_moon"] = house_from(sign, moon_sign)
                if name not in ("Rahu", "Ketu") and r.get("Retrograde") is True:
                    entry["flags"] = ["R"]
                result[name] = entry
            return result
    return None


def _vargas(blocks: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for b in blocks:
        label = _VARGA_IDS.get(b.get("Identifier", ""))
        if not label or not isinstance(b.get("output"), dict):
            continue
        chart: dict[str, str] = {}
        for row in b["output"].values():
            if not isinstance(row, dict):
                continue
            name = "Lagna" if row.get("name") == "Ascendant" else row.get("name")
            sign = sign_name(row.get("current_sign"))
            if sign and (name == "Lagna" or name in GRAHAS):
                chart[name] = sign
        out[label] = chart
    return out


# ------------------------------------------------------------- top level ---

def compact_chart(n8n_output: Any, *, chart_type: str) -> dict[str, Any]:
    """Build the compact payload for a natal or Prashna chart.

    `chart_type` is "natal" or "prashna". Raises PayloadError when the output
    is unusable (status not active, no Kundali block).
    """
    blocks = _blocks(n8n_output)
    kundali = _find_kundali(blocks)
    lagna = build_lagna(kundali)
    planets = build_planets(kundali, _extended_flags(blocks))
    missing = [g for g in GRAHAS if g not in planets or not isinstance(planets[g].get("house"), int)]
    if missing:
        # Never charge for an incomplete chart.
        raise PayloadError(f"chart incomplete, missing: {', '.join(missing)}")
    aspects = parse_aspects(blocks)
    moon_sign = planets.get("Moon", {}).get("sign")

    kundali_label = "Janam Kundali" if chart_type == "natal" else "Prashna Kundali"
    moon = kundali.get("Moon", {})
    result: dict[str, Any] = {
        "status": "active",
        "type": chart_type,
        "settings": "Sidereal, Lahiri ayanamsa, whole-sign houses",
        kundali_label: {
            "lagna": lagna,
            "moon": {"sign": moon_sign, "nak": planets.get("Moon", {}).get("nak"),
                     "nak_lord": moon.get("nakshatra_vimsottari_lord")},
            "planets": planets,
            "houses": build_houses(lagna["sign"], planets, aspects),
        },
    }
    if aspects is not None:
        result[_ASPECTS_ID] = aspects
    for b in blocks:
        if b.get("Identifier") == _YOGA_ID and isinstance(b.get("output"), list):
            result[_YOGA_ID] = [
                f"{y.get('yoga')}: {y.get('reason')}" if y.get("reason") else y.get("yoga")
                for y in b["output"] if isinstance(y, dict)
            ]
        if b.get("Identifier") == _SHANI_ID and isinstance(b.get("output"), dict):
            o = b["output"]
            result[_SHANI_ID] = {
                "as_of": o.get("analysis_date"),
                "sade_sathi": o.get("sade_sathi"),
                "dhaiya": o.get("dhaiya"),
                "kantak_shani": o.get("kantak_shani"),
            }
    if chart_type == "natal":
        m = manglik(lagna["sign"], planets)
        if m:
            result["Manglik"] = m
    current = _current_dasha(blocks)
    if current:
        result["Current Dasha"] = current
    table = _dasha_table(blocks)
    if table:
        result[_DASHA_TABLE_ID] = table
    gochar = _gochar(blocks, lagna["sign"], moon_sign)
    if gochar:
        result["Current Transit / Gochar"] = gochar
    result.update(_vargas(blocks))
    return result
