-- ====================================================================
-- AskAstroBot Gateway - migration 0002: MCP (ChatGPT plugin) support
--
-- Additive only: nullable columns, constant defaults, one constraint, two
-- indexes. Existing GPT Action rows keep working (new columns stay NULL /
-- default 'gpt').
--
-- APPLY THIS BEFORE DEPLOYING THE GATEWAY CODE THAT USES IT
-- (app/gpt/proxy.py reads gw_query_log.channel).
--
-- Run each PART separately in the Supabase SQL editor (PART 3 cannot run
-- inside a transaction). Idempotent: safe to re-run.
-- ====================================================================

-- ---------- PART 1: columns (metadata-only in PG 11+, fails fast if blocked)
BEGIN;
SET LOCAL lock_timeout = '5s';

ALTER TABLE public.gw_oauth_authz_session
  ADD COLUMN IF NOT EXISTS code_challenge        TEXT,
  ADD COLUMN IF NOT EXISTS code_challenge_method TEXT,
  ADD COLUMN IF NOT EXISTS resource              TEXT;

ALTER TABLE public.gw_oauth_codes
  ADD COLUMN IF NOT EXISTS client_id             TEXT,
  ADD COLUMN IF NOT EXISTS code_challenge        TEXT,
  ADD COLUMN IF NOT EXISTS code_challenge_method TEXT,
  ADD COLUMN IF NOT EXISTS resource              TEXT;

-- resource IS NULL  -> legacy Custom GPT token (accepted only by /v1/gpt/*)
-- resource = <mcp>  -> MCP token (accepted only by /mcp)
ALTER TABLE public.gw_oauth_tokens
  ADD COLUMN IF NOT EXISTS client_id TEXT,
  ADD COLUMN IF NOT EXISTS resource  TEXT;

-- MCP rows store NO question text and NO birth details; input_hash is a keyed HMAC.
ALTER TABLE public.gw_query_log
  ADD COLUMN IF NOT EXISTS channel    TEXT NOT NULL DEFAULT 'gpt',
  ADD COLUMN IF NOT EXISTS tool_name  TEXT,
  ADD COLUMN IF NOT EXISTS input_hash TEXT,
  -- TRUE = identical repeat inside the dedupe window: counts for the rate limit, never for quota.
  ADD COLUMN IF NOT EXISTS is_repeat  BOOLEAN NOT NULL DEFAULT FALSE;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'gw_query_log_channel_chk') THEN
    ALTER TABLE public.gw_query_log
      ADD CONSTRAINT gw_query_log_channel_chk CHECK (channel IN ('gpt', 'mcp')) NOT VALID;
  END IF;
END $$;
COMMIT;

-- ---------- PART 2: validate the constraint (scans without blocking writes)
ALTER TABLE public.gw_query_log VALIDATE CONSTRAINT gw_query_log_channel_chk;

-- ---------- PART 3: indexes, built without blocking (run outside a transaction)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_gw_query_log_mcp_user
  ON public.gw_query_log(user_id, created_at DESC)
  WHERE channel = 'mcp';

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_gw_query_log_mcp_dedupe
  ON public.gw_query_log(user_id, input_hash, created_at DESC)
  WHERE channel = 'mcp';
