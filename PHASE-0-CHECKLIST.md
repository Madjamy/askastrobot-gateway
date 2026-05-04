# Phase 0 — what you do, in order

These steps unlock everything. Code is already written; nothing else can go
live until you finish this. Each step says **what** to do and **why**.

Estimated time: **90 minutes total**, spread across however many sittings you want.

---

## 1. DNS — `api.askastrobot.com` → VPS  *(5 min)*

In your domain registrar / Cloudflare:

- Add an **A record**: `api` → `46.28.44.45`.
- If using Cloudflare proxy (orange cloud), set **SSL → Full (strict)**.
  Otherwise (DNS-only, gray cloud) just point and forget.

Verify after ~5 minutes:
```
nslookup api.askastrobot.com
# Should resolve to 46.28.44.45
```

---

## 2. GitHub repo  *(3 min)*

Create a new **private** repo at: https://github.com/new

- Name: `askastrobot-gateway`
- Owner: `Madjamy`
- Visibility: **Private**
- Do NOT initialize with README, .gitignore, or license.

After creation, run from your local repo folder:

```bash
cd "c:/Users/madhu/OneDrive/AI Project/Cursor/askastrobot-gateway"
git init
git add -A
git commit -m "Initial commit: gateway scaffold + Lovable hand-off"
git branch -M main
git remote add origin git@github.com:Madjamy/askastrobot-gateway.git
git push -u origin main
```

(If you don't have SSH keys set up to GitHub, use the HTTPS URL with a Personal Access Token.)

---

## 3. VPS access for CI/CD  *(15 min)*

The GitHub Actions deploy workflow needs SSH access to the VPS.

### 3a. Create a deploy SSH key on your local PC

```bash
ssh-keygen -t ed25519 -f "$HOME/.ssh/aab_gateway_deploy" -C "github-actions deploy" -N ""
```

This creates two files:
- `~/.ssh/aab_gateway_deploy`        (private key — NEVER commit)
- `~/.ssh/aab_gateway_deploy.pub`    (public key)

### 3b. Add the public key to the VPS

Hostinger web console → connect to VPS → run:

```bash
mkdir -p ~/.ssh
cat >> ~/.ssh/authorized_keys
# paste the contents of aab_gateway_deploy.pub, then Ctrl+D
chmod 600 ~/.ssh/authorized_keys
```

Verify from your PC:
```bash
ssh -i ~/.ssh/aab_gateway_deploy root@46.28.44.45 "echo connected"
# expect: connected
```

### 3c. Add the private key as a GitHub secret

GitHub → askastrobot-gateway → Settings → Secrets and variables → Actions → New repository secret. Add three secrets:

| Name | Value |
|---|---|
| `VPS_SSH_PRIVATE_KEY` | the full contents of `~/.ssh/aab_gateway_deploy` (the private file, including BEGIN/END lines) |
| `VPS_HOST` | `46.28.44.45` |
| `VPS_USER` | `root` |

---

## 4. Clone the repo on the VPS  *(5 min)*

VPS web console:

```bash
cd /opt/askastrobot
git clone git@github.com:Madjamy/askastrobot-gateway.git gateway
# OR if SSH not set on VPS:
# git clone https://github.com/Madjamy/askastrobot-gateway.git gateway

cd gateway
ls   # confirm files are there
```

Don't `docker compose up` yet — we need .env first (next step).

---

## 5. Generate gateway secrets and write `.env` on the VPS  *(10 min)*

On your local PC, generate four random secrets:

```bash
python -c "import secrets; [print(secrets.token_hex(32)) for _ in range(4)]"
```

That prints 4 lines. They become:
- Line 1 → `OAUTH_CLIENT_ID`
- Line 2 → `OAUTH_CLIENT_SECRET`
- Line 3 → `GATEWAY_JWT_SECRET`
- Line 4 → `GATEWAY_SHARED_SECRET`

**Save all four** in a secure note (1Password, etc.) — you need them again
in steps 6, 7, 9, and the Lovable hand-off.

On the VPS:

```bash
cd /opt/askastrobot/gateway
cp .env.example .env
nano .env    # paste in real values, save with Ctrl+X, Y, Enter
```

You also need:
- `SUPABASE_ANON_KEY` — from Supabase Dashboard → Settings → API → anon public
- `DATABASE_URL` — from Supabase Dashboard → Settings → Database → Connection string (URI). Use the **Session pooler** URL on port 5432 (NOT 6543 — that's the transaction pooler which doesn't support advisory locks).

---

## 6. Configure Supabase Google Auth  *(15 min)*

### 6a. Google Cloud Console

https://console.cloud.google.com/ — create or pick a project.

- APIs & Services → OAuth consent screen → set up (External, app name "AskAstroBot", your email).
- APIs & Services → Credentials → Create Credentials → OAuth 2.0 Client ID → Web application.
  - Name: "Supabase — AskAstroBot Gateway"
  - **Authorised redirect URIs** (single):
    `https://bdtzzykdhszmdlvpzlku.supabase.co/auth/v1/callback`
- Save. Copy the **Client ID** and **Client secret** values.

### 6b. Supabase

Supabase Dashboard → Authentication → Providers → Google → Enable.
- Paste Google Client ID and Client Secret.
- Save.

Authentication → URL Configuration → Redirect URLs → **Add**:
`https://api.askastrobot.com/oauth/google-callback`

---

## 7. Apply the database migration  *(2 min)*

Supabase Dashboard → SQL Editor → New query → paste the entire contents of
`migrations/0001_aab_gateway.sql` → **Run**.

It's idempotent (uses `IF NOT EXISTS`) so safe to re-run.

Verify:
```sql
SELECT count(*) FROM information_schema.tables
 WHERE table_schema = 'public'
   AND table_name IN ('oauth_codes','oauth_tokens','query_log','subscriptions','stripe_webhook_log','email_send_log');
-- expect: 6
```

---

## 8. First gateway deploy  *(5 min)*

On your local PC, push to main if you haven't already (step 2 covered that).

GitHub Actions will run automatically and deploy. Watch:
GitHub → askastrobot-gateway → Actions tab → latest run.

Or deploy manually on VPS:

```bash
cd /opt/askastrobot/gateway
docker compose up -d --build
docker logs aab-gateway --tail 50
curl http://localhost:8003/health
```

Once container is running, verify the public URL:

```bash
curl https://api.askastrobot.com/health
# expect: {"status":"ok","version":"..."}
```

---

## 9. Stripe setup  *(20 min — can run in parallel with steps 1-8)*

Open `lovable-handoff/03-stripe-dashboard-checklist.md` and follow it
exactly. At the end you'll have:

- 9 `price_*` IDs (see note below about the products you sent)
- A webhook signing secret (`whsec_...`) — but the URL doesn't exist yet,
  so create the webhook endpoint **after** the Lovable Edge Functions are deployed.
- Your Stripe secret key (`sk_*`)

Save these for the Lovable hand-off.

> ⚠️ **About the products you sent me:** you sent **product IDs**
> (`prod_SaqK...` etc.) but Stripe Checkout needs **price IDs**
> (`price_*`). For each of the 4 bot products, create a monthly price at
> the right rate ($7.99 for Prashna/Horoscope, $5.99 for Career/Marriage).
> For the universal Day Pass product, create 4 one-time prices of $2.99
> (one per bot) so per-bot reporting stays clean. Then create a NEW
> product "AskAstroBot — Master (all 4 bots)" with a $12.99/mo price.
> Send all 9 `price_*` IDs back to me.

---

## 10. Send the Lovable team the hand-off  *(2 min)*

Email or Slack the entire `lovable-handoff/` folder — it's self-contained.
Tell them:

> "This folder has everything you need to wire the website into the new
> gateway. Read README.md first, then 01-pages-spec.md and 02-edge-functions-spec.md.
> Use the test-mode Stripe keys until I give you live keys.
> Questions to me, not Claude."

Pass them:
- The 9 `price_*` IDs from step 9
- The Stripe secret key (test mode first, live after testing)
- The `GATEWAY_SHARED_SECRET` value from step 5

---

## 11. Configure GPT Builder for the test bot  *(10 min)*

Open the test bot in ChatGPT → Edit GPT → Configure:

1. **Actions** → edit existing action → click the trash icon to remove the
   old donation action (if present).
2. Add a new action:
   - Authentication → **OAuth**
   - Client ID: `<OAUTH_CLIENT_ID from step 5>`
   - Client Secret: `<OAUTH_CLIENT_SECRET from step 5>`
   - Authorization URL: `https://api.askastrobot.com/oauth/authorize`
   - Token URL: `https://api.askastrobot.com/oauth/token`
   - Scope: `read:astro write:query`
   - Token Exchange Method: **Default (POST request)**
   - Save.
3. **Schema** → paste the contents of `gpt-bots/prashna/openapi.json` (or
   whichever bot the test is — the schemas are identical aside from the path).
4. **Instructions** (system prompt):
   - Remove the old donation/2-query block.
   - Append the contents of `gpt-bots/_shared/system-prompt-paywall-block.md`.
5. **Save** the bot.
6. **Test**: open the bot in chat, ask any astro question. You'll be
   prompted to sign in with Google. Sign in. Verify you get an answer.
   Ask 2 more questions — third one should hit the paywall.

---

## 12. End-to-end test on the test bot  *(15 min)*

Run each scenario in `lovable-handoff/05-test-plan.md`:
S1, S2, S3 (with Stripe test card `4242 4242 4242 4242`),
S5, S6, S7, S8, S9.

**Do not roll out to production bots until all 9 scenarios pass.**

---

## 13. Roll out to the 4 production bots  *(30 min)*

Repeat step 11 for each of:
- Prashna (use `gpt-bots/prashna/openapi.json`)
- Horoscope Analysis (`gpt-bots/horoscope/openapi.json`)
- Career Astrology (`gpt-bots/career/openapi.json`)
- Kundali Milan (`gpt-bots/marriage/openapi.json`)

---

## 14. Add `X-Gateway-Secret` check on each n8n workflow  *(15 min)*

For each of the 4 n8n workflows (Prashna, Horoscope, Career, Marriage):

1. Open the workflow in n8n editor.
2. Insert a **Function** (or **IF**) node at the very start, before the
   astro logic.
3. Set the condition:
   - If `$json.headers["x-gateway-secret"] !== "<GATEWAY_SHARED_SECRET>"` → return HTTP 401 / abort.
   - Else → pass through to the existing logic.
4. Save and **activate** the workflow.

This ensures direct external calls to the n8n webhooks (bypassing the
gateway) get rejected. The gateway always sends the header.

---

## What you do NOT need to do

- ❌ Set up SSL / Certbot — Traefik handles this automatically via the
  existing `mytlschallenge` resolver on the VPS.
- ❌ Open firewall ports — Traefik is already on 80/443.
- ❌ Migrate the existing 100 web users — they keep their parallel auth.
- ❌ Build the donation page anything — old donation flow is removed
  from the GPT prompts.

---

## Where to ask for help

If anything in this list breaks, copy the error message and ping me
(Claude Code) — I'll diagnose. The most likely failure points are:
- DNS not propagated yet (wait a few minutes)
- Supabase connection string used the transaction pooler (port 6543) instead
  of session pooler (5432) → advisory locks fail
- VPS deploy key not added correctly → CI fails on SSH step

---

**End of Phase 0.**
