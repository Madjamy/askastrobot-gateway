"""AskAstroBot MCP server (ChatGPT plugin), mounted on the gateway at /mcp.

Tools (S1): get_prashna_chart, get_birth_chart, get_account.
Charts come from the existing n8n workflows (same webhooks as the Custom GPTs)
and are returned in the compact format (app.mcp.payload). Nothing about a chart
is stored. Errors use the codes the master skill expects:
INVALID_INPUT, AMBIGUOUS_PLACE, INSUFFICIENT_CREDITS, RATE_LIMITED, BACKEND_UNAVAILABLE.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field

from app.db import get_pool
from app.gpt.n8n import N8nForwardError, forward_to_n8n
from app.logging_setup import get_logger
from app.mcp import entitlement
from app.mcp.geo import GeoError, NonexistentLocalTime, checked_offset_hours, now_in, resolve_place, utc_offset_hours
from app.mcp.payload import PayloadError, compact_chart
from app.settings import get_settings

log = get_logger(__name__)

QueryType = Literal[
    "general", "career", "finance", "marriage", "education", "health",
    "children", "property", "travel", "legal", "spiritual",
]

_BACKEND_DEADLINE_SECONDS = 38.0
_UNAVAILABLE = "BACKEND_UNAVAILABLE: The chart could not be retrieved at the moment. Kindly try again later"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")

_PAID = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)
_FREE = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

INSTRUCTIONS = (
    "AskAstroBot Vedic astrology charts. Always fetch a fresh chart for every new question: "
    "get_prashna_chart for a question chart cast now, get_birth_chart for a birth chart. "
    "Never invent placements; interpret only the blocks returned. Each chart call uses one "
    "question from the user's AskAstroBot plan; get_account is free."
)


# ------------------------------------------------------------------- auth ---

class GatewayTokenVerifier:
    """Opaque bearer tokens issued by the gateway's /oauth/token, bound to the MCP resource."""

    async def verify_token(self, token: str) -> AccessToken | None:
        settings = get_settings()
        pool = get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT t.user_id, t.scope, t.expires_at, t.client_id, t.resource, u.email
                  FROM public.gw_oauth_tokens t
                  JOIN public.gw_users u ON u.id = t.user_id
                 WHERE t.access_token = $1
                   AND t.revoked_at IS NULL
                   AND t.resource = $2
                """,
                token, settings.mcp_resource_url,
            )
        if row is None or row["expires_at"] <= datetime.now(tz=timezone.utc):
            return None
        return AccessToken(
            token=token,
            client_id=row["client_id"] or "",
            scopes=(row["scope"] or "").split(),
            expires_at=int(row["expires_at"].timestamp()),
            resource=row["resource"],
            subject=str(row["user_id"]),
            claims={"iss": settings.gateway_base_url, "email": row["email"]},
        )


def _current_user() -> tuple[str, str]:
    tok = get_access_token()
    if tok is None or not tok.subject:
        raise ToolError("UNAUTHENTICATED: Please sign in to AskAstroBot to continue.")
    return tok.subject, str((tok.claims or {}).get("email", ""))


# ---------------------------------------------------------------- helpers ---

def _quota_message(decision: entitlement.Decision) -> str:
    plans = f"{get_settings().app_base_url}/plans"
    if decision.reason == "rate_limited":
        return "RATE_LIMITED: Too many chart requests in the last hour. Please try again later."
    return (
        "INSUFFICIENT_CREDITS: This needs 1 question from an AskAstroBot plan; your account has "
        f"no free questions left and no active plan. Account details: {plans}"
    )


def _geo_error(exc: GeoError) -> ToolError:
    if exc.kind == "ambiguous":
        return ToolError("AMBIGUOUS_PLACE: Please choose one of: " + "; ".join(exc.candidates))
    if exc.kind == "not_found":
        return ToolError("INVALID_INPUT: Place not found. Please give the city and country.")
    return ToolError("BACKEND_UNAVAILABLE: The location service is unavailable. Please try again shortly.")


def _parse_local(date_str: str, time_str: str) -> datetime:
    if not _DATE_RE.match(date_str) or not _TIME_RE.match(time_str):
        raise ToolError("INVALID_INPUT: Use date YYYY-MM-DD and 24-hour time HH:MM.")
    try:
        local = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise ToolError("INVALID_INPUT: That date or time does not exist.") from exc
    if not (1800 <= local.year <= datetime.now().year + 1):
        raise ToolError("INVALID_INPUT: Year out of range.")
    return local


def _n8n_body(local: datetime, lat: float, lon: float, offset: float, query_type: str) -> bytes:
    # Same fields the Custom GPT Actions send. No question text is forwarded.
    return json.dumps({
        "year": local.year, "month": local.month, "day": local.day,
        "hours": local.hour, "minutes": local.minute,
        "latitude": round(lat, 4), "longitude": round(lon, 4), "timezone": offset,
        "query_type": query_type, "query_text": f"[plugin] {query_type}",
    }).encode()


async def _charged_chart(
    *, tool: str, bot_slug: str, chart_type: str, query_type: str,
    hash_inputs: dict[str, Any], body: bytes,
) -> dict[str, Any]:
    user_id, email = _current_user()
    ihash = entitlement.input_hash(user_id, tool, hash_inputs)
    try:
        decision = await entitlement.reserve(
            user_id=user_id, email=email, bot_slug=bot_slug, tool_name=tool,
            query_type=query_type, ihash=ihash,
        )
    except Exception as exc:  # DB trouble: sanitized error, no tool arguments in the log
        log.error("mcp.entitlement_failed", tool=tool, error=type(exc).__name__)
        raise ToolError(_UNAVAILABLE) from None
    if not decision.allowed:
        log.info("mcp.denied", tool=tool, reason=decision.reason)
        raise ToolError(_quota_message(decision))
    assert decision.reservation_id is not None

    try:
        raw, _ct, elapsed_ms = await asyncio.wait_for(
            forward_to_n8n(bot_slug, body, "application/json"), timeout=_BACKEND_DEADLINE_SECONDS,
        )
        chart = compact_chart(json.loads(raw), chart_type=chart_type)
    except Exception as exc:
        log.error("mcp.backend_failed", tool=tool, error=type(exc).__name__)
        try:
            await entitlement.release(decision.reservation_id)
        except Exception:
            log.error("mcp.release_failed", tool=tool)
        raise ToolError(_UNAVAILABLE) from None

    try:
        await entitlement.complete(decision.reservation_id, elapsed_ms)
    except Exception:
        log.error("mcp.complete_failed", tool=tool)   # timing only; the chart is still returned
    if decision.reason == "free" and decision.free_left is not None:
        chart["account_note"] = f"Free questions left: {decision.free_left}"
    log.info("mcp.chart_ok", tool=tool, reason=decision.reason, elapsed_ms=elapsed_ms)
    return chart


# ------------------------------------------------------------------ server --

def build_server() -> MCPServer:
    settings = get_settings()
    server = MCPServer(
        name="AskAstroBot",
        instructions=INSTRUCTIONS,
        website_url=settings.app_base_url,
        token_verifier=GatewayTokenVerifier(),
        auth=AuthSettings(
            issuer_url=settings.gateway_base_url,
            resource_server_url=settings.mcp_resource_url,
            validate_token_resource=True,
            required_scopes=None,
        ),
    )

    @server.tool(
        name="get_prashna_chart",
        title="Prashna (question) chart",
        description=(
            "Cast a Vedic Prashna (horary) chart for this exact moment in the user's current city, "
            "to answer one specific question. Call again for every new question. Uses 1 question "
            "from the user's AskAstroBot plan. Do NOT send the question text."
        ),
        annotations=_PAID,
    )
    async def get_prashna_chart(
        city_country: Annotated[str, Field(min_length=2, max_length=120,
                                           description="User's current city and country, e.g. 'Point Cook, Australia'")],
        query_type: Annotated[QueryType, Field(description="Topic of the question")],
    ) -> dict[str, Any]:
        try:
            place = await resolve_place(city_country)
        except GeoError as exc:
            raise _geo_error(exc) from exc
        local = now_in(place.tz_name)
        offset = utc_offset_hours(place.tz_name, local)
        chart = await _charged_chart(
            tool="get_prashna_chart", bot_slug="prashna", chart_type="prashna", query_type=query_type,
            hash_inputs={"lat": round(place.latitude, 4), "lon": round(place.longitude, 4), "qt": query_type},
            body=_n8n_body(local, place.latitude, place.longitude, offset, query_type),
        )
        chart["moment"] = f"{local:%Y-%m-%d %H:%M} (UTC{offset:+g})"
        chart["place"] = place.name
        chart["query_type"] = query_type
        return chart

    @server.tool(
        name="get_birth_chart",
        title="Birth chart (Janam Kundali)",
        description=(
            "Fetch the Vedic birth chart: D1 with house table, aspects, yogas, Sade Sati/Dhaiya/Kantak, "
            "Manglik, current and upcoming dasha, today's gochar, D9 and D10. Confirm birth details with "
            "the user first. Call again for every new question. Uses 1 question from the user's plan."
        ),
        annotations=_PAID,
    )
    async def get_birth_chart(
        birth_date: Annotated[str, Field(description="Date of birth, YYYY-MM-DD")],
        birth_time: Annotated[str, Field(description="Local time of birth, 24-hour HH:MM")],
        birth_place: Annotated[str, Field(min_length=2, max_length=120,
                                          description="City and country of birth, e.g. 'Delhi, India'")],
        query_type: Annotated[QueryType, Field(description="Topic of the question; 'general' for a full reading")],
    ) -> dict[str, Any]:
        local = _parse_local(birth_date, birth_time)
        try:
            place = await resolve_place(birth_place)
        except GeoError as exc:
            raise _geo_error(exc) from exc
        try:
            offset, ambiguous = checked_offset_hours(place.tz_name, local)
        except NonexistentLocalTime as exc:
            raise ToolError(
                "INVALID_INPUT: That birth time did not exist in that place (clocks moved forward). "
                "Please check the time with the user."
            ) from exc
        chart = await _charged_chart(
            tool="get_birth_chart", bot_slug="horoscope", chart_type="natal", query_type=query_type,
            hash_inputs={"d": birth_date, "t": birth_time, "lat": round(place.latitude, 4),
                         "lon": round(place.longitude, 4), "qt": query_type},
            body=_n8n_body(local, place.latitude, place.longitude, offset, query_type),
        )
        chart["birth"] = {"date": birth_date, "time": birth_time, "place": place.name,
                          "utc_offset": f"UTC{offset:+g}"}
        if ambiguous:
            chart["birth"]["note"] = ("This local time occurred twice (clocks moved back); the first "
                                      "occurrence was used.")
        chart["query_type"] = query_type
        return chart

    @server.tool(
        name="get_account",
        title="AskAstroBot account",
        description="Show the user's free questions left and active plan. Free; no chart details needed.",
        annotations=_FREE,
    )
    async def get_account() -> dict[str, Any]:
        user_id, _email = _current_user()
        return await entitlement.account_summary(user_id)

    return server


def build_asgi_app(server: MCPServer):
    host = get_settings().gateway_base_url.split("://", 1)[-1]
    return server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[host, f"{host}:*", "localhost:*", "127.0.0.1:*"],
            allowed_origins=["https://chatgpt.com", "https://chat.openai.com",
                             "http://localhost:*", "http://127.0.0.1:*"],
        ),
    )
