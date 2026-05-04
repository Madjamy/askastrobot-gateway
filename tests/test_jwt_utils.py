"""Tests for JWT mint/verify."""
from __future__ import annotations

import time

import jwt as pyjwt
import pytest

from app.oauth.jwt_utils import mint_upgrade_token, verify_upgrade_token


def test_mint_and_verify_roundtrip() -> None:
    token = mint_upgrade_token(
        user_id="11111111-2222-3333-4444-555555555555",
        email="user@example.com",
        bot_slug="prashna",
        google_id="google-12345",
    )
    claims = verify_upgrade_token(token, expected_purpose="upgrade")
    assert claims["sub"] == "11111111-2222-3333-4444-555555555555"
    assert claims["email"] == "user@example.com"
    assert claims["bot"] == "prashna"
    assert claims["purpose"] == "upgrade"


def test_purpose_mismatch_raises() -> None:
    token = mint_upgrade_token(
        user_id="x", email="x@example.com", bot_slug="prashna", purpose="upgrade",
    )
    with pytest.raises(pyjwt.InvalidTokenError):
        verify_upgrade_token(token, expected_purpose="portal")


def test_expired_token_raises() -> None:
    token = mint_upgrade_token(
        user_id="x", email="x@example.com", bot_slug="prashna",
    )
    # Manually decode and re-encode with past exp.
    decoded = pyjwt.decode(
        token, options={"verify_signature": False},
    )
    decoded["exp"] = int(time.time()) - 60
    from app.settings import get_settings
    expired = pyjwt.encode(decoded, get_settings().gateway_jwt_secret, algorithm="HS256")
    with pytest.raises(pyjwt.ExpiredSignatureError):
        verify_upgrade_token(expired, expected_purpose="upgrade")


def test_tampered_signature_raises() -> None:
    token = mint_upgrade_token(
        user_id="x", email="x@example.com", bot_slug="prashna",
    )
    # Flip a character in the signature segment.
    parts = token.split(".")
    sig = parts[2]
    flipped = sig[:-2] + ("a" if sig[-2] != "a" else "b") + sig[-1]
    bad = ".".join([parts[0], parts[1], flipped])
    with pytest.raises(pyjwt.InvalidTokenError):
        verify_upgrade_token(bad)


def test_portal_purpose_roundtrip() -> None:
    token = mint_upgrade_token(
        user_id="u", email="u@example.com", bot_slug="all", purpose="portal",
    )
    claims = verify_upgrade_token(token, expected_purpose="portal")
    assert claims["purpose"] == "portal"
    assert claims["bot"] == "all"
