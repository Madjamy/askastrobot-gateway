"""JWT mint + verify for upgrade and portal tokens."""
from __future__ import annotations

import time
import uuid
from typing import Literal, TypedDict

import jwt

from app.settings import get_settings


class UpgradeClaims(TypedDict):
    iss: str
    sub: str           # users.id (UUID string)
    email: str
    google_id: str | None
    bot: str           # bot_slug or 'all'
    purpose: Literal["upgrade", "portal"]
    iat: int
    exp: int
    jti: str


def mint_upgrade_token(
    user_id: str,
    email: str,
    bot_slug: str,
    google_id: str | None = None,
    purpose: Literal["upgrade", "portal"] = "upgrade",
) -> str:
    settings = get_settings()
    ttl = settings.upgrade_token_ttl if purpose == "upgrade" else settings.portal_token_ttl
    now = int(time.time())
    claims: UpgradeClaims = {
        "iss": settings.gateway_base_url,
        "sub": user_id,
        "email": email,
        "google_id": google_id,
        "bot": bot_slug,
        "purpose": purpose,
        "iat": now,
        "exp": now + ttl,
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(claims, settings.gateway_jwt_secret, algorithm="HS256")


def verify_upgrade_token(
    token: str,
    expected_purpose: Literal["upgrade", "portal"] = "upgrade",
) -> UpgradeClaims:
    settings = get_settings()
    decoded = jwt.decode(
        token,
        settings.gateway_jwt_secret,
        algorithms=["HS256"],
        options={"require": ["sub", "email", "bot", "purpose", "exp", "iat"]},
    )
    if decoded.get("purpose") != expected_purpose:
        raise jwt.InvalidTokenError(
            f"Token purpose '{decoded.get('purpose')}' does not match expected '{expected_purpose}'"
        )
    if decoded.get("iss") != settings.gateway_base_url:
        raise jwt.InvalidTokenError("Token issuer does not match")
    return decoded  # type: ignore[return-value]
