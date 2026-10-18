"""Server-side calls to the website's account system (Supabase Auth / GoTrue).

The plugin signs users in with their AskAstroBot (website) account, so a plan
bought on askastrobot.com applies in ChatGPT whatever the ChatGPT login is.
Only the public anon key is used; Supabase session tokens are discarded after
the user is identified.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from app.logging_setup import get_logger
from app.settings import get_settings

log = get_logger(__name__)

_TIMEOUT = 10


@dataclass(frozen=True)
class AccountIdentity:
    supabase_user_id: str
    email: str
    name: str | None


class AccountLoginError(Exception):
    """`kind`: 'invalid_credentials' | 'email_not_confirmed' | 'unavailable' | 'invalid_response'."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


def _base() -> str:
    url = get_settings().supabase_url.rstrip("/")
    if not url:
        raise AccountLoginError("unavailable")
    return f"{url}/auth/v1"


def _headers() -> dict[str, str]:
    key = get_settings().supabase_anon_key
    return {"apikey": key, "Content-Type": "application/json", "Accept": "application/json"}


def new_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def google_authorize_url(redirect_to: str, code_challenge: str) -> str:
    qs = urlencode({
        "provider": "google",
        "redirect_to": redirect_to,
        "code_challenge": code_challenge,
        "code_challenge_method": "s256",
    })
    return f"{_base()}/authorize?{qs}"


def _identity_from_token_response(body: dict) -> AccountIdentity:
    user = body.get("user") if isinstance(body, dict) else None
    if not isinstance(user, dict) or not user.get("id") or not user.get("email"):
        raise AccountLoginError("invalid_response")
    if not (user.get("email_confirmed_at") or user.get("confirmed_at")):
        raise AccountLoginError("email_not_confirmed")
    meta = user.get("user_metadata") or {}
    return AccountIdentity(
        supabase_user_id=str(user["id"]),
        email=str(user["email"]).strip().lower(),
        name=meta.get("full_name") or meta.get("name"),
    )


async def _token(grant_type: str, payload: dict) -> AccountIdentity:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(f"{_base()}/token?grant_type={grant_type}", json=payload, headers=_headers())
    except httpx.HTTPError as exc:
        log.error("account.supabase_unreachable", grant=grant_type, error=type(exc).__name__)
        raise AccountLoginError("unavailable") from exc
    if resp.status_code == 200:
        body = resp.json()
        identity = _identity_from_token_response(body)
        await _sign_out(body.get("access_token"))
        return identity
    detail = ""
    try:
        err = resp.json()
        detail = f"{err.get('error_code', '')} {err.get('error_description') or err.get('msg') or err.get('error') or ''}".lower()
    except ValueError:
        pass
    log.warning("account.login_rejected", grant=grant_type, status=resp.status_code, code=detail[:60])
    if "not confirmed" in detail or "email_not_confirmed" in detail:
        raise AccountLoginError("email_not_confirmed")
    if resp.status_code in (400, 401, 403, 422):
        raise AccountLoginError("invalid_credentials")
    raise AccountLoginError("unavailable")


async def password_login(email: str, password: str) -> AccountIdentity:
    return await _token("password", {"email": email.strip(), "password": password})


async def exchange_google_code(auth_code: str, code_verifier: str) -> AccountIdentity:
    return await _token("pkce", {"auth_code": auth_code, "code_verifier": code_verifier})


async def _sign_out(access_token: str | None) -> None:
    """Best effort: we only needed the identity, so end the Supabase session."""
    if not access_token:
        return
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                f"{_base()}/logout?scope=local",
                headers={**_headers(), "Authorization": f"Bearer {access_token}"},
            )
    except httpx.HTTPError:
        pass
