"""End-to-end MCP calls through the mounted /mcp app.

DB and geocoder are faked; the astrology API is faked with the REAL responses
captured for the Owner's chart (tests/fixtures/api_owner_chart_raw.json:
21 May 1982 12:38 Delhi, Leo Lagna, Aries Moon); the transit table is synthetic.
"""
from __future__ import annotations

import copy
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

import app.mcp.astro_api as api
import app.mcp.entitlement as ent
import app.mcp.server as srv
import app.mcp.transit_table as tt
from app.mcp.geo import Place
from tests.transit_fakes import make_table

BASE = "https://api.askastrobot.com"
FIX = Path(__file__).parent / "fixtures"
RAW = json.loads((FIX / "api_owner_chart_raw.json").read_text())


def _out(key):
    o = RAW[key]["output"]
    return json.loads(o) if isinstance(o, str) else o
HEADERS = {"Authorization": "Bearer good-token", "Accept": "application/json, text/event-stream"}


class FakeConn:
    def __init__(self, state):
        self.s = state

    async def fetchrow(self, sql, *args):
        if "gw_oauth_tokens" in sql:
            if args[0] != "good-token" or args[1] != f"{BASE}/mcp":
                return None
            return {"user_id": "u-1", "scope": "read:astro write:query", "client_id": "https://chatgpt.com/oauth/client.json",
                    "resource": f"{BASE}/mcp", "email": "seeker@example.com",
                    "expires_at": datetime.now(tz=timezone.utc) + timedelta(hours=1)}
        if "gw_subscriptions" in sql:
            return None
        raise AssertionError(sql)

    async def fetchval(self, sql, *args):
        if "INSERT INTO public.gw_query_log" in sql:
            self.s["inserted"].append(args)
            return len(self.s["inserted"])
        if "INTERVAL '1 hour'" in sql:
            return self.s["recent"]
        if "input_hash = $2" in sql:
            return self.s["duplicate"]
        if "subject_hash = $2" in sql:
            return self.s["follow_up"]
        if "gw_subscriptions" in sql:
            return self.s["sub"]
        if "was_paid_query = FALSE" in sql:
            return self.s["free_used"]
        raise AssertionError(sql)

    async def execute(self, sql, *args):
        if "DELETE FROM public.gw_query_log" in sql:
            self.s["released"].append(args[0])
        elif "UPDATE public.gw_query_log" in sql:
            self.s["completed"].append(args)

    def transaction(self):
        return _Ctx(None)


class _Ctx:
    def __init__(self, v):
        self.v = v

    async def __aenter__(self):
        return self.v

    async def __aexit__(self, *a):
        return False


class FakePool:
    def __init__(self, state):
        self.state = state

    def acquire(self):
        return _Ctx(FakeConn(self.state))


@pytest.fixture
def state(monkeypatch):
    s = {"recent": 0, "duplicate": None, "follow_up": None, "sub": None, "free_used": 0, "inserted": [],
         "released": [], "completed": [], "api_calls": [], "api_down": False, "kundali_override": None}
    pool = FakePool(s)
    monkeypatch.setattr(srv, "get_pool", lambda: pool)
    monkeypatch.setattr(ent, "get_pool", lambda: pool)

    async def fake_place(q):
        return Place(name="Delhi, India", latitude=28.6139, longitude=77.209, tz_name="Asia/Kolkata")

    async def kundali(body, deadline=None):
        s["api_calls"].append(("kundali", body))
        if s["api_down"]:
            raise api.AstroApiError("planets/extended: HTTP 503")
        return s["kundali_override"] or copy.deepcopy(_out("planets_extended"))

    async def varga_raw(chart, body, deadline=None):
        s["api_calls"].append((chart, body))
        return {"D9": _out("navamsa"), "D10": _out("d10")}[chart]

    async def varga(chart, body, deadline=None):          # topic vargas: unavailable unless a test overrides
        raise api.AstroApiError("not in fixture")

    async def dasha_table(body, deadline=None):
        s["api_calls"].append(("dasha_table", body))
        return _out("maha_antar")

    async def dasha_now(body, event, lat, lon, offset, deadline=None):
        s["api_calls"].append(("dasha_now", body))
        return _out("dasa_info")

    async def shadbala(body, deadline=None):
        raise api.AstroApiError("not in fixture")

    table = make_table()

    async def load(force=False):
        return table

    for name, fn in (("kundali", kundali), ("varga_raw", varga_raw), ("varga", varga), ("dasha_table", dasha_table),
                     ("dasha_now", dasha_now), ("shadbala", shadbala)):
        monkeypatch.setattr(api, name, fn)
    monkeypatch.setattr(tt, "load", load)
    monkeypatch.setattr(srv, "resolve_place", fake_place)
    return s


def _call(client, name, args):
    return client.post("/mcp", headers=HEADERS, json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": args}})


def _result(resp):
    assert resp.status_code == 200, resp.text
    return resp.json()["result"]


BIRTH = {"birth_date": "1982-05-21", "birth_time": "12:38", "birth_place": "Delhi, India", "query_type": "career"}


def test_tools_listed_with_annotations(state, live_client) -> None:
    client = live_client
    if True:
        r = client.post("/mcp", headers=HEADERS, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tools = {t["name"]: t for t in r.json()["result"]["tools"]}
    assert set(tools) == {"get_prashna_chart", "get_birth_chart", "get_divisional_chart", "get_transits", "get_account"}
    paid = tools["get_birth_chart"]["annotations"]
    assert paid["readOnlyHint"] is False and paid["destructiveHint"] is True and paid["openWorldHint"] is False
    assert tools["get_account"]["annotations"]["readOnlyHint"] is True
    props = tools["get_birth_chart"]["inputSchema"]["properties"]
    assert "query_text" not in props and set(props) == {"birth_date", "birth_time", "birth_place", "query_type"}


def test_birth_chart_free_call(state, live_client) -> None:
    client = live_client
    if True:
        res = _result(_call(client, "get_birth_chart", BIRTH))
    assert not res.get("isError")
    chart = res["structuredContent"]
    assert chart["Janam Kundali"]["lagna"]["sign"] == "Leo"
    assert chart["birth"]["utc_offset"] == "UTC+5.5"
    assert chart["account_note"] == "Free questions left: 2"
    assert chart["Yoga Detection"] == ["Budha-Aditya Yoga: Sun and Mercury in same sign",
                                       "Parivartan Yoga: Jupiter and Venus are in mutual sign exchange"]
    assert chart["Current Dasha"]["md"].startswith("Mars")
    g = chart["Current Transit / Gochar"]
    assert g["Saturn"]["where"] == "Pisces, H8 / M12" and g["Saturn"]["over_natal"] == ["Venus"]
    assert g["Shani Sade Sathi, Dhaiya, Kantak Shani"]["now"]["sade_sathi"] == "Yes - 1st Phase"
    assert chart["Shani Sade Sathi, Dhaiya, Kantak Shani"]["sade_sathi"] == "Yes - 1st Phase"
    calls = dict((name, body) for name, body in state["api_calls"])
    assert calls["kundali"]["settings"]["observation_point"] == "topocentric"        # as n8n
    assert calls["D9"]["config"]["observation_point"] == "geocentric"                # as n8n
    assert calls["kundali"]["hours"] == 12 and calls["kundali"]["timezone"] == 5.5
    (row,) = state["inserted"]
    assert row[2] == "horoscope" and row[4] is False and row[7] is False   # bot, was_paid, is_repeat
    assert state["completed"][0][0] == 1 and state["released"] == []


def test_quota_exhausted_is_neutral_and_uncharged(state, live_client) -> None:
    state["free_used"] = 3
    client = live_client
    if True:
        res = _result(_call(client, "get_birth_chart", BIRTH))
    assert res["isError"] is True
    text = res["content"][0]["text"]
    assert "INSUFFICIENT_CREDITS:" in text and "/plans" in text
    message = text.split("INSUFFICIENT_CREDITS:", 1)[1].lower()
    for word in ("$", "buy", "upgrade", "price", "subscribe"):
        assert word not in message
    assert state["api_calls"] == [] and state["inserted"] == []


def test_duplicate_is_logged_as_repeat(state, live_client) -> None:
    state["free_used"] = 3
    state["duplicate"] = 1
    client = live_client
    if True:
        res = _result(_call(client, "get_birth_chart", BIRTH))
    assert not res.get("isError")
    assert state["inserted"][0][7] is True       # is_repeat


def test_rate_limited(state, live_client) -> None:
    state["recent"] = 30
    client = live_client
    if True:
        res = _result(_call(client, "get_birth_chart", BIRTH))
    assert "RATE_LIMITED:" in res["content"][0]["text"]


def test_invalid_input(state, live_client) -> None:
    client = live_client
    if True:
        res = _result(_call(client, "get_birth_chart", {**BIRTH, "birth_date": "1990-02-30"}))
    assert "INVALID_INPUT:" in res["content"][0]["text"]
    assert state["api_calls"] == []


def test_input_formats_are_strict_and_uncharged(state, live_client) -> None:
    r = live_client.post("/mcp", headers=HEADERS, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = {t["name"]: t for t in r.json()["result"]["tools"]}
    props = tools["get_birth_chart"]["inputSchema"]["properties"]
    assert props["birth_date"]["pattern"] and props["birth_time"]["pattern"]
    assert "pattern" in str(tools["get_transits"]["inputSchema"]["properties"]["from_date"])
    for bad in ({"birth_time": "9:05"}, {"birth_time": "12:38 PM"}, {"birth_date": "21/05/1982"}):
        res = _result(_call(live_client, "get_birth_chart", {**BIRTH, **bad}))
        assert res["isError"] is True and next(iter(bad)) in res["content"][0]["text"]
    assert state["api_calls"] == [] and state["inserted"] == []


def test_gpt_token_rejected_at_mcp(state, live_client) -> None:
    client = live_client
    if True:
        r = client.post("/mcp", headers={**HEADERS, "Authorization": "Bearer gpt-token"},
                        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401


def test_backend_failure_releases_reservation(state, live_client) -> None:
    state["api_down"] = True
    res = _result(_call(live_client, "get_birth_chart", BIRTH))
    assert "BACKEND_UNAVAILABLE:" in res["content"][0]["text"]
    assert len(state["inserted"]) == 1 and state["released"] == [1]


def test_incomplete_chart_not_charged(state, live_client) -> None:
    k = copy.deepcopy(_out("planets_extended"))
    del k["Saturn"]
    state["kundali_override"] = k
    res = _result(_call(live_client, "get_birth_chart", BIRTH))
    assert "BACKEND_UNAVAILABLE:" in res["content"][0]["text"]
    assert state["released"] == [1]


def test_dst_gap_rejected(state, live_client, monkeypatch) -> None:
    async def melbourne(q):
        return Place(name="Melbourne, Victoria, Australia", latitude=-37.81, longitude=144.96,
                     tz_name="Australia/Melbourne")
    monkeypatch.setattr(srv, "resolve_place", melbourne)
    res = _result(_call(live_client, "get_birth_chart",
                        {**BIRTH, "birth_date": "2026-10-04", "birth_time": "02:30"}))
    assert "INVALID_INPUT:" in res["content"][0]["text"]
    assert state["inserted"] == []


def test_general_reading_adds_shadbala_and_moon_chart(state, live_client, monkeypatch) -> None:
    import app.mcp.astro_api as api

    async def sb(body, deadline=None):
        return {p: {"rupas": 6.0, "strength_pct": 110.0, "ishta": 30.0, "kashta": 10.0}
                for p in ("Sun", "Moon", "Mars", "Mercury", "Jupiter", "Venus", "Saturn")}
    monkeypatch.setattr(api, "shadbala", sb)
    chart = _result(_call(live_client, "get_birth_chart", {**BIRTH, "query_type": "general"}))["structuredContent"]
    assert chart["Shadbala"]["planets"]["Sun"]["rupas"] == 6.0
    assert "Ashtakavarga" not in chart
    assert chart["Moon Chart (Chandra Lagna)"]["lagna"] == "Aries"
    assert "unavailable_charts" not in chart          # D9/D10 already in the main chart

    async def down(body, deadline=None):
        raise api.AstroApiError("down")
    monkeypatch.setattr(api, "shadbala", down)
    chart = _result(_call(live_client, "get_birth_chart", {**BIRTH, "query_type": "general"}))["structuredContent"]
    assert chart["unavailable_charts"] == ["Shadbala"] and "Janam Kundali" in chart


def test_every_topic_gets_the_moon_chart(state, live_client) -> None:
    chart = _result(_call(live_client, "get_birth_chart", {**BIRTH, "query_type": "career"}))["structuredContent"]
    assert chart["Moon Chart (Chandra Lagna)"]["lagna"] == "Aries"
    assert "Shadbala" not in chart                    # Shadbala (an API call) stays with 'general' only


def test_children_topic_adds_d7_or_reports_unavailable(state, live_client, monkeypatch) -> None:
    import app.mcp.astro_api as api

    chart = _result(_call(live_client, "get_birth_chart", {**BIRTH, "query_type": "children"}))["structuredContent"]
    assert chart["unavailable_charts"] == ["D7"]
    assert chart["Janam Kundali"]["lagna"]["sign"] == "Leo"          # main chart still returned

    async def d7(chart, body, deadline=None):
        return [{"name": n, "current_sign": i + 1, "house_number": i + 1} for i, n in enumerate(
            ["Ascendant", "Sun", "Moon", "Mars", "Mercury", "Jupiter", "Venus", "Saturn", "Rahu", "Ketu"])]
    monkeypatch.setattr(api, "varga", d7)
    chart = _result(_call(live_client, "get_birth_chart", {**BIRTH, "query_type": "children"}))["structuredContent"]
    assert chart["D7"]["Lagna"] == "Aries" and chart["D7"]["Sun"] == "Taurus (2)"


def test_divisional_chart_follow_up_is_not_counted(state, live_client, monkeypatch) -> None:
    import app.mcp.astro_api as api

    async def d60(chart, body, deadline=None):
        return [{"name": n, "current_sign": 5, "house_number": 1} for n in
                ["Ascendant", "Sun", "Moon", "Mars", "Mercury", "Jupiter", "Venus", "Saturn", "Rahu", "Ketu"]]
    monkeypatch.setattr(api, "varga", d60)
    state["free_used"] = 3
    state["follow_up"] = 1
    res = _result(_call(live_client, "get_divisional_chart", {**{k: v for k, v in BIRTH.items() if k != "query_type"},
                                                               "charts": ["D60"]}))
    assert not res.get("isError"), res
    assert res["structuredContent"]["charts"]["D60"]["Lagna"] == "Leo"
    assert "no extra question" in res["structuredContent"]["account_note"]
    assert state["inserted"][0][7] is True and state["inserted"][0][8]     # not counted; subject hash stored


def test_divisional_chart_without_prior_question_needs_quota(state, live_client) -> None:
    state["free_used"] = 3
    res = _result(_call(live_client, "get_divisional_chart", {**{k: v for k, v in BIRTH.items() if k != "query_type"},
                                                               "charts": ["D60"]}))
    assert "INSUFFICIENT_CREDITS:" in res["content"][0]["text"]


def test_transits_period(state, live_client) -> None:
    today = datetime.now(timezone.utc).date()
    args = {**{k: v for k, v in BIRTH.items() if k != "query_type"},
            "from_date": today.isoformat(), "to_date": (today + timedelta(days=400)).isoformat()}
    res = _result(_call(live_client, "get_transits", args))
    assert not res.get("isError"), res
    out = res["structuredContent"]
    assert out["houses"].startswith("H = house from Lagna (Leo)")
    sat = out["Saturn"]["periods"]
    assert sat[0]["where"] == "Pisces, H8 / M12" and sat[-1]["where"].startswith("Aries, H9 / M1")
    assert {"date": "2027-03-14", "to": "Rahu / Rahu"} in out["dasha_changes"]
    assert [c[0] for c in state["api_calls"]] in (["kundali", "dasha_table"], ["dasha_table", "kundali"])   # 2 calls only


def test_prashna_uses_two_calls(state, live_client) -> None:
    res = _result(_call(live_client, "get_prashna_chart", {"city_country": "Delhi, India", "query_type": "career"}))
    assert not res.get("isError"), res
    chart = res["structuredContent"]
    assert chart["Prashna Kundali"]["lagna"]["sign"] == "Leo" and chart["moment"]
    assert sorted(c[0] for c in state["api_calls"]) == ["dasha_now", "kundali"]
