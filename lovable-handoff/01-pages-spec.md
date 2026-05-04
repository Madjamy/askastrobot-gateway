# Pages Spec — askastrobot.com (Lovable)

Three pages, all in the existing React + Supabase frontend.

---

## Page 1: `/upgrade`

**URL example:**
`https://askastrobot.com/upgrade?token=eyJhbGciOiJIUzI1NiI...&bot=prashna`

### Inputs (from URL)
- `token` (string, required): the upgrade JWT from the gateway. 15-minute TTL.
- `bot` (string, required): one of `prashna`, `horoscope`, `career`, `marriage`, `all`.

### Behaviour on mount

1. Read `token` and `bot` from the query string.
2. **Immediately call** `history.replaceState({}, '', '/upgrade')` to strip the
   token from the address bar. This prevents leaking the JWT via Referer headers
   to fonts/analytics/Stripe assets.
3. Call `GET https://api.askastrobot.com/v1/upgrade/validate?token=<token>` from
   the browser (no `X-Gateway-Secret` header).
4. **If 200:** the response is `{user_id, bot_slug, valid_until}`. Render the
   page (see "Layout" below).
5. **If 401:** the token is expired or invalid. Render the "expired" state:
   > **This upgrade link has expired.**
   > Return to ChatGPT and ask any question — the bot will give you a fresh upgrade link.
6. **If network error:** show a "try again" button.

### Layout (happy path)

```
┌──────────────────────────────────────────────────────────────────┐
│  AskAstroBot Premium                                             │
│                                                                  │
│  Upgrade to continue your <Bot Name> reading.                    │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  24-hour Day Pass                                $2.99   │   │
│  │  Unlimited <Bot Name> queries for 24 hours.    [BUY →]   │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  Monthly                                         $7.99   │   │
│  │  Unlimited <Bot Name> queries.                 [BUY →]   │   │
│  │  Cancel anytime.                                          │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  All 4 bots — Master Plan                       $12.99   │   │
│  │  Unlimited queries on every AskAstroBot.       [BUY →]   │   │
│  │  Best value if you use multiple bots.                     │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  Secure payment via Stripe. We never see your card details.      │
└──────────────────────────────────────────────────────────────────┘
```

**Bot name display map (use this):**
- `prashna` → "Prashna (Ask Any Question)"
- `horoscope` → "Horoscope Analysis"
- `career` → "Career Astrology"
- `marriage` → "Kundali Milan (Marriage)"

**Monthly price interpolation:**
- `prashna` or `horoscope` → $7.99/mo
- `career` or `marriage` → $5.99/mo
- (Master is always $12.99/mo)

### Behaviour on button click

1. POST to Edge Function:
   ```
   POST https://<supabase>.supabase.co/functions/v1/create-checkout-session
   Headers:
     Content-Type: application/json
     Authorization: Bearer <SUPABASE_ANON_KEY>
   Body: { token: <jwt-from-state>, plan: "day_pass"|"monthly"|"master", bot_slug: "<bot>" }
   ```
   Note: store the JWT in React state on mount BEFORE the `history.replaceState` strip,
   so it remains in memory for the click.
2. Receive `{ checkout_url }`.
3. `window.location.href = checkout_url` (full-page redirect to Stripe Checkout).

### Error states

- Edge Function returns 401: token expired (race vs UI) → show the expired message + a "back to ChatGPT" link.
- Edge Function returns 500: show generic error + retry button.

---

## Page 2: `/upgrade/success`

**URL:** `https://askastrobot.com/upgrade/success`

Stripe redirects here on successful payment (no params).

### Layout

```
┌──────────────────────────────────────────────────────────────────┐
│                                                                  │
│      ✓  Payment received                                         │
│                                                                  │
│   Your subscription is active. Return to ChatGPT and             │
│   continue your conversation — the bot will recognise you.       │
│                                                                  │
│   [ Return to ChatGPT ]   ← https://chatgpt.com                  │
│                                                                  │
│   Need to manage or cancel later? Open any AskAstroBot           │
│   and ask "manage my subscription".                              │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

No tokens, no API calls. Pure static page.

---

## Page 3: `/account/billing`

**URL example:**
`https://askastrobot.com/account/billing?token=eyJhbGc...`

This page is reached via a **portal token** (different purpose than the upgrade
token — purpose claim is `"portal"`). Users land here when they ask the GPT to
"manage my subscription" — the GPT returns this URL with a fresh token.

### Behaviour on mount

1. Read `token` from URL.
2. `history.replaceState` to strip it.
3. POST to Edge Function:
   ```
   POST https://<supabase>.supabase.co/functions/v1/create-portal-session
   Body: { token: <portal-jwt> }
   ```
4. Receive `{ portal_url }`.
5. `window.location.href = portal_url` (immediate redirect to Stripe Customer Portal).

### Loading state

While the Edge Function is working (≤2 seconds usually):
> Opening your subscription manager…

### Error states

- 401: "This link has expired. Open any AskAstroBot and ask 'manage my subscription' for a fresh link."
- 500: generic error + retry.

---

## `/account/billing/done`

Stripe Customer Portal redirects here when the user clicks "Return".
Same as `/upgrade/success` content-wise: a friendly "you're all set" message
and a "Return to ChatGPT" link. No params, no API calls.

---

## What's NOT on these pages

- No login form, no sign-in button, no Google OAuth widget.
- No "create account" flow.
- No password recovery.
- No display of the user's email beyond what `/v1/upgrade/validate` returns
  (which is nothing in the browser — the public response excludes email).

If a designer asks "should we add a login here?" — the answer is **no, not in
v1**. The signed token IS the identity.
