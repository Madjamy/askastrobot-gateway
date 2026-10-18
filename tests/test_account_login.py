"""Plugin sign-in with the AskAstroBot website account (Supabase Auth), DB and Supabase faked."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

import app.oauth.account_login as al
import app.oauth.authorize as az
from app.main import app
from app.oauth import supabase_auth
from app.oauth.authorize import _sign_state

BASE = "https://api.askastrobot.com"
STATE = "internal-state-123"
CHATGPT_REDIRECT = "https://chatgpt.com/connector_platform_oauth_redirect"


class _Ctx:
    def __init__(self, v):
        self.v = v

    async def __aenter__(self):
        return self.v

    async def __aexit__(self, *a):
        return False


class FakeConn:
    def __init__(self, s):
        self.s = s

    def transaction(self):
        return _Ctx(None)

    async def fetchrow(self, sql, *args):
        assert "gw_oauth_authz_session" in sql
        return self.s["session"] if args[0] == STATE else None

    async def fetchval(self, sql, *args):
        self.s["fetchval"].append(sql.split()[0] + ":" + ("supabase_user_id = $1" in sql and "by_id" or
                                                           "email = $2" in sql and "by_email" or "insert"))
        if "WHERE supabase_user_id = $1" in sql:
            return self.s["by_id"]
        if "WHERE email = $2 AND supabase_user_id IS NULL" in sql:
            return self.s["by_email"]
        if "password_attempts + 1" in sql:
            if self.s["session"]["password_attempts"] >= args[1]:
                return None
            self.s["session"]["password_attempts"] += 1
            return self.s["session"]["password_attempts"]
        if "INSERT INTO public.gw_users" in sql:
            self.s["inserted_user"] = args
            return self.s["insert_returns"]
        raise AssertionError(sql)

    async def execute(self, sql, *args):
        self.s["executed"].append((sql.split()[0], sql, args))


class FakePool:
    def __init__(self, s):
        self.s = s

    def acquire(self):
        return _Ctx(FakeConn(self.s))


@pytest.fixture
def s(monkeypatch):
    al._attempts.clear()
    state = {
        "session": {
            "client_id": "https://chatgpt.com/oauth/client.json", "redirect_uri": CHATGPT_REDIRECT,
            "scope": "read:astro write:query", "original_state": "chatgpt-state",
            "expires_at": datetime.now(tz=timezone.utc) + timedelta(minutes=5),
            "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM", "code_challenge_method": "S256",
            "resource": f"{BASE}/mcp", "supabase_code_verifier": "stored-verifier", "password_attempts": 0,
        },
        "by_id": None, "by_email": None, "insert_returns": "new-user-id",
        "fetchval": [], "executed": [], "calls": [],
    }
    pool = FakePool(state)
    monkeypatch.setattr(al, "get_pool", lambda: pool)
    monkeypatch.setattr(az, "get_pool", lambda: pool)
    monkeypatch.setattr(al.get_settings(), "supabase_url", "https://proj.supabase.co")
    monkeypatch.setattr(al.get_settings(), "supabase_anon_key", "anon-key")
    return state


def _client(with_cookie=True):
    c = TestClient(app, base_url=BASE, follow_redirects=False)
    if with_cookie:
        c.cookies.set("gw_oauth_state", _sign_state(STATE), domain="api.askastrobot.com", path="/oauth")
    return c


def _identity(uid="sb-user-1", email="seeker@example.com"):
    return supabase_auth.AccountIdentity(supabase_user_id=uid, email=email, name="Seeker")


def test_mcp_authorize_goes_to_account_login_and_gpt_still_to_google(s) -> None:
    c = _client(with_cookie=False)
    r = c.get("/oauth/authorize", params={
        "client_id": "https://chatgpt.com/oauth/client.json", "redirect_uri": CHATGPT_REDIRECT,
        "response_type": "code", "state": "cs", "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        "code_challenge_method": "S256", "resource": f"{BASE}/mcp"})
    assert r.status_code == 302 and r.headers["location"] == "/oauth/login"
    assert "gw_oauth_state" in r.headers.get("set-cookie", "")
    r = c.get("/oauth/authorize", params={
        "client_id": "test-client-id", "redirect_uri": "https://chatgpt.com/aip/g-abc/oauth/callback",
        "response_type": "code", "state": "cs"})
    assert r.status_code == 302 and r.headers["location"].startswith("https://accounts.google.com/")


def test_login_page_requires_cookie_and_renders_safely(s) -> None:
    assert _client(with_cookie=False).get("/oauth/login").status_code == 401
    r = _client().get("/oauth/login", params={"error": "invalid_credentials"})
    assert r.status_code == 200
    assert "Continue with Google" in r.text and "doesn't need to match your ChatGPT login" in r.text
    assert "didn&#x27;t match" in r.text or "didn't match" in r.text
    assert r.headers["x-frame-options"] == "DENY" and r.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


def test_password_wrong_then_locked(s, monkeypatch) -> None:
    async def bad(email, password):
        raise supabase_auth.AccountLoginError("invalid_credentials")
    monkeypatch.setattr(supabase_auth, "password_login", bad)
    r = _client().post("/oauth/login/password", data={"email": "a@b.com", "password": "x"})
    assert r.status_code == 200 and "match an AskAstroBot account" in r.text
    assert s["session"]["password_attempts"] == 1
    s["session"]["password_attempts"] = 5
    r = _client().post("/oauth/login/password", data={"email": "a@b.com", "password": "x"})
    assert "Too many attempts" in r.text


def test_password_success_creates_account_and_returns_code(s, monkeypatch) -> None:
    async def ok(email, password):
        return _identity()
    monkeypatch.setattr(supabase_auth, "password_login", ok)
    r = _client().post("/oauth/login/password", data={"email": "Seeker@Example.com", "password": "pw"})
    assert r.status_code == 302
    loc = urlparse(r.headers["location"])
    q = parse_qs(loc.query)
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == CHATGPT_REDIRECT
    assert q["state"] == ["chatgpt-state"] and q["iss"] == [BASE] and len(q["code"][0]) > 40
    assert s["inserted_user"][2] == "sb-user-1"                      # supabase_user_id stored
    code_insert = [a for kind, sql, a in s["executed"] if "gw_oauth_codes" in sql][0]
    assert code_insert[5] == "https://chatgpt.com/oauth/client.json" and code_insert[8] == f"{BASE}/mcp"
    assert any("DELETE FROM public.gw_oauth_authz_session" in sql for _, sql, _ in s["executed"])


def test_existing_gateway_user_is_linked_by_email(s, monkeypatch) -> None:
    async def ok(email, password):
        return _identity()
    monkeypatch.setattr(supabase_auth, "password_login", ok)
    s["by_email"] = "old-gpt-user-id"
    r = _client().post("/oauth/login/password", data={"email": "seeker@example.com", "password": "pw"})
    assert r.status_code == 302 and "inserted_user" not in s
    code_insert = [a for kind, sql, a in s["executed"] if "gw_oauth_codes" in sql][0]
    assert code_insert[1] == "old-gpt-user-id"


def test_google_round_trip(s, monkeypatch) -> None:
    r = _client().get("/oauth/login/google")
    assert r.status_code == 302
    loc = urlparse(r.headers["location"])
    q = parse_qs(loc.query)
    assert loc.netloc == "proj.supabase.co" and loc.path == "/auth/v1/authorize"
    assert q["provider"] == ["google"] and q["code_challenge_method"] == ["s256"]
    assert q["redirect_to"] == [f"{BASE}/oauth/supabase-callback"]
    stored = [a for _, sql, a in s["executed"] if "supabase_code_verifier = $2" in sql][0][1]
    assert len(stored) >= 43

    seen = {}

    async def exchange(code, verifier):
        seen["args"] = (code, verifier)
        return _identity()
    monkeypatch.setattr(supabase_auth, "exchange_google_code", exchange)
    r = _client().get("/oauth/supabase-callback", params={"code": "sb-code"})
    assert r.status_code == 302 and r.headers["location"].startswith(CHATGPT_REDIRECT)
    assert seen["args"] == ("sb-code", "stored-verifier")


def test_unconfirmed_email_rejected() -> None:
    with pytest.raises(supabase_auth.AccountLoginError) as e:
        supabase_auth._identity_from_token_response({"user": {"id": "u", "email": "a@b.com"}})
    assert e.value.kind == "email_not_confirmed"
    ident = supabase_auth._identity_from_token_response(
        {"user": {"id": "u", "email": "A@B.com", "email_confirmed_at": "2026-01-01", "user_metadata": {"full_name": "A"}}})
    assert ident.email == "a@b.com" and ident.name == "A"


def test_throttle_is_not_reset_by_flooding(monkeypatch) -> None:
    al._attempts.clear()
    for _ in range(al._EMAIL_LIMIT):
        assert al._throttled("email:victim@example.com", al._EMAIL_LIMIT) is False
    assert al._throttled("email:victim@example.com", al._EMAIL_LIMIT) is True
    monkeypatch.setattr(al, "_attempts", {**al._attempts, **{f"email:x{i}": [10**12] for i in range(50_000)}})
    assert al._throttled("email:new@example.com", al._EMAIL_LIMIT) is True        # fail closed when full
    assert al._throttled("email:victim@example.com", al._EMAIL_LIMIT) is True      # still limited
