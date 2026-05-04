# End-to-end test plan

Run on the **test bot** before flipping production. Use Stripe **test mode**
(toggle in Stripe Dashboard → upper right). Test cards from
https://stripe.com/docs/testing.

## Scenarios

### S1 — First-time auth + 2 free queries

1. Open the test GPT in ChatGPT.
2. Ask any astro question.
3. Expect "Sign in with AskAstroBot" prompt → click → Google sign-in popup.
4. Pick a Google account, grant consent.
5. Action retries automatically; bot answers.
6. Verify in Supabase SQL editor:
   ```sql
   SELECT * FROM gw_users WHERE signup_source = 'gpt' ORDER BY created_at DESC LIMIT 1;
   SELECT * FROM gw_query_log ORDER BY created_at DESC LIMIT 1;
   ```
7. Ask a 2nd question. Check `query_log` has 2 rows for the user.

### S2 — Paywall on 3rd query

1. Ask a 3rd astro question on the same bot, same Google account.
2. Expect the bot to render:
   - The free-limit message.
   - A clickable "Upgrade" link.
   - Three pricing options.
3. Click the upgrade link.
4. The `/upgrade` page should render in a new tab with three buttons. The URL
   should NOT show the `?token=...` after page-mount (replaceState'd).

### S3 — Day Pass purchase

1. From the `/upgrade` page, click "Day Pass — $2.99".
2. Stripe Checkout opens. Email field is pre-filled and **read-only**.
3. Enter test card `4242 4242 4242 4242`, expiry `12/34`, CVC `123`.
4. Submit. Expect redirect to `/upgrade/success`.
5. Verify in Stripe Dashboard → Payments: charge appears.
6. Verify webhook log:
   ```sql
   SELECT * FROM gw_stripe_webhook_log ORDER BY received_at DESC LIMIT 5;
   ```
   Should show `event_type = checkout.session.completed`, `status = processed`.
7. Verify subscription:
   ```sql
   SELECT * FROM gw_subscriptions WHERE user_id = '<user-id>';
   ```
   Should have `plan='day_pass', status='active', expires_at` ~24h in future.
8. Verify branded welcome email arrived in the test Google account inbox.
9. Return to ChatGPT, ask a 4th query. Should succeed.
10. Verify in `query_log` that the latest row has `was_paid_query = TRUE`.

### S4 — Day Pass expiry

1. Manually expire the day pass in SQL:
   ```sql
   UPDATE gw_subscriptions SET expires_at = NOW() - INTERVAL '1 minute'
    WHERE user_id = '<user-id>' AND plan = 'day_pass';
   ```
2. Ask another query. Should hit the paywall again (since 24h of free quota
   already consumed). If you want to test free queries return after sub
   expiry, also delete or age out the `query_log` rows.

### S5 — Monthly subscription + manage flow

1. From a fresh user, hit paywall, buy "Monthly — $7.99".
2. Verify `subscriptions.plan='monthly'`, `expires_at` ~30 days out.
3. In ChatGPT, ask: *"manage my subscription"*.
4. Bot returns a `/account/billing?token=...` link.
5. Click it. Should redirect (silently, ~2s) to Stripe Customer Portal.
6. Click "Cancel subscription". Confirm.
7. Verify webhook events arrive:
   - `customer.subscription.updated` (cancel_at_period_end=true)
   - Eventually (period end): `customer.subscription.deleted`.
8. Verify `subscriptions.cancel_at_period_end = TRUE` and `status` still `'active'`
   until period end.

### S6 — Master plan all-bots

1. Fresh user. Hit paywall on Prashna.
2. Buy Master ($12.99).
3. Verify `subscriptions.bot_slug = 'all'`.
4. Open Horoscope (different bot). Ask 3 queries. All should succeed without
   paywall (master applies).
5. Open Career, Marriage. Same — no paywall.

### S7 — Forced n8n outage (resilience)

1. In the gateway env, temporarily set `N8N_WEBHOOK_PRASHNA` to a black-hole URL.
2. Sign in with a fresh user, ask one query.
3. Expect: bot says "Astrology engine is temporarily unavailable…"
4. Verify `query_log` has NO new row (failed query doesn't count).
5. Verify `query_error_log` has the error.
6. Restore the env var.

### S8 — Token refresh (long-running)

1. Sign in. In Supabase, manually expire the access token:
   ```sql
   UPDATE gw_oauth_tokens SET expires_at = NOW() - INTERVAL '1 minute'
    WHERE user_id = '<user-id>' AND revoked_at IS NULL;
   ```
2. Ask a query. ChatGPT should silently call `/oauth/token` with the
   refresh_token, get a new access token, and proceed.
3. Verify a new `oauth_tokens` row was created and the old one has
   `revoked_at` set.

### S9 — Identity binding cannot be broken

1. User A signs in with `usera@gmail.com`. Hits paywall, gets upgrade link.
2. Copy the upgrade URL. Paste it in a different browser session signed
   into a different Google account (or no account at all).
3. Click the upgrade button. Stripe Checkout shows email
   `usera@gmail.com` (read-only, locked).
4. Pay with test card.
5. Verify `subscriptions.user_id` matches user A, NOT the other Google account.
6. Verify on user A's next query (back in their original ChatGPT) — they get
   the unlocked behaviour.

This is the critical test for the identity-binding design.
