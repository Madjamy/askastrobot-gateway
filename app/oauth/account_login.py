"""Plugin (MCP) sign-in with the AskAstroBot website account.

Flow for the ChatGPT plugin connector (the legacy Custom GPT flow is untouched):

    ChatGPT -> /oauth/authorize (validated, session + signed state cookie)
            -> /oauth/login            sign-in page: Google or email + password
            -> Google:   /oauth/login/google -> Supabase Auth -> /oauth/supabase-callback
               Password: POST /oauth/login/password
            -> account found/linked in gw_users by supabase_user_id
            -> one-time code -> back to ChatGPT (with RFC 9207 iss)

The account is the website's Supabase Auth user, so it is independent of the
user's ChatGPT login, and a plan bought on askastrobot.com applies here.
"""
from __future__ import annotations

import html
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from app.db import get_pool
from app.logging_setup import get_logger
from app.oauth import supabase_auth
from app.oauth.authorize import _OAUTH_CODE_TTL_SECONDS, _STATE_COOKIE_NAME, _verify_state_cookie
from app.settings import get_settings

router = APIRouter()
log = get_logger(__name__)

MAX_PASSWORD_ATTEMPTS = 5            # per sign-in session
_IP_LIMIT, _EMAIL_LIMIT, _WINDOW_SECONDS = 20, 5, 900
_attempts: dict[str, list[float]] = {}


def _throttled(key: str, limit: int) -> bool:
    """In-memory sliding window (single gateway container); also protects the shared
    Supabase rate-limit bucket from one abusive client."""
    now = time.monotonic()
    recent = [t for t in _attempts.get(key, []) if now - t < _WINDOW_SECONDS]
    if len(recent) >= limit:
        _attempts[key] = recent
        return True
    if key not in _attempts and len(_attempts) >= 50_000:
        # Prune expired windows; if still full, fail closed rather than forgetting
        # existing counters (flooding with fresh keys must not reset anyone's limit).
        for k in [k for k, ts in _attempts.items() if not ts or now - ts[-1] >= _WINDOW_SECONDS]:
            del _attempts[k]
        if len(_attempts) >= 50_000:
            return True
    recent.append(now)
    _attempts[key] = recent
    return False

_MESSAGES = {
    "invalid_credentials": "That email and password didn't match an AskAstroBot account.",
    "email_not_confirmed": "Please confirm your email address first (check your inbox), then sign in.",
    "unavailable": "Sign-in is temporarily unavailable. Please try again in a moment.",
    "invalid_response": "Sign-in failed. Please try again.",
    "too_many_attempts": "Too many attempts. Please wait 15 minutes, then start the connection again from ChatGPT.",
    "google_failed": "Google sign-in didn't complete. Please try again.",
    "account_conflict": "This email is already linked to another AskAstroBot account. Please contact support.",
}

_PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    # form-action also governs the redirect after the form POST (Chromium), so the
    # ChatGPT callback host must be allowed or the final hop is blocked.
    "Content-Security-Policy": ("default-src 'none'; style-src 'unsafe-inline'; "
                                "form-action 'self' https://chatgpt.com; frame-ancestors 'none'"),
}


# ----------------------------------------------------------------- helpers ---

def _state_from_cookie(request: Request) -> str:
    cookie = request.cookies.get(_STATE_COOKIE_NAME) or ""
    state = cookie.rsplit(".", 1)[0] if "." in cookie else ""
    if not state or not _verify_state_cookie(cookie, state):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing_or_invalid_state_cookie")
    return state


async def _load_mcp_session(state: str) -> Any:
    pool = get_pool()
    async with pool.acquire() as conn:
        sess = await conn.fetchrow(
            """
            SELECT client_id, redirect_uri, scope, original_state, expires_at,
                   code_challenge, code_challenge_method, resource,
                   supabase_code_verifier, password_attempts
              FROM public.gw_oauth_authz_session
             WHERE state = $1
            """,
            state,
        )
    if sess is None or sess["resource"] is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unknown_state")
    if sess["expires_at"] < datetime.now(tz=timezone.utc):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "state_expired")
    return sess


def _render(message_key: str | None = None, email: str = "") -> HTMLResponse:
    s = get_settings()
    app = html.escape(s.app_base_url)
    msg = f'<p class="err" role="alert">{html.escape(_MESSAGES.get(message_key, ""))}</p>' if message_key else ""
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in to AskAstroBot</title>
<style>
 body{{margin:0;font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:#1a0a2e;color:#f0e6ff;display:flex;min-height:100vh;align-items:center;justify-content:center}}
 main{{width:100%;max-width:400px;margin:16px;padding:28px;border-radius:16px;background:rgba(255,255,255,.06);border:1px solid rgba(196,168,224,.2)}}
 h1{{font-size:1.4rem;margin:0 0 6px}} p{{color:#cbb8e6;font-size:.95rem;line-height:1.45}}
 .err{{color:#ffb4b4}} label{{display:block;font-size:.85rem;margin:14px 0 6px;color:#cbb8e6}}
 input{{width:100%;box-sizing:border-box;padding:11px;border-radius:10px;border:1px solid rgba(196,168,224,.35);background:rgba(0,0,0,.25);color:#fff;font-size:1rem}}
 .btn{{display:block;width:100%;box-sizing:border-box;text-align:center;padding:12px;border-radius:10px;font-weight:600;font-size:1rem;text-decoration:none;cursor:pointer;border:0;margin-top:16px}}
 .gold{{background:#d4af37;color:#1a0a2e}} .light{{background:#fff;color:#222}}
 .sep{{text-align:center;color:#9a88b5;margin:18px 0 0;font-size:.85rem}} .links{{margin-top:16px;font-size:.85rem}} a{{color:#e6c96a}}
 :focus-visible{{outline:3px solid #e6c96a;outline-offset:2px}}
</style></head><body><main>
<h1>Sign in to AskAstroBot</h1>
<p>Connect your AskAstroBot account to ChatGPT. It doesn't need to match your ChatGPT login; any plan on this account will work in ChatGPT.</p>
{msg}
<a class="btn light" href="/oauth/login/google">Continue with Google</a>
<p class="sep">or use your AskAstroBot email and password</p>
<form method="post" action="/oauth/login/password">
 <label for="email">Email</label><input id="email" name="email" type="email" autocomplete="email" required value="{html.escape(email)}">
 <label for="password">Password</label><input id="password" name="password" type="password" autocomplete="current-password" required>
 <button class="btn gold" type="submit">Sign in</button>
</form>
<p class="links"><a href="{app}/auth" target="_blank" rel="noopener">Create an account</a> &middot; <a href="{app}/auth" target="_blank" rel="noopener">Forgot password?</a></p>
</main></body></html>"""
    return HTMLResponse(page, headers=_PAGE_HEADERS)


async def _complete(state: str, sess: Any, identity: supabase_auth.AccountIdentity) -> RedirectResponse:
    """Find or link the gw_users row by website account id, issue the one-time code."""
    settings = get_settings()
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            user_id = await conn.fetchval(
                "UPDATE public.gw_users SET last_seen_at = NOW() WHERE supabase_user_id = $1 RETURNING id",
                identity.supabase_user_id,
            )
            if user_id is None:
                # Existing gateway user with this verified email but not yet linked.
                user_id = await conn.fetchval(
                    """
                    UPDATE public.gw_users
                       SET supabase_user_id = $1, name = COALESCE(name, $3), last_seen_at = NOW()
                     WHERE email = $2 AND supabase_user_id IS NULL
                    RETURNING id
                    """,
                    identity.supabase_user_id, identity.email, identity.name,
                )
            if user_id is None:
                user_id = await conn.fetchval(
                    """
                    INSERT INTO public.gw_users (email, name, signup_source, supabase_user_id, last_seen_at)
                    VALUES ($1, $2, 'mcp', $3, NOW())
                    ON CONFLICT (email) DO NOTHING
                    RETURNING id
                    """,
                    identity.email, identity.name, identity.supabase_user_id,
                )
            if user_id is None:
                log.warning("account.email_linked_to_other_account")
                return _render("account_conflict")  # type: ignore[return-value]

            code = secrets.token_urlsafe(48)
            await conn.execute(
                """
                INSERT INTO public.gw_oauth_codes
                    (code, user_id, redirect_uri, scope, expires_at,
                     client_id, code_challenge, code_challenge_method, resource)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                """,
                code, user_id, sess["redirect_uri"], sess["scope"],
                datetime.now(tz=timezone.utc) + timedelta(seconds=_OAUTH_CODE_TTL_SECONDS),
                sess["client_id"], sess["code_challenge"], sess["code_challenge_method"], sess["resource"],
            )
            await conn.execute("DELETE FROM public.gw_oauth_authz_session WHERE state = $1", state)

    qs = urlencode({"code": code, "state": sess["original_state"], "iss": settings.gateway_base_url})
    response = RedirectResponse(url=f"{sess['redirect_uri']}?{qs}", status_code=302)
    response.delete_cookie(_STATE_COOKIE_NAME, path="/oauth")
    log.info("account.connected", user_id=str(user_id))
    return response


# ------------------------------------------------------------------ routes ---

@router.get("/oauth/login")
async def login_page(request: Request):
    state = _state_from_cookie(request)
    await _load_mcp_session(state)
    return _render(request.query_params.get("error"))


@router.get("/oauth/login/google")
async def login_google(request: Request):
    state = _state_from_cookie(request)
    await _load_mcp_session(state)
    verifier, challenge = supabase_auth.new_pkce_pair()
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE public.gw_oauth_authz_session SET supabase_code_verifier = $2 WHERE state = $1",
            state, verifier,
        )
    redirect_to = f"{get_settings().gateway_base_url}/oauth/supabase-callback"
    try:
        url = supabase_auth.google_authorize_url(redirect_to, challenge)
    except supabase_auth.AccountLoginError:
        return _render("unavailable")
    return RedirectResponse(url=url, status_code=302)


@router.get("/oauth/supabase-callback")
async def supabase_callback(request: Request):
    state = _state_from_cookie(request)
    sess = await _load_mcp_session(state)
    code = request.query_params.get("code", "")
    if not code or not sess["supabase_code_verifier"]:
        return RedirectResponse(url="/oauth/login?error=google_failed", status_code=302)
    pool = get_pool()
    async with pool.acquire() as conn:   # one use only
        await conn.execute(
            "UPDATE public.gw_oauth_authz_session SET supabase_code_verifier = NULL WHERE state = $1", state,
        )
    try:
        identity = await supabase_auth.exchange_google_code(code, sess["supabase_code_verifier"])
    except supabase_auth.AccountLoginError as exc:
        key = exc.kind if exc.kind in ("email_not_confirmed", "unavailable") else "google_failed"
        return RedirectResponse(url=f"/oauth/login?error={key}", status_code=302)
    return await _complete(state, sess, identity)


@router.post("/oauth/login/password")
async def login_password(request: Request):
    state = _state_from_cookie(request)
    sess = await _load_mcp_session(state)
    form = await request.form()
    email = str(form.get("email") or "").strip()[:254]
    password = str(form.get("password") or "")[:256]
    if not email or not password:
        return _render("invalid_credentials", email)
    client_ip = request.client.host if request.client else "unknown"
    if _throttled(f"ip:{client_ip}", _IP_LIMIT) or _throttled(f"email:{email.lower()}", _EMAIL_LIMIT):
        log.warning("account.password_throttled")
        return _render("too_many_attempts")
    pool = get_pool()
    async with pool.acquire() as conn:
        allowed = await conn.fetchval(
            """
            UPDATE public.gw_oauth_authz_session
               SET password_attempts = password_attempts + 1
             WHERE state = $1 AND password_attempts < $2
            RETURNING password_attempts
            """,
            state, MAX_PASSWORD_ATTEMPTS,
        )
    if allowed is None:
        return _render("too_many_attempts")
    try:
        identity = await supabase_auth.password_login(email, password)
    except supabase_auth.AccountLoginError as exc:
        return _render(exc.kind, email)
    return await _complete(state, sess, identity)
