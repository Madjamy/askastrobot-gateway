# AskAstroBot Custom GPT Paywall & OAuth Gateway — Design Spec

| | |
|---|---|
| **Project codename** | AskAstroBot Gateway (AAB-GW) |
| **Spec date** | 2026-05-04 |
| **Author** | Claude Code, with Madhur Sharma |
| **Status** | Approved (rev 2 — review patches + email feature) |
| **Source brief** | `askastrobot_gateway_build_brief.md` v1.0 |
| **Revision** | rev 2: applied spec-reviewer fixes + customer email feature |

This spec supersedes the source brief on the points it explicitly revises. Anything not revised here defers to the brief.

---

## 1. Goal

Replace the bypassable, donation-soft 2-query limit on the four AskAstroBot Custom GPTs with a **server-side OAuth-gated paywall** that:

- Authenticates every user via Google sign-in (one-time, inside ChatGPT).
- Enforces a hard 2-queries-per-bot-per-rolling-24h free tier in the database (uncircumventable by starting a new chat).
- Unlocks unlimited use behind Stripe-backed subscriptions or a 24-hour day pass.
- Logs every query against an identity for analytics.
- Leaves the existing n8n workflows + astro pipeline + JSON contract untouched.
- Splits responsibility so the **gateway** owns auth + quota + proxy, and the **website** (Lovable + Supabase Edge Functions) owns Stripe + subscription UI.

## 2. Why these revisions to the source brief

Five items in the v1.0 brief assume textbook behavior that doesn't match how ChatGPT Custom GPT Actions actually behave or how your existing AskAstroBot stack is organized. This spec changes them:

| # | Brief said | This spec says | Reason |
|---|---|---|---|
| 1 | Return HTTP 402 with paywall body | Return HTTP 200 with a structured paywall body | ChatGPT only specially handles 429 / 5xx. 402 is treated as a generic error and the upgrade link won't reliably render. Markdown links only render clickable when they appear in a model response. |
| 2 | Implement OAuth (any flow) | OAuth Authorization Code, **no PKCE**, `/token` reads form-encoded body, return **401 only** for expired tokens | Custom GPT Actions don't support PKCE. `/token` parses form-encoded body, not JSON. Only HTTP 401 triggers ChatGPT's silent re-auth; 400/403 strands the user with "missing access token" and no recovery UX. |
| 3 | Node.js + TypeScript + Express | Python 3.12 + FastAPI | Existing askastrobot services (`kundali-pdf`, `soul-purpose`) are FastAPI. Same runtime, same Docker pattern, no new toolchain. |
| 4 | Gateway hosts `/v1/checkout/create-session` and `/stripe/webhook` | Stripe Checkout creation + Stripe webhook live in **Supabase Edge Functions** on the website side | Lovable + Supabase Edge Functions is the website's natural payments pattern. Gateway never touches Stripe. The two systems share Supabase, so the only contract between them is the `subscriptions` table shape. |
| 5 | Upgrade page reads `?uid=…&token=…&bot=…` | Upgrade page reads `?token=…&bot=…` only — `user_id` is **inside** the signed JWT, not in the URL | Putting `user_id` in a separate query param invites tampering and identity-drift bugs. The signed token IS the identity, end-to-end. |

Pricing is also updated per user direction (see §7).

## 2.1 Rev-2 patches (applied 2026-05-04)

The following changes were applied after the spec-reviewer pass and a user-requested email-confirmation feature.

| # | Change | Location |
|---|---|---|
| P1 | OAuth redirect-URI: drop the manual per-bot allowlist; use a single regex `^https://(chat\.openai\.com\|chatgpt\.com)/aip/g-[A-Za-z0-9]+/oauth/callback$`. Removes the §13.5 step-6 manual register-the-URI ritual. | §6.1, §13.5 |
| P2 | Quota race fix: replace SELECT-then-INSERT with a single atomic CTE that conditionally inserts only when count < 2. Specified in §7.6. | §7, §8 |
| P3 | Subscription uniqueness: add `UNIQUE(stripe_subscription_id)` and `UNIQUE(stripe_checkout_session_id)` to `subscriptions`. Webhook handler uses `INSERT ... ON CONFLICT DO UPDATE` (upsert). | §7.4, §9.2 |
| P4 | Refresh-token rotation race: use atomic `UPDATE ... WHERE refresh_token=$1 AND revoked_at IS NULL RETURNING user_id`; only mint new pair if the UPDATE returned a row. 30-second grace window where the immediately-rotated old token still validates. | §6.3 |
| P5 | Identity-binding lock: `users.stripe_customer_id` find-or-create uses `INSERT ... ON CONFLICT (id) DO UPDATE` with explicit row lock to prevent dual customer creation. | §5.2, §9.2 |
| P6 | `/v1/upgrade/validate` returns only `{user_id, bot_slug, valid_until}` (not `email`) when called from the browser; the Edge Function gets the email by re-validating server-side. Removes email leak via Referer. | §8, §9.2 |
| P7 | `/upgrade` page strips the JWT from the URL via `history.replaceState` immediately after reading it. | §9.1 |
| P8 | n8n timeout reduced from 38s to 30s to leave a real budget for gateway overhead under ChatGPT's 45s cap. | §8.2 |
| P9 | New §11.6 Rollback runbook: tagged Docker images, prior system-prompt commits, manual sub-revoke SQL. | §11 |
| P10 | New §11.7 Observability: structured JSON logs, Sentry, deep `/health`, UptimeRobot at launch. | §11 |
| P11 | Feature: customer payment-confirmation email + cancel-link (see §9.4). Three layers: Stripe automatic receipt + Stripe Customer Portal magic-link + branded Resend "Welcome to Premium" email. | new §9.4 |
| P12 | `birth_details_json` retention: 90 days, daily cleanup job. Mention in privacy policy. | §7.3 |

## 3. Pricing (final)

| Bot | Day Pass (24h) | Monthly |
|---|---|---|
| Prashna | $2.99 | $7.99 |
| Horoscope Analysis | $2.99 | $7.99 |
| Career Astrology | $2.99 | $5.99 |
| Kundali Milan (Marriage) | $2.99 | $5.99 |

**Master subscription (all four bots): $12.99 / month.**

All USD; Stripe Checkout handles local-currency display. Free tier remains: **2 queries per bot per rolling 24 hours per user.**

## 4. Architecture

### 4.1 Topology

```
ChatGPT user
    │ uses one of 4 Custom GPTs
    ▼
Custom GPT (Prashna / Horoscope / Career / Marriage)
    • OAuth: api.askastrobot.com
    • Action URL: api.askastrobot.com/v1/gpt/{slug}/query
    │
    │ Authorization: Bearer <gateway-minted access_token>
    ▼
api.askastrobot.com   ← NEW (Gateway, FastAPI on Hostinger VPS)
    1. Validate bearer → resolve user_id
    2. Check active subscription (subscriptions table)
       • Active for this bot OR master?  → forward
    3. Else check query_log (rolling 24h, this bot, this user, was_paid_query=FALSE)
       • count < 2 → insert log row, forward
       • count ≥ 2 → return 200 + paywall JSON
    4. On forward: POST to correct n8n URL with X-Gateway-Secret header
    │                                              ▲
    │                                              │ (no change to n8n)
    ▼                                              │
n8n webhook (existing) ────── astro pipeline (existing)


Free user hits paywall  →  clicks markdown link in chat  →

askastrobot.com/upgrade?token=<JWT>&bot=prashna   ← Lovable React page
    1. Calls api.askastrobot.com/v1/upgrade/validate?token=…
    2. Renders 3 buttons: Day Pass / Monthly / Master
    3. On click → POSTs to Supabase Edge Function

Supabase Edge Function: create-checkout-session
    1. Re-validates JWT via gateway
    2. Find or create stripe_customer for users.id (locks email)
    3. Creates Checkout Session with customer:cus_xxx (email is read-only)
    4. Returns Checkout URL → browser redirects to Stripe

Stripe Checkout (hosted)  →  user pays  →  redirects to /upgrade/success

Supabase Edge Function: stripe-webhook
    Receives checkout.session.completed / customer.subscription.* events
    Verifies Stripe signature
    Upserts public.gw_subscriptions row keyed by metadata.user_id
    Logs in stripe_webhook_log
```

### 4.2 Component responsibilities

| Component | Owns |
|---|---|
| **Gateway** (`api.askastrobot.com`, FastAPI, VPS) | OAuth provider for ChatGPT, gateway-minted access/refresh tokens, quota enforcement, sub lookup, n8n proxy, query logging, upgrade-token mint + validate. **Never touches Stripe.** |
| **Website** (`askastrobot.com`, Lovable React) | `/upgrade`, `/upgrade/success`, `/account/billing`. Reads JWT from URL — **never asks the user to log in for the paywall flow.** |
| **Supabase Edge Functions** (`create-checkout-session`, `stripe-webhook`, `create-portal-session`) | All Stripe API calls, all webhook receipts, all writes to `subscriptions`. **Never touches OAuth tokens or n8n.** |
| **Supabase DB** (shared, `bdtzzykdhszmdlvpzlku.supabase.co`) | Single source of truth for users, tokens, subs, logs. |
| **n8n workflows** (existing on VPS) | Astro pipeline (unchanged). Add only: header check on `X-Gateway-Secret` at workflow start. |

The contract between the gateway and the website-side is **the shape of the `subscriptions` table** plus the JWT format. That's it.

## 5. Identity binding (the "wrong-account-on-website" problem)

The user's concern: what if they sign in to ChatGPT with Google A, click the upgrade link, and accidentally log in to the website with Google B (or a different account entirely) and pay there? Then the payment is against the wrong identity.

**Resolution: the website never has a login step in the paywall flow.** Identity is established once, at OAuth-time inside ChatGPT, and carried as a signed token thereafter.

### 5.1 The signed upgrade token

When the gateway returns the paywall response (free quota exhausted, no active sub), it mints a JWT:

```
HS256, signed with GATEWAY_JWT_SECRET (32-byte hex, env var)

Header:  { "alg": "HS256", "typ": "JWT" }
Payload: {
  "iss": "api.askastrobot.com",
  "sub": "<users.id UUID>",
  "email": "<users.email>",
  "google_id": "<users.google_id>",
  "bot": "prashna" | "horoscope" | "career" | "marriage",
  "iat": <epoch>,
  "exp": <epoch + 900>,    // 15 minutes
  "purpose": "upgrade"
}
```

The token is the *only* identity carrier from chat → website → Stripe → webhook. It cannot be forged (HMAC), cannot be tampered with (signature), and expires in 15 minutes.

### 5.2 Why this prevents the wrong-identity scenario

- The `/upgrade` page **has no login form, no sign-in button, no auth provider integration.** Just `?token=…` parsing and a call to `/v1/upgrade/validate`.
- The Edge Function reads `user_id` and `email` from the *re-validated* token. It does not trust the URL.
- It looks up `users.stripe_customer_id`; if null, creates a new Stripe customer with `email = <token.email>` and stores the id back. **One gateway user → exactly one Stripe customer, forever, locked to the OAuth Google email.**
- It creates the Checkout Session with `customer: cus_xxx` (not `customer_email`). When `customer` is set, **Stripe Checkout makes the email field read-only** — the user cannot edit it.
- Webhook reads `metadata.user_id` (set at Checkout creation) and binds the resulting `subscriptions` row to that exact user. Belt-and-suspenders: if `metadata.user_id` is missing, fall back to looking up `users` by `stripe_customer_id`.

### 5.3 Edge cases

| Case | Behavior |
|---|---|
| Token expired (>15 min) | `/upgrade` shows "This link has expired — return to ChatGPT and ask any question to get a fresh link." Gateway issues a new token in the next paywall response. |
| User clicks an old upgrade link they bookmarked | Same as expired — re-issue from chat. |
| User shares the link with someone else who pays | Payment binds to original ChatGPT user (encoded in token). The original user gets the sub. No abuse vector worse than "someone bought you a sub." |
| User has two Google accounts on different devices | Two separate `users` rows with different `google_id`. They pay for whichever they're using. Out of scope for v1 — v2 can offer email-verified merge. |
| Card billing email differs from Google email | Fine. Stripe `customer.email` (locked) is the OAuth Google email; card billing is independent. |
| User wants to manage/cancel subscription | "Manage subscription" link in `/upgrade/success` and inside the GPT calls a portal-token endpoint, redirects to Stripe Customer Portal. Same JWT-only pattern, no website login. |

## 6. OAuth flow (Custom GPT ↔ Gateway)

Authorization Code flow, **no PKCE** (Custom GPT Actions don't support it). All endpoints on the gateway.

### 6.1 Authorize: `GET /oauth/authorize`

Query params from ChatGPT: `client_id, redirect_uri, response_type=code, scope=read:astro write:query, state`.

1. Validate `client_id` against the configured `OAUTH_CLIENT_ID`. **Reject with 401 if mismatch.**
2. Validate `redirect_uri` against the **strict regex** `^https://(chat\.openai\.com|chatgpt\.com)/aip/g-[A-Za-z0-9_-]+/oauth/callback$`. No manual per-bot allowlist required. Reject with 400 if mismatch (open-redirect protection).
3. **Echo `state`** through the entire flow — ChatGPT requires the original state to be returned in the callback.
4. Begin server-side Supabase Google sign-in: redirect to `<SUPABASE_URL>/auth/v1/authorize?provider=google&redirect_to=<GATEWAY_BASE_URL>/oauth/google-callback&state=<our-internal-state>`.
5. Stash the original `{client_id, redirect_uri, scope, state}` in a server-side `oauth_authz_session` row, keyed by our internal state.

### 6.2 Google callback: `GET /oauth/google-callback`

Supabase redirects here after Google auth.

1. Extract Supabase access token (server-side Supabase Auth handler).
2. Resolve the Google identity → email, google_id, name.
3. Upsert into `users` (insert if new, with `signup_source='gpt'`; update `last_seen_at`).
4. Look up the original authz session by state.
5. Generate a one-time `oauth_codes` row: `{code: random_64, user_id, redirect_uri, expires_at: now+5min}`.
6. Redirect the browser to the original `redirect_uri` (ChatGPT's callback) with `?code=<code>&state=<original-state>`.

### 6.3 Token: `POST /oauth/token`

Body: **`application/x-www-form-urlencoded`** (NOT JSON).

Two grant types:

**Auth code exchange:**
- Body: `grant_type=authorization_code, client_id, client_secret, code, redirect_uri`.
- Validate `client_id`, `client_secret`. On failure → **HTTP 401**.
- Validate the `oauth_codes` row: matches code + redirect_uri + not expired + not used.
- Mark `used_at = NOW()` (single-use).
- Mint:
  - `access_token` = random 64-byte hex, stored in `oauth_tokens` with TTL **30 days**.
  - `refresh_token` = random 64-byte hex, stored alongside, TTL **90 days**.
- Response (JSON):
  ```json
  { "access_token": "...", "token_type": "Bearer",
    "refresh_token": "...", "expires_in": 2592000, "scope": "read:astro write:query" }
  ```

**Refresh:**
- Body: `grant_type=refresh_token, client_id, client_secret, refresh_token`.
- On any failure (bad client creds, unknown refresh, expired, revoked) → **HTTP 401**. Never 400/403 — only 401 triggers ChatGPT's silent re-auth.
- **Atomic rotation** (race-safe):
  ```sql
  UPDATE gw_oauth_tokens
     SET revoked_at = NOW()
   WHERE refresh_token = $1
     AND revoked_at IS NULL
     AND refresh_expires_at > NOW()
   RETURNING user_id, scope;
  ```
  Only mint a new pair if the UPDATE returned a row. Two concurrent refreshes with the same token: only one wins; the other gets 401 and ChatGPT silently retries.
- **30-second grace window**: a token rotated within the last 30s still validates (returns the *new* pair, idempotent — handles network retries without forcing re-auth). Implemented as: on miss, look up `oauth_tokens` where `refresh_token=$1 AND revoked_at > NOW() - INTERVAL '30 seconds'`; if found, return the token row that was issued in the immediately following row's place.
- Response: same JSON shape as auth code exchange.

### 6.4 Bearer middleware (all `/v1/*` endpoints)

- Read `Authorization: Bearer <token>`.
- Look up `oauth_tokens` where `access_token = ? AND revoked_at IS NULL AND expires_at > NOW()`.
- On miss → **HTTP 401** (so ChatGPT auto-refreshes).
- On hit → set `req.state.user_id`, `req.state.email`, continue.

### 6.5 What ChatGPT sees

- First action attempt → ChatGPT auto-injects a "Sign in to AskAstroBot" button in chat.
- User clicks → popup goes to `/oauth/authorize` → Supabase Google → back to ChatGPT with code.
- Token exchange happens silently. Subsequent action calls include the bearer automatically.
- When access token expires, ChatGPT calls `/oauth/token` with `refresh_token` before the next action.
- If refresh returns 401, ChatGPT silently re-prompts sign-in.

## 7. Database schema (delta on shared Supabase)

Apply via migration `migrations/0001_aab_gateway.sql` in the new repo. Idempotent.

```sql
-- 7.1 Extend existing users table
ALTER TABLE users
  ADD COLUMN IF NOT EXISTS google_id TEXT UNIQUE,
  ADD COLUMN IF NOT EXISTS signup_source TEXT DEFAULT 'web',
  ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS stripe_customer_id TEXT UNIQUE;

-- 7.2 OAuth provider state (gateway-issued tokens for ChatGPT)
CREATE TABLE IF NOT EXISTS oauth_codes (
  code TEXT PRIMARY KEY,
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  redirect_uri TEXT NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL,
  used_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS oauth_tokens (
  access_token TEXT PRIMARY KEY,
  refresh_token TEXT UNIQUE NOT NULL,
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  expires_at TIMESTAMPTZ NOT NULL,
  refresh_expires_at TIMESTAMPTZ NOT NULL,
  scope TEXT DEFAULT 'read:astro write:query',
  created_at TIMESTAMPTZ DEFAULT NOW(),
  revoked_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_oauth_tokens_user ON oauth_tokens(user_id);
CREATE INDEX IF NOT EXISTS idx_oauth_tokens_refresh ON oauth_tokens(refresh_token) WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS oauth_authz_session (
  state TEXT PRIMARY KEY,
  client_id TEXT NOT NULL,
  redirect_uri TEXT NOT NULL,
  scope TEXT,
  original_state TEXT NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL
);

-- 7.3 Query logging
CREATE TABLE IF NOT EXISTS query_log (
  id BIGSERIAL PRIMARY KEY,
  user_id UUID NOT NULL REFERENCES users(id),
  email TEXT NOT NULL,
  bot_slug TEXT NOT NULL,
  query_text TEXT,
  query_type TEXT,
  birth_details_json JSONB,
  n8n_response_ms INTEGER,
  was_paid_query BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_query_log_quota
  ON query_log(user_id, bot_slug, created_at DESC)
  WHERE was_paid_query = FALSE;

-- Retention: scheduled job (pg_cron) deletes free-tier rows older than 90 days
-- to limit retention of birth_details_json (privacy-sensitive). Paid queries kept indefinitely.

CREATE TABLE IF NOT EXISTS query_error_log (
  id BIGSERIAL PRIMARY KEY,
  user_id UUID,
  bot_slug TEXT,
  error_type TEXT,
  error_message TEXT,
  request_body JSONB,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 7.4 Subscriptions (the contract between gateway and website)
CREATE TABLE IF NOT EXISTS subscriptions (
  id BIGSERIAL PRIMARY KEY,
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  plan TEXT NOT NULL CHECK (plan IN ('day_pass','monthly','master')),
  bot_slug TEXT NOT NULL CHECK (bot_slug IN ('prashna','horoscope','career','marriage','all')),
  status TEXT NOT NULL CHECK (status IN ('active','cancelled','past_due','expired')),
  expires_at TIMESTAMPTZ NOT NULL,
  stripe_customer_id TEXT,
  stripe_subscription_id TEXT UNIQUE,
  stripe_checkout_session_id TEXT UNIQUE,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);
-- Partial index optimised for the active-sub hot-path lookup
CREATE INDEX IF NOT EXISTS idx_subs_active_lookup
  ON subscriptions(user_id, bot_slug, expires_at)
  WHERE status = 'active';

-- 7.5 Stripe webhook audit (written by Edge Function, read by gateway for debugging)
CREATE TABLE IF NOT EXISTS stripe_webhook_log (
  id BIGSERIAL PRIMARY KEY,
  event_id TEXT UNIQUE NOT NULL,
  event_type TEXT NOT NULL,
  payload JSONB NOT NULL,
  processed_at TIMESTAMPTZ DEFAULT NOW(),
  status TEXT NOT NULL CHECK (status IN ('processed','failed','duplicate'))
);
```

**Active sub check (gateway hot path):**

```sql
SELECT 1 FROM gw_subscriptions
WHERE user_id = $1
  AND status = 'active'
  AND expires_at > NOW()
  AND bot_slug IN ($2, 'all')
LIMIT 1;
```

**Atomic quota-check-and-insert (gateway hot path, fixes race):**

The naive SELECT-then-INSERT has a race: two concurrent queries at count=1 can both read 1 and both insert. Replaced with a single CTE that inserts only when the count is below the limit, and returns whether it inserted:

```sql
WITH quota AS (
  SELECT COUNT(*) AS used
  FROM gw_query_log
  WHERE user_id = $1
    AND bot_slug = $2
    AND was_paid_query = FALSE
    AND created_at > NOW() - INTERVAL '24 hours'
),
ins AS (
  INSERT INTO query_log (user_id, email, bot_slug, query_text, query_type, birth_details_json, was_paid_query)
  SELECT $1, $3, $2, $4, $5, $6, FALSE
  WHERE (SELECT used FROM quota) < 2
  RETURNING id
)
SELECT
  (SELECT used FROM quota) AS prior_used,
  EXISTS (SELECT 1 FROM ins) AS allowed,
  (SELECT id FROM ins) AS log_id;
```

Returns `{prior_used, allowed, log_id}`. If `allowed=FALSE`, the gateway returns the paywall response. If `allowed=TRUE`, the row is already in `query_log` with the placeholder `n8n_response_ms = NULL`; the gateway updates `n8n_response_ms` after n8n responds successfully. On n8n failure, the gateway **deletes the row** by `log_id` (failed queries don't count) and writes to `query_error_log` instead.

This is one DB round trip and is atomic at row-insert time. Two concurrent queries at count=1 will each attempt the INSERT, but only one will succeed in pushing count past 2 within a single transaction's snapshot — the second one's `WHERE (SELECT used FROM quota) < 2` re-evaluation under MVCC is fine here because both queries are racing for the same logical "<2" outcome and both will allow at most one to win the count=2 boundary. To strictly serialize at the boundary, wrap in a `SELECT ... FROM gw_users WHERE id=$1 FOR UPDATE` advisory step (per-user lock). The implementation uses `pg_advisory_xact_lock(hashtext($1::text || $2))` for a per-(user,bot) lock — cheap, deadlock-free, and releases on transaction end.

## 8. Gateway endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/oauth/authorize` | client_id | Start OAuth (redirects to Supabase Google) |
| `GET` | `/oauth/google-callback` | Supabase session | Internal — Supabase returns here |
| `POST` | `/oauth/token` | client_id + secret (form body) | Auth code exchange + refresh. **Returns 401 on any failure.** |
| `POST` | `/v1/gpt/{bot_slug}/query` | Bearer | Quota + sub check + n8n forward + log |
| `GET`  | `/v1/upgrade/validate` | None (token in query) | Verify upgrade-JWT, return decoded payload |
| `POST` | `/v1/upgrade/portal-token` | Bearer | Mint a portal-access JWT (for "manage subscription" flow) |
| `GET`  | `/health` | None | Liveness probe |

`bot_slug ∈ {prashna, horoscope, career, marriage}`.

### 8.1 The paywall response shape

Returned from `/v1/gpt/{slug}/query` when free quota is exhausted and no active sub.

**HTTP 200** with JSON body (NOT 402):

```json
{
  "status": "free_limit_reached",
  "message": "You've used your 2 free queries for this bot in the last 24 hours. Upgrade to continue:",
  "upgrade_url": "https://askastrobot.com/upgrade?token=eyJhbGc...&bot=prashna",
  "options": [
    { "label": "24-hour Day Pass — $2.99", "plan": "day_pass" },
    { "label": "Monthly — $7.99", "plan": "monthly" },
    { "label": "All 4 bots — $12.99/mo", "plan": "master" }
  ],
  "instruction_to_model": "Render the message and the upgrade_url as a clickable markdown link. Do NOT call this action again until the user confirms upgrade."
}
```

System-prompt addition for each bot (replaces the donation rule):

> If the action response includes `"status": "free_limit_reached"`, render the `message` followed by the `upgrade_url` as a clickable markdown link, and list the `options`. Do NOT call the action again until the user explicitly confirms they have completed payment. If they say they have paid but the action still returns `"free_limit_reached"`, ask them to wait 30 seconds (Stripe webhook processing) and try again.

### 8.2 The n8n forwarding contract

- Gateway POSTs the original request body **unmodified** to the per-bot n8n URL.
- Adds header `X-Gateway-Secret: <GATEWAY_SHARED_SECRET>`.
- Timeout: **30 seconds** (leaves real headroom under ChatGPT's 45s cap for gateway overhead, DB queries, log writes, and response serialization).
- On n8n 2xx → return n8n's response body to ChatGPT, write `query_log` row with `n8n_response_ms`.
- On n8n non-2xx, timeout, or network error → return **HTTP 503** to ChatGPT with `{"status":"upstream_unavailable","message":"Astrology engine is temporarily unavailable. Please try again in a moment."}`. **Do NOT write a `query_log` row** — failed queries don't count against quota. Write to `query_error_log` instead.

### 8.3 Bot slug → n8n URL mapping (env vars)

```
N8N_WEBHOOK_PRASHNA   = https://app.askastrobot.com/webhook/e6971529-467e-43d6-9224-3bdce40f4b3f
N8N_WEBHOOK_HOROSCOPE = https://app.askastrobot.com/webhook/1124ff92-9662-4167-bc00-da7420919f75
N8N_WEBHOOK_CAREER    = https://app.askastrobot.com/webhook/dcb303fc-1346-403a-a261-e5e1705b9aa5
N8N_WEBHOOK_MARRIAGE  = https://app.askastrobot.com/webhook/35f8126a-f8ae-4bac-9211-ad0fc25e6e04
```

## 9. Lovable / website hand-off

This is the spec the Lovable team must implement on `askastrobot.com`. The gateway and the website are completely decoupled aside from the `subscriptions` table contract and the JWT format.

### 9.1 Pages

#### `/upgrade?token=<jwt>&bot=<slug>`

1. On mount: parse `token` and `bot` from URL.
2. Fetch `GET https://api.askastrobot.com/v1/upgrade/validate?token=<token>`.
   - `200` → render the page with the returned `{user_id, email, bot_slug}`.
   - `401` → render an "expired link" message: *"This upgrade link has expired. Return to ChatGPT and ask any question to get a fresh link."* No retry button needed — the GPT will issue a new one.
3. Render exactly three buttons in this order, each with a clear price:
   - **"24-hour Day Pass — $2.99"**  → `plan: day_pass, bot_slug: <bot>`
   - **"Monthly — $7.99 or $5.99"** (interpolate based on bot) → `plan: monthly, bot_slug: <bot>`
   - **"All 4 bots — $12.99/mo"** → `plan: master, bot_slug: 'all'`
4. On click: POST to Edge Function `create-checkout-session` with `{token, plan, bot_slug}`. Receive `{checkout_url}`. Redirect.
5. Display the user's email (read-only, from token) above the buttons: *"Signed in as <email>"*.

#### `/upgrade/success`

- Static page.
- Copy: *"You're unlocked. Return to ChatGPT and continue your question."*
- Include a small "Manage subscription" link → `/account/billing` (which redirects to Stripe Customer Portal).
- Optional: trigger a one-time confetti / celebration animation.

#### `/account/billing` (optional v1, recommended)

- This page is reachable only via a fresh portal-token from the GPT (the GPT's system prompt instructs it: "If the user asks to manage or cancel their subscription, call the action with `query_text: 'manage_subscription'` and the gateway returns a portal link.").
- Reads `?token=<portal_jwt>` from URL, calls Edge Function `create-portal-session`, redirects to Stripe Customer Portal.

### 9.2 Supabase Edge Functions

These run as TypeScript Deno functions inside Supabase. The team should already be familiar with the pattern (the blog endpoint `create-blog-post` is one).

#### Function 1: `create-checkout-session`

**Trigger:** POST from `/upgrade` page.

**Input:**
```json
{ "token": "<jwt>", "plan": "day_pass|monthly|master", "bot_slug": "prashna|horoscope|career|marriage|all" }
```

**Logic:**
1. Re-validate the JWT via `GET https://api.askastrobot.com/v1/upgrade/validate?token=<token>`. On 401 → return 401.
2. Extract `user_id`, `email` from the validation response.
3. Validate `bot_slug`: if `plan == 'master'`, require `bot_slug == 'all'`; otherwise require `bot_slug` matches the token's `bot`.
4. Find or create Stripe customer:
   ```ts
   let { data: user } = await supabase.from('users').select('stripe_customer_id').eq('id', user_id).single();
   if (!user.stripe_customer_id) {
     const cust = await stripe.customers.create({ email, metadata: { user_id } });
     await supabase.from('users').update({ stripe_customer_id: cust.id }).eq('id', user_id);
     user.stripe_customer_id = cust.id;
   }
   ```
5. Resolve the Stripe price ID from a static map (env vars):
   ```
   PRICE_DAY_PASS_PRASHNA   PRICE_MONTHLY_PRASHNA
   PRICE_DAY_PASS_HOROSCOPE PRICE_MONTHLY_HOROSCOPE
   PRICE_DAY_PASS_CAREER    PRICE_MONTHLY_CAREER
   PRICE_DAY_PASS_MARRIAGE  PRICE_MONTHLY_MARRIAGE
   PRICE_MASTER_MONTHLY
   ```
6. Create the Checkout Session:
   ```ts
   const session = await stripe.checkout.sessions.create({
     customer: user.stripe_customer_id,
     mode: plan === 'day_pass' ? 'payment' : 'subscription',
     line_items: [{ price: priceId, quantity: 1 }],
     success_url: 'https://askastrobot.com/upgrade/success',
     cancel_url:  'https://askastrobot.com/upgrade?token=<token>&bot=<bot>&cancelled=1',
     metadata: { user_id, plan, bot_slug },
     // For subscription mode, also propagate metadata to the subscription itself:
     subscription_data: plan !== 'day_pass' ? { metadata: { user_id, plan, bot_slug } } : undefined
   });
   return { checkout_url: session.url };
   ```

**Output:** `{ "checkout_url": "https://checkout.stripe.com/..." }` or `{ "error": "..." }` with appropriate HTTP code.

#### Function 2: `stripe-webhook`

**Trigger:** Stripe → `https://<project>.supabase.co/functions/v1/stripe-webhook`.

**Logic:**
1. Verify Stripe signature using `STRIPE_WEBHOOK_SECRET`. On failure → 400.
2. Idempotency: insert into `stripe_webhook_log` with `event.id`. If duplicate (UNIQUE violation), log `status='duplicate'` and return 200.
3. Branch on event type:

   - **`checkout.session.completed`** (covers both day passes and new subscriptions):
     - Read `session.metadata.user_id`, `session.metadata.plan`, `session.metadata.bot_slug`.
     - For `plan == 'day_pass'`: insert a new `subscriptions` row with `expires_at = NOW() + INTERVAL '24 hours'`, `status='active'`, `stripe_checkout_session_id = session.id`. Day passes do NOT have a `stripe_subscription_id` — they're one-time payments.
     - For `plan == 'monthly'` or `'master'`: insert a `subscriptions` row with `stripe_subscription_id = session.subscription`, `expires_at` from the subscription's `current_period_end`, `status='active'`.

   - **`customer.subscription.updated`** (renewals, plan changes):
     - Find row by `stripe_subscription_id`. Update `expires_at = current_period_end`, `status` based on Stripe `status` (`active`, `past_due`, `cancelled`).

   - **`customer.subscription.deleted`** (cancellation effective):
     - Find row by `stripe_subscription_id`. Set `status='cancelled'`. **Do NOT change `expires_at`** — access continues until the period end.

   - **`invoice.payment_failed`**:
     - Find row by `stripe_subscription_id`. Set `status='past_due'`.

4. Update `stripe_webhook_log.status='processed'` (or `'failed'` with error).

**Important:** the Edge Function is the **only** writer to the `subscriptions` table. The gateway is read-only. This eliminates write-write races.

#### Function 3: `create-portal-session`

**Input:** `{ "token": "<portal-jwt>" }`.

**Logic:**
1. Re-validate JWT via gateway.
2. Look up `users.stripe_customer_id` by `user_id`.
3. `stripe.billingPortal.sessions.create({ customer, return_url: 'https://askastrobot.com/account/billing/done' })`.
4. Return `{ portal_url }`.

### 9.3 Stripe Dashboard configuration (Madhur does this manually)

#### Products (recurring monthly unless noted)

| Stripe Product | Price | Type | Env var name |
|---|---|---|---|
| AskAstroBot — Prashna Day Pass | $2.99 | one-time | `PRICE_DAY_PASS_PRASHNA` |
| AskAstroBot — Prashna Monthly | $7.99 | recurring monthly | `PRICE_MONTHLY_PRASHNA` |
| AskAstroBot — Horoscope Day Pass | $2.99 | one-time | `PRICE_DAY_PASS_HOROSCOPE` |
| AskAstroBot — Horoscope Monthly | $7.99 | recurring monthly | `PRICE_MONTHLY_HOROSCOPE` |
| AskAstroBot — Career Day Pass | $2.99 | one-time | `PRICE_DAY_PASS_CAREER` |
| AskAstroBot — Career Monthly | $5.99 | recurring monthly | `PRICE_MONTHLY_CAREER` |
| AskAstroBot — Marriage Day Pass | $2.99 | one-time | `PRICE_DAY_PASS_MARRIAGE` |
| AskAstroBot — Marriage Monthly | $5.99 | recurring monthly | `PRICE_MONTHLY_MARRIAGE` |
| AskAstroBot — Master Monthly (all bots) | $12.99 | recurring monthly | `PRICE_MASTER_MONTHLY` |

#### Webhook
- Endpoint: `https://<supabase-project>.supabase.co/functions/v1/stripe-webhook`
- Events: `checkout.session.completed`, `customer.subscription.updated`, `customer.subscription.deleted`, `invoice.payment_failed`
- Copy signing secret → Edge Function env var `STRIPE_WEBHOOK_SECRET`.

#### Customer Portal
- Stripe Dashboard → Settings → Customer Portal → enable, configure: customers can cancel, swap plan, update payment, view invoices.
- **Enable "Login link"** — Stripe sends a magic-link email when a user types their email on the portal landing page. Lets users self-serve cancel/manage **without our website having any login**.
- Configure cancellation: **"Cancel at end of billing period"** (NOT immediate cancel). This ensures users keep access for the period they paid for.

### 9.4 Customer email layer (rev-2 feature)

Three independent emails fire on a successful payment, in order of when they arrive:

#### Layer 1 — Stripe automatic receipt (zero code)
- **Sender:** Stripe (`receipts@stripe.com`).
- **Trigger:** automatic on every successful charge (one-time + recurring).
- **Setup:** Stripe Dashboard → Settings → Customer emails → ✅ "Successful payments" + ✅ "Refunds".
- **Content:** Stripe-templated receipt with line item, amount, card last-4, hosted invoice link. Customer can click the hosted invoice link from any device to view/download.

#### Layer 2 — Stripe Customer Portal magic-link (zero code)
- **Trigger:** customer goes to a Stripe-hosted login page (URL provided in receipts and welcome email) and types their email.
- **Setup:** Stripe Dashboard → Settings → Customer Portal → ✅ "Login link".
- **Use:** the cancel-anytime escape hatch. Even if the user loses every link we send, they can go to that page, type their email, and Stripe emails them a one-click portal-access link.

#### Layer 3 — Branded "Welcome to Premium" email via Resend (Edge Function code)

Sent by the `stripe-webhook` Edge Function on `checkout.session.completed`, after the `subscriptions` row is committed.

**Triggered by event types:** only `checkout.session.completed` (NOT renewals — renewals get the Stripe receipt only, not a re-onboarding email).

**Edge Function adds:**

```ts
// after writing the subscriptions row, in stripe-webhook
const portalSession = await stripe.billingPortal.sessions.create({
  customer: stripe_customer_id,
  return_url: 'https://askastrobot.com/account/billing/done'
});

await fetch('https://api.resend.com/emails', {
  method: 'POST',
  headers: {
    'Authorization': `Bearer ${RESEND_API_KEY}`,
    'Content-Type': 'application/json'
  },
  body: JSON.stringify({
    from: 'AskAstroBot <hello@askastrobot.com>',
    to: email,
    subject: planLabel(plan, bot_slug) + ' active — start asking',
    html: renderWelcomeHtml({
      plan, bot_slug, expires_at,
      manage_url: portalSession.url,
      chatgpt_url: 'https://chatgpt.com'  // returns to ChatGPT home; user picks their bot
    })
  })
});
```

**Email content (template):**

```
Subject: Your AskAstroBot {plan label} is active — return to your reading

Hi,

Thank you for upgrading. Your {plan label} for {bot name} is now active.

  Plan:    {plan label}
  Bot(s):  {bot or "All four bots"}
  Active until: {expires_at, formatted}
  Amount:  {amount with currency}

You can return to your conversation here:
  → {chatgpt_url}

To manage or cancel your subscription, use this link:
  → {manage_url}    (valid for 24 hours; if expired, see below)

If this link expires later, you can always:
  • Reply to a Stripe receipt email — every receipt has a "Manage subscription" link
  • Or, in any AskAstroBot bot, ask "manage my subscription" and the bot will give you a fresh link

Om Namah Shivaya,
AskAstroBot
```

**`manage_url` notes:**
- The portal session URL Stripe returns is **24-hour valid**. Long enough to be useful from email; short enough to limit damage if the email is forwarded.
- After 24h, the user uses the in-GPT path (Scenario 3) or Stripe's own magic-link page.

**Edge Function env vars (Resend):**
```
RESEND_API_KEY=<from Resend dashboard, same key the kundali pipeline uses>
```

**Failure handling:** if the Resend call fails, log to `email_send_log` table but **do not fail the webhook** — the Stripe receipt (Layer 1) is the authoritative payment confirmation. The branded email is a nice-to-have.

**Schema addition:**

```sql
CREATE TABLE IF NOT EXISTS email_send_log (
  id BIGSERIAL PRIMARY KEY,
  user_id UUID REFERENCES users(id),
  to_email TEXT NOT NULL,
  template TEXT NOT NULL,           -- 'welcome_premium', 'cancellation_confirmed', etc.
  resend_id TEXT,
  status TEXT NOT NULL CHECK (status IN ('sent','failed')),
  error_message TEXT,
  sent_at TIMESTAMPTZ DEFAULT NOW()
);
```

**Future cancellation-confirmation email (v1.5):** when `customer.subscription.deleted` fires, send a "Subscription cancelled — access continues until X" email via Resend. Same pattern. Out of scope for v1.

## 10. Sequence diagrams (text)

### 10.1 First-time auth + free query

```
ChatGPT user: "What does my Saturn return mean?"
Custom GPT (Prashna): attempts action call → no token
ChatGPT: shows "Sign in to AskAstroBot" button
User clicks → opens /oauth/authorize
Gateway → Supabase Google OAuth → user picks account
Supabase → /oauth/google-callback
Gateway: upserts user, mints oauth_codes row, redirects to ChatGPT callback with ?code=...&state=...
ChatGPT: POST /oauth/token (grant_type=authorization_code) → access+refresh tokens
ChatGPT: retries action with Bearer token
Gateway: validates token → user_id resolved
Gateway: subscription check → no active sub
Gateway: quota check → 0 free queries used in last 24h
Gateway: forwards to n8n with X-Gateway-Secret
n8n: returns astro context JSON
Gateway: inserts query_log row (was_paid_query=FALSE), returns n8n body
ChatGPT: model interprets and answers user
```

### 10.2 Hitting the paywall

```
User asks 3rd query in 24h on the same bot
Gateway: subscription check → no active sub
Gateway: quota check → 2 free queries used → BLOCK
Gateway: mint upgrade JWT (15-min TTL)
Gateway: returns 200 with { status: "free_limit_reached", upgrade_url, options, instruction_to_model }
ChatGPT model: renders the message + clickable markdown link
User: clicks link → browser opens askastrobot.com/upgrade?token=...&bot=prashna
```

### 10.3 Pay and unlock

```
/upgrade page: GET /v1/upgrade/validate → { user_id, email, bot_slug }
User: clicks "Day Pass — $2.99"
Browser → POST Edge Function create-checkout-session
Edge Function: re-validates JWT, finds-or-creates Stripe customer, creates Checkout Session
Edge Function: returns { checkout_url }
Browser: redirects to checkout.stripe.com/...
User: pays
Stripe: redirects to /upgrade/success
Stripe: fires checkout.session.completed → Edge Function stripe-webhook
Edge Function: verifies signature, dedup against stripe_webhook_log
Edge Function: inserts subscriptions row { user_id, plan: 'day_pass', bot_slug, expires_at: NOW()+24h, status: 'active' }
User: returns to ChatGPT, asks a query
Gateway: subscription check → active row found → forward
Gateway: forwards to n8n, logs query_log with was_paid_query=TRUE
```

### 10.4 Renewal / cancellation

```
30 days later, Stripe auto-renews monthly sub
Stripe: fires customer.subscription.updated
Edge Function: updates subscriptions.expires_at to new current_period_end

User cancels in Stripe Customer Portal
Stripe: fires customer.subscription.updated with status=active, cancel_at_period_end=true
Edge Function: updates row, status remains 'active' until expires_at
At period end, Stripe fires customer.subscription.deleted
Edge Function: status='cancelled', expires_at unchanged
Gateway: subscription check after expires_at → no active sub → free tier resumes
```

## 11. Deployment

### 11.1 Repo + path

- **New repo:** `Madjamy/askastrobot-gateway` (separate, per user direction).
- **Local clone path:** `c:\Users\madhu\OneDrive\AI Project\Cursor\askastrobot-gateway\` (alongside the existing AskAstrobot folder).
- **VPS clone path:** `/opt/askastrobot/gateway/` (sibling to `/opt/askastrobot/kundali-pdf/`).

### 11.2 Stack

| | |
|---|---|
| Language | Python 3.12 |
| Framework | FastAPI + Uvicorn |
| Container | Docker (matching existing `kundali-pdf` Dockerfile pattern) |
| Reverse proxy | Existing Traefik on `root_default` network |
| TLS | Existing Traefik `mytlschallenge` resolver |
| Process manager | Docker (no PM2; matches your other services) |
| CI/CD | GitHub Actions on push to `main` → SSH to VPS → `git pull && docker compose up -d --build` |

### 11.3 Project layout

```
askastrobot-gateway/
├── .env.example
├── .github/workflows/deploy.yml
├── docker-compose.yml             # Traefik labels, port 8003, root_default network
├── Dockerfile                     # python:3.12-slim, FastAPI + uvicorn
├── requirements.txt
├── README.md                      # ops runbook
├── migrations/
│   └── 0001_aab_gateway.sql       # the schema in §7
├── app/
│   ├── main.py                    # FastAPI app, lifespan, router includes
│   ├── settings.py                # pydantic-settings, env loading
│   ├── db.py                      # asyncpg pool, supabase service-role client
│   ├── deps.py                    # bearer auth dependency, user resolver
│   ├── oauth/
│   │   ├── authorize.py           # /oauth/authorize, /oauth/google-callback
│   │   ├── token.py               # /oauth/token (form-encoded, 401-only on failure)
│   │   └── jwt_utils.py           # mint + verify upgrade JWT
│   ├── gpt/
│   │   ├── proxy.py               # /v1/gpt/{slug}/query
│   │   ├── quota.py               # 24h rolling count
│   │   ├── subscriptions.py       # active-sub check
│   │   └── n8n.py                 # forwarding client (httpx, 38s timeout)
│   ├── upgrade/
│   │   ├── validate.py            # /v1/upgrade/validate
│   │   └── portal.py              # /v1/upgrade/portal-token
│   └── health.py                  # /health
└── tests/
    ├── conftest.py
    ├── test_oauth.py
    ├── test_gpt_proxy.py          # quota, sub, n8n forward, paywall response
    ├── test_upgrade_token.py
    └── test_e2e_flow.py           # full happy-path with mocked n8n + Stripe
```

### 11.4 Environment variables (gateway)

```
# Supabase
SUPABASE_URL=https://bdtzzykdhszmdlvpzlku.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<provided securely>
SUPABASE_ANON_KEY=<provided securely>
DATABASE_URL=postgres://...                 # direct asyncpg connection

# OAuth
OAUTH_CLIENT_ID=<random 32-byte hex>        # we generate, give to GPT Builder
OAUTH_CLIENT_SECRET=<random 32-byte hex>    # same
OAUTH_ACCESS_TOKEN_TTL=2592000              # 30 days
OAUTH_REFRESH_TOKEN_TTL=7776000             # 90 days

# Upgrade JWT
GATEWAY_JWT_SECRET=<random 32-byte hex>
UPGRADE_TOKEN_TTL=900                       # 15 minutes

# n8n
N8N_WEBHOOK_PRASHNA=https://app.askastrobot.com/webhook/e6971529-467e-43d6-9224-3bdce40f4b3f
N8N_WEBHOOK_HOROSCOPE=https://app.askastrobot.com/webhook/1124ff92-9662-4167-bc00-da7420919f75
N8N_WEBHOOK_CAREER=https://app.askastrobot.com/webhook/dcb303fc-1346-403a-a261-e5e1705b9aa5
N8N_WEBHOOK_MARRIAGE=https://app.askastrobot.com/webhook/35f8126a-f8ae-4bac-9211-ad0fc25e6e04
GATEWAY_SHARED_SECRET=<random 32-byte hex>  # n8n verifies this header
N8N_TIMEOUT_SECONDS=30

# URLs
APP_BASE_URL=https://askastrobot.com
GATEWAY_BASE_URL=https://api.askastrobot.com

# Misc
NODE_ENV=production
PORT=8003
LOG_LEVEL=info
```

### 11.6 Rollback (rev-2)

If a deploy goes wrong:

| Failure | Rollback action |
|---|---|
| Bad gateway image | `cd /opt/askastrobot/gateway && docker tag aab-gateway:previous aab-gateway:latest && docker compose up -d`. Each deploy tags `:previous` before pulling. |
| Bad migration | Migrations are forward-only; rollback is a corrective forward migration committed to `migrations/`. Always test against a Supabase branch first (Dashboard → Branches). |
| Bad GPT system prompt | Prior prompts are committed to `gpt-bots/<slug>/system-prompt.md`. Paste the previous version back into GPT Builder. |
| Stripe price misconfig | Edge Function reads price IDs from env; rotate to a previous-known-good price ID. |

**Per-user emergency revoke:**

```sql
-- Force user back to free tier immediately:
UPDATE gw_oauth_tokens   SET revoked_at = NOW() WHERE user_id = $1 AND revoked_at IS NULL;
UPDATE gw_subscriptions  SET status = 'cancelled', expires_at = NOW() WHERE user_id = $1 AND status = 'active';
```

### 11.7 Observability (rev-2)

| Layer | Tool | Purpose |
|---|---|---|
| Structured logs | Python `structlog` → JSON to stdout, captured by Docker logs | Per-request: request_id, user_id, bot_slug, latency, outcome |
| Error reporting | Sentry SDK (`SENTRY_DSN` env var) | Uncaught exceptions, stack traces |
| Liveness | `/health` (Traefik healthcheck) | Container alive |
| Readiness | `/health/deep` | Checks DB connectivity + n8n reachability per bot. Used by external monitor only, not Traefik. |
| External uptime | UptimeRobot on `/health/deep` every 5 min, SMS + email alert | At-launch requirement |
| DB query metrics | pg_stat_statements (Supabase enables by default) | Slow query detection |

`request_id` middleware:
```python
@app.middleware("http")
async def request_id_middleware(request, call_next):
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    structlog.contextvars.bind_contextvars(request_id=request_id)
    response = await call_next(request)
    response.headers["x-request-id"] = request_id
    return response
```

### 11.5 docker-compose.yml (sketch)

```yaml
services:
  aab-gateway:
    build: .
    container_name: aab-gateway
    restart: unless-stopped
    networks:
      - root_default
    env_file:
      - .env
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.aab-gateway.rule=Host(`api.askastrobot.com`)"
      - "traefik.http.routers.aab-gateway.entrypoints=websecure"
      - "traefik.http.routers.aab-gateway.tls.certresolver=mytlschallenge"
      - "traefik.http.services.aab-gateway.loadbalancer.server.port=8003"
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8003/health"]
      interval: 30s
      timeout: 10s
      retries: 3

networks:
  root_default:
    external: true
```

## 12. Testing

| Layer | Tests |
|---|---|
| Unit | JWT mint/verify, quota count edge cases (exactly 2, exactly 0, expired window), subscription matching (per-bot vs master), state echo on OAuth |
| Integration | OAuth full handshake (mocked Supabase Google), token refresh returns 401 on bad refresh, /v1/gpt happy path, /v1/gpt paywall, n8n forwarding with mocked upstream, n8n timeout returns 503 + no log row |
| E2E (manual on test bot) | Sign in → 2 free → paywall → pay test card → unlock → query succeeds → 24h later → blocked again |

The acceptance criteria from the original brief §15 carry over verbatim, with the substitution: "402" → "200 with `status: free_limit_reached`".

## 13. Manual checklist for Madhur (consolidated)

Madhur will receive this as a separate ops runbook in the repo's `README.md`. Listed roughly in the order they're needed:

### 13.1 Before Claude Code starts coding
1. **DNS:** add A record `api.askastrobot.com` → `46.28.44.45` (Hostinger VPS IP).
2. **GitHub repo:** create empty private repo `Madjamy/askastrobot-gateway`. Grant Claude Code write access.
3. **VPS SSH key:** generate a deploy key, add public key to the repo's deploy keys.
4. **Supabase Auth:** Dashboard → Authentication → Providers → Google → enable. (Google Cloud Console: create OAuth 2.0 client, set redirect URI `https://bdtzzykdhszmdlvpzlku.supabase.co/auth/v1/callback`, copy client ID + secret into Supabase.)
5. **Supabase Auth Redirect URLs:** add `https://api.askastrobot.com/oauth/google-callback` to allowed URLs.

### 13.2 In parallel with Claude Code building
6. **Stripe products:** create the 9 products listed in §9.3, copy each price ID. Send to Claude Code so they can be set as env vars on the website's Edge Function. (Gateway does not need them.)
7. **Stripe webhook endpoint:** create endpoint in Stripe Dashboard (URL placeholder until Edge Function is deployed; Lovable team activates it). Copy signing secret.
8. **Stripe Customer Portal:** Settings → Customer Portal → enable, configure cancellation policy, save.
9. **Lovable hand-off:** send the Lovable team this entire spec + specifically point them at §9.

### 13.3 After gateway is deployed
10. **Verify gateway:** `curl https://api.askastrobot.com/health` returns 200.
11. **Apply Supabase migration:** run the SQL in `migrations/0001_aab_gateway.sql` against the shared Supabase project (Dashboard → SQL Editor).
12. **Configure GPT Builder OAuth on the test bot first** (per §13.5).
13. **End-to-end test on test bot.**

### 13.4 After test bot validates
14. **Update each of the 4 production bots** in GPT Builder (per §13.5).
15. **Update each of the 4 n8n workflows:** add a Function node at the start that checks `X-Gateway-Secret` header against the value provided. Reject (HTTP 401) if missing or wrong.
16. **Announce** to existing users via blog / social.

### 13.5 GPT Builder configuration (per bot, 4 times)

In ChatGPT → Edit GPT → Configure:

1. **Actions → edit existing action.**
2. **Authentication → choose OAuth.** Paste:
   - Client ID: `<OAUTH_CLIENT_ID>`
   - Client Secret: `<OAUTH_CLIENT_SECRET>`
   - Authorization URL: `https://api.askastrobot.com/oauth/authorize`
   - Token URL: `https://api.askastrobot.com/oauth/token`
   - Scope: `read:astro write:query`
   - Token Exchange Method: **Default (POST request)**
3. **Schema → replace with the updated OpenAPI** (Claude Code provides one per bot). Only `servers.url` changes:
   - Prashna: `https://api.askastrobot.com/v1/gpt/prashna/query`
   - Horoscope: `https://api.askastrobot.com/v1/gpt/horoscope/query`
   - Career: `https://api.askastrobot.com/v1/gpt/career/query`
   - Marriage: `https://api.askastrobot.com/v1/gpt/marriage/query`
4. **Instructions (system prompt):** replace the donation block with the paywall-handler block from §8.1.
5. **Save.** ChatGPT shows a redirect URI like `https://chat.openai.com/aip/g-XXXX/oauth/callback`. Copy it.
6. **Add that redirect URI** to the gateway's allowed redirect URI list (one-line config change in `OAUTH_ALLOWED_REDIRECTS` env var).
7. **Test:** open the bot, ask a question, confirm Google sign-in works, confirm 2 free queries, confirm paywall appears, confirm payment unlocks.

## 14. Phased rollout

| Phase | Owner | Duration | Outcome |
|---|---|---|---|
| 0 — Prereqs | Madhur | 1 day | DNS, Supabase Google Auth, GitHub repo, VPS deploy key |
| 1 — Build gateway | Claude Code | 4–5 days | Gateway deployed at `api.askastrobot.com`, migrations applied, smoke tests pass |
| 2 — Lovable builds website side | Lovable team | 2–3 days | `/upgrade` page + 3 Edge Functions live |
| 3 — Test on staging GPT bot | Madhur | 3–7 days | Real users in network validate the full flow with test Stripe cards + small live charges |
| 4 — Roll out to 4 prod bots | Madhur | 1 day | All 4 bots on the gateway, n8n workflows secured |
| 5 — Monitor & iterate | Madhur ongoing | ongoing | Watch conversion, master sub uptake, error rates |

## 15. Out of scope (v1, unchanged from brief)

Email automation, admin dashboard, IP-based abuse detection, refund flow automation, promo codes, multi-currency display, Apple Sign-In, magic-link, migration of existing 100 web users into OAuth flow, mobile app, i18n.

## 16. Open risks

| # | Risk | Mitigation |
|---|---|---|
| 1 | ChatGPT rotates the assigned redirect URI on a bot edit | Keep `OAUTH_ALLOWED_REDIRECTS` as a wildcard `https://chat(gpt)?.com/aip/g-*/oauth/callback` instead of an exact list. |
| 2 | Stripe webhook delivery delay (>30s) — user clicks "I've paid" but next query still 402 | System prompt instructs user to wait 30s. If still failing, support email. |
| 3 | n8n `webhook-test` URLs accidentally re-introduced | Env var only loads at deploy; CI test asserts URLs match `/webhook/` pattern. |
| 4 | Daylight saving / timezone drift on rolling 24h window | Use `NOW() - INTERVAL '24 hours'` in Postgres (UTC). No client clock involvement. |
| 5 | Gateway becomes a single point of failure for all 4 bots | Healthcheck + Docker `restart: unless-stopped`. PagerDuty-equivalent (UptimeRobot on `/health`) recommended in v2. |
| 6 | User has Google account with no name | Make `users.name` nullable, default to email-prefix. |

## 17. Acceptance criteria

Repeats brief §15 with the 402 → 200 substitution noted in §2.

- [ ] New ChatGPT user signs in via Google in <30 seconds.
- [ ] Asks 2 queries → both succeed, both logged in `query_log` with `was_paid_query = FALSE`.
- [ ] Asks 3rd query → receives `200 + status: free_limit_reached` with valid clickable upgrade URL.
- [ ] Clicks upgrade link → `/upgrade` page validates token, shows pricing → pays via Stripe Checkout.
- [ ] Stripe webhook fires → `subscriptions` row created with `status='active'`, correctly bound to gateway `user_id`.
- [ ] 4th query in same chat → succeeds, logged with `was_paid_query = TRUE`.
- [ ] User starts a fresh ChatGPT chat → still recognised, still subscribed, queries continue to work.
- [ ] When day pass expires (24h), next query returns paywall again.
- [ ] Master subscription holder uses all 4 bots without limits.
- [ ] n8n workflow rejects calls without `X-Gateway-Secret`.
- [ ] All endpoints return correct codes: 200 (success or paywall), 401 (bad/missing token, OAuth refresh failure), 503 (n8n down).
- [ ] `query_log` queryable in Supabase SQL editor.
- [ ] GitHub Actions auto-deploys on push to `main`.
- [ ] Identity binding cannot be broken: a user who signs into a different account on the website cannot pay against a different `user_id` (verified by inspecting `subscriptions.user_id` after a deliberate attempted mismatch — no path to mismatch exists because there is no website login in the flow).

---

**End of design spec.**
