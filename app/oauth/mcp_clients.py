"""OAuth helpers for MCP clients (the ChatGPT plugin connector).

ChatGPT's MCP connector is a *public* client (no secret) that identifies itself
with a Client ID Metadata Document (CIMD) URL and must use PKCE (S256):

    client_id     https://chatgpt.com/oauth/client.json                 (stable)
                  https://chatgpt.com/oauth/{callback_id}/client.json   (per callback)
    redirect_uri  https://chatgpt.com/connector_platform_oauth_redirect (stable)
                  https://chatgpt.com/connector/oauth/{callback_id}     (per callback)

Rather than fetching arbitrary client metadata URLs (an SSRF risk), we accept
only ChatGPT's documented client IDs and require the redirect URI to be the one
that belongs to that client ID.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re

_CIMD_CLIENT_RE = re.compile(r"^https://chatgpt\.com/oauth/(?:(?P<cb>[A-Za-z0-9_-]{1,128})/)?client\.json$")
_STABLE_REDIRECT = "https://chatgpt.com/connector_platform_oauth_redirect"
_CALLBACK_REDIRECT_RE = re.compile(r"^https://chatgpt\.com/connector/oauth/(?P<cb>[A-Za-z0-9_-]{1,128})$")

# RFC 7636: verifier = 43-128 chars of [A-Z a-z 0-9 - . _ ~]; S256 challenge = 43 chars base64url.
_VERIFIER_RE = re.compile(r"^[A-Za-z0-9\-._~]{43,128}$")
_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


def is_mcp_client(client_id: str | None) -> bool:
    return bool(client_id) and _CIMD_CLIENT_RE.match(client_id) is not None


def is_valid_mcp_redirect(client_id: str, redirect_uri: str) -> bool:
    """The redirect URI must be the one paired with this client ID."""
    m = _CIMD_CLIENT_RE.match(client_id or "")
    if m is None or not redirect_uri:
        return False
    cb = m.group("cb")
    if cb is None:
        return redirect_uri == _STABLE_REDIRECT
    r = _CALLBACK_REDIRECT_RE.match(redirect_uri)
    return r is not None and r.group("cb") == cb


def is_valid_code_challenge(challenge: str | None, method: str | None) -> bool:
    return method == "S256" and bool(challenge) and _CHALLENGE_RE.match(challenge) is not None


def verify_pkce(code_verifier: str | None, code_challenge: str) -> bool:
    if not code_verifier or _VERIFIER_RE.match(code_verifier) is None:
        return False
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return hmac.compare_digest(computed, code_challenge)
