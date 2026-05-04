#!/usr/bin/env bash
# =====================================================================
# AskAstroBot Gateway — VPS bootstrap (one-shot)
#
# Run this ONCE on the Hostinger VPS to deploy the gateway.
# Safe to re-run; checks for existing state.
#
# Usage from Hostinger Web Console (after the repo is public OR after
# you have set up a deploy key on the VPS):
#   curl -fsSL https://raw.githubusercontent.com/Madjamy/askastrobot-gateway/main/scripts/vps-bootstrap.sh | bash
#
# Secrets are written to /root/.aab-gateway-secrets (mode 600), NOT to stdout.
# After this finishes:
#   - Gateway runs at https://api.askastrobot.com (after DNS propagates)
#   - You will be told to `cat /root/.aab-gateway-secrets` and copy values into
#     a password manager, then `shred` the file.
# =====================================================================
set -euo pipefail

REPO_URL="https://github.com/Madjamy/askastrobot-gateway.git"
INSTALL_DIR="/opt/askastrobot/gateway"
ENV_FILE="$INSTALL_DIR/.env"
SECRETS_OUT="/root/.aab-gateway-secrets"

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
# 2. Build .env if missing — secrets written ONLY to .env and SECRETS_OUT
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
  c_yellow "I need values from your Supabase + Google Cloud Console."
  echo ""
  echo "Supabase:  https://supabase.com/dashboard/project/bdtzzykdhszmdlvpzlku/settings/api"
  echo "  - Get DATABASE_URL from: Settings → Database → Connection string → URI"
  echo "    Use the TRANSACTION POOLER (port 6543), not the session pooler."
  echo ""
  read -r -p "Paste DATABASE_URL: " SB_DBURL
  echo ""
  echo "Google Cloud Console: https://console.cloud.google.com/apis/credentials"
  echo "  - Create OAuth 2.0 Client (Web application)"
  echo "  - Authorised redirect URI: https://api.askastrobot.com/oauth/google-callback"
  echo "  - Then paste Client ID + Secret below."
  echo ""
  read -r -p "Paste GOOGLE_CLIENT_ID: " GOOGLE_CID
  read -r -s -p "Paste GOOGLE_CLIENT_SECRET (input hidden): " GOOGLE_CSEC
  echo ""

  cat > "$ENV_FILE" <<ENVEOF
ENVIRONMENT=production
LOG_LEVEL=info
PORT=8003

DATABASE_URL=$SB_DBURL

OAUTH_CLIENT_ID=$OAUTH_CID
OAUTH_CLIENT_SECRET=$OAUTH_CSEC
OAUTH_ACCESS_TOKEN_TTL=2592000
OAUTH_REFRESH_TOKEN_TTL=7776000

GOOGLE_CLIENT_ID=$GOOGLE_CID
GOOGLE_CLIENT_SECRET=$GOOGLE_CSEC

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

  # Write secrets to a separate locked file the user reads ONCE then shreds.
  cat > "$SECRETS_OUT" <<SECEOF
# AskAstroBot Gateway secrets (generated $(date -Iseconds))
# Save these into a password manager, then shred this file:
#   shred -u $SECRETS_OUT

OAUTH_CLIENT_ID         = $OAUTH_CID
OAUTH_CLIENT_SECRET     = $OAUTH_CSEC
GATEWAY_JWT_SECRET      = $JWT_SECRET
GATEWAY_SHARED_SECRET   = $SHARED_SEC
SECEOF
  chmod 600 "$SECRETS_OUT"

  echo ""
  c_green "✓ .env created"
  c_yellow "Secrets written to $SECRETS_OUT (mode 600)."
  c_yellow "Run:   cat $SECRETS_OUT"
  c_yellow "Save the values into a password manager, then:   shred -u $SECRETS_OUT"
  echo ""
fi

# ---------------------------------------------------------------------
# 3. Build & start the container, with rollback on healthcheck failure
# ---------------------------------------------------------------------
c_blue "Building and starting aab-gateway container"
cd "$INSTALL_DIR"

# Tag any existing image as :previous for rollback safety
HAD_PREVIOUS_IMAGE=0
if docker image inspect aab-gateway:latest >/dev/null 2>&1; then
  docker tag aab-gateway:latest aab-gateway:previous
  HAD_PREVIOUS_IMAGE=1
fi

GATEWAY_BUILD_VERSION="$(git rev-parse --short HEAD)" docker compose up -d --build

# ---------------------------------------------------------------------
# 4. Wait for health; rollback if it fails
# ---------------------------------------------------------------------
c_blue "Waiting for gateway healthcheck"
HEALTH_OK=0
for i in 1 2 3 4 5 6 7 8 9 10; do
  if curl -fsS http://localhost:8003/health >/dev/null 2>&1; then
    c_green "✓ Gateway is healthy on localhost:8003"
    HEALTH_OK=1
    break
  fi
  sleep 3
done

if [[ $HEALTH_OK -eq 0 ]]; then
  c_red "Healthcheck failed after 10 retries. Recent logs:"
  docker logs aab-gateway --tail 80 || true

  if [[ $HAD_PREVIOUS_IMAGE -eq 1 ]]; then
    c_yellow "Rolling back to previous image"
    docker compose down
    docker tag aab-gateway:previous aab-gateway:latest
    docker compose up -d
    sleep 5
    if curl -fsS http://localhost:8003/health >/dev/null 2>&1; then
      c_green "✓ Rolled back; previous version is healthy"
    else
      c_red "Rollback also failed — manual intervention required"
    fi
  fi
  exit 1
fi

# ---------------------------------------------------------------------
# 5. Generate SSH deploy key for GitHub Actions (idempotent)
#    Key is written to disk; not echoed to stdout.
# ---------------------------------------------------------------------
DEPLOY_KEY="/root/.ssh/aab_gateway_deploy"
if [[ ! -f "$DEPLOY_KEY" ]]; then
  c_blue "Generating SSH deploy key"
  mkdir -p /root/.ssh
  chmod 700 /root/.ssh
  ssh-keygen -t ed25519 -f "$DEPLOY_KEY" -C "github-actions-aab-gateway" -N "" -q
  # Append public key to authorized_keys, dedupe.
  if ! grep -qFf "${DEPLOY_KEY}.pub" /root/.ssh/authorized_keys 2>/dev/null; then
    cat "${DEPLOY_KEY}.pub" >> /root/.ssh/authorized_keys
    chmod 600 /root/.ssh/authorized_keys
  fi
fi

echo ""
c_green "=========================================="
c_green "= Gateway deployed successfully           ="
c_green "=========================================="
echo ""
c_yellow "Test the public URL once DNS has propagated for api.askastrobot.com:"
echo "  curl https://api.askastrobot.com/health"
echo ""

# ---------------------------------------------------------------------
# 6. SSH deploy key — instructions only, no key on stdout
# ---------------------------------------------------------------------
c_blue "=== NEXT: ADD GITHUB ACTIONS SECRETS ==="
echo ""
echo "Go to: https://github.com/Madjamy/askastrobot-gateway/settings/secrets/actions"
echo ""
echo "Add these 3 repository secrets:"
echo "  Name: VPS_HOST              Value: 46.28.44.45"
echo "  Name: VPS_USER              Value: root"
echo "  Name: VPS_SSH_PRIVATE_KEY   Value: <contents of $DEPLOY_KEY>"
echo ""
echo "To copy the private key safely, run:"
echo "  cat $DEPLOY_KEY"
echo ""
echo "Then paste the entire BEGIN/END block into the GitHub secret form."
echo ""
c_yellow "Do not screenshot, log, or paste this key into chat. Use the GitHub secrets UI directly."
echo ""
c_blue "Next: apply the Supabase migration → Dashboard → SQL Editor → paste"
echo "      migrations/0001_aab_gateway.sql → Run."
