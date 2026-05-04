# Stripe Dashboard checklist (Madhur)

Manual setup in Stripe before the gateway and Edge Functions can run end-to-end.

## A. Products and prices

You already created 5 products. The Edge Function uses **product IDs** and looks
up active prices at runtime — same pattern Lovable already uses on your other
flows. So you just need to confirm each product has the correct price attached
in the Dashboard, then create the missing 6th product.

### Existing products (you sent these)

| Plan | Product ID | Required price |
|---|---|---|
| Day Pass (universal, used for all 4 bots) | `prod_SbCD46RqUYiCYc` | $2.99 USD, **one-time** |
| Prashna monthly | `prod_SaqKZkw2DCqHJu` | $7.99 USD, **recurring monthly** |
| Horoscope monthly | `prod_SbBgZDHE6SmidW` | $7.99 USD, **recurring monthly** |
| Career monthly | `prod_UF3hZGPaITxMaR` | $5.99 USD, **recurring monthly** |
| Marriage monthly | `prod_SbBjFDchcsRfZn` | $5.99 USD, **recurring monthly** |

For each product:
1. Open it in Stripe Dashboard → Products.
2. Confirm there is exactly one **active** price at the rate above and the
   correct billing type (one-time vs recurring).
3. If there isn't, click "Add another price" and create one. The Edge Function
   picks the first active price matching the expected billing type.

### Missing product — Master plan

Create:
- **Product:** `AskAstroBot — Master (all 4 bots)`
- **Price:** $12.99 USD, **recurring monthly**
- Save and copy the new `prod_*` ID — that becomes `PRODUCT_MASTER` in the
  Edge Function env.

## B. Customer emails (built-in receipts)

Settings → Customer emails:
- ✅ Successful payments
- ✅ Refunds

Save. Stripe now emails a receipt on every successful charge automatically.

## C. Customer Portal (built-in self-service)

Settings → Billing → Customer Portal:

- **Activate** the portal.
- **Login link:** ✅ enable. Lets users access the portal by typing their
  email at a Stripe-hosted page; Stripe sends a magic-link.
- **Cancellation behaviour:** select **"Cancel at end of billing period"**.
  Critical — users keep access for the period they paid for.
- **Customer can update:** payment method ✅, billing address ✅.
- **Save.**

## D. Webhook endpoint

Webhooks → Add endpoint:

- **Endpoint URL:** `https://bdtzzykdhszmdlvpzlku.supabase.co/functions/v1/stripe-webhook`
  (this URL won't exist until Lovable deploys the Edge Function; create the
  webhook AFTER they deploy.)
- **Events to send:**
  - `checkout.session.completed`
  - `customer.subscription.updated`
  - `customer.subscription.deleted`
  - `invoice.payment_failed`
- **Save.**
- Click the new endpoint, reveal the **Signing secret** (`whsec_...`), copy it.
  → Edge Function env var `STRIPE_WEBHOOK_SECRET`.

## E. API keys

Developers → API keys → copy the **Secret key** (`sk_*`).
→ Edge Function env var `STRIPE_SECRET_KEY`.

## F. Final hand-off bundle to Lovable

Send to the dev team:

1. The 6 product IDs (5 existing + Master, after you create it).
2. The webhook signing secret (`whsec_...`).
3. The Stripe secret key (`sk_*`).
4. The `GATEWAY_SHARED_SECRET` value (Madhur knows this from gateway `.env`).

These go into Supabase Edge Function secrets per `04-supabase-env-vars.md`.
