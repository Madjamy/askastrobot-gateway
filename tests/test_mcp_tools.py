"""End-to-end MCP calls through the mounted /mcp app (DB, n8n and geocoder faked)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import app.mcp.entitlement as ent
import app.mcp.server as srv
from app.mcp.geo import Place

BASE = "https://api.askastrobot.com"
FIX = Path(__file__).parent / "fixtures"
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
    s = {"recent": 0, "duplicate": None, "follow_up": None, "sub": None, "free_used": 0, "inserted": [], "n8n_bodies": [],
         "released": [], "completed": [], "n8n_file": "n8n_horoscope_sample.json"}
    pool = FakePool(s)
    monkeypatch.setattr(srv, "get_pool", lambda: pool)
    monkeypatch.setattr(ent, "get_pool", lambda: pool)

    async def fake_place(q):
        return Place(name="Delhi, India", latitude=28.6139, longitude=77.209, tz_name="Asia/Kolkata")

    async def fake_n8n(bot, body, ct):
        s["n8n_bodies"].append((bot, json.loads(body)))
        if s["n8n_file"] is None:
            from app.gpt.n8n import N8nForwardError
            raise N8nForwardError("down", kind="network")
        if isinstance(s["n8n_file"], bytes):
            return s["n8n_file"], "application/json", 10
        return (FIX / s["n8n_file"]).read_bytes(), "application/json", 1234

    monkeypatch.setattr(srv, "resolve_place", fake_place)
    monkeypatch.setattr(srv, "forward_to_n8n", fake_n8n)
    return s


def _call(client, name, args):
    return client.post("/mcp", headers=HEADERS, json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": args}})


def _result(resp):
    assert resp.status_code == 200, resp.text
    return resp.json()["result"]


BIRTH = {"birth_date": "1990-07-06", "birth_time": "14:30", "birth_place": "Delhi, India", "query_type": "career"}


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
    assert chart["Janam Kundali"]["lagna"]["sign"] == "Libra"
    assert chart["birth"]["utc_offset"] == "UTC+5.5"
    assert chart["account_note"] == "Free questions left: 2"
    bot, body = state["n8n_bodies"][0]
    assert bot == "horoscope" and body["timezone"] == 5.5 and body["hours"] == 14
    assert body["query_text"] == "[plugin] career"            # never the user's question
    (row,) = state["inserted"]
    assert row[2] == "horoscope" and row[4] is False and row[7] is False   # bot, was_paid, is_repeat
    assert state["completed"] == [(1, 1234)] and state["released"] == []


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
    assert state["n8n_bodies"] == [] and state["inserted"] == []


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
    assert state["n8n_bodies"] == []


def test_gpt_token_rejected_at_mcp(state, live_client) -> None:
    client = live_client
    if True:
        r = client.post("/mcp", headers={**HEADERS, "Authorization": "Bearer gpt-token"},
                        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401


def test_backend_failure_releases_reservation(state, live_client) -> None:
    state["n8n_file"] = None
    res = _result(_call(live_client, "get_birth_chart", BIRTH))
    assert "BACKEND_UNAVAILABLE:" in res["content"][0]["text"]
    assert len(state["inserted"]) == 1 and state["released"] == [1]


def test_incomplete_chart_not_charged(state, live_client) -> None:
    raw = json.loads((FIX / "n8n_horoscope_sample.json").read_text())
    del raw[0]["data"][0]["output"]["Saturn"]
    state["n8n_file"] = json.dumps(raw).encode()
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

    async def sb(body):
        return {p: {"rupas": 6.0, "strength_pct": 110.0, "ishta": 30.0, "kashta": 10.0}
                for p in ("Sun", "Moon", "Mars", "Mercury", "Jupiter", "Venus", "Saturn")}
    monkeypatch.setattr(api, "shadbala", sb)
    chart = _result(_call(live_client, "get_birth_chart", {**BIRTH, "query_type": "general"}))["structuredContent"]
    assert chart["Shadbala"]["planets"]["Sun"]["rupas"] == 6.0
    assert "Ashtakavarga" not in chart
    assert chart["Moon Chart (Chandra Lagna)"]["lagna"] == "Taurus"
    assert "unavailable_charts" not in chart          # D9/D10 already in the n8n bundle

    async def down(body):
        raise api.AstroApiError("down")
    monkeypatch.setattr(api, "shadbala", down)
    chart = _result(_call(live_client, "get_birth_chart", {**BIRTH, "query_type": "general"}))["structuredContent"]
    assert chart["unavailable_charts"] == ["Shadbala"] and "Janam Kundali" in chart


def test_children_topic_adds_d7_or_reports_unavailable(state, live_client, monkeypatch) -> None:
    import app.mcp.astro_api as api

    async def no_key(chart, body):
        raise api.AstroApiError("API key not configured")
    monkeypatch.setattr(api, "varga", no_key)
    chart = _result(_call(live_client, "get_birth_chart", {**BIRTH, "query_type": "children"}))["structuredContent"]
    assert chart["unavailable_charts"] == ["D7"]
    assert chart["Janam Kundali"]["lagna"]["sign"] == "Libra"        # main chart still returned

    async def d7(chart, body):
        return [{"name": n, "current_sign": i + 1, "house_number": i + 1} for i, n in enumerate(
            ["Ascendant", "Sun", "Moon", "Mars", "Mercury", "Jupiter", "Venus", "Saturn", "Rahu", "Ketu"])]
    monkeypatch.setattr(api, "varga", d7)
    chart = _result(_call(live_client, "get_birth_chart", {**BIRTH, "query_type": "children"}))["structuredContent"]
    assert chart["D7"]["Lagna"] == "Aries" and chart["D7"]["Sun"] == "Taurus (2)"


def test_divisional_chart_follow_up_is_not_counted(state, live_client, monkeypatch) -> None:
    import app.mcp.astro_api as api

    async def d60(chart, body):
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


def test_transits_period(state, live_client, monkeypatch) -> None:
    import app.mcp.astro_api as api

    async def planets(body):
        assert body["config"]["observation_point"] == "geocentric"
        sat = "Pisces" if (body["month"], body["date"]) < (3, 10) else "Aries"
        return {"Sun": ("Leo", False), "Moon": ("Libra", False), "Mars": ("Cancer", False),
                "Mercury": ("Libra", False), "Jupiter": ("Cancer", False), "Venus": ("Libra", True),
                "Saturn": (sat, False), "Rahu": ("Aquarius", False), "Ketu": ("Leo", False)}
    monkeypatch.setattr(api, "planet_signs", planets)
    args = {**{k: v for k, v in BIRTH.items() if k != "query_type"}, "from_date": "2027-01-01", "to_date": "2027-06-01"}
    res = _result(_call(live_client, "get_transits", args))
    out = res["structuredContent"]
    assert len(out["snapshots"]) == 6
    (ing,) = out["ingress"]
    assert (ing["date"], ing["planet"], ing["enters"], ing["from_lagna"], ing["from_moon"]) == (
        "2027-03-10", "Saturn", "Aries", 7, 12)
    assert out["snapshots"][3]["sade_sathi"] == "Yes – 1st Phase"      # Saturn 12th from Taurus Moon
    assert out["snapshots"][0]["planets"]["Saturn"] == "Pisces (6/11)"
