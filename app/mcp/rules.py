"""Chart rules ported one-for-one from the live n8n workflows (18 Oct 2026),
so answers stay the same now that the gateway calls the API directly.

Source nodes (docs/n8n-logic-extract.txt): "Aspect1/Aspect2", "Combust + Debilitated",
"Yogas", "Shani Sade Sathi, Dhaiya, Kantak Shani1" (now app/mcp/shani.py),
"Code1" (Maha/Antar dasha window).

Input `kundali` is the planets/extended output: {"Ascendant": {...}, "Sun": {"current_sign": 2,
"house_number": 10, "fullDegree": ..., "isRetro": "false", ...}, ...}.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

# --- Aspects (Aspect1 / Aspect2) -----------------------------------------------
ASPECT_MAP: dict[str, list[int]] = {
    "Saturn": [3, 7, 10],
    "Mars": [4, 7, 8],
    "Jupiter": [5, 7, 9],
    "Rahu": [5, 7, 9],
    "Sun": [7],
    "Moon": [7],
    "Mercury": [7],
    "Venus": [7],
}


def _wrap(house: int) -> int:
    return ((house - 1 + 12) % 12) + 1


def aspected_houses(planet: str, house: int) -> list[int]:
    return [_wrap(house + offset - 1) for offset in ASPECT_MAP.get(planet, [])]


def aspects_block(kundali: dict[str, Any]) -> dict[str, Any]:
    output = {}
    for planet in ASPECT_MAP:
        data = kundali.get(planet)
        if not data or not data.get("house_number"):
            continue
        house = data["house_number"]
        output[f"{planet} (occupies House {house})"] = {"aspects_on_houses": aspected_houses(planet, house)}
    return {"Identifier": "Planetary Drishti (Aspects)",
            "note": "The following aspects are based on house numbers (Bhavas), not zodiac signs (Rashis).",
            "output": output}


# --- Combustion and dignity ("Combust + Debilitated") ----------------------------
EXALTATION = {"Sun": 1, "Moon": 2, "Mars": 10, "Mercury": 6, "Jupiter": 4, "Venus": 12, "Saturn": 7, "Rahu": 2, "Ketu": 8}
DEBILITATION = {"Sun": 7, "Moon": 8, "Mars": 4, "Mercury": 12, "Jupiter": 10, "Venus": 6, "Saturn": 1, "Rahu": 8, "Ketu": 2}
_EXCLUDED = {"Ascendant", "Pluto", "Neptune", "Uranus"}


def _deg_diff(a: float, b: float) -> float:
    d = abs(a - b)
    return min(d, 360 - d)


def dignity_block(kundali: dict[str, Any]) -> dict[str, Any]:
    sun_deg = kundali["Sun"]["fullDegree"]
    combust_orb = {
        "Mars": 17,
        "Mercury": 14 if kundali.get("Mercury", {}).get("isRetro") == "true" else 12,
        "Venus": 10 if kundali.get("Venus", {}).get("isRetro") == "true" else 8,
        "Jupiter": 11,
        "Saturn": 15,
    }
    rows = []
    for planet, p in kundali.items():
        if planet in _EXCLUDED or not isinstance(p, dict):
            continue
        sign = p.get("current_sign")
        rows.append({
            "planet": planet,
            "zodiacSign": p.get("zodiac_sign_name"),
            "degree": f"{float(p.get('fullDegree', 0)):.2f}",
            "houseNumber": p.get("house_number"),
            "isRetro": "Not Applicable" if planet in ("Rahu", "Ketu") else p.get("isRetro") == "true",
            "isCombust": (_deg_diff(float(p["fullDegree"]), float(sun_deg)) <= combust_orb[planet])
                         if planet in combust_orb else "Not Applicable",
            "isExalted": EXALTATION.get(planet) == sign,
            "isDebilitated": DEBILITATION.get(planet) == sign,
        })
    return {"planets": rows}


# --- Yogas ("Yogas") --------------------------------------------------------------
SIGN_LORDS = {1: "Mars", 2: "Venus", 3: "Mercury", 4: "Moon", 5: "Sun", 6: "Mercury",
              7: "Venus", 8: "Mars", 9: "Jupiter", 10: "Saturn", 11: "Saturn", 12: "Jupiter"}
_KENDRA = {1, 4, 7, 10}
_TRIKONA = {1, 5, 9}


def _house_lord(house: int, asc_sign: int) -> str:
    return SIGN_LORDS[((asc_sign - 1 + house - 1) % 12) + 1]


def yogas_block(kundali: dict[str, Any]) -> dict[str, Any]:
    def get(planet: str, prop: str) -> Any:
        p = kundali.get(planet)
        if not isinstance(p, dict):
            return None
        return p.get("current_sign") if prop == "sign" else p.get(prop)

    results: list[dict[str, str]] = []

    def add(yoga: str, reason: str) -> None:
        results.append({"yoga": yoga, "reason": reason})

    asc = get("Ascendant", "sign")
    if asc:
        moon_h, jup_h = get("Moon", "house_number"), get("Jupiter", "house_number")
        if moon_h in _KENDRA and jup_h in _KENDRA:
            add("Gaj Kesari Yoga", "Moon and Jupiter in Kendra houses")
        if jup_h in _KENDRA and get("Jupiter", "sign") in (4, 9, 12):
            add("Hans Yoga", "Jupiter in Kendra and in own/exalted sign")
        if get("Venus", "house_number") in _KENDRA and get("Venus", "sign") in (2, 7, 12):
            add("Malavya Yoga", "Venus in Kendra and in own/exalted sign")
        mer_sign = get("Mercury", "sign")
        if get("Mercury", "house_number") in _KENDRA and mer_sign in (3, 6):
            add("Bhadra Yoga", "Mercury in Kendra and in own/exalted sign")
        if get("Saturn", "house_number") in _KENDRA and get("Saturn", "sign") in (10, 11):
            add("Sasa Yoga", "Saturn in Kendra and in own sign")
        if get("Mars", "house_number") in _KENDRA and get("Mars", "sign") in (1, 8, 10):
            add("Ruchaka Yoga", "Mars in Kendra and in own/exalted sign")
        sun_sign = get("Sun", "sign")
        if sun_sign and sun_sign == mer_sign:
            add("Budha-Aditya Yoga", "Sun and Mercury in same sign")
        for lord in (_house_lord(h, asc) for h in (6, 8, 12)):
            data = kundali.get(lord)
            if isinstance(data, dict) and data.get("house_number") in (6, 8, 12):
                add("Vipreet Raj Yoga", f"{lord} is a dusthana lord placed in another dusthana house")
                break
        planets = [p for p, v in kundali.items() if isinstance(v, dict) and v.get("current_sign") is not None]
        for i, p1 in enumerate(planets):
            s1 = kundali[p1]["current_sign"]
            lord1 = SIGN_LORDS.get(s1)
            for p2 in planets[i + 1:]:
                s2 = kundali[p2]["current_sign"]
                lord2 = SIGN_LORDS.get(s2)
                if lord1 == p2 and lord2 == p1:
                    add("Parivartan Yoga", f"{p1} and {p2} are in mutual sign exchange")
                    h1, h2 = get(p1, "house_number"), get(p2, "house_number")
                    if (h1 in _KENDRA and h2 in _TRIKONA) or (h2 in _KENDRA and h1 in _TRIKONA):
                        add("Maha Parivartan Raj Yoga",
                            f"{p1} (in House {h1}) and {p2} (in House {h2}) are in Kendra-Trikona sign exchange")
    return {"Identifier": "Yoga Detection",
            "note": "These yogas are detected based on house and sign placements." if results else "No major yogas detected.",
            "output": results}


# --- Maha/Antar dasha table ("Code1"), window relative to today --------------------
def dasha_table_block(maha_antar: dict[str, Any], today: date | None = None,
                      years_back: int = 1, years_ahead: int = 15) -> dict[str, Any]:
    """n8n kept a fixed 2025-06 .. 2040-12 window; here: 1 year back .. 15 years ahead."""
    t = today or datetime.now(timezone.utc).date()
    lo = date(t.year - years_back, t.month, min(t.day, 28)).isoformat()
    hi = date(t.year + years_ahead, t.month, min(t.day, 28)).isoformat()

    def in_range(s: str | None) -> bool:
        return bool(s) and lo <= s[:10] <= hi

    filtered: dict[str, Any] = {}
    for md, antars in maha_antar.items():
        if not isinstance(antars, dict):
            continue
        keep = {ad: {"start_time": v["start_time"], "end_time": v["end_time"]}
                for ad, v in antars.items()
                if isinstance(v, dict) and (in_range(v.get("start_time")) or in_range(v.get("end_time")))}
        if keep:
            filtered[md] = keep
    return {"Identifier": "Maha Dasha Antar Dasha", "output": filtered}


def current_dasha_block(dasa_info: dict[str, Any], label: str = "Current Dasha") -> dict[str, Any]:
    return {"Dasha": label,
            "maha_dasa": dasa_info.get("maha_dasa"),
            "antar_dasa": dasa_info.get("antar_dasa"),
            "pratyantar_dasa": dasa_info.get("pratyantar_dasa")}
