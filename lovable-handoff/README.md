# Lovable hand-off — AskAstroBot Gateway website integration

This folder contains everything the Lovable team needs to wire the
`askastrobot.com` website into the new gateway-based subscription system.

## What you're building

Three pages on `askastrobot.com` and three Supabase Edge Functions. Together
they form the **payment + subscription management** half of the system. The
other half — OAuth, query gating, bot proxy — is the gateway service at
`api.askastrobot.com`, already deployed.

**Key principle: the website never asks the user to log in for the paywall flow.**
Identity is carried in a 15-minute signed JWT issued by the gateway. Your code
re-validates that JWT with the gateway and proceeds. Do not add a login step.

## Files in this folder

| File | What it is |
|---|---|
| `01-pages-spec.md` | The three React pages — props, query params, copy, behaviour. |
| `02-edge-functions-spec.md` | The three Edge Functions — exact request/response shapes, Stripe calls, error handling. |
| `03-stripe-dashboard-checklist.md` | Step-by-step manual setup in Stripe (products, webhook, customer portal). |
| `04-supabase-env-vars.md` | All env vars to set on the Edge Functions in Supabase. |
| `05-test-plan.md` | End-to-end test scenarios with Stripe test cards. |

## The contract you must honour

Two contracts between the gateway and your code. Do not deviate.

### Contract 1 — `subscriptions` table shape

Already created by the gateway's migration (`migrations/0001_aab_gateway.sql`).
Your `stripe-webhook` Edge Function is the **only writer**. The gateway is read-only.

```sql
public.subscriptions (
  id, user_id, plan, bot_slug, status, expires_at,
  stripe_customer_id, stripe_subscription_id, stripe_checkout_session_id,
  cancel_at_period_end, created_at, updated_at
)
```

- `plan ∈ {'day_pass', 'monthly', 'master'}`
- `bot_slug ∈ {'prashna', 'horoscope', 'career', 'marriage', 'all'}`
  (use `'all'` for master subs)
- `status ∈ {'active', 'cancelled', 'past_due', 'expired'}`
- `stripe_subscription_id` is UNIQUE — use it as the upsert key for renewals.
- `stripe_checkout_session_id` is UNIQUE — use it as the upsert key for day passes
  (which have no `stripe_subscription_id`).

### Contract 2 — JWT validation via the gateway

To resolve a token to `{user_id, email, bot_slug}`, call:

```
GET https://api.askastrobot.com/v1/upgrade/validate?token=<jwt>
Headers:
  X-Gateway-Secret: <GATEWAY_SHARED_SECRET>     ← server-side only
```

- With `X-Gateway-Secret`: response includes `email` and `google_id`.
- Without it (browser-side): response excludes those — public-safe subset only.

Your Edge Functions MUST send the secret. Your React page MUST NOT.

## Order of operations to deploy

1. Madhur completes Stripe setup (`03-stripe-dashboard-checklist.md`) and
   sends Lovable: 9 price IDs, the webhook signing secret, the Stripe API key.
2. Lovable creates the three Edge Functions in Supabase with env vars from `04-supabase-env-vars.md`.
3. Lovable builds the three React pages in `01-pages-spec.md`.
4. Stripe Dashboard → webhook endpoint → point at the deployed Edge Function URL.
5. Run `05-test-plan.md` against test bot.
6. Madhur flips the four production GPTs.

## Questions

Send to Madhur (project owner) — he will route to the gateway team.
