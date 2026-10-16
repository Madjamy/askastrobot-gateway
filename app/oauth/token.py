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
from app.oauth.mcp_clients import is_mcp_client, verify_pkce
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

    grant_type = str(form.get("grant_type") or "")
    client_id = str(form.get("client_id") or "")
    client_secret = str(form.get("client_secret") or "")

    if not grant_type or not client_id:
        return _err("invalid_request")

    # MCP connector: public client (token_endpoint_auth_method "none"), PKCE-bound.
    # mcp_client_id is None for the legacy Custom GPT client.
    mcp_client_id: str | None = None
    if is_mcp_client(client_id):
        mcp_client_id = client_id
    else:
        if not client_secret:
            return _err("invalid_request")
        if (
            not hmac.compare_digest(client_id, settings.oauth_client_id)
            or not hmac.compare_digest(client_secret, settings.oauth_client_secret)
        ):
            log.warning("oauth.token.bad_client_creds")
            return _err("invalid_client")

    resource = str(form.get("resource") or "") or None

    if grant_type == "authorization_code":
        code = str(form.get("code") or "")
        redirect_uri = str(form.get("redirect_uri") or "")
        if not code or not redirect_uri:
            return _err("invalid_request")
        return await _exchange_code(
            code, redirect_uri,
            mcp_client_id=mcp_client_id,
            code_verifier=str(form.get("code_verifier") or "") or None,
            resource=resource,
        )

    if grant_type == "refresh_token":
        refresh_token = str(form.get("refresh_token") or "")
        if not refresh_token:
            return _err("invalid_request")
        if mcp_client_id is not None and resource is not None and resource != settings.mcp_resource_url:
            return _err("invalid_target")
        return await _refresh(refresh_token, mcp_client_id=mcp_client_id)

    return _err("unsupported_grant_type")


async def _exchange_code(
    code: str,
    redirect_uri: str,
    *,
    mcp_client_id: str | None = None,
    code_verifier: str | None = None,
    resource: str | None = None,
) -> JSONResponse:
    settings = get_settings()
    pool = get_pool()

    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT user_id, redirect_uri, scope, expires_at, used_at,
                       client_id, code_challenge, resource
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
            # A code may only be redeemed by the kind of client it was issued to.
            if row["client_id"] != mcp_client_id:
                log.warning("oauth.token.client_mismatch")
                return _err("invalid_grant")
            if mcp_client_id is not None:
                if not row["code_challenge"] or not verify_pkce(code_verifier, row["code_challenge"]):
                    log.warning("oauth.token.pkce_failed")
                    return _err("invalid_grant")
                if resource is not None and resource != row["resource"]:
                    return _err("invalid_target")

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
                    (access_token, refresh_token, user_id, expires_at, refresh_expires_at, scope,
                     client_id, resource)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                access_token,
                refresh_token,
                row["user_id"],
                now + timedelta(seconds=settings.oauth_access_token_ttl),
                now + timedelta(seconds=settings.oauth_refresh_token_ttl),
                row["scope"] or "read:astro write:query",
                row["client_id"],
                row["resource"],
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


async def _refresh(presented_refresh: str, *, mcp_client_id: str | None = None) -> JSONResponse:
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
                   AND client_id IS NOT DISTINCT FROM $2
                RETURNING access_token, user_id, scope, client_id, resource
                """,
                presented_refresh, mcp_client_id,
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
                       AND t1.client_id IS NOT DISTINCT FROM $3
                    """,
                    presented_refresh, _REFRESH_GRACE_SECONDS, mcp_client_id,
                )
                if grace is None:
                    if mcp_client_id is not None:
                        # Replay of an already-rotated MCP refresh token outside the grace
                        # window: assume theft and sign out this user's plugin sessions
                        # (RFC 9700 4.14.2). Legacy GPT behaviour is unchanged.
                        revoked = await conn.execute(
                            """
                            UPDATE public.gw_oauth_tokens AS t
                               SET revoked_at = NOW()
                              FROM public.gw_oauth_tokens AS old
                             WHERE old.refresh_token = $1
                               AND old.revoked_at IS NOT NULL
                               AND old.client_id = $2
                               AND t.user_id = old.user_id
                               AND t.client_id = $2
                               AND t.revoked_at IS NULL
                            """,
                            presented_refresh, mcp_client_id,
                        )
                        if revoked != "UPDATE 0":
                            log.warning("oauth.refresh.replay_detected", revoked=revoked)
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
                    (access_token, refresh_token, user_id, expires_at, refresh_expires_at, scope,
                     client_id, resource)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                new_access, new_refresh, row["user_id"],
                now + timedelta(seconds=settings.oauth_access_token_ttl),
                now + timedelta(seconds=settings.oauth_refresh_token_ttl),
                row["scope"] or "read:astro write:query",
                row["client_id"], row["resource"],
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
