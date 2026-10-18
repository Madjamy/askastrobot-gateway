"""Ashtakavarga (Parashari / BPHS tables), calculated from D1 sign positions.

For each of the 7 planets (Bhinnashtakavarga, BAV), a bindu is given to the
houses counted from each of 8 references (the 7 planets + Lagna). The
Sarvashtakavarga (SAV) is the sum of the 7 BAVs per sign. Classical totals:
Sun 48, Moon 49, Mars 39, Mercury 54, Jupiter 56, Venus 52, Saturn 39 = 337,
which every correct calculation reproduces, whatever the chart.

The API does not provide Ashtakavarga, so it is computed here (no I/O).
"""
from __future__ import annotations

from typing import Any

from app.mcp.payload import SIGNS

PLANETS = ("Sun", "Moon", "Mars", "Mercury", "Jupiter", "Venus", "Saturn")
REFERENCES = PLANETS + ("Lagna",)

# BENEFIC_HOUSES[planet][reference] = houses (counted from the reference) that get a bindu.
BENEFIC_HOUSES: dict[str, dict[str, tuple[int, ...]]] = {
    "Sun": {
        "Sun": (1, 2, 4, 7, 8, 9, 10, 11), "Moon": (3, 6, 10, 11), "Mars": (1, 2, 4, 7, 8, 9, 10, 11),
        "Mercury": (3, 5, 6, 9, 10, 11, 12), "Jupiter": (5, 6, 9, 11), "Venus": (6, 7, 12),
        "Saturn": (1, 2, 4, 7, 8, 9, 10, 11), "Lagna": (3, 4, 6, 10, 11, 12),
    },
    "Moon": {
        "Sun": (3, 6, 7, 8, 10, 11), "Moon": (1, 3, 6, 7, 10, 11), "Mars": (2, 3, 5, 6, 9, 10, 11),
        "Mercury": (1, 3, 4, 5, 7, 8, 10, 11), "Jupiter": (1, 4, 7, 8, 10, 11, 12), "Venus": (3, 4, 5, 7, 9, 10, 11),
        "Saturn": (3, 5, 6, 11), "Lagna": (3, 6, 10, 11),
    },
    "Mars": {
        "Sun": (3, 5, 6, 10, 11), "Moon": (3, 6, 11), "Mars": (1, 2, 4, 7, 8, 10, 11),
        "Mercury": (3, 5, 6, 11), "Jupiter": (6, 10, 11, 12), "Venus": (6, 8, 11, 12),
        "Saturn": (1, 4, 7, 8, 9, 10, 11), "Lagna": (1, 3, 6, 10, 11),
    },
    "Mercury": {
        "Sun": (5, 6, 9, 11, 12), "Moon": (2, 4, 6, 8, 10, 11), "Mars": (1, 2, 4, 7, 8, 9, 10, 11),
        "Mercury": (1, 3, 5, 6, 9, 10, 11, 12), "Jupiter": (6, 8, 11, 12), "Venus": (1, 2, 3, 4, 5, 8, 9, 11),
        "Saturn": (1, 2, 4, 7, 8, 9, 10, 11), "Lagna": (1, 2, 4, 6, 8, 10, 11),
    },
    "Jupiter": {
        "Sun": (1, 2, 3, 4, 7, 8, 9, 10, 11), "Moon": (2, 5, 7, 9, 11), "Mars": (1, 2, 4, 7, 8, 10, 11),
        "Mercury": (1, 2, 4, 5, 6, 9, 10, 11), "Jupiter": (1, 2, 3, 4, 7, 8, 10, 11), "Venus": (2, 5, 6, 9, 10, 11),
        "Saturn": (3, 5, 6, 12), "Lagna": (1, 2, 4, 5, 6, 7, 9, 10, 11),
    },
    "Venus": {
        "Sun": (8, 11, 12), "Moon": (1, 2, 3, 4, 5, 8, 9, 11, 12), "Mars": (3, 5, 6, 9, 11, 12),
        "Mercury": (3, 5, 6, 9, 11), "Jupiter": (5, 8, 9, 10, 11), "Venus": (1, 2, 3, 4, 5, 8, 9, 10, 11),
        "Saturn": (3, 4, 5, 8, 9, 10, 11), "Lagna": (1, 2, 3, 4, 5, 8, 9, 11),
    },
    "Saturn": {
        "Sun": (1, 2, 4, 7, 8, 10, 11), "Moon": (3, 6, 11), "Mars": (3, 5, 6, 10, 11, 12),
        "Mercury": (6, 8, 9, 10, 11, 12), "Jupiter": (5, 6, 11, 12), "Venus": (6, 11, 12),
        "Saturn": (3, 5, 6, 11), "Lagna": (1, 3, 4, 6, 10, 11),
    },
}

CLASSICAL_TOTALS = {"Sun": 48, "Moon": 49, "Mars": 39, "Mercury": 54, "Jupiter": 56, "Venus": 52, "Saturn": 39}
SAV_TOTAL = 337


def _check_tables() -> None:
    for planet, refs in BENEFIC_HOUSES.items():
        total = sum(len(h) for h in refs.values())
        if total != CLASSICAL_TOTALS[planet] or set(refs) != set(REFERENCES):
            raise AssertionError(f"Ashtakavarga table for {planet} is wrong (total {total})")


_check_tables()


def compute(lagna_sign: str, planet_signs: dict[str, str]) -> dict[str, Any]:
    """BAV per planet and SAV, both by house counted from Lagna (house 1 = Lagna sign)."""
    positions = {**{p: SIGNS.index(planet_signs[p]) for p in PLANETS}, "Lagna": SIGNS.index(lagna_sign)}
    bav_by_sign: dict[str, list[int]] = {}
    for planet in PLANETS:
        bindus = [0] * 12
        for ref, houses in BENEFIC_HOUSES[planet].items():
            for h in houses:
                bindus[(positions[ref] + h - 1) % 12] += 1
        bav_by_sign[planet] = bindus

    sav_by_sign = [sum(bav_by_sign[p][i] for p in PLANETS) for i in range(12)]
    lagna_idx = positions["Lagna"]

    def by_house(values: list[int]) -> list[int]:
        return [values[(lagna_idx + h) % 12] for h in range(12)]

    sav_houses = by_house(sav_by_sign)
    result = {
        "system": "Parashari (BPHS)",
        "SAV_by_house": {str(h + 1): sav_houses[h] for h in range(12)},
        "SAV_total": sum(sav_by_sign),
        "BAV_by_house": {p: by_house(bav_by_sign[p]) for p in PLANETS},
        "BAV_totals": {p: sum(bav_by_sign[p]) for p in PLANETS},
        "note": "Houses counted from Lagna (1 = Lagna).",
    }
    assert result["SAV_total"] == SAV_TOTAL
    return result
