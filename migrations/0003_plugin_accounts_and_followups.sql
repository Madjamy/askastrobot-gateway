-- ====================================================================
-- AskAstroBot Gateway - migration 0003: one AskAstroBot account for the
-- plugin and the website, and "follow-up" charging.
--
-- Additive only. Apply BEFORE deploying the code that uses it.
-- Run PART 1 and PART 2 separately (PART 2 cannot run inside a transaction).
-- Idempotent.
-- ====================================================================

-- ---------- PART 1
BEGIN;
SET LOCAL lock_timeout = '5s';

-- Link to the website account (Supabase Auth user id). The plugin signs in
-- through the website's account system, so a purchase on askastrobot.com
-- applies to the same plugin account regardless of the ChatGPT login.
ALTER TABLE public.gw_users
  ADD COLUMN IF NOT EXISTS supabase_user_id UUID;

-- PKCE verifier for the gateway -> Supabase Auth leg of the plugin sign-in,
-- and failed password attempts per sign-in session.
ALTER TABLE public.gw_oauth_authz_session
  ADD COLUMN IF NOT EXISTS supabase_code_verifier TEXT,
  ADD COLUMN IF NOT EXISTS password_attempts      INTEGER NOT NULL DEFAULT 0;

-- Keyed HMAC of the person's birth details (no birth data stored): lets extra
-- charts / transits for the same person count as a follow-up, not a new question.
ALTER TABLE public.gw_query_log
  ADD COLUMN IF NOT EXISTS subject_hash TEXT;
COMMIT;

-- ---------- PART 2 (outside a transaction)
CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_gw_users_supabase_user_id
  ON public.gw_users(supabase_user_id) WHERE supabase_user_id IS NOT NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_gw_query_log_mcp_subject
  ON public.gw_query_log(user_id, subject_hash, created_at DESC)
  WHERE channel = 'mcp';

-- ---------- PART 3: verify (fails if an interrupted CONCURRENTLY build left an
-- invalid index; if so: DROP INDEX CONCURRENTLY <name>; then re-run PART 2).
DO $$
DECLARE bad TEXT;
BEGIN
  SELECT string_agg(c.relname, ', ') INTO bad
    FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
   WHERE c.relname IN ('uq_gw_users_supabase_user_id', 'idx_gw_query_log_mcp_subject',
                       'idx_gw_query_log_mcp_user', 'idx_gw_query_log_mcp_dedupe')
     AND NOT i.indisvalid;
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'Invalid index(es): %. Drop and rebuild before deploying.', bad;
  END IF;
  IF (SELECT count(*) FROM pg_class WHERE relname IN ('uq_gw_users_supabase_user_id', 'idx_gw_query_log_mcp_subject')) <> 2 THEN
    RAISE EXCEPTION 'Migration 0003 indexes missing.';
  END IF;
END $$;
