"""POST /v1/gpt/{bot_slug}/query — the main action endpoint for the Custom GPTs.

Flow:
1. Resolve user from bearer token (deps.require_bearer).
2. Check active subscription. If active for this bot or 'all' → forward.
3. Else atomic quota check + insert: if free count < 2, insert FALSE row, forward.
   If >= 2, return paywall response (HTTP 200 with status: free_limit_reached).
4. Forward to n8n with X-Gateway-Secret. On success, update n8n_response_ms.
5. On n8n failure, delete the just-inserted query_log row + write to query_error_log + return 503.
"""
from __future__ import annotations

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

# In-chat intent words that should not consume a query — they should return
# an upgrade-management link instead.
_PORTAL_INTENT_WORDS = (
    "manage_subscription",
    "cancel_subscription",
    "manage my subscription",
    "cancel my subscription",
    "manage subscription",
    "cancel subscription",
)


def _has_portal_intent(query_text: str | None, query_type: str | None) -> bool:
    if not query_text and not query_type:
        return False
    haystack = " ".join(s for s in (query_text or "", query_type or "") if s).lower()
    return any(word in haystack for word in _PORTAL_INTENT_WORDS)


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

    # Best-effort parse for logging + portal intent detection.
    parsed: dict[str, Any] = {}
    if raw_body:
        try:
            parsed = json.loads(raw_body)
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

    # ---------- Active-sub check ---------- #
    async with pool.acquire() as conn:
        active_sub = await conn.fetchval(
            """
            SELECT 1 FROM public.subscriptions
             WHERE user_id = $1
               AND status = 'active'
               AND expires_at > NOW()
               AND bot_slug IN ($2, 'all')
             LIMIT 1
            """,
            user.user_id, bot_slug,
        )

    if active_sub:
        return await _forward_and_log(
            user=user, bot_slug=bot_slug, raw_body=raw_body, content_type=content_type,
            parsed=parsed, query_text=query_text, query_type=query_type, was_paid=True,
        )

    # ---------- Atomic quota check + insert ---------- #
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Per-(user, bot) advisory lock — serialises concurrent requests.
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"{user.user_id}:{bot_slug}",
            )
            allowed_log_id = await conn.fetchval(
                """
                WITH quota AS (
                  SELECT COUNT(*) AS used
                    FROM public.query_log
                   WHERE user_id = $1
                     AND bot_slug = $2
                     AND was_paid_query = FALSE
                     AND created_at > NOW() - INTERVAL '24 hours'
                ),
                ins AS (
                  INSERT INTO public.query_log
                      (user_id, email, bot_slug, query_text, query_type, birth_details_json, was_paid_query)
                  SELECT $1, $3, $2, $4, $5, $6::jsonb, FALSE
                  WHERE (SELECT used FROM quota) < 2
                  RETURNING id
                )
                SELECT (SELECT id FROM ins)
                """,
                user.user_id,
                bot_slug,
                user.email,
                query_text,
                query_type,
                json.dumps(parsed) if parsed else None,
            )

    if allowed_log_id is None:
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
                "You've used your 2 free queries for this bot in the last 24 hours. "
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

    # ---------- Allowed: forward to n8n. On failure, delete the log row. ---------- #
    return await _forward_and_log(
        user=user, bot_slug=bot_slug, raw_body=raw_body, content_type=content_type,
        parsed=parsed, query_text=query_text, query_type=query_type, was_paid=False,
        log_row_id=allowed_log_id,
    )


async def _forward_and_log(
    *,
    user: CurrentUser,
    bot_slug: str,
    raw_body: bytes,
    content_type: str,
    parsed: dict[str, Any],
    query_text: str | None,
    query_type: str | None,
    was_paid: bool,
    log_row_id: int | None = None,
) -> Response:
    pool = get_pool()
    try:
        body_out, ct_out, elapsed_ms = await forward_to_n8n(bot_slug, raw_body, content_type)
    except N8nForwardError as exc:
        # Failed query — clean up the placeholder row (free-tier path),
        # log the error, and return 503.
        async with pool.acquire() as conn:
            if log_row_id is not None and not was_paid:
                await conn.execute("DELETE FROM public.query_log WHERE id = $1", log_row_id)
            await conn.execute(
                """
                INSERT INTO public.query_error_log
                    (user_id, bot_slug, error_type, error_message, request_body)
                VALUES ($1, $2, $3, $4, $5::jsonb)
                """,
                user.user_id, bot_slug, exc.kind, str(exc),
                json.dumps(parsed) if parsed else None,
            )
        return JSONResponse(
            {
                "status": "upstream_unavailable",
                "message": "Astrology engine is temporarily unavailable. Please try again in a moment.",
            },
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    # Successful — update log row with response time. For paid queries the row
    # didn't exist yet (we skipped the quota CTE). Insert it now.
    async with pool.acquire() as conn:
        if log_row_id is not None:
            await conn.execute(
                "UPDATE public.query_log SET n8n_response_ms = $1 WHERE id = $2",
                elapsed_ms, log_row_id,
            )
        else:
            await conn.execute(
                """
                INSERT INTO public.query_log
                    (user_id, email, bot_slug, query_text, query_type,
                     birth_details_json, n8n_response_ms, was_paid_query)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8)
                """,
                user.user_id, user.email, bot_slug, query_text, query_type,
                json.dumps(parsed) if parsed else None, elapsed_ms, was_paid,
            )

    return Response(content=body_out, media_type=ct_out, status_code=status.HTTP_200_OK)


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
