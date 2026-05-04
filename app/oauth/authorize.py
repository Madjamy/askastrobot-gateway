"""OAuth /authorize and /google-callback endpoints.

Flow:
1. ChatGPT redirects user to /oauth/authorize?client_id=...&redirect_uri=...&state=...
2. We validate inputs, stash the original ChatGPT state in oauth_authz_session,
   and redirect to Supabase Google sign-in (via Supabase's hosted /auth/v1/authorize).
3. After Google, Supabase redirects to /oauth/google-callback?code=... (Supabase auth code).
4. We exchange that with Supabase for the user identity, upsert into users,
   issue our own one-time oauth_codes row, and redirect back to ChatGPT's redirect_uri
   with ?code=...&state=<the original ChatGPT state>.
5. ChatGPT exchanges the code at /oauth/token (in token.py).
"""
from __future__ import annotations

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

_AUTHZ_SESSION_TTL_SECONDS = 600          # 10 min - long enough for the user to pick a Google account
_OAUTH_CODE_TTL_SECONDS = 300             # 5 min  - short window for ChatGPT to exchange the code


@router.get("/oauth/authorize")
async def oauth_authorize(request: Request) -> RedirectResponse:
    settings = get_settings()
    params = request.query_params

    client_id = params.get("client_id", "")
    redirect_uri = params.get("redirect_uri", "")
    response_type = params.get("response_type", "")
    scope = params.get("scope", "read:astro write:query")
    chatgpt_state = params.get("state", "")

    if response_type != "code":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Only response_type=code is supported")
    if client_id != settings.oauth_client_id:
        # 401 keeps in line with our OAuth-error policy (only 401 triggers ChatGPT silent re-auth).
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid client_id")
    if not is_valid_chatgpt_redirect(redirect_uri):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "redirect_uri not allowed")
    if not chatgpt_state:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "state is required")

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

    supabase_authorize = settings.supabase_url.rstrip("/") + "/auth/v1/authorize"
    redirect_to = settings.supabase_google_callback_url
    qs = urlencode({
        "provider": "google",
        "redirect_to": f"{redirect_to}?gw_state={internal_state}",
    })
    log.info("oauth.authorize.redirect_to_supabase", state=internal_state)
    return RedirectResponse(url=f"{supabase_authorize}?{qs}", status_code=302)


@router.get("/oauth/google-callback")
async def oauth_google_callback(request: Request) -> RedirectResponse:
    """Supabase redirects back here after Google sign-in.

    Supabase delivers the session via URL fragment / query depending on flow.
    For the implicit flow Supabase uses by default, we receive ?code=... that
    we exchange for a session at `/auth/v1/token?grant_type=pkce` style.
    Here we use the "PKCE-less" Supabase REST exchange via /auth/v1/user with
    the `access_token` Supabase puts on the URL.

    This handler implements the simplest path: Supabase puts ?code=<code>&gw_state=<state>
    on the URL, we POST to /auth/v1/token?grant_type=authorization_code to get the user.
    """
    settings = get_settings()
    params = request.query_params

    gw_state = params.get("gw_state", "")
    supabase_code = params.get("code", "")
    if not gw_state or not supabase_code:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Missing gw_state or code")

    pool = get_pool()
    async with pool.acquire() as conn:
        sess = await conn.fetchrow(
            """
            SELECT client_id, redirect_uri, scope, original_state, expires_at
              FROM public.gw_oauth_authz_session
             WHERE state = $1
            """,
            gw_state,
        )
    if sess is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown gw_state")
    if sess["expires_at"] < datetime.now(tz=timezone.utc):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "gw_state expired")

    # Exchange Supabase code for the user identity.
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{settings.supabase_url.rstrip('/')}/auth/v1/token",
            params={"grant_type": "pkce"},
            headers={"apikey": settings.supabase_anon_key, "Content-Type": "application/json"},
            json={"auth_code": supabase_code},
        )
    if resp.status_code != 200:
        log.error("oauth.supabase_exchange_failed", status=resp.status_code, body=resp.text[:500])
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Supabase exchange failed")

    sb_data = resp.json()
    sb_user = sb_data.get("user") or {}
    email = (sb_user.get("email") or "").lower()
    google_id = (sb_user.get("user_metadata") or {}).get("provider_id") or sb_user.get("id")
    name = (sb_user.get("user_metadata") or {}).get("full_name")

    if not email:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Supabase returned no email")

    # Upsert user.
    async with pool.acquire() as conn:
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
        user_id = str(user_row["id"])

        # Issue one-time oauth_codes row.
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

        # One-shot use of the gw_state - delete it so it can't be replayed.
        await conn.execute("DELETE FROM public.gw_oauth_authz_session WHERE state = $1", gw_state)

    # Redirect to ChatGPT with the original state echoed back.
    qs = urlencode({"code": oauth_code, "state": sess["original_state"]})
    log.info("oauth.authorize.redirect_to_chatgpt", user_id=user_id)
    return RedirectResponse(url=f"{sess['redirect_uri']}?{qs}", status_code=302)
