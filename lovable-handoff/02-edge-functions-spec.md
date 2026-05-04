# Edge Functions Spec — Supabase Edge Functions (Deno + TypeScript)

Three functions to create in the existing Supabase project
`bdtzzykdhszmdlvpzlku.supabase.co`. All run on the same Supabase project that
the gateway reads from, so they share the database directly.

| Function | Purpose | Trigger |
|---|---|---|
| `create-checkout-session` | Create a Stripe Checkout Session and return its URL | POST from `/upgrade` page |
| `stripe-webhook` | Receive Stripe events; write to `subscriptions`; send Resend welcome email | Stripe → webhook |
| `create-portal-session` | Create a Stripe Customer Portal session | POST from `/account/billing` page |

---

## Function 1 — `create-checkout-session`

### Endpoint

`POST https://<project>.supabase.co/functions/v1/create-checkout-session`

### Request body

```json
{
  "token": "<upgrade JWT>",
  "plan": "day_pass" | "monthly" | "master",
  "bot_slug": "prashna" | "horoscope" | "career" | "marriage" | "all"
}
```

### Response

```json
{ "checkout_url": "https://checkout.stripe.com/c/pay/..." }
```

Or `{"error":"<message>"}` with 4xx/5xx.

### Logic

```ts
import Stripe from "https://esm.sh/stripe@17.5.0?target=denonext";
import { createClient } from "jsr:@supabase/supabase-js@2";

const stripe = new Stripe(Deno.env.get("STRIPE_SECRET_KEY")!, {
  apiVersion: "2024-12-18.acacia",
});
const supabase = createClient(
  Deno.env.get("SUPABASE_URL")!,
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
);
const GATEWAY_BASE_URL = Deno.env.get("GATEWAY_BASE_URL")!;
const GATEWAY_SHARED_SECRET = Deno.env.get("GATEWAY_SHARED_SECRET")!;

// Map (plan, bot_slug) -> Stripe Product ID. Each product has its prices
// configured in the Stripe Dashboard; we look up the active price at runtime.
// This matches the existing Lovable pattern of passing product IDs.
const PRODUCTS: Record<string, string> = {
  "day_pass:prashna":   Deno.env.get("PRODUCT_DAY_PASS")!,   // same product, all bots
  "day_pass:horoscope": Deno.env.get("PRODUCT_DAY_PASS")!,
  "day_pass:career":    Deno.env.get("PRODUCT_DAY_PASS")!,
  "day_pass:marriage":  Deno.env.get("PRODUCT_DAY_PASS")!,
  "monthly:prashna":    Deno.env.get("PRODUCT_PRASHNA")!,
  "monthly:horoscope":  Deno.env.get("PRODUCT_HOROSCOPE")!,
  "monthly:career":     Deno.env.get("PRODUCT_CAREER")!,
  "monthly:marriage":   Deno.env.get("PRODUCT_MARRIAGE")!,
  "master:all":         Deno.env.get("PRODUCT_MASTER")!,
};

async function resolvePriceId(productId: string, expectedRecurring: boolean): Promise<string> {
  const prices = await stripe.prices.list({
    product: productId,
    active: true,
    limit: 10,
  });
  // Filter by type. Day passes are one-time; monthly/master are recurring.
  const match = prices.data.find((p) =>
    expectedRecurring ? p.recurring !== null : p.recurring === null
  );
  if (!match) {
    throw new Error(
      `No active ${expectedRecurring ? "recurring" : "one-time"} price for product ${productId}`
    );
  }
  return match.id;
}

Deno.serve(async (req) => {
  if (req.method !== "POST") return json({ error: "method_not_allowed" }, 405);

  const { token, plan, bot_slug } = await req.json();
  if (!token || !plan || !bot_slug) {
    return json({ error: "missing_fields" }, 400);
  }
  if (plan === "master" && bot_slug !== "all") {
    return json({ error: "master_must_use_bot_slug_all" }, 400);
  }

  // 1. Re-validate the JWT against the gateway (server-side, includes email).
  const validateRes = await fetch(
    `${GATEWAY_BASE_URL}/v1/upgrade/validate?token=${encodeURIComponent(token)}`,
    { headers: { "X-Gateway-Secret": GATEWAY_SHARED_SECRET } },
  );
  if (!validateRes.ok) {
    return json({ error: "invalid_or_expired_token" }, 401);
  }
  const claims = await validateRes.json();
  // claims.bot_slug must match (or be 'all' for master).
  if (plan !== "master" && claims.bot_slug !== bot_slug) {
    return json({ error: "bot_slug_mismatch" }, 400);
  }

  // 2. Find or create Stripe customer, locked to the OAuth email.
  const { data: user, error: userErr } = await supabase
    .from("users")
    .select("id, email, stripe_customer_id")
    .eq("id", claims.user_id)
    .single();
  if (userErr || !user) return json({ error: "user_not_found" }, 404);

  let customerId = user.stripe_customer_id;
  if (!customerId) {
    const cust = await stripe.customers.create({
      email: user.email,
      metadata: { user_id: user.id },
    });
    customerId = cust.id;
    await supabase
      .from("users")
      .update({ stripe_customer_id: customerId })
      .eq("id", user.id);
  }

  // 3. Resolve product ID -> active price ID at runtime.
  const productKey = plan === "master" ? "master:all" : `${plan}:${bot_slug}`;
  const productId = PRODUCTS[productKey];
  if (!productId) return json({ error: "product_not_configured", productKey }, 500);

  const priceId = await resolvePriceId(productId, plan !== "day_pass");

  // 4. Create the Checkout Session.
  const session = await stripe.checkout.sessions.create({
    customer: customerId,
    mode: plan === "day_pass" ? "payment" : "subscription",
    line_items: [{ price: priceId, quantity: 1 }],
    success_url: "https://askastrobot.com/upgrade/success",
    cancel_url:  "https://askastrobot.com/upgrade?cancelled=1",
    metadata: { user_id: user.id, plan, bot_slug },
    subscription_data: plan === "day_pass" ? undefined : {
      metadata: { user_id: user.id, plan, bot_slug },
    },
    allow_promotion_codes: true,
  });

  return json({ checkout_url: session.url });
});

function json(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "Content-Type": "application/json",
      "Access-Control-Allow-Origin": "https://askastrobot.com",
    },
  });
}
```

---

## Function 2 — `stripe-webhook`

### Endpoint

`POST https://<project>.supabase.co/functions/v1/stripe-webhook`

This is the URL you register in Stripe Dashboard → Webhooks.

### Events to subscribe to

- `checkout.session.completed`
- `customer.subscription.updated`
- `customer.subscription.deleted`
- `invoice.payment_failed`

### Logic

```ts
import Stripe from "https://esm.sh/stripe@17.5.0?target=denonext";
import { createClient } from "jsr:@supabase/supabase-js@2";

const stripe = new Stripe(Deno.env.get("STRIPE_SECRET_KEY")!, {
  apiVersion: "2024-12-18.acacia",
});
const supabase = createClient(
  Deno.env.get("SUPABASE_URL")!,
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
);
const WEBHOOK_SECRET = Deno.env.get("STRIPE_WEBHOOK_SECRET")!;
const RESEND_API_KEY = Deno.env.get("RESEND_API_KEY")!;

Deno.serve(async (req) => {
  const sig = req.headers.get("stripe-signature");
  const body = await req.text();
  if (!sig) return new Response("missing_signature", { status: 400 });

  let event: Stripe.Event;
  try {
    event = await stripe.webhooks.constructEventAsync(body, sig, WEBHOOK_SECRET);
  } catch (err) {
    return new Response(`bad_signature: ${err}`, { status: 400 });
  }

  // Idempotency: try to insert event_id; UNIQUE prevents reprocessing.
  const { error: dupeErr } = await supabase
    .from("stripe_webhook_log")
    .insert({
      event_id: event.id,
      event_type: event.type,
      payload: event,
      status: "received",
    });
  if (dupeErr && dupeErr.code === "23505") {
    return new Response("duplicate", { status: 200 });
  }
  if (dupeErr) {
    return new Response(`db_error: ${dupeErr.message}`, { status: 500 });
  }

  try {
    switch (event.type) {
      case "checkout.session.completed":
        await handleCheckoutCompleted(event.data.object as Stripe.Checkout.Session);
        break;
      case "customer.subscription.updated":
        await handleSubUpdated(event.data.object as Stripe.Subscription);
        break;
      case "customer.subscription.deleted":
        await handleSubDeleted(event.data.object as Stripe.Subscription);
        break;
      case "invoice.payment_failed":
        await handlePaymentFailed(event.data.object as Stripe.Invoice);
        break;
    }
    await supabase
      .from("stripe_webhook_log")
      .update({ processed_at: new Date().toISOString(), status: "processed" })
      .eq("event_id", event.id);
    return new Response("ok", { status: 200 });
  } catch (err) {
    await supabase
      .from("stripe_webhook_log")
      .update({ status: "failed", error_message: String(err).slice(0, 500) })
      .eq("event_id", event.id);
    // Return 500 so Stripe retries.
    return new Response(`processing_error: ${err}`, { status: 500 });
  }
});

async function handleCheckoutCompleted(session: Stripe.Checkout.Session) {
  const md = session.metadata ?? {};
  const user_id = md.user_id;
  const plan = md.plan;        // 'day_pass' | 'monthly' | 'master'
  const bot_slug = md.bot_slug; // 'prashna' | ... | 'all'
  if (!user_id || !plan || !bot_slug) {
    throw new Error(`missing_metadata: ${JSON.stringify(md)}`);
  }

  let expires_at: string;
  let stripe_subscription_id: string | null = null;

  if (plan === "day_pass") {
    expires_at = new Date(Date.now() + 24 * 60 * 60 * 1000).toISOString();
  } else {
    // Pull the Stripe subscription to learn current_period_end.
    const sub = await stripe.subscriptions.retrieve(session.subscription as string);
    stripe_subscription_id = sub.id;
    expires_at = new Date(sub.current_period_end * 1000).toISOString();
  }

  await supabase.from("subscriptions").upsert({
    user_id,
    plan,
    bot_slug,
    status: "active",
    expires_at,
    stripe_customer_id: session.customer as string,
    stripe_subscription_id,
    stripe_checkout_session_id: session.id,
    cancel_at_period_end: false,
  }, {
    onConflict: stripe_subscription_id ? "stripe_subscription_id" : "stripe_checkout_session_id",
  });

  // Send branded welcome email via Resend (best-effort — never fails the webhook).
  await sendWelcomeEmail({
    user_id,
    customerId: session.customer as string,
    plan,
    bot_slug,
    expires_at,
    amount_total: session.amount_total ?? 0,
    currency: (session.currency ?? "usd").toUpperCase(),
  });
}

async function handleSubUpdated(sub: Stripe.Subscription) {
  const newStatus =
    sub.status === "active" ? "active"
    : sub.status === "past_due" ? "past_due"
    : sub.status === "canceled" ? "cancelled"
    : "expired";

  await supabase
    .from("subscriptions")
    .update({
      status: newStatus,
      expires_at: new Date(sub.current_period_end * 1000).toISOString(),
      cancel_at_period_end: sub.cancel_at_period_end,
    })
    .eq("stripe_subscription_id", sub.id);
}

async function handleSubDeleted(sub: Stripe.Subscription) {
  // Stripe fires this when the period actually ends (with cancel_at_period_end=true)
  // OR immediately if admin cancels with `prorate=false&invoice_now=false`.
  // We DO NOT change expires_at — the user keeps access until the period ends.
  await supabase
    .from("subscriptions")
    .update({ status: "cancelled" })
    .eq("stripe_subscription_id", sub.id);
}

async function handlePaymentFailed(inv: Stripe.Invoice) {
  if (!inv.subscription) return;
  await supabase
    .from("subscriptions")
    .update({ status: "past_due" })
    .eq("stripe_subscription_id", inv.subscription as string);
}

async function sendWelcomeEmail(args: {
  user_id: string;
  customerId: string;
  plan: string;
  bot_slug: string;
  expires_at: string;
  amount_total: number;
  currency: string;
}) {
  const { data: user } = await supabase
    .from("users").select("email").eq("id", args.user_id).single();
  if (!user) return;

  // Mint a 24-hour Stripe Customer Portal link.
  const portal = await stripe.billingPortal.sessions.create({
    customer: args.customerId,
    return_url: "https://askastrobot.com/account/billing/done",
  });

  const planLabel = labelFor(args.plan, args.bot_slug);
  const html = welcomeHtml({
    planLabel,
    amount_total: args.amount_total,
    currency: args.currency,
    expires_at: args.expires_at,
    chatgpt_url: "https://chatgpt.com",
    manage_url: portal.url,
  });

  const resp = await fetch("https://api.resend.com/emails", {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${RESEND_API_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      from: "AskAstroBot <hello@askastrobot.com>",
      to: user.email,
      subject: `Your AskAstroBot ${planLabel} is active — return to your reading`,
      html,
    }),
  });

  const status = resp.ok ? "sent" : "failed";
  const data = await resp.json().catch(() => ({}));
  await supabase.from("email_send_log").insert({
    user_id: args.user_id,
    to_email: user.email,
    template: "welcome_premium",
    resend_id: data.id ?? null,
    status,
    error_message: resp.ok ? null : JSON.stringify(data).slice(0, 500),
  });
}

function labelFor(plan: string, bot_slug: string): string {
  if (plan === "master") return "Master Plan (all 4 bots)";
  if (plan === "day_pass") return `${cap(bot_slug)} Day Pass`;
  return `${cap(bot_slug)} Monthly`;
}

function cap(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function welcomeHtml(args: {
  planLabel: string;
  amount_total: number;
  currency: string;
  expires_at: string;
  chatgpt_url: string;
  manage_url: string;
}): string {
  const amount = (args.amount_total / 100).toFixed(2);
  const expiresFmt = new Date(args.expires_at).toUTCString();
  return `
<div style="font-family:system-ui,sans-serif;max-width:560px;margin:0 auto;padding:24px;color:#222">
  <h2 style="color:#7c3aed">Your ${args.planLabel} is active</h2>
  <p>Thank you for upgrading. You can return to ChatGPT now and your bot will recognise you.</p>
  <table style="width:100%;border-collapse:collapse;margin:16px 0">
    <tr><td style="padding:8px;border-top:1px solid #eee"><b>Plan</b></td>
        <td style="padding:8px;border-top:1px solid #eee">${args.planLabel}</td></tr>
    <tr><td style="padding:8px;border-top:1px solid #eee"><b>Active until</b></td>
        <td style="padding:8px;border-top:1px solid #eee">${expiresFmt}</td></tr>
    <tr><td style="padding:8px;border-top:1px solid #eee;border-bottom:1px solid #eee"><b>Amount</b></td>
        <td style="padding:8px;border-top:1px solid #eee;border-bottom:1px solid #eee">${args.currency} ${amount}</td></tr>
  </table>
  <p>
    <a href="${args.chatgpt_url}" style="display:inline-block;padding:12px 20px;background:#7c3aed;color:#fff;text-decoration:none;border-radius:6px">Return to ChatGPT</a>
  </p>
  <p style="margin-top:32px;font-size:14px;color:#555">
    <b>Manage or cancel:</b> <a href="${args.manage_url}">Open subscription manager</a>
    (link valid for 24 hours).
    <br>
    If this link expires, open any AskAstroBot and ask <i>"manage my subscription"</i>
    — the bot will give you a fresh link.
  </p>
  <p style="margin-top:24px;font-size:12px;color:#888">Om Namah Shivaya — AskAstroBot</p>
</div>`;
}
```

---

## Function 3 — `create-portal-session`

### Endpoint

`POST https://<project>.supabase.co/functions/v1/create-portal-session`

### Request body

```json
{ "token": "<portal JWT, purpose='portal'>" }
```

### Response

```json
{ "portal_url": "https://billing.stripe.com/p/session/..." }
```

### Logic

```ts
import Stripe from "https://esm.sh/stripe@17.5.0?target=denonext";
import { createClient } from "jsr:@supabase/supabase-js@2";

const stripe = new Stripe(Deno.env.get("STRIPE_SECRET_KEY")!, {
  apiVersion: "2024-12-18.acacia",
});
const supabase = createClient(
  Deno.env.get("SUPABASE_URL")!,
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
);
const GATEWAY_BASE_URL = Deno.env.get("GATEWAY_BASE_URL")!;
const GATEWAY_SHARED_SECRET = Deno.env.get("GATEWAY_SHARED_SECRET")!;

Deno.serve(async (req) => {
  if (req.method !== "POST") return json({ error: "method_not_allowed" }, 405);
  const { token } = await req.json();
  if (!token) return json({ error: "missing_token" }, 400);

  // The validate endpoint only checks 'upgrade' purpose by default; for portal we
  // accept either. The gateway exposes /v1/upgrade/validate?purpose=portal — but
  // for v1 simplicity we accept either purpose here and let the gateway enforce.
  // (Or use a dedicated /v1/portal/validate endpoint — same pattern.)
  const res = await fetch(
    `${GATEWAY_BASE_URL}/v1/upgrade/validate?token=${encodeURIComponent(token)}&purpose=portal`,
    { headers: { "X-Gateway-Secret": GATEWAY_SHARED_SECRET } },
  );
  if (!res.ok) return json({ error: "invalid_or_expired_token" }, 401);
  const claims = await res.json();

  const { data: user } = await supabase
    .from("users").select("stripe_customer_id").eq("id", claims.user_id).single();
  if (!user?.stripe_customer_id) {
    return json({ error: "no_stripe_customer" }, 404);
  }

  const portal = await stripe.billingPortal.sessions.create({
    customer: user.stripe_customer_id,
    return_url: "https://askastrobot.com/account/billing/done",
  });

  return json({ portal_url: portal.url });
});

function json(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "Content-Type": "application/json",
      "Access-Control-Allow-Origin": "https://askastrobot.com",
    },
  });
}
```

---

## Important notes

1. **Idempotency:** the gateway and webhook are both idempotent. Stripe retries
   are handled by `stripe_webhook_log.event_id` UNIQUE constraint.
2. **Service-role key safety:** Edge Functions run server-side; the service-role
   key never reaches the browser. Do NOT expose it via `SUPABASE_ANON_KEY` calls.
3. **Resend failures don't fail the webhook:** if Resend is down, the
   subscription is still active — Stripe's automatic receipt covers the user.
4. **Email is the OAuth Google email** — never read the email from the request
   body or from Stripe's billing email. Always read from `users.email`.
