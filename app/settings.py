"""Application settings loaded from environment."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    environment: Literal["development", "staging", "production"] = "production"
    log_level: str = "info"
    port: int = 8003

    # Supabase (DB only — auth is direct Google OAuth, not via Supabase Auth)
    database_url: str

    # OAuth provider creds we issue to ChatGPT
    oauth_client_id: str
    oauth_client_secret: str
    oauth_access_token_ttl: int = 2_592_000   # 30 days
    oauth_refresh_token_ttl: int = 7_776_000  # 90 days

    # Google OAuth (we authenticate users with Google directly)
    google_client_id: str
    google_client_secret: str

    # Upgrade JWT
    gateway_jwt_secret: str
    upgrade_token_ttl: int = 900   # 15 minutes
    portal_token_ttl: int = 900

    # n8n forwarding
    n8n_webhook_prashna: str
    n8n_webhook_horoscope: str
    n8n_webhook_career: str
    n8n_webhook_marriage: str
    gateway_shared_secret: str
    n8n_timeout_seconds: int = 30

    # URLs
    app_base_url: str = "https://askastrobot.com"
    gateway_base_url: str = "https://api.askastrobot.com"

    # Observability
    sentry_dsn: str = ""

    @property
    def n8n_url_for_bot(self) -> dict[str, str]:
        return {
            "prashna": self.n8n_webhook_prashna,
            "horoscope": self.n8n_webhook_horoscope,
            "career": self.n8n_webhook_career,
            "marriage": self.n8n_webhook_marriage,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
