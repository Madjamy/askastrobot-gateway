"""HTTP client for forwarding to n8n webhooks."""
from __future__ import annotations

import time

import httpx

from app.logging_setup import get_logger
from app.settings import get_settings

log = get_logger(__name__)


class N8nForwardError(Exception):
    """Raised when the upstream n8n call fails (timeout, non-2xx, network)."""

    def __init__(self, message: str, *, kind: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code


async def forward_to_n8n(
    bot_slug: str,
    body: bytes,
    content_type: str,
) -> tuple[bytes, str, int]:
    """POST `body` to the n8n webhook for `bot_slug` with the gateway shared secret header.

    Returns (response_body, response_content_type, elapsed_ms).
    Raises N8nForwardError on timeout / non-2xx / network failure.
    """
    settings = get_settings()
    url = settings.n8n_url_for_bot.get(bot_slug)
    if url is None:
        raise N8nForwardError(f"Unknown bot_slug: {bot_slug}", kind="invalid_bot")

    headers = {
        "Content-Type": content_type,
        "X-Gateway-Secret": settings.gateway_shared_secret,
        "User-Agent": "AskAstroBot-Gateway/0.1",
    }

    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=settings.n8n_timeout_seconds) as client:
            resp = await client.post(url, content=body, headers=headers)
    except httpx.TimeoutException as exc:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        log.error("n8n.timeout", bot_slug=bot_slug, elapsed_ms=elapsed_ms)
        raise N8nForwardError("n8n upstream timeout", kind="timeout") from exc
    except httpx.HTTPError as exc:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        log.error("n8n.network_error", bot_slug=bot_slug, elapsed_ms=elapsed_ms, error=str(exc))
        raise N8nForwardError(f"n8n network error: {exc}", kind="network") from exc

    elapsed_ms = int((time.perf_counter() - start) * 1000)
    if resp.status_code >= 400:
        log.error(
            "n8n.upstream_error",
            bot_slug=bot_slug,
            status=resp.status_code,
            body_preview=resp.text[:500],
            elapsed_ms=elapsed_ms,
        )
        raise N8nForwardError(
            f"n8n returned {resp.status_code}",
            kind="upstream_error",
            status_code=resp.status_code,
        )

    log.info("n8n.forward.ok", bot_slug=bot_slug, status=resp.status_code, elapsed_ms=elapsed_ms)
    return resp.content, resp.headers.get("content-type", "application/json"), elapsed_ms
