"""Saturn transit status relative to the natal Moon sign.

Same rule as the live n8n workflow (Owner confirmed 18 Oct 2026, over Key_rules):
- Sade Sati: Saturn in the 12th / 1st / 2nd sign from the Moon = 1st / 2nd / 3rd phase.
- Dhaiya: Saturn in the 4th from the Moon.
- Kantak Shani: Saturn in the 8th from the Moon.
"""
from __future__ import annotations

from app.mcp.payload import house_from

# Exact strings of the live n8n node.
_SADE_SATI = {12: "Yes - 1st Phase", 1: "Yes - 2nd Phase", 2: "Yes - 3rd Phase"}
DHAIYA_HOUSES = {4}
KANTAK_HOUSES = {8}


def status(moon_sign: str, saturn_sign: str) -> dict[str, str]:
    h = house_from(saturn_sign, moon_sign)
    return {
        "sade_sathi": _SADE_SATI.get(h, "No"),
        "dhaiya": "Yes - 4th from Moon (Dhaiya)" if h in DHAIYA_HOUSES else "No",
        "kantak_shani": "Yes - 8th from Moon (Kantak Shani)" if h in KANTAK_HOUSES else "No",
    }
