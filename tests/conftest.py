"""Pytest config — sets up env vars for unit tests so settings load cleanly."""
from __future__ import annotations

import os

# Set required env vars BEFORE importing the app, so pydantic-settings picks them up.
os.environ.setdefault("DATABASE_URL", "postgres://test:test@localhost:5432/test")

os.environ.setdefault("OAUTH_CLIENT_ID", "test-client-id")
os.environ.setdefault("OAUTH_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-google-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "test-google-client-secret")
os.environ.setdefault("GATEWAY_JWT_SECRET", "0" * 64)

os.environ.setdefault("N8N_WEBHOOK_PRASHNA",   "https://app.askastrobot.com/webhook/test-prashna")
os.environ.setdefault("N8N_WEBHOOK_HOROSCOPE", "https://app.askastrobot.com/webhook/test-horoscope")
os.environ.setdefault("N8N_WEBHOOK_CAREER",    "https://app.askastrobot.com/webhook/test-career")
os.environ.setdefault("N8N_WEBHOOK_MARRIAGE",  "https://app.askastrobot.com/webhook/test-marriage")
os.environ.setdefault("GATEWAY_SHARED_SECRET", "test-shared-secret")

os.environ.setdefault("APP_BASE_URL", "https://askastrobot.com")
os.environ.setdefault("GATEWAY_BASE_URL", "https://api.askastrobot.com")
os.environ.setdefault("ENVIRONMENT", "development")


# Patch DB pool so unit tests don't try to actually connect.
import app.db as _db  # noqa: E402


async def _noop_init_pool():  # type: ignore[override]
    return None


async def _noop_close_pool():  # type: ignore[override]
    return None


_db.init_pool = _noop_init_pool  # type: ignore[assignment]
_db.close_pool = _noop_close_pool  # type: ignore[assignment]
