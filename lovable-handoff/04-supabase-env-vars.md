# Supabase Edge Function env vars

Set these on each of the three Edge Functions in the Supabase dashboard
(Project Settings → Edge Functions → Manage Secrets, or via `supabase secrets set`).

The same secrets are shared across all three functions — you don't need to set
them per-function on Supabase.

```
# Supabase
SUPABASE_URL=https://bdtzzykdhszmdlvpzlku.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<from Supabase dashboard - Settings - API>

# Stripe
STRIPE_SECRET_KEY=<sk_live_... from Stripe dashboard>
STRIPE_WEBHOOK_SECRET=<whsec_... from the webhook endpoint you created>

# Stripe price IDs — fill in after running 03-stripe-dashboard-checklist.md
PRICE_DAY_PASS_PRASHNA=
PRICE_DAY_PASS_HOROSCOPE=
PRICE_DAY_PASS_CAREER=
PRICE_DAY_PASS_MARRIAGE=
PRICE_MONTHLY_PRASHNA=
PRICE_MONTHLY_HOROSCOPE=
PRICE_MONTHLY_CAREER=
PRICE_MONTHLY_MARRIAGE=
PRICE_MASTER_MONTHLY=

# Resend (already in your stack — same key the kundali pipeline uses)
RESEND_API_KEY=re_...

# Gateway integration
GATEWAY_BASE_URL=https://api.askastrobot.com
GATEWAY_SHARED_SECRET=<provided by gateway team — same value as the gateway's GATEWAY_SHARED_SECRET env var>
```

**Setting via CLI:**

```bash
supabase secrets set --project-ref bdtzzykdhszmdlvpzlku \
  STRIPE_SECRET_KEY=sk_live_... \
  STRIPE_WEBHOOK_SECRET=whsec_... \
  PRICE_DAY_PASS_PRASHNA=price_... \
  ... etc
```

## CORS

The Edge Functions emit
`Access-Control-Allow-Origin: https://askastrobot.com`. If you serve the
website from a different domain (e.g., a Lovable preview URL), update the
header in each function's `json()` helper, or change to a permissive CORS
during development and lock it down before launch.
