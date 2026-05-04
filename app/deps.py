"""Common FastAPI dependencies — bearer token resolution to user identity."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import Depends, Header, HTTPException, status

from app.db import get_pool


@dataclass
class CurrentUser:
    user_id: str
    email: str
    scope: str
    google_id: str | None


async def require_bearer(
    authorization: str | None = Header(default=None),
) -> CurrentUser:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    access_token = authorization.split(" ", 1)[1].strip()
    if not access_token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Empty bearer token")

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT t.user_id, t.scope, t.expires_at, u.email, u.google_id
              FROM public.oauth_tokens t
              JOIN public.users u ON u.id = t.user_id
             WHERE t.access_token = $1
               AND t.revoked_at IS NULL
            """,
            access_token,
        )
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")
    if row["expires_at"] < datetime.now(tz=timezone.utc):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token expired")

    return CurrentUser(
        user_id=str(row["user_id"]),
        email=row["email"],
        scope=row["scope"] or "",
        google_id=row["google_id"],
    )


CurrentUserDep = Depends(require_bearer)
