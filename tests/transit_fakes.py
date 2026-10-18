"""A synthetic transit table with simple linear motion, for tests (no DB, no API)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from app.mcp.transit_table import Table

# (longitude at day 0, degrees per day). Saturn starts in Pisces (H8 for Leo, H12 for Aries).
MOTION = {
    "Sun": (200.0, 1.0), "Moon": (10.0, 13.2), "Mars": (100.0, 0.5), "Mercury": (205.0, 1.2),
    "Jupiter": (125.0, 0.083), "Venus": (190.0, 1.1), "Saturn": (346.0, 0.0333),
    "Rahu": (302.0, -0.053), "Ketu": (122.0, -0.053),
}


def make_table(start: date | None = None, days: int = 900, motion: dict | None = None,
               retro: dict[str, tuple[int, int]] | None = None) -> Table:
    start = start or (datetime.now(timezone.utc).date() - timedelta(days=60))
    motion = motion or MOTION
    rows = {}
    for i in range(days):
        d = start + timedelta(days=i)
        rows[d] = {}
        for planet, (lon0, speed) in motion.items():
            is_retro = bool(retro and planet in retro and retro[planet][0] <= i < retro[planet][1])
            rows[d][planet] = ((lon0 + speed * i) % 360, is_retro)
    return Table(days=sorted(rows), rows=rows)
