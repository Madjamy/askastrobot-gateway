"""Transit table: interpolation, crossings (incl. two per day, retrograde), personalised gochar."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from app.mcp.transit_table import NAK_SPAN, Table, nakshatra_of, personal_gochar, sign_of, transits_window
from tests.transit_fakes import make_table

D0 = date(2027, 1, 1)


def _table(motion, days=10, retro=None) -> Table:
    return make_table(start=D0, days=days, motion=motion, retro=retro)


def test_position_interpolates_and_wraps() -> None:
    t = _table({"Moon": (355.0, 13.0), **{p: (0.0, 0.0) for p in
               ("Sun", "Mars", "Mercury", "Jupiter", "Venus", "Saturn", "Rahu", "Ketu")}})
    lon, _ = t.position("Moon", datetime(2027, 1, 1, 12, 0))
    assert abs(lon - 1.5) < 1e-9                       # 355 + 6.5 wraps to 1.5
    assert sign_of(lon) == "Aries" and nakshatra_of(lon) == "Ashwini-1"


def test_sign_crossing_time_is_exact_for_linear_motion() -> None:
    t = _table({"Moon": (25.0, 13.0), **{p: (0.0, 0.0) for p in
               ("Sun", "Mars", "Mercury", "Jupiter", "Venus", "Saturn", "Rahu", "Ketu")}})
    (when, sign), = t.sign_changes("Moon", datetime(2027, 1, 1), datetime(2027, 1, 1, 23, 59))
    expected = datetime(2027, 1, 1) + timedelta(days=5.0 / 13.0)
    assert sign == "Taurus" and abs((when - expected).total_seconds()) < 60


def test_two_nakshatra_changes_in_one_day() -> None:
    t = _table({"Moon": (13.0, 15.4), **{p: (0.0, 0.0) for p in
               ("Sun", "Mars", "Mercury", "Jupiter", "Venus", "Saturn", "Rahu", "Ketu")}})
    hits = t.nakshatra_changes("Moon", datetime(2027, 1, 1), datetime(2027, 1, 1, 23, 59))
    assert [n for _, n in hits] == ["Bharani", "Krittika"]
    assert hits[0][0] < hits[1][0]
    assert abs((hits[0][0] - datetime(2027, 1, 1)).total_seconds() - (NAK_SPAN - 13.0) / 15.4 * 86400) < 60


def test_retrograde_crossing_goes_backwards() -> None:
    t = _table({"Rahu": (300.2, -0.5), **{p: (0.0, 0.0) for p in
               ("Sun", "Moon", "Mars", "Mercury", "Jupiter", "Venus", "Saturn", "Ketu")}})
    (when, sign), = t.sign_changes("Rahu", datetime(2027, 1, 1), datetime(2027, 1, 5))
    assert sign == "Capricorn" and when.date() == date(2027, 1, 1)


def test_personal_gochar_for_owner_chart() -> None:
    t = make_table()
    natal = {"Sun": "Taurus", "Moon": "Aries", "Mars": "Virgo", "Mercury": "Taurus", "Jupiter": "Libra",
             "Venus": "Pisces", "Saturn": "Virgo", "Rahu": "Gemini", "Ketu": "Sagittarius"}
    g = personal_gochar(t, lagna="Leo", moon="Aries", natal_planets=natal, at=datetime.combine(t.days[60], datetime.min.time()))
    sat = g["Saturn"]
    assert sat["where"] == "Pisces, H8 / M12"
    assert sat["over_natal"] == ["Venus"]
    assert sat["aspects"] == ["H10 Taurus (natal Sun, Mercury)", "H2 Virgo (natal Mars, Saturn)",
                              "H5 Sagittarius (natal Ketu)"]
    assert sat["next"][0].startswith("Aries, H9 / M1 from ")
    assert g["Moon"]["nakshatra"] and "until" in g["Moon"] and "nakshatra_until" in g["Moon"]
    assert g["Shani Sade Sathi, Dhaiya, Kantak Shani"]["now"]["sade_sathi"] == "Yes - 1st Phase"
    assert g["Shani Sade Sathi, Dhaiya, Kantak Shani"]["changes"][0]["sade_sathi"] == "Yes - 2nd Phase"


def test_transits_window_periods_and_moon_rule() -> None:
    t = make_table()
    natal = {"Moon": "Aries", "Sun": "Taurus"}
    start = t.days[60]
    w = transits_window(t, lagna="Leo", moon="Aries", natal_planets=natal, start=start, end=start + timedelta(days=400))
    assert "Moon" not in w                                  # long window: no Moon periods
    sat = w["Saturn"]["periods"]
    assert sat[0]["where"] == "Pisces, H8 / M12" and sat[-1]["where"].startswith("Aries")
    assert w["Shani Sade Sathi, Dhaiya, Kantak Shani"][0]["sade_sathi"] == "Yes - 1st Phase"
    short = transits_window(t, lagna="Leo", moon="Aries", natal_planets=natal, start=start, end=start + timedelta(days=3))
    assert len(short["Moon"]["periods"]) >= 1


def test_load_covering_reloads_stale_cache(monkeypatch) -> None:
    import asyncio
    import app.mcp.transit_table as tt
    short, full = make_table(days=30), make_table(days=900)
    calls = []

    async def fake_load(force=False):
        calls.append(force)
        return full if force else short
    monkeypatch.setattr(tt, "load", fake_load)
    start = datetime.combine(full.days[100], datetime.min.time())
    got = asyncio.run(tt.load_covering(start, start + timedelta(days=200)))
    assert got is full and calls == [False, True]
