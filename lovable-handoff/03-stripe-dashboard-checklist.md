# Stripe Dashboard checklist (Madhur)

This is what Madhur configures manually in the Stripe Dashboard before
the gateway and Edge Functions can be tested end-to-end.

## A. Products and prices

You sent the following PRODUCT IDs (note: these are products, not prices):

| Bot / Plan | Product ID |
|---|---|
| Day Pass (universal) | `prod_SbCD46RqUYiCYc` |
| Marriage | `prod_SbBjFDchcsRfZn` |
| Career | `prod_UF3hZGPaITxMaR` |
| Horoscope | `prod_SbBgZDHE6SmidW` |
| Prashna | `prod_SaqKZkw2DCqHJu` |

For each product, you need to create the corresponding **price(s)**. Stripe
Checkout requires `price_*` IDs, not `prod_*` IDs.

### Required prices

For each of the four bot products (`prod_SaqK...` Prashna, `prod_SbBg...` Horoscope,
`prod_UF3h...` Career, `prod_SbBj...` Marriage), create:

- **One Monthly recurring price** at the bot's monthly rate.
  - Prashna: $7.99/mo
  - Horoscope: $7.99/mo
  - Career: $5.99/mo
  - Marriage: $5.99/mo

For the universal Day Pass product (`prod_SbCD...`), create either:
- **Option A (recommended): four one-time prices** of $2.99, one per bot, so
  the Edge Function can pass the right `price_*` per bot. This makes per-bot
  reporting clean.
- **Option B: one $2.99 price** and we tag the bot in checkout `metadata`.
  Simpler, but reporting per bot becomes a metadata join.

**Recommendation: go with Option A — four day-pass prices on the same product.**

### Master plan — needs a new product

You did NOT send a master-plan product. Create one:
- **Product:** "AskAstroBot — Master (all 4 bots)"
- **Price:** $12.99 USD recurring monthly

### Final list of price IDs to send back to the dev team

After creating prices, copy each `price_*` ID and send back:

```
PRICE_DAY_PASS_PRASHNA   = price_...
PRICE_DAY_PASS_HOROSCOPE = price_...
PRICE_DAY_PASS_CAREER    = price_...
PRICE_DAY_PASS_MARRIAGE  = price_...

PRICE_MONTHLY_PRASHNA    = price_...
PRICE_MONTHLY_HOROSCOPE  = price_...
PRICE_MONTHLY_CAREER     = price_...
PRICE_MONTHLY_MARRIAGE   = price_...

PRICE_MASTER_MONTHLY     = price_...
```

## B. Customer emails (built-in receipts)

Settings → Customer emails → ✅ Successful payments → Save.
Settings → Customer emails → ✅ Refunds → Save.

This sends a beautifully formatted Stripe receipt to every paying customer
automatically, with a hosted invoice link. Free, zero code.

## C. Customer Portal (built-in self-service)

Settings → Billing → Customer Portal:

- **Enable** the portal.
- **Login link:** ✅ enable. (Lets users access the portal by typing their
  email at a Stripe-hosted page; Stripe sends them a magic link.)
- **Cancellation:** select **"Cancel at end of billing period"** (NOT "Cancel
  immediately"). This is critical — users must keep access for the period they paid for.
- **Customer can update:** payment method ✅, billing address ✅.
- **Save.**

## D. Webhook endpoint

Webhooks → Add endpoint:

- **Endpoint URL:** `https://bdtzzykdhszmdlvpzlku.supabase.co/functions/v1/stripe-webhook`
- **Events to send:**
  - `checkout.session.completed`
  - `customer.subscription.updated`
  - `customer.subscription.deleted`
  - `invoice.payment_failed`
- **Save.**
- Click the new endpoint, reveal the **Signing secret** (`whsec_...`), copy it.
  That goes into the Edge Function env var `STRIPE_WEBHOOK_SECRET`.

## E. API keys

Developers → API keys:
- Copy the **Secret key** (`sk_live_...` or `sk_test_...`) → this goes into
  the Edge Function env var `STRIPE_SECRET_KEY`.

## F. Final hand-off bundle

Send to the dev team:

1. The 9 `price_*` IDs (above).
2. The webhook signing secret (`whsec_...`).
3. The Stripe secret key (`sk_*`).

These go into Supabase Edge Function env vars (`04-supabase-env-vars.md`).
