"""GET /v1/upgrade/validate — verify a JWT issued by the gateway.

Two flavours of caller:
  - Browser (Lovable /upgrade page): public-safe response, no email leak.
  - Edge Function (server-side re-validation): full claims, including email,
    when the caller presents the gateway shared secret in the X-Gateway-Secret header.

The `purpose` query param lets a server caller validate either an upgrade
token or a portal token via the same endpoint.
"""
from __future__ import annotations

import hmac
from typing import Annotated, Literal

from fastapi import APIRouter, Header, HTTPException, Query, status
from fastapi.responses import JSONResponse

import jwt as pyjwt

from app.logging_setup import get_logger
from app.oauth.jwt_utils import verify_upgrade_token
from app.settings import get_settings

router = APIRouter()
log = get_logger(__name__)


def _is_server_caller(provided: str | None) -> bool:
    if not provided:
        return False
    expected = get_settings().gateway_shared_secret
    return hmac.compare_digest(provided, expected)


@router.get("/v1/upgrade/validate")
async def validate_upgrade_token(
    token: Annotated[str, Query(min_length=10, max_length=4096)],
    purpose: Annotated[Literal["upgrade", "portal"], Query()] = "upgrade",
    x_gateway_secret: Annotated[str | None, Header(alias="X-Gateway-Secret")] = None,
) -> JSONResponse:
    is_server_caller = _is_server_caller(x_gateway_secret)

    # Browser callers may only validate upgrade tokens (not portal tokens).
    # This prevents a leaked portal token from being verified by a stranger.
    if not is_server_caller and purpose != "upgrade":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")

    try:
        claims = verify_upgrade_token(token, expected_purpose=purpose)
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token expired")
    except pyjwt.InvalidTokenError as exc:
        log.info("upgrade.token.invalid", reason=str(exc))
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")

    if is_server_caller:
        return JSONResponse({
            "user_id": claims["sub"],
            "email": claims["email"],
            "google_id": claims.get("google_id"),
            "bot_slug": claims["bot"],
            "purpose": claims["purpose"],
            "valid_until": claims["exp"],
        })

    # Browser caller: do not leak email or google_id.
    return JSONResponse({
        "user_id": claims["sub"],
        "bot_slug": claims["bot"],
        "valid_until": claims["exp"],
    })
