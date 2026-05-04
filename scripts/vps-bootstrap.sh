#!/usr/bin/env bash
# =====================================================================
# AskAstroBot Gateway — VPS bootstrap (one-shot)
#
# Run this ONCE on the Hostinger VPS to deploy the gateway.
# Safe to re-run; checks for existing state.
#
# Usage from Hostinger Web Console:
#   curl -fsSL https://raw.githubusercontent.com/Madjamy/askastrobot-gateway/main/scripts/vps-bootstrap.sh | bash
# OR paste this script directly.
#
# After this finishes:
#   - Gateway runs at https://api.askastrobot.com (after DNS propagates)
#   - You get an SSH deploy key printed at the end. Add the PRIVATE key as a
#     GitHub Actions secret named VPS_SSH_PRIVATE_KEY so future pushes auto-deploy.
# =====================================================================
set -euo pipefail

REPO_URL="https://github.com/Madjamy/askastrobot-gateway.git"
INSTALL_DIR="/opt/askastrobot/gateway"
ENV_FILE="$INSTALL_DIR/.env"

c_blue() { printf "\033[1;34m%s\033[0m\n" "$*"; }
c_green() { printf "\033[1;32m%s\033[0m\n" "$*"; }
c_yellow() { printf "\033[1;33m%s\033[0m\n" "$*"; }
c_red() { printf "\033[1;31m%s\033[0m\n" "$*"; }

c_blue "=== AskAstroBot Gateway VPS bootstrap ==="
echo ""

# ---------------------------------------------------------------------
# 0. Sanity: are we on the right VPS?
# ---------------------------------------------------------------------
if [[ ! -d /opt/askastrobot ]]; then
  c_red "ERROR: /opt/askastrobot does not exist."
  c_red "Are you on the right VPS? Expected the kundali-pdf service to be installed already."
  exit 1
fi

if ! docker network inspect root_default >/dev/null 2>&1; then
  c_red "ERROR: docker network 'root_default' missing. Is Traefik running?"
  exit 1
fi

c_green "✓ /opt/askastrobot exists; root_default network present"

# ---------------------------------------------------------------------
# 1. Clone repo (idempotent)
# ---------------------------------------------------------------------
if [[ -d "$INSTALL_DIR/.git" ]]; then
  c_yellow "Gateway repo already cloned — pulling latest"
  cd "$INSTALL_DIR"
  git fetch --all
  git reset --hard origin/main
else
  c_blue "Cloning gateway repo to $INSTALL_DIR"
  git clone "$REPO_URL" "$INSTALL_DIR"
  cd "$INSTALL_DIR"
fi
c_green "✓ Repo at $INSTALL_DIR"

# ---------------------------------------------------------------------
# 2. Build .env if missing
# ---------------------------------------------------------------------
if [[ -f "$ENV_FILE" ]]; then
  c_yellow ".env already exists — leaving untouched"
  c_yellow "(if you need to regenerate, move it aside: mv .env .env.bak)"
else
  c_blue "Generating .env"

  # Generate 4 secrets locally
  OAUTH_CID=$(python3 -c "import secrets; print(secrets.token_hex(32))")
  OAUTH_CSEC=$(python3 -c "import secrets; print(secrets.token_hex(32))")
  JWT_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
  SHARED_SEC=$(python3 -c "import secrets; print(secrets.token_hex(32))")

  echo ""
  c_yellow "I need two values from your Supabase Dashboard:"
  echo "  https://supabase.com/dashboard/project/bdtzzykdhszmdlvpzlku/settings/api"
  echo ""
  read -r -p "Paste SUPABASE_ANON_KEY (the anon public key): " SB_ANON
  echo ""
  echo "Get DATABASE_URL from:"
  echo "  Settings → Database → Connection string → URI"
  echo "  Use the SESSION POOLER (port 5432), NOT the transaction pooler (6543)."
  read -r -p "Paste DATABASE_URL: " SB_DBURL

  cat > "$ENV_FILE" <<ENVEOF
ENVIRONMENT=production
LOG_LEVEL=info
PORT=8003

SUPABASE_URL=https://bdtzzykdhszmdlvpzlku.supabase.co
SUPABASE_ANON_KEY=$SB_ANON
DATABASE_URL=$SB_DBURL
SUPABASE_GOOGLE_CALLBACK_URL=https://api.askastrobot.com/oauth/google-callback

OAUTH_CLIENT_ID=$OAUTH_CID
OAUTH_CLIENT_SECRET=$OAUTH_CSEC
OAUTH_ACCESS_TOKEN_TTL=2592000
OAUTH_REFRESH_TOKEN_TTL=7776000

GATEWAY_JWT_SECRET=$JWT_SECRET
UPGRADE_TOKEN_TTL=900
PORTAL_TOKEN_TTL=900

N8N_WEBHOOK_PRASHNA=https://app.askastrobot.com/webhook/e6971529-467e-43d6-9224-3bdce40f4b3f
N8N_WEBHOOK_HOROSCOPE=https://app.askastrobot.com/webhook/1124ff92-9662-4167-bc00-da7420919f75
N8N_WEBHOOK_CAREER=https://app.askastrobot.com/webhook/dcb303fc-1346-403a-a261-e5e1705b9aa5
N8N_WEBHOOK_MARRIAGE=https://app.askastrobot.com/webhook/35f8126a-f8ae-4bac-9211-ad0fc25e6e04
GATEWAY_SHARED_SECRET=$SHARED_SEC
N8N_TIMEOUT_SECONDS=30

APP_BASE_URL=https://askastrobot.com
GATEWAY_BASE_URL=https://api.askastrobot.com

SENTRY_DSN=
ENVEOF

  chmod 600 "$ENV_FILE"

  echo ""
  c_green "✓ .env created with 4 freshly-generated secrets"
  c_yellow "WRITE THESE DOWN — you'll need them for GPT Builder + Lovable:"
  echo ""
  echo "  OAUTH_CLIENT_ID:       $OAUTH_CID"
  echo "  OAUTH_CLIENT_SECRET:   $OAUTH_CSEC"
  echo "  GATEWAY_JWT_SECRET:    $JWT_SECRET"
  echo "  GATEWAY_SHARED_SECRET: $SHARED_SEC"
  echo ""
  c_yellow "Press Enter once you've saved them in a password manager."
  read -r _
fi

# ---------------------------------------------------------------------
# 3. Build & start the container
# ---------------------------------------------------------------------
c_blue "Building and starting aab-gateway container"
cd "$INSTALL_DIR"

# Tag any existing image as :previous for rollback safety
if docker image inspect aab-gateway:latest >/dev/null 2>&1; then
  docker tag aab-gateway:latest aab-gateway:previous
fi

GATEWAY_BUILD_VERSION="$(git rev-parse --short HEAD)" docker compose up -d --build

# ---------------------------------------------------------------------
# 4. Wait for health
# ---------------------------------------------------------------------
c_blue "Waiting for gateway healthcheck"
for i in 1 2 3 4 5 6 7 8 9 10; do
  if curl -fsS http://localhost:8003/health >/dev/null 2>&1; then
    c_green "✓ Gateway is healthy on localhost:8003"
    break
  fi
  if [[ $i -eq 10 ]]; then
    c_red "Healthcheck failed after 10 retries. Check logs:"
    docker logs aab-gateway --tail 50
    exit 1
  fi
  sleep 3
done

# ---------------------------------------------------------------------
# 5. Generate SSH deploy key for GitHub Actions (idempotent)
# ---------------------------------------------------------------------
DEPLOY_KEY="/root/.ssh/aab_gateway_deploy"
if [[ ! -f "$DEPLOY_KEY" ]]; then
  c_blue "Generating SSH deploy key"
  mkdir -p /root/.ssh
  chmod 700 /root/.ssh
  ssh-keygen -t ed25519 -f "$DEPLOY_KEY" -C "github-actions-aab-gateway" -N ""
  cat "${DEPLOY_KEY}.pub" >> /root/.ssh/authorized_keys
  chmod 600 /root/.ssh/authorized_keys
fi

echo ""
c_green "=========================================="
c_green "= Gateway deployed successfully           ="
c_green "=========================================="
echo ""
c_yellow "Test public URL once DNS has propagated for api.askastrobot.com:"
echo "  curl https://api.askastrobot.com/health"
echo ""

# ---------------------------------------------------------------------
# 6. Print SSH deploy key for GitHub Actions
# ---------------------------------------------------------------------
c_blue "=== ADD THESE TO GITHUB ACTIONS SECRETS ==="
echo ""
echo "Go to: https://github.com/Madjamy/askastrobot-gateway/settings/secrets/actions"
echo "Add these 3 repository secrets (click 'New repository secret' each time):"
echo ""
echo "1. Name:  VPS_HOST"
echo "   Value: 46.28.44.45"
echo ""
echo "2. Name:  VPS_USER"
echo "   Value: root"
echo ""
echo "3. Name:  VPS_SSH_PRIVATE_KEY"
echo "   Value: (paste the BLOCK between BEGIN and END below, including those lines)"
echo ""
echo "----- BEGIN PRIVATE KEY BLOCK (copy from here) -----"
cat "$DEPLOY_KEY"
echo "----- END PRIVATE KEY BLOCK -----"
echo ""
c_yellow "Once those 3 secrets are saved, future 'git push' to main will auto-deploy."
echo ""
c_blue "Next: apply the Supabase migration → Dashboard → SQL Editor → paste migrations/0001_aab_gateway.sql → Run."
