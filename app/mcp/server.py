"""AskAstroBot MCP server (ChatGPT plugin), mounted on the gateway at /mcp.

Tools: get_prashna_chart, get_birth_chart (+ topic vargas, Ashtakavarga and Moon
chart for a general reading), get_divisional_chart, get_transits, get_account.
All chart data comes directly from the astrology API (n8n is no longer used;
its rules are ported in app/mcp/rules.py). Gochar comes from the shared transit
table. Nothing about a person's chart is stored. Errors use the codes the master skill expects:
INVALID_INPUT, AMBIGUOUS_PLACE, INSUFFICIENT_CREDITS, RATE_LIMITED, BACKEND_UNAVAILABLE.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Annotated, Any, Awaitable, Callable, Literal

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field

from app.db import get_pool
from app.logging_setup import get_logger
from app.mcp import astro_api, enrich, entitlement, natal, transit_table
from app.mcp.geo import (GeoError, NonexistentLocalTime, Place, checked_offset_hours, now_in, resolve_place,
                         utc_offset_hours)
from app.mcp.payload import GRAHAS, PayloadError, sign_name
from app.settings import get_settings

log = get_logger(__name__)

QueryType = Literal[
    "general", "career", "finance", "marriage", "education", "health", "children",
    "siblings", "parents", "property", "travel", "legal", "spiritual", "past_life",
]
Varga = Literal["D2", "D3", "D4", "D5", "D6", "D7", "D8", "D9", "D10", "D11", "D12",
                "D16", "D20", "D24", "D27", "D30", "D40", "D45", "D60"]

BirthDate = Annotated[str, Field(description="Date of birth, YYYY-MM-DD")]
BirthTime = Annotated[str, Field(description="Local time of birth, 24-hour HH:MM")]
BirthPlace = Annotated[str, Field(min_length=2, max_length=120,
                                  description="City and country of birth, e.g. 'Delhi, India'")]

_BACKEND_DEADLINE_SECONDS = 38.0
_ENRICH_MAX_SECONDS = 10.0
_UNAVAILABLE = "BACKEND_UNAVAILABLE: The chart could not be retrieved at the moment. Kindly try again later"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")

_PAID = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)
_FREE = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

INSTRUCTIONS = (
    "AskAstroBot Vedic astrology charts. Always fetch a fresh chart for every new question: "
    "get_prashna_chart for a question chart cast now, get_birth_chart for a birth chart. "
    "Use get_divisional_chart for other vargas and get_transits for future dates or periods. "
    "Never invent placements; interpret only the blocks returned. A new question uses one "
    "question from the user's AskAstroBot plan; follow-ups for the same person and get_account do not."
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

def _quota_message(decision: entitlement.Decision, email: str) -> str:
    plans = f"{get_settings().app_base_url}/plans"
    if decision.reason == "rate_limited":
        return "RATE_LIMITED: Too many chart requests in the last hour. Please try again later."
    return (
        "INSUFFICIENT_CREDITS: This needs 1 question from an AskAstroBot plan; the connected "
        f"AskAstroBot account ({entitlement.mask_email(email)}) has no free questions left and no "
        f"active plan. Account details: {plans}"
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


@dataclass(frozen=True)
class Birth:
    date: str
    time: str
    local: datetime
    place: Place
    offset: float
    ambiguous: bool

    def hash_inputs(self) -> dict[str, Any]:
        # Canonical values from the parsed datetime ("9:00" and "09:00" are the same person).
        return {"d": self.local.date().isoformat(), "t": self.local.strftime("%H:%M"),
                "lat": round(self.place.latitude, 4), "lon": round(self.place.longitude, 4)}

    def api_body(self) -> dict[str, Any]:
        return astro_api.birth_body(self.local, self.place.latitude, self.place.longitude, self.offset)

    def summary(self) -> dict[str, Any]:
        out = {"date": self.date, "time": self.time, "place": self.place.name,
               "utc_offset": f"UTC{self.offset:+g}"}
        if self.ambiguous:
            out["note"] = "This local time occurred twice (clocks moved back); the first occurrence was used."
        return out


async def _resolve_birth(birth_date: str, birth_time: str, birth_place: str) -> Birth:
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
    return Birth(birth_date, birth_time, local, place, offset, ambiguous)


async def _charged(
    *, tool: str, bot_slug: str, query_type: str | None, hash_inputs: dict[str, Any],
    work: Callable[[], Awaitable[tuple[dict[str, Any], int]]],
    subject: dict[str, Any] | None = None, follow_up_ok: bool = False,
) -> dict[str, Any]:
    """Reserve quota, run `work`, release the reservation on any failure."""
    user_id, email = _current_user()
    ihash = entitlement.input_hash(user_id, tool, hash_inputs)
    shash = entitlement.subject_hash(user_id, subject) if subject else None
    try:
        decision = await entitlement.reserve(
            user_id=user_id, email=email, bot_slug=bot_slug, tool_name=tool,
            query_type=query_type, ihash=ihash, subject=shash, follow_up_ok=follow_up_ok,
        )
    except Exception as exc:  # DB trouble: sanitized error, no tool arguments in the log
        log.error("mcp.entitlement_failed", tool=tool, error=type(exc).__name__)
        raise ToolError(_UNAVAILABLE) from None
    if not decision.allowed:
        log.info("mcp.denied", tool=tool, reason=decision.reason)
        raise ToolError(_quota_message(decision, email))
    assert decision.reservation_id is not None

    try:
        result, elapsed_ms = await asyncio.wait_for(work(), timeout=_BACKEND_DEADLINE_SECONDS)
    except BaseException as exc:
        # Includes cancellation (client went away): never keep the reservation.
        log.error("mcp.backend_failed", tool=tool, error=type(exc).__name__)
        try:
            await asyncio.shield(entitlement.release(decision.reservation_id))
        except BaseException:
            log.error("mcp.release_failed", tool=tool)
        if isinstance(exc, Exception):
            raise ToolError(_UNAVAILABLE) from None
        raise

    try:
        await entitlement.complete(decision.reservation_id, elapsed_ms)
    except Exception:
        log.error("mcp.complete_failed", tool=tool)   # timing only; the result is still returned
    if decision.reason == "free" and decision.free_left is not None:
        result["account_note"] = f"Free questions left: {decision.free_left}"
    elif decision.reason == "follow_up":
        result["account_note"] = "Follow-up to the same question: no extra question used."
    log.info("mcp.tool_ok", tool=tool, reason=decision.reason, elapsed_ms=elapsed_ms)
    return result


def dasha_running(table: dict[str, Any], day: date) -> dict[str, str] | None:
    """Maha/Antar dasha running on `day`, with its boundaries."""
    iso = day.isoformat()
    for md, antars in (table or {}).items():
        for ad, period in antars.items():
            s, e = str(period.get("start_time", ""))[:10], str(period.get("end_time", ""))[:10]
            if s and e and s <= iso < e:
                return {"running": f"{md} / {ad}", "from": s, "to": e}
    return None


def dasha_changes(table: dict[str, Any], start: date, end: date) -> list[dict[str, str]]:
    """Maha/Antar dasha changes inside [start, end] from the API's dasha table."""
    out = []
    for md, antars in (table or {}).items():
        for ad, period in antars.items():
            s = str(period.get("start_time", ""))[:10]
            if s and start.isoformat() < s <= end.isoformat():
                out.append({"date": s, "to": f"{md} / {ad}"})
    return sorted(out, key=lambda x: x["date"])


def _timed(coro_factory: Callable[[], Awaitable[dict[str, Any]]]) -> Callable[[], Awaitable[tuple[dict[str, Any], int]]]:
    async def run() -> tuple[dict[str, Any], int]:
        t0 = time.perf_counter()
        result = await coro_factory()
        return result, int((time.perf_counter() - t0) * 1000)
    return run


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
        deadline = time.monotonic() + _BACKEND_DEADLINE_SECONDS
        chart = await _charged(
            tool="get_prashna_chart", bot_slug="prashna", query_type=query_type,
            hash_inputs={"lat": round(place.latitude, 4), "lon": round(place.longitude, 4), "qt": query_type},
            work=_timed(lambda: natal.prashna_chart(local, place.latitude, place.longitude, offset, deadline)),
        )
        chart["moment"] = f"{local:%Y-%m-%d %H:%M} (UTC{offset:+g})"
        chart["place"] = place.name
        chart["query_type"] = query_type
        return chart

    @server.tool(
        name="get_birth_chart",
        title="Birth chart (Janam Kundali)",
        description=(
            "Fetch the Vedic birth chart for one question: D1 with house table, aspects, yogas, "
            "Sade Sati/Dhaiya/Kantak, Manglik, dashas, today's gochar, D9, D10, plus the divisional charts "
            "for the topic (e.g. children D7, past_life D60/D12/D30). 'general' adds Ashtakavarga and the "
            "Moon chart. Confirm birth details first. Call again for every new question (1 question "
            "from the user's plan)."
        ),
        annotations=_PAID,
    )
    async def get_birth_chart(
        birth_date: BirthDate, birth_time: BirthTime, birth_place: BirthPlace,
        query_type: Annotated[QueryType, Field(description="Topic of the question; 'general' for a full reading")],
    ) -> dict[str, Any]:
        birth = await _resolve_birth(birth_date, birth_time, birth_place)

        async def work() -> tuple[dict[str, Any], int]:
            t0 = time.perf_counter()
            deadline = time.monotonic() + _BACKEND_DEADLINE_SECONDS - 12.0   # leave room for enrichment
            chart = await natal.birth_chart(birth.local, birth.place.latitude, birth.place.longitude,
                                            birth.offset, deadline)
            remaining = _BACKEND_DEADLINE_SECONDS - (time.perf_counter() - t0) - 2.0
            await enrich.add_topic_charts(chart, query_type, birth.api_body(),
                                          timeout=max(1.0, min(_ENRICH_MAX_SECONDS, remaining)))
            return chart, int((time.perf_counter() - t0) * 1000)

        chart = await _charged(
            tool="get_birth_chart", bot_slug="horoscope", query_type=query_type,
            hash_inputs={**birth.hash_inputs(), "qt": query_type}, work=work,
            subject=birth.hash_inputs(),
        )
        chart["birth"] = birth.summary()
        chart["query_type"] = query_type
        return chart

    @server.tool(
        name="get_divisional_chart",
        title="Divisional (varga) charts",
        description=(
            "Fetch up to 6 divisional charts (D2-D60) for a birth chart, e.g. D7 children, D12 parents, "
            "D30 misfortunes, D60 past karma. Use when the question needs a varga that "
            "get_birth_chart didn't return. A follow-up within the same question uses no extra question."
        ),
        annotations=_PAID,
    )
    async def get_divisional_chart(
        birth_date: BirthDate, birth_time: BirthTime, birth_place: BirthPlace,
        charts: Annotated[list[Varga], Field(min_length=1, max_length=6, description="Charts, e.g. ['D7', 'D60']")],
        include_images: Annotated[bool, Field(description="Also return chart image links")] = False,
    ) -> dict[str, Any]:
        birth = await _resolve_birth(birth_date, birth_time, birth_place)
        wanted = list(dict.fromkeys(charts))

        async def work() -> dict[str, Any]:
            body = birth.api_body()
            got, missing = await enrich.fetch_vargas(wanted, body)
            if not got:
                raise RuntimeError("no charts")
            result: dict[str, Any] = {"status": "active", "charts": got,
                                      "legend": "Sign (house in that varga)"}
            if missing:
                result["unavailable_charts"] = missing
            if include_images:
                urls = await asyncio.gather(*(astro_api.chart_image_url(c, body) for c in wanted),
                                            return_exceptions=True)
                result["images"] = {c: u for c, u in zip(wanted, urls) if isinstance(u, str)}
            return result

        result = await _charged(
            tool="get_divisional_chart", bot_slug="horoscope", query_type=None,
            hash_inputs={**birth.hash_inputs(), "charts": sorted(wanted), "img": include_images},
            work=_timed(work), subject=birth.hash_inputs(), follow_up_ok=True,
        )
        result["birth"] = birth.summary()
        return result

    @server.tool(
        name="get_transits",
        title="Gochar (transits) for a date or period",
        description=(
            "Transits relative to the birth chart for one date, or monthly across a period of up to 24 "
            "months (e.g. 'next 9 months'): planet signs with house from Lagna and from Moon, exact "
            "dates slow planets change sign, Sade Sati/Dhaiya/Kantak and the running dasha. Use for "
            "future-timing questions instead of today's gochar. A follow-up within the same question "
            "uses no extra question."
        ),
        annotations=_PAID,
    )
    async def get_transits(
        birth_date: BirthDate, birth_time: BirthTime, birth_place: BirthPlace,
        from_date: Annotated[str, Field(description="First date, YYYY-MM-DD")],
        to_date: Annotated[str | None, Field(description="Last date, YYYY-MM-DD; omit for a single date")] = None,
    ) -> dict[str, Any]:
        birth = await _resolve_birth(birth_date, birth_time, birth_place)
        try:
            start = date.fromisoformat(from_date)
            end = date.fromisoformat(to_date) if to_date else None
        except ValueError as exc:
            raise ToolError("INVALID_INPUT: Use dates as YYYY-MM-DD.") from exc
        if end is not None and (end < start or (end - start).days > 731):
            raise ToolError("INVALID_INPUT: The period must end after it starts and be at most 24 months.")
        try:
            tbl = await transit_table.load()
            tbl.require(datetime(start.year, start.month, start.day),
                        datetime((end or start).year, (end or start).month, (end or start).day, 23, 59))
        except transit_table.TransitDataMissing as exc:
            raise ToolError(f"INVALID_INPUT: Transit data is available for {str(exc).split('covers ', 1)[-1]}. "
                            "Please choose dates in that range.") from None
        except Exception as exc:
            log.error("mcp.transit_table_failed", error=type(exc).__name__)
            raise ToolError(_UNAVAILABLE) from None

        async def work() -> dict[str, Any]:
            deadline = time.monotonic() + _BACKEND_DEADLINE_SECONDS
            kundali, dasha_tbl = await natal.natal_reference(
                birth.local, birth.place.latitude, birth.place.longitude, birth.offset, deadline)
            table = await transit_table.load()
            planets = {p: sign_name(kundali[p]["current_sign"]) for p in GRAHAS}
            result = transit_table.transits_window(
                table, lagna=sign_name(kundali["Ascendant"]["current_sign"]), moon=planets["Moon"],
                natal_planets=planets, start=start, end=end or start)
            result["dasha_at_start"] = dasha_running(dasha_tbl, start)
            result["dasha_changes"] = dasha_changes(dasha_tbl, start, end or start)
            return result

        result = await _charged(
            tool="get_transits", bot_slug="horoscope", query_type="transit",
            hash_inputs={**birth.hash_inputs(), "from": from_date, "to": to_date},
            work=_timed(work), subject=birth.hash_inputs(), follow_up_ok=True,
        )
        result["birth"] = birth.summary()
        return result

    @server.tool(
        name="get_account",
        title="AskAstroBot account",
        description=("Show which AskAstroBot account is connected, free questions left and the active "
                     "plan. Free; no chart details needed."),
        annotations=_FREE,
    )
    async def get_account() -> dict[str, Any]:
        user_id, email = _current_user()
        return await entitlement.account_summary(user_id, email)

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
