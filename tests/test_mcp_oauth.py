"""MCP OAuth additions: client/redirect pairing, PKCE, metadata, /mcp challenge."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from app.oauth.mcp_clients import is_mcp_client, is_valid_code_challenge, is_valid_mcp_redirect, verify_pkce

STABLE_CLIENT = "https://chatgpt.com/oauth/client.json"
STABLE_REDIRECT = "https://chatgpt.com/connector_platform_oauth_redirect"
BASE = "https://api.askastrobot.com"


def test_client_and_redirect_pairing() -> None:
    assert is_mcp_client(STABLE_CLIENT)
    assert is_mcp_client("https://chatgpt.com/oauth/abc123/client.json")
    assert not is_mcp_client("https://evil.com/oauth/client.json")
    assert not is_mcp_client("test-client-id")
    assert is_valid_mcp_redirect(STABLE_CLIENT, STABLE_REDIRECT)
    assert not is_valid_mcp_redirect(STABLE_CLIENT, "https://chatgpt.com/connector/oauth/abc123")
    cb_client = "https://chatgpt.com/oauth/abc123/client.json"
    assert is_valid_mcp_redirect(cb_client, "https://chatgpt.com/connector/oauth/abc123")
    assert not is_valid_mcp_redirect(cb_client, "https://chatgpt.com/connector/oauth/other")
    assert not is_valid_mcp_redirect(cb_client, STABLE_REDIRECT)
    assert not is_valid_mcp_redirect(STABLE_CLIENT, STABLE_REDIRECT + "?x=1")


def test_pkce_rfc7636_vector() -> None:
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    challenge = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    assert is_valid_code_challenge(challenge, "S256")
    assert not is_valid_code_challenge(challenge, "plain")
    assert verify_pkce(verifier, challenge)
    assert not verify_pkce(verifier[:-1] + "A", challenge)
    assert not verify_pkce(None, challenge)
    assert not verify_pkce("short", challenge)


def test_authorization_server_metadata() -> None:
    body = TestClient(app).get("/.well-known/oauth-authorization-server").json()
    assert body["issuer"] == BASE
    assert body["code_challenge_methods_supported"] == ["S256"]
    assert body["client_id_metadata_document_supported"] is True
    assert body["authorization_response_iss_parameter_supported"] is True
    assert "none" in body["token_endpoint_auth_methods_supported"]


def test_protected_resource_metadata_and_challenge(live_client) -> None:
    client = live_client
    if True:
        prm = client.get("/.well-known/oauth-protected-resource/mcp")
        assert prm.status_code == 200
        assert prm.json()["resource"] == f"{BASE}/mcp"
        assert prm.json()["authorization_servers"][0].rstrip("/") == BASE
        resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                           headers={"Accept": "application/json, text/event-stream"})
        assert resp.status_code == 401
        assert "resource_metadata" in resp.headers.get("www-authenticate", "")


def test_authorize_rejects_mcp_client_without_pkce() -> None:
    client = TestClient(app, base_url=BASE)
    resp = client.get("/oauth/authorize", params={
        "client_id": STABLE_CLIENT, "redirect_uri": STABLE_REDIRECT,
        "response_type": "code", "state": "s1",
    }, follow_redirects=False)
    assert resp.status_code == 401


def test_authorize_rejects_wrong_resource() -> None:
    client = TestClient(app, base_url=BASE)
    resp = client.get("/oauth/authorize", params={
        "client_id": STABLE_CLIENT, "redirect_uri": STABLE_REDIRECT, "response_type": "code",
        "state": "s1", "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        "code_challenge_method": "S256", "resource": "https://evil.example/mcp",
    }, follow_redirects=False)
    assert resp.status_code == 401


def test_token_mcp_client_needs_no_secret_but_legacy_does() -> None:
    client = TestClient(app, base_url=BASE)
    # Legacy client without secret -> invalid_request (no DB touched).
    r = client.post("/oauth/token", data={"grant_type": "authorization_code", "client_id": "test-client-id",
                                          "code": "x", "redirect_uri": "y"})
    assert r.status_code == 401 and r.json()["error"] == "invalid_request"
    # Unknown non-ChatGPT client id with a secret -> invalid_client.
    r = client.post("/oauth/token", data={"grant_type": "authorization_code", "client_id": "nope",
                                          "client_secret": "nope", "code": "x", "redirect_uri": "y"})
    assert r.json()["error"] == "invalid_client"
