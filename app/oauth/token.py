"""OAuth /token endpoint.

Receives form-encoded body (NOT JSON — ChatGPT requirement).
Supports grant_type=authorization_code and grant_type=refresh_token.
Returns HTTP 401 on any failure (only 401 triggers ChatGPT silent re-auth).

We bypass FastAPI's Form() validation (which would 422 on missing fields)
and parse the body manually so EVERY failure mode returns 401.
"""
from __future__ import annotations

import hmac
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from app.db import get_pool
from app.logging_setup import get_logger
from app.settings import get_settings

router = APIRouter()
log = get_logger(__name__)

# Window during which a just-rotated refresh token still validates and returns
# the new pair (handles ChatGPT-side network retries without forcing re-auth).
_REFRESH_GRACE_SECONDS = 30


def _new_token() -> str:
    return secrets.token_urlsafe(48)


def _err(detail: str) -> JSONResponse:
    """Return a 401 with no-store (RFC 6749 §5.1)."""
    return JSONResponse(
        {"error": detail},
        status_code=status.HTTP_401_UNAUTHORIZED,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


@router.post("/oauth/token")
async def oauth_token(request: Request) -> JSONResponse:
    settings = get_settings()

    # Manual form parse so all failures route through _err() → 401.
    try:
        form = await request.form()
    except Exception:
        return _err("invalid_request")

    grant_type = form.get("grant_type") or ""
    client_id = form.get("client_id") or ""
    client_secret = form.get("client_secret") or ""

    if not grant_type or not client_id or not client_secret:
        return _err("invalid_request")

    if (
        not hmac.compare_digest(str(client_id), settings.oauth_client_id)
        or not hmac.compare_digest(str(client_secret), settings.oauth_client_secret)
    ):
        log.warning("oauth.token.bad_client_creds")
        return _err("invalid_client")

    if grant_type == "authorization_code":
        code = form.get("code") or ""
        redirect_uri = form.get("redirect_uri") or ""
        if not code or not redirect_uri:
            return _err("invalid_request")
        return await _exchange_code(str(code), str(redirect_uri))

    if grant_type == "refresh_token":
        refresh_token = form.get("refresh_token") or ""
        if not refresh_token:
            return _err("invalid_request")
        return await _refresh(str(refresh_token))

    return _err("unsupported_grant_type")


async def _exchange_code(code: str, redirect_uri: str) -> JSONResponse:
    settings = get_settings()
    pool = get_pool()

    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT user_id, redirect_uri, scope, expires_at, used_at
                  FROM public.gw_oauth_codes
                 WHERE code = $1
                 FOR UPDATE
                """,
                code,
            )
            if row is None or row["used_at"] is not None:
                return _err("invalid_grant")
            if not hmac.compare_digest(row["redirect_uri"], redirect_uri):
                return _err("redirect_uri_mismatch")
            if row["expires_at"] < datetime.now(tz=timezone.utc):
                return _err("code_expired")

            await conn.execute(
                "UPDATE public.gw_oauth_codes SET used_at = NOW() WHERE code = $1",
                code,
            )

            access_token = _new_token()
            refresh_token = _new_token()
            now = datetime.now(tz=timezone.utc)
            await conn.execute(
                """
                INSERT INTO public.gw_oauth_tokens
                    (access_token, refresh_token, user_id, expires_at, refresh_expires_at, scope)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                access_token,
                refresh_token,
                row["user_id"],
                now + timedelta(seconds=settings.oauth_access_token_ttl),
                now + timedelta(seconds=settings.oauth_refresh_token_ttl),
                row["scope"] or "read:astro write:query",
            )

    log.info("oauth.token.minted", grant="authorization_code")
    return JSONResponse(
        {
            "access_token": access_token,
            "token_type": "Bearer",
            "refresh_token": refresh_token,
            "expires_in": settings.oauth_access_token_ttl,
            "scope": row["scope"] or "read:astro write:query",
        },
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


async def _refresh(presented_refresh: str) -> JSONResponse:
    settings = get_settings()
    pool = get_pool()

    async with pool.acquire() as conn:
        async with conn.transaction():
            # Atomic claim: revoke an active token only if it's still active.
            row = await conn.fetchrow(
                """
                UPDATE public.gw_oauth_tokens
                   SET revoked_at = NOW()
                 WHERE refresh_token = $1
                   AND revoked_at IS NULL
                   AND refresh_expires_at > NOW()
                RETURNING access_token, user_id, scope
                """,
                presented_refresh,
            )

            if row is None:
                # Possibly within grace window for an immediately-prior rotation.
                grace = await conn.fetchrow(
                    """
                    SELECT t2.access_token, t2.refresh_token, t2.expires_at, t2.scope
                      FROM public.gw_oauth_tokens t1
                      JOIN public.gw_oauth_tokens t2 ON t2.access_token = t1.rotated_to
                     WHERE t1.refresh_token = $1
                       AND t1.revoked_at IS NOT NULL
                       AND t1.revoked_at > NOW() - make_interval(secs => $2)
                       AND t2.revoked_at IS NULL
                    """,
                    presented_refresh, _REFRESH_GRACE_SECONDS,
                )
                if grace is None:
                    log.warning("oauth.refresh.invalid")
                    return _err("invalid_grant")
                # Idempotent retry: return the post-rotation pair.
                expires_in = int(
                    (grace["expires_at"] - datetime.now(tz=timezone.utc)).total_seconds()
                )
                return JSONResponse(
                    {
                        "access_token": grace["access_token"],
                        "token_type": "Bearer",
                        "refresh_token": grace["refresh_token"],
                        "expires_in": max(60, expires_in),
                        "scope": grace["scope"] or "read:astro write:query",
                    },
                    headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
                )

            new_access = _new_token()
            new_refresh = _new_token()
            now = datetime.now(tz=timezone.utc)
            await conn.execute(
                """
                INSERT INTO public.gw_oauth_tokens
                    (access_token, refresh_token, user_id, expires_at, refresh_expires_at, scope)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                new_access, new_refresh, row["user_id"],
                now + timedelta(seconds=settings.oauth_access_token_ttl),
                now + timedelta(seconds=settings.oauth_refresh_token_ttl),
                row["scope"] or "read:astro write:query",
            )
            await conn.execute(
                "UPDATE public.gw_oauth_tokens SET rotated_to = $1 WHERE access_token = $2",
                new_access, row["access_token"],
            )

    log.info("oauth.token.minted", grant="refresh_token")
    return JSONResponse(
        {
            "access_token": new_access,
            "token_type": "Bearer",
            "refresh_token": new_refresh,
            "expires_in": settings.oauth_access_token_ttl,
            "scope": row["scope"] or "read:astro write:query",
        },
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )
