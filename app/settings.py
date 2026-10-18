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

    # Website account system (Supabase Auth): the plugin signs users in with it.
    # The anon key is the public (publishable) key, not a secret.
    supabase_url: str = ""
    supabase_anon_key: str = ""

    # freeastrologyapi.com (extra divisional charts, chart images, transits)
    freeastrology_api_key: str = ""
    astro_api_concurrency: int = 3
    astro_api_observation_point: Literal["topocentric", "geocentric"] = "topocentric"

    # MCP (ChatGPT plugin) endpoint
    mcp_free_queries: int = 3          # lifetime free chart calls per user across the plugin
    mcp_dedupe_seconds: int = 120      # identical repeat call within this window is not charged
    mcp_rate_limit_per_hour: int = 30  # chart calls per user per hour
    mcp_followup_minutes: int = 60     # extra charts/transits for the same person = follow-up, not a new question
    geocoder_user_agent: str = "AskAstroBot/1.0 (+https://askastrobot.com/contact)"

    @property
    def mcp_resource_url(self) -> str:
        """Canonical MCP resource identifier (RFC 8707 / RFC 9728)."""
        return f"{self.gateway_base_url}/mcp"

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
