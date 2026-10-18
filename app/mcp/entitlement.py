"""Who may make a chargeable MCP chart call, and recording it.

Order of checks for every chart tool call:
1. Rate limit: at most `mcp_rate_limit_per_hour` successful calls per user per hour.
2. Repeat protection: an identical call (same user, tool and inputs) inside
   `mcp_dedupe_seconds` is served again without using quota. Repeats are still
   logged (is_repeat = TRUE) so they count towards the rate limit.
3. Follow-up: extra charts / transits for the SAME person within `mcp_followup_minutes`
   of a counted birth-chart question are part of that question (Owner: only a new
   question uses a free question). Logged with is_repeat = TRUE (not counted).
4. Active subscription for the tool's bot or 'all' (the existing gw_subscriptions,
   written by the website's Stripe webhook) -> paid call.
5. Otherwise the plugin's lifetime free quota (`mcp_free_queries`, counted over
   channel='mcp' rows only, so it is separate from the Custom GPT quota).

The log row is inserted INSIDE the locked check (a reservation), so concurrent
calls cannot all pass the quota or rate limit. If the backend call then fails
the row is deleted (`release`), so failed calls never use up anything. MCP rows
store no question text and no birth details; `input_hash` is a keyed HMAC.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.db import get_pool
from app.settings import get_settings


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str            # 'subscription' | 'free' | 'duplicate' | 'follow_up' | 'quota_exhausted' | 'rate_limited'
    was_paid: bool = False
    free_left: int | None = None
    reservation_id: int | None = None   # gw_query_log.id reserved for this call


def subject_hash(user_id: str, birth: dict[str, Any]) -> str:
    """Keyed hash identifying the person (birth date/time/place) without storing it."""
    return input_hash(user_id, "subject", birth)


def input_hash(user_id: str, tool: str, inputs: dict[str, Any]) -> str:
    secret = get_settings().gateway_jwt_secret.encode()
    canonical = json.dumps({"u": user_id, "t": tool, "i": inputs}, sort_keys=True, separators=(",", ":"))
    return hmac.new(secret, canonical.encode(), hashlib.sha256).hexdigest()


async def reserve(
    *, user_id: str, email: str, bot_slug: str, tool_name: str, query_type: str | None, ihash: str,
    subject: str | None = None, follow_up_ok: bool = False,
) -> Decision:
    settings = get_settings()
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"{user_id}:mcp")

            recent = await conn.fetchval(
                """
                SELECT COUNT(*) FROM public.gw_query_log
                 WHERE user_id = $1 AND channel = 'mcp'
                   AND created_at > NOW() - INTERVAL '1 hour'
                """,
                user_id,
            )
            if (recent or 0) >= settings.mcp_rate_limit_per_hour:
                return Decision(False, "rate_limited")

            duplicate = await conn.fetchval(
                """
                SELECT 1 FROM public.gw_query_log
                 WHERE user_id = $1 AND channel = 'mcp' AND input_hash = $2
                   AND is_repeat = FALSE
                   AND created_at > NOW() - make_interval(secs => $3)
                 LIMIT 1
                """,
                user_id, ihash, settings.mcp_dedupe_seconds,
            )
            follow_up = False
            if not duplicate and follow_up_ok and subject:
                follow_up = bool(await conn.fetchval(
                    """
                    SELECT 1 FROM public.gw_query_log
                     WHERE user_id = $1 AND channel = 'mcp' AND subject_hash = $2
                       AND tool_name = 'get_birth_chart' AND is_repeat = FALSE
                       AND n8n_response_ms IS NOT NULL   -- completed, not a pending reservation
                       AND created_at > NOW() - make_interval(mins => $3)
                     LIMIT 1
                    """,
                    user_id, subject, settings.mcp_followup_minutes,
                ))

            if duplicate:
                decision = Decision(True, "duplicate")
            elif follow_up:
                decision = Decision(True, "follow_up")
            elif await _active_subscription(conn, user_id, bot_slug):
                decision = Decision(True, "subscription", was_paid=True)
            else:
                used = await _free_used(conn, user_id)
                left = max(0, settings.mcp_free_queries - used)
                if left <= 0:
                    return Decision(False, "quota_exhausted", free_left=0)
                decision = Decision(True, "free", was_paid=False, free_left=left - 1)

            row_id = await conn.fetchval(
                """
                INSERT INTO public.gw_query_log
                    (user_id, email, bot_slug, query_text, query_type, birth_details_json,
                     was_paid_query, channel, tool_name, input_hash, is_repeat, subject_hash)
                VALUES ($1, $2, $3, NULL, $4, NULL, $5, 'mcp', $6, $7, $8, $9)
                RETURNING id
                """,
                user_id, email, bot_slug, query_type, decision.was_paid, tool_name, ihash,
                decision.reason in ("duplicate", "follow_up"), subject,
            )
            return Decision(decision.allowed, decision.reason, decision.was_paid,
                            decision.free_left, reservation_id=row_id)


async def complete(reservation_id: int, elapsed_ms: int) -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE public.gw_query_log SET n8n_response_ms = $2 WHERE id = $1",
            reservation_id, elapsed_ms,
        )


async def release(reservation_id: int) -> None:
    """Backend failed: drop the reservation so nothing is used up."""
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM public.gw_query_log WHERE id = $1", reservation_id)


def mask_email(email: str) -> str:
    """'madhur@gmail.com' -> 'ma••••@gmail.com' (enough to recognise the account)."""
    local, _, domain = (email or "").partition("@")
    if not domain:
        return ""
    return f"{local[:2]}{'•' * max(2, len(local) - 2)}@{domain}"


async def account_summary(user_id: str, email: str = "") -> dict[str, Any]:
    settings = get_settings()
    pool = get_pool()
    async with pool.acquire() as conn:
        sub = await conn.fetchrow(
            """
            SELECT plan, bot_slug, expires_at FROM public.gw_subscriptions
             WHERE user_id = $1 AND status = 'active' AND expires_at > NOW()
             ORDER BY expires_at DESC LIMIT 1
            """,
            user_id,
        )
        used = await _free_used(conn, user_id)
    summary: dict[str, Any] = {
        "signed_in_as": mask_email(email),
        "free_questions_left": max(0, settings.mcp_free_queries - used),
        "active_plan": None,
        "account_page": f"{settings.app_base_url}/plans",
    }
    if sub:
        expires: datetime = sub["expires_at"]
        summary["active_plan"] = {"plan": sub["plan"], "covers": sub["bot_slug"],
                                  "valid_until": expires.date().isoformat()}
    return summary


async def _active_subscription(conn: Any, user_id: str, bot_slug: str) -> bool:
    return bool(await conn.fetchval(
        """
        SELECT 1 FROM public.gw_subscriptions
         WHERE user_id = $1 AND status = 'active' AND expires_at > NOW()
           AND bot_slug IN ($2, 'all')
         LIMIT 1
        """,
        user_id, bot_slug,
    ))


async def _free_used(conn: Any, user_id: str) -> int:
    return int(await conn.fetchval(
        """
        SELECT COUNT(*) FROM public.gw_query_log
         WHERE user_id = $1 AND channel = 'mcp' AND was_paid_query = FALSE AND is_repeat = FALSE
        """,
        user_id,
    ) or 0)
