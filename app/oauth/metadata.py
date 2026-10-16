"""RFC 8414 authorization server metadata, read by ChatGPT's MCP connector.

The protected-resource metadata (RFC 9728) for /mcp is served by the MCP SDK.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.settings import get_settings

router = APIRouter()


@router.get("/.well-known/oauth-authorization-server")
async def authorization_server_metadata() -> JSONResponse:
    base = get_settings().gateway_base_url
    return JSONResponse(
        {
            "issuer": base,
            "authorization_endpoint": f"{base}/oauth/authorize",
            "token_endpoint": f"{base}/oauth/token",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            # "none" = public MCP clients with PKCE; client_secret_post = legacy Custom GPT Actions.
            "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
            "client_id_metadata_document_supported": True,
            "authorization_response_iss_parameter_supported": True,
            "scopes_supported": ["read:astro", "write:query"],
        },
        headers={"Cache-Control": "public, max-age=3600"},
    )
