# AskAstroBot Gateway

OAuth + paywall + n8n proxy that sits in front of the four AskAstroBot Custom GPTs.

| | |
|---|---|
| **Public URL** | `https://api.askastrobot.com` |
| **Stack** | Python 3.12, FastAPI, asyncpg, Docker, Traefik |
| **Database** | Shared Supabase (`bdtzzykdhszmdlvpzlku.supabase.co`) |
| **Deploy** | GitHub Actions → SSH → VPS git pull → docker compose up |
| **Spec** | `docs/superpowers/specs/2026-05-04-askastrobot-gateway-design.md` |

## What it does

Replaces the old donation-soft 2-query limit on the four Custom GPTs with a
real, server-side, OAuth-gated paywall:

- **Authenticates** every user via Google sign-in (Supabase Auth as identity provider).
- **Enforces** 2 free queries per bot per rolling 24h, hard-coded in the DB.
- **Forwards** authorised queries to the existing n8n webhooks unchanged.
- **Mints** short-lived JWTs that carry user identity to the website's
  Stripe checkout flow — so the user never logs in to the website.

What this gateway does NOT touch:
- Stripe (lives entirely in the website's Supabase Edge Functions).
- The n8n workflows or astro pipeline (forwarded byte-for-byte).
- The PDF report pipeline (`pdf.askastrobot.com`, `soul.askastrobot.com`) —
  separate services, also on this VPS.

## Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET`  | `/oauth/authorize` | client_id | Begin OAuth (delegates to Supabase Google sign-in) |
| `GET`  | `/oauth/google-callback` | Supabase session | Internal — Supabase returns here |
| `POST` | `/oauth/token` | client_id + secret (form body) | Auth code exchange + refresh. **Returns 401 on any failure.** |
| `POST` | `/v1/gpt/{bot_slug}/query` | Bearer | Quota + sub check + n8n forward |
| `GET`  | `/v1/upgrade/validate` | None (browser) / X-Gateway-Secret (server) | Verify upgrade JWT |
| `GET`  | `/health` | None | Liveness |
| `GET`  | `/health/deep` | None | Readiness (DB + n8n reachability) |

## Local dev

```bash
cp .env.example .env
# Fill in real values — see Settings section below
python -m venv .venv
source .venv/Scripts/activate    # Git Bash on Windows: source .venv/Scripts/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8003
```

Test with curl:
```bash
curl http://localhost:8003/health
```

## Deploy

GitHub Actions auto-deploys on push to `main`. To deploy manually:

```bash
ssh root@46.28.44.45
cd /opt/askastrobot/gateway
git pull
docker compose up -d --build
curl https://api.askastrobot.com/health
```

### First-time VPS setup

```bash
# 1. Clone the repo to the existing askastrobot folder
cd /opt/askastrobot
git clone git@github.com:Madjamy/askastrobot-gateway.git gateway
cd gateway

# 2. Create .env (paste from a secure source — never commit)
nano .env

# 3. Apply DB migration via Supabase SQL editor
#    (paste migrations/0001_aab_gateway.sql into Dashboard → SQL Editor → Run)

# 4. Build and start
docker compose up -d --build

# 5. Verify
curl http://localhost:8003/health
curl https://api.askastrobot.com/health
```

Container name: `aab-gateway`. Port 8003 (internal). Traefik routes
`api.askastrobot.com` to it on the `root_default` network.

## Apply the DB migration

```sql
-- Paste migrations/0001_aab_gateway.sql into Supabase Dashboard → SQL Editor → Run.
-- Idempotent — safe to re-run.
```

## Generate secrets

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Use this for: `OAUTH_CLIENT_ID`, `OAUTH_CLIENT_SECRET`, `GATEWAY_JWT_SECRET`,
`GATEWAY_SHARED_SECRET`. Keep the same `GATEWAY_SHARED_SECRET` value in the
website's Supabase Edge Function env vars and in n8n workflows.

## Settings

See `.env.example` for the full env-var list.

## Operations

### View logs

```bash
ssh root@46.28.44.45
docker logs -f aab-gateway
```

Logs are JSON; pipe through `jq` for readability.

### Manually revoke a user

```sql
-- Force user back to free tier immediately:
UPDATE gw_oauth_tokens   SET revoked_at = NOW()
  WHERE user_id = '<uuid>' AND revoked_at IS NULL;
UPDATE gw_subscriptions  SET status = 'cancelled', expires_at = NOW()
  WHERE user_id = '<uuid>' AND status = 'active';
```

### Rollback a bad deploy

```bash
ssh root@46.28.44.45
cd /opt/askastrobot/gateway
docker tag aab-gateway:previous aab-gateway:latest
docker compose up -d --no-build
```

The deploy workflow tags `:previous` before each new build.

### Rotate `GATEWAY_SHARED_SECRET`

1. Edit `.env` on VPS, change the value.
2. `docker compose up -d` (no rebuild needed since env is loaded at runtime).
3. Update the same value in Supabase Edge Function secrets.
4. Update n8n workflow header check.
5. Allow ~5 minute overlap by accepting both old and new values briefly.

## Tests

```bash
pip install pytest pytest-asyncio httpx
pytest -q tests/
```

Integration tests (require live DB) are gated with the `integration` marker:
```bash
pytest -q -m integration
```

## Project layout

```
.
├── app/
│   ├── main.py            FastAPI app, lifespan, middleware
│   ├── settings.py        Pydantic-settings env loading
│   ├── db.py              asyncpg pool
│   ├── deps.py            Bearer auth dependency
│   ├── logging_setup.py   structlog JSON
│   ├── health.py          /health, /health/deep
│   ├── oauth/
│   │   ├── authorize.py   /oauth/authorize, /oauth/google-callback
│   │   ├── token.py       /oauth/token (form-encoded, 401-on-failure, atomic refresh)
│   │   ├── jwt_utils.py   Upgrade JWT mint + verify (HS256)
│   │   └── redirect_uri.py  ChatGPT redirect-URI regex validation
│   ├── gpt/
│   │   ├── proxy.py       /v1/gpt/{slug}/query (atomic quota, sub check, n8n forward)
│   │   └── n8n.py         httpx forwarding client
│   └── upgrade/
│       └── validate.py    /v1/upgrade/validate
├── migrations/
│   └── 0001_aab_gateway.sql
├── lovable-handoff/       Specs for the website team
├── gpt-bots/              Paste-ready OpenAPI + system prompts (one folder per bot)
├── tests/
├── .github/workflows/deploy.yml
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .env.example
```
