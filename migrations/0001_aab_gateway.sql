-- ====================================================================
-- AskAstroBot Gateway - migration 0001
-- All tables prefixed with gw_ so the gateway owns its own namespace
-- and the existing public.users table (100+ web users) is untouched.
--
-- Idempotent. Apply via Supabase Dashboard -> SQL Editor.
-- Project: bdtzzykdhszmdlvpzlku.supabase.co
-- ====================================================================

BEGIN;

-- --------------------------------------------------------------------
-- 1. Gateway-owned users (separate from public.users, NEVER touched)
-- --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.gw_users (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email               TEXT UNIQUE NOT NULL,
  google_id           TEXT UNIQUE,
  name                TEXT,
  signup_source       TEXT NOT NULL DEFAULT 'gpt',
  stripe_customer_id  TEXT UNIQUE,
  last_seen_at        TIMESTAMPTZ,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_gw_users_google_id ON public.gw_users(google_id);
CREATE INDEX IF NOT EXISTS idx_gw_users_email     ON public.gw_users(email);

-- --------------------------------------------------------------------
-- 2. OAuth provider state
-- --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.gw_oauth_authz_session (
  state           TEXT PRIMARY KEY,
  client_id       TEXT NOT NULL,
  redirect_uri    TEXT NOT NULL,
  scope           TEXT,
  original_state  TEXT NOT NULL,
  expires_at      TIMESTAMPTZ NOT NULL,
  created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.gw_oauth_codes (
  code          TEXT PRIMARY KEY,
  user_id       UUID NOT NULL REFERENCES public.gw_users(id) ON DELETE CASCADE,
  redirect_uri  TEXT NOT NULL,
  scope         TEXT,
  expires_at    TIMESTAMPTZ NOT NULL,
  used_at       TIMESTAMPTZ,
  created_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.gw_oauth_tokens (
  access_token         TEXT PRIMARY KEY,
  refresh_token        TEXT UNIQUE NOT NULL,
  user_id              UUID NOT NULL REFERENCES public.gw_users(id) ON DELETE CASCADE,
  expires_at           TIMESTAMPTZ NOT NULL,
  refresh_expires_at   TIMESTAMPTZ NOT NULL,
  scope                TEXT DEFAULT 'read:astro write:query',
  created_at           TIMESTAMPTZ DEFAULT NOW(),
  revoked_at           TIMESTAMPTZ,
  rotated_to           TEXT
);

CREATE INDEX IF NOT EXISTS idx_gw_oauth_tokens_user
  ON public.gw_oauth_tokens(user_id) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_gw_oauth_tokens_refresh_active
  ON public.gw_oauth_tokens(refresh_token) WHERE revoked_at IS NULL;

-- --------------------------------------------------------------------
-- 3. Query logs (per-bot, used for quota + analytics)
-- --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.gw_query_log (
  id                  BIGSERIAL PRIMARY KEY,
  user_id             UUID NOT NULL REFERENCES public.gw_users(id),
  email               TEXT NOT NULL,
  bot_slug            TEXT NOT NULL CHECK (bot_slug IN ('prashna','horoscope','career','marriage')),
  query_text          TEXT,
  query_type          TEXT,
  birth_details_json  JSONB,
  n8n_response_ms     INTEGER,
  was_paid_query      BOOLEAN NOT NULL DEFAULT FALSE,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_gw_query_log_quota
  ON public.gw_query_log(user_id, bot_slug, created_at DESC)
  WHERE was_paid_query = FALSE;

CREATE INDEX IF NOT EXISTS idx_gw_query_log_user_created
  ON public.gw_query_log(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS public.gw_query_error_log (
  id              BIGSERIAL PRIMARY KEY,
  user_id         UUID,
  bot_slug        TEXT,
  error_type      TEXT,
  error_message   TEXT,
  request_body    JSONB,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- --------------------------------------------------------------------
-- 4. Subscriptions (the contract between gateway + website Edge Functions)
-- --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.gw_subscriptions (
  id                              BIGSERIAL PRIMARY KEY,
  user_id                         UUID NOT NULL REFERENCES public.gw_users(id) ON DELETE CASCADE,
  plan                            TEXT NOT NULL CHECK (plan IN ('day_pass','monthly','master')),
  bot_slug                        TEXT NOT NULL CHECK (bot_slug IN ('prashna','horoscope','career','marriage','all')),
  status                          TEXT NOT NULL CHECK (status IN ('active','cancelled','past_due','expired')),
  expires_at                      TIMESTAMPTZ NOT NULL,
  stripe_customer_id              TEXT,
  stripe_subscription_id          TEXT UNIQUE,
  stripe_checkout_session_id      TEXT UNIQUE,
  cancel_at_period_end            BOOLEAN DEFAULT FALSE,
  created_at                      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at                      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_gw_subs_active_lookup
  ON public.gw_subscriptions(user_id, bot_slug, expires_at)
  WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_gw_subs_user_history
  ON public.gw_subscriptions(user_id, created_at DESC);

-- --------------------------------------------------------------------
-- 5. Stripe webhook idempotency log (written by website Edge Function)
-- --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.gw_stripe_webhook_log (
  id              BIGSERIAL PRIMARY KEY,
  event_id        TEXT UNIQUE NOT NULL,
  event_type      TEXT NOT NULL,
  payload         JSONB NOT NULL,
  received_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  processed_at    TIMESTAMPTZ,
  status          TEXT NOT NULL CHECK (status IN ('received','processed','failed','duplicate')),
  error_message   TEXT
);

CREATE INDEX IF NOT EXISTS idx_gw_stripe_webhook_log_event
  ON public.gw_stripe_webhook_log(event_id);

-- --------------------------------------------------------------------
-- 6. Email send log (written by website Edge Function via Resend)
-- --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.gw_email_send_log (
  id              BIGSERIAL PRIMARY KEY,
  user_id         UUID REFERENCES public.gw_users(id),
  to_email        TEXT NOT NULL,
  template        TEXT NOT NULL,
  resend_id       TEXT,
  status          TEXT NOT NULL CHECK (status IN ('sent','failed')),
  error_message   TEXT,
  sent_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- --------------------------------------------------------------------
-- 7. updated_at trigger for subscriptions
-- --------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.gw_tg_subscriptions_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS gw_subscriptions_updated_at ON public.gw_subscriptions;
CREATE TRIGGER gw_subscriptions_updated_at
  BEFORE UPDATE ON public.gw_subscriptions
  FOR EACH ROW EXECUTE FUNCTION public.gw_tg_subscriptions_updated_at();

COMMIT;
