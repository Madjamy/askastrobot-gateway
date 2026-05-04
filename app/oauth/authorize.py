"""OAuth /authorize and /google-callback endpoints.

The gateway is an OAuth provider TO ChatGPT and an OAuth client OF Google.
We do not delegate to Supabase Auth — we hit Google's OAuth 2 endpoints
directly via authlib, which keeps the flow standard and well-tested.

Flow:
1. ChatGPT redirects to /oauth/authorize?client_id=...&redirect_uri=...&state=...
2. We validate inputs, set a signed `gw_oauth_state` cookie binding the user
   agent to this flow, stash the original ChatGPT state in gw_oauth_authz_session,
   then 302 to Google's /o/oauth2/v2/auth.
3. Google authenticates the user and redirects to /oauth/google-callback?code=...&state=...
4. We require the cookie to match the state (CSRF binding), exchange the code
   with Google for an ID token, upsert the user, mint a one-time
   gw_oauth_codes row, redirect back to ChatGPT's redirect_uri with the original state.
5. ChatGPT exchanges the code at /oauth/token (in token.py).

Errors during the flow always clean up the session row before raising.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import RedirectResponse

from app.db import get_pool
from app.logging_setup import get_logger
from app.oauth.redirect_uri import is_valid_chatgpt_redirect
from app.settings import get_settings

router = APIRouter()
log = get_logger(__name__)

_AUTHZ_SESSION_TTL_SECONDS = 600   # 10 min — long enough for the user to pick a Google account
_OAUTH_CODE_TTL_SECONDS = 300      # 5 min  — short window for ChatGPT to exchange the code
_STATE_COOKIE_NAME = "gw_oauth_state"

GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


def _google_redirect_uri() -> str:
    return f"{get_settings().gateway_base_url}/oauth/google-callback"


def _sign_state(state: str) -> str:
    """HMAC-sign the cookie value so it can't be forged by a browser."""
    secret = get_settings().gateway_jwt_secret.encode()
    sig = hmac.new(secret, state.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{state}.{sig}"


def _verify_state_cookie(cookie_value: str | None, expected_state: str) -> bool:
    if not cookie_value:
        return False
    expected = _sign_state(expected_state)
    return hmac.compare_digest(cookie_value, expected)


@router.get("/oauth/authorize")
async def oauth_authorize(request: Request):
    settings = get_settings()
    params = request.query_params

    client_id = params.get("client_id", "")
    redirect_uri = params.get("redirect_uri", "")
    response_type = params.get("response_type", "")
    scope = params.get("scope", "read:astro write:query")
    chatgpt_state = params.get("state", "")

    # Per spec: only HTTP 401 triggers ChatGPT silent re-auth, so OAuth-flow
    # failures all return 401 (never 400/403).
    if response_type != "code":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unsupported_response_type")
    if not hmac.compare_digest(client_id, settings.oauth_client_id):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid_client")
    if not is_valid_chatgpt_redirect(redirect_uri):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "redirect_uri_not_allowed")
    if not chatgpt_state:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing_state")

    internal_state = secrets.token_urlsafe(48)
    expires_at = datetime.now(tz=timezone.utc) + timedelta(seconds=_AUTHZ_SESSION_TTL_SECONDS)

    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO public.gw_oauth_authz_session
                (state, client_id, redirect_uri, scope, original_state, expires_at)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            internal_state, client_id, redirect_uri, scope, chatgpt_state, expires_at,
        )

    qs = urlencode({
        "client_id": settings.google_client_id,
        "redirect_uri": _google_redirect_uri(),
        "response_type": "code",
        "scope": "openid email profile",
        "state": internal_state,
        "access_type": "online",
        "prompt": "select_account",
    })
    google_url = f"{GOOGLE_AUTHORIZE_URL}?{qs}"

    response = RedirectResponse(url=google_url, status_code=302)
    # CSRF binding: cookie holds signed state; callback must present matching cookie.
    response.set_cookie(
        key=_STATE_COOKIE_NAME,
        value=_sign_state(internal_state),
        max_age=_AUTHZ_SESSION_TTL_SECONDS,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/oauth",
    )
    log.info("oauth.authorize.redirect_to_google", state=internal_state)
    return response


@router.get("/oauth/google-callback")
async def oauth_google_callback(request: Request):
    settings = get_settings()
    params = request.query_params

    state = params.get("state", "")
    google_code = params.get("code", "")
    google_error = params.get("error", "")

    pool = get_pool()

    async def _consume_session(s: str):
        """Always drop the session row (success or failure) to prevent replay."""
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM public.gw_oauth_authz_session WHERE state = $1", s,
            )

    if google_error:
        if state:
            await _consume_session(state)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"google_error: {google_error}")
    if not state or not google_code:
        if state:
            await _consume_session(state)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing_state_or_code")

    # CSRF binding check: cookie must match state.
    state_cookie = request.cookies.get(_STATE_COOKIE_NAME)
    if not _verify_state_cookie(state_cookie, state):
        await _consume_session(state)
        log.warning("oauth.callback.state_cookie_mismatch")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "state_cookie_mismatch")

    async with pool.acquire() as conn:
        sess = await conn.fetchrow(
            """
            SELECT redirect_uri, scope, original_state, expires_at
              FROM public.gw_oauth_authz_session
             WHERE state = $1
            """,
            state,
        )
    if sess is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unknown_state")
    if sess["expires_at"] < datetime.now(tz=timezone.utc):
        await _consume_session(state)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "state_expired")

    # Exchange Google code for tokens (form-encoded).
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            token_resp = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "code": google_code,
                    "client_id": settings.google_client_id,
                    "client_secret": settings.google_client_secret,
                    "redirect_uri": _google_redirect_uri(),
                    "grant_type": "authorization_code",
                },
                headers={"Accept": "application/json"},
            )
    except httpx.HTTPError as exc:
        await _consume_session(state)
        log.error("oauth.google_token_network_error", error=str(exc))
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "google_unreachable")

    if token_resp.status_code != 200:
        await _consume_session(state)
        log.error(
            "oauth.google_token_exchange_failed",
            status=token_resp.status_code, body=token_resp.text[:500],
        )
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "google_token_exchange_failed")

    google_access = token_resp.json().get("access_token")
    if not google_access:
        await _consume_session(state)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "no_access_token")

    # Fetch userinfo (email, sub, name).
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            ui = await client.get(
                GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {google_access}"},
            )
    except httpx.HTTPError as exc:
        await _consume_session(state)
        log.error("oauth.google_userinfo_network_error", error=str(exc))
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "google_userinfo_unreachable")

    if ui.status_code != 200:
        await _consume_session(state)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "google_userinfo_failed")

    user_data = ui.json()
    email = (user_data.get("email") or "").lower()
    google_id = user_data.get("sub") or ""
    name = user_data.get("name")
    email_verified = user_data.get("email_verified", False)

    if not email or not google_id:
        await _consume_session(state)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "google_no_identity")
    if not email_verified:
        await _consume_session(state)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "google_email_unverified")

    # Upsert user + issue one-time oauth_code, then drop the session.
    async with pool.acquire() as conn:
        async with conn.transaction():
            user_row = await conn.fetchrow(
                """
                INSERT INTO public.gw_users (email, google_id, name, signup_source, last_seen_at)
                VALUES ($1, $2, $3, 'gpt', NOW())
                ON CONFLICT (email) DO UPDATE
                  SET google_id     = COALESCE(public.gw_users.google_id, EXCLUDED.google_id),
                      name          = COALESCE(public.gw_users.name, EXCLUDED.name),
                      last_seen_at  = NOW()
                RETURNING id
                """,
                email, google_id, name,
            )

            oauth_code = secrets.token_urlsafe(48)
            await conn.execute(
                """
                INSERT INTO public.gw_oauth_codes (code, user_id, redirect_uri, scope, expires_at)
                VALUES ($1, $2, $3, $4, $5)
                """,
                oauth_code,
                user_row["id"],
                sess["redirect_uri"],
                sess["scope"],
                datetime.now(tz=timezone.utc) + timedelta(seconds=_OAUTH_CODE_TTL_SECONDS),
            )

            await conn.execute(
                "DELETE FROM public.gw_oauth_authz_session WHERE state = $1", state,
            )

    qs = urlencode({"code": oauth_code, "state": sess["original_state"]})
    response = RedirectResponse(url=f"{sess['redirect_uri']}?{qs}", status_code=302)
    # Clear the state cookie now that the flow is complete.
    response.delete_cookie(_STATE_COOKIE_NAME, path="/oauth")
    log.info("oauth.callback.redirect_to_chatgpt", user_id=str(user_row["id"]))
    return response
