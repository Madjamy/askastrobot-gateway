"""POST /v1/gpt/{bot_slug}/query — main action endpoint for the Custom GPTs.

Flow:
1. Resolve user from bearer token (deps.require_bearer).
2. Active subscription check. If active for this bot or 'all' → forward.
3. Else atomic quota check (per-(user, bot) advisory lock).
   - If count >= 2: return paywall response (HTTP 200 with status: free_limit_reached).
   - If count < 2: forward to n8n. ON SUCCESS, insert the gw_query_log row.
   This ordering means a failed n8n call does NOT consume a free query and
   never leaves a row to GC. Insert-on-success removes the C6 race entirely.
4. n8n forward respects a 38-second deadline (45s ChatGPT cap minus 7s gateway budget).
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Path, Request, status
from fastapi.responses import JSONResponse, Response

from app.db import get_pool
from app.deps import CurrentUser, CurrentUserDep
from app.gpt.n8n import N8nForwardError, forward_to_n8n
from app.logging_setup import get_logger
from app.oauth.jwt_utils import mint_upgrade_token
from app.settings import get_settings

router = APIRouter()
log = get_logger(__name__)

VALID_BOTS = {"prashna", "horoscope", "career", "marriage"}

# Total budget for the entire endpoint, leaving headroom under ChatGPT's 45s cap.
_TOTAL_DEADLINE_SECONDS = 38.0

# Structured intent: ChatGPT sends one of these in query_type to invoke
# the subscription-management flow without a substring match against query_text.
_PORTAL_INTENT_QUERY_TYPES = {"subscription", "manage_subscription", "cancel_subscription"}


def _has_portal_intent(query_text: str | None, query_type: str | None) -> bool:
    if query_type and query_type.strip().lower() in _PORTAL_INTENT_QUERY_TYPES:
        return True
    # Exact, full-text match on a structured sentinel passed by the bot's
    # system prompt (not a substring search — see I3 fix).
    if query_text and query_text.strip().lower() == "manage_subscription":
        return True
    return False


@router.post("/v1/gpt/{bot_slug}/query")
async def gpt_query(
    request: Request,
    user: CurrentUser = CurrentUserDep,
    bot_slug: str = Path(..., min_length=1, max_length=32),
) -> Response:
    if bot_slug not in VALID_BOTS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown bot: {bot_slug}")

    settings = get_settings()
    raw_body = await request.body()
    content_type = request.headers.get("content-type", "application/json")

    parsed: dict[str, Any] = {}
    if raw_body:
        try:
            parsed = json.loads(raw_body)
            if not isinstance(parsed, dict):
                parsed = {}
        except json.JSONDecodeError:
            parsed = {}

    query_text = parsed.get("query_text") if isinstance(parsed, dict) else None
    query_type = parsed.get("query_type") if isinstance(parsed, dict) else None

    # ---------- Portal intent: short-circuit before quota/sub check ---------- #
    if _has_portal_intent(query_text, query_type):
        portal_token = mint_upgrade_token(
            user_id=user.user_id,
            email=user.email,
            bot_slug=bot_slug,
            google_id=user.google_id,
            purpose="portal",
        )
        portal_url = f"{settings.app_base_url}/account/billing?token={portal_token}"
        return JSONResponse({
            "status": "subscription_management",
            "message": (
                "Click the link below to manage or cancel your subscription. "
                "It will open in a new tab."
            ),
            "manage_url": portal_url,
            "instruction_to_model": (
                "Render the message and the manage_url as a clickable markdown link. "
                "Do not call the action again until the user reports back."
            ),
        })

    pool = get_pool()

    # ---------- Combined active-sub + quota check in one transaction ---------- #
    # Holding the per-(user, bot) advisory lock from sub-check through quota
    # decision means a sub that expires mid-flight cannot let a query slip
    # through the wrong path.
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"{user.user_id}:{bot_slug}",
            )

            active_sub = await conn.fetchval(
                """
                SELECT 1 FROM public.gw_subscriptions
                 WHERE user_id = $1
                   AND status = 'active'
                   AND expires_at > NOW()
                   AND bot_slug IN ($2, 'all')
                 LIMIT 1
                """,
                user.user_id, bot_slug,
            )

            if active_sub:
                was_paid = True
                allowed = True
            else:
                # Lifetime free-tier limit: 2 free queries per (user, bot) ever.
                # Once exhausted, paywall is permanent until the user subscribes.
                used = await conn.fetchval(
                    """
                    SELECT COUNT(*)
                      FROM public.gw_query_log
                     WHERE user_id = $1
                       AND bot_slug = $2
                       AND was_paid_query = FALSE
                    """,
                    user.user_id, bot_slug,
                )
                was_paid = False
                allowed = (used or 0) < 2

    # ---------- Paywall: not allowed, not subscribed ---------- #
    if not allowed:
        upgrade_token = mint_upgrade_token(
            user_id=user.user_id,
            email=user.email,
            bot_slug=bot_slug,
            google_id=user.google_id,
            purpose="upgrade",
        )
        upgrade_url = f"{settings.app_base_url}/upgrade?token={upgrade_token}&bot={bot_slug}"
        log.info("gpt.paywall", user_id=user.user_id, bot_slug=bot_slug)
        return JSONResponse({
            "status": "free_limit_reached",
            "message": (
                "You've used your 2 free queries for this bot. "
                "Upgrade below to continue:"
            ),
            "upgrade_url": upgrade_url,
            "options": _pricing_options(bot_slug),
            "instruction_to_model": (
                "Render the message verbatim, then the upgrade_url as a clickable markdown link, "
                "then the options as a bulleted list. Do not call the action again until the user "
                "confirms they have completed payment. If they say they have paid but the action "
                "still returns 'free_limit_reached', ask them to wait 30 seconds (Stripe webhook "
                "processing) and try again."
            ),
        })

    # ---------- Forward to n8n with a deadline; insert log ON SUCCESS only ---------- #
    try:
        body_out, ct_out, elapsed_ms = await asyncio.wait_for(
            forward_to_n8n(bot_slug, raw_body, content_type),
            timeout=_TOTAL_DEADLINE_SECONDS,
        )
    except asyncio.TimeoutError:
        log.error("gpt.deadline_exceeded", user_id=user.user_id, bot_slug=bot_slug)
        await _log_error(
            user_id=user.user_id, bot_slug=bot_slug,
            error_type="deadline", error_message="gateway deadline exceeded",
            parsed=parsed,
        )
        return _upstream_unavailable()
    except N8nForwardError as exc:
        await _log_error(
            user_id=user.user_id, bot_slug=bot_slug,
            error_type=exc.kind, error_message=str(exc),
            parsed=parsed,
        )
        return _upstream_unavailable()

    # n8n succeeded — record the query.
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO public.gw_query_log
                (user_id, email, bot_slug, query_text, query_type,
                 birth_details_json, n8n_response_ms, was_paid_query)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8)
            """,
            user.user_id, user.email, bot_slug, query_text, query_type,
            json.dumps(parsed) if isinstance(parsed, dict) else None,
            elapsed_ms, was_paid,
        )

    return Response(content=body_out, media_type=ct_out, status_code=status.HTTP_200_OK)


async def _log_error(
    *,
    user_id: str,
    bot_slug: str,
    error_type: str,
    error_message: str,
    parsed: dict[str, Any],
) -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO public.gw_query_error_log
                (user_id, bot_slug, error_type, error_message, request_body)
            VALUES ($1, $2, $3, $4, $5::jsonb)
            """,
            user_id, bot_slug, error_type, error_message[:500],
            json.dumps(parsed) if isinstance(parsed, dict) else None,
        )


def _upstream_unavailable() -> JSONResponse:
    return JSONResponse(
        {
            "status": "upstream_unavailable",
            "message": "Astrology engine is temporarily unavailable. Please try again in a moment.",
        },
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
    )


def _pricing_options(bot_slug: str) -> list[dict[str, Any]]:
    monthly = 7.99 if bot_slug in {"prashna", "horoscope"} else 5.99
    return [
        {"label": "24-hour Day Pass — $2.99", "plan": "day_pass", "price_usd": 2.99,
         "duration": "24 hours"},
        {"label": f"Monthly — ${monthly:.2f}", "plan": "monthly", "price_usd": monthly,
         "duration": "30 days"},
        {"label": "All 4 bots — $12.99/mo", "plan": "master", "price_usd": 12.99,
         "duration": "30 days, all bots"},
    ]
