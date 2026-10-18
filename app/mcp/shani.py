"""Saturn transit status relative to the natal Moon sign (Owner's Key_rules).

- Sade Sati: Saturn in the 12th / 1st / 2nd sign from the Moon = 1st / 2nd / 3rd phase.
- Dhaiya (small panoti): Saturn in the 4th or 8th from the Moon.
- Kantak Shani: Saturn in the 4th, 8th or 10th from the Moon (Key_rules_final_1706.json).
Values match the n8n block: "Yes – 1st Phase" / "Yes – 2nd Phase" / "Yes – 3rd Phase" / "Yes" / "No".
"""
from __future__ import annotations

from app.mcp.payload import house_from

_SADE_SATI = {12: "Yes – 1st Phase", 1: "Yes – 2nd Phase", 2: "Yes – 3rd Phase"}
DHAIYA_HOUSES = {4, 8}
KANTAK_HOUSES = {4, 8, 10}


def status(moon_sign: str, saturn_sign: str) -> dict[str, str]:
    h = house_from(saturn_sign, moon_sign)
    return {
        "sade_sathi": _SADE_SATI.get(h, "No"),
        "dhaiya": "Yes" if h in DHAIYA_HOUSES else "No",
        "kantak_shani": "Yes" if h in KANTAK_HOUSES else "No",
    }
