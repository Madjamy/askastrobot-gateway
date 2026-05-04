"""Test the /v1/upgrade/validate endpoint shape — public vs server caller."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from app.oauth.jwt_utils import mint_upgrade_token


def test_validate_browser_response_excludes_email() -> None:
    token = mint_upgrade_token(
        user_id="abc", email="user@example.com", bot_slug="prashna", google_id="g-1",
    )
    client = TestClient(app)
    resp = client.get(f"/v1/upgrade/validate?token={token}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == "abc"
    assert body["bot_slug"] == "prashna"
    assert "email" not in body
    assert "google_id" not in body


def test_validate_server_response_includes_email() -> None:
    token = mint_upgrade_token(
        user_id="abc", email="user@example.com", bot_slug="prashna", google_id="g-1",
    )
    client = TestClient(app)
    resp = client.get(
        f"/v1/upgrade/validate?token={token}",
        headers={"X-Gateway-Secret": "test-shared-secret"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["email"] == "user@example.com"
    assert body["google_id"] == "g-1"


def test_validate_invalid_token_returns_401() -> None:
    client = TestClient(app)
    resp = client.get("/v1/upgrade/validate?token=not.a.real.jwt")
    assert resp.status_code == 401


def test_validate_wrong_secret_treated_as_browser() -> None:
    token = mint_upgrade_token(
        user_id="abc", email="user@example.com", bot_slug="prashna",
    )
    client = TestClient(app)
    resp = client.get(
        f"/v1/upgrade/validate?token={token}",
        headers={"X-Gateway-Secret": "wrong-secret"},
    )
    assert resp.status_code == 200
    assert "email" not in resp.json()
