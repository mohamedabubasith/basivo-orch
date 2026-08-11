#!/bin/bash
# Re-exec under bash before anything else.
#
# Lightsail prepends its own initialisation to user data, which displaces this
# shebang. cloud-init then runs the whole thing with /bin/sh — dash on Ubuntu —
# where `set -o pipefail` and the process substitution below are syntax errors,
# and the only symptom is `cloud-init status: error` with no bootstrap log.
# `$$` escapes the interpolation: templatefile would otherwise try to
# evaluate BASH_VERSION as a Terraform expression and fail to render.
if [ -z "$${BASH_VERSION:-}" ]; then exec /bin/bash "$0" "$@"; fi

# First-boot provisioning. Runs once, as root, before the instance is useful.
#
# Everything it writes is idempotent, because Lightsail will re-run user data
# if the instance is rebuilt from a snapshot and a half-applied bootstrap is
# worse than one that repeats.
set -euo pipefail
exec > >(tee -a /var/log/basivo-bootstrap.log) 2>&1
echo "=== bootstrap started $(date -Is) ==="

export DEBIAN_FRONTEND=noninteractive

# --- swap ------------------------------------------------------------------
# The box has 1 GB. The Vite build alone peaks above that, and without swap the
# kernel kills it partway through with no useful message. 2 GB of swap on the
# 40 GB volume costs nothing and turns a failed deploy into a slow one.
if [ ! -f /swapfile ]; then
    fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
    # Prefer RAM heavily; swap is an overflow for build spikes, not a place to
    # run Postgres from.
    sysctl -w vm.swappiness=10
    echo 'vm.swappiness=10' > /etc/sysctl.d/99-basivo.conf
fi

# --- packages --------------------------------------------------------------
apt-get update -y
apt-get install -y --no-install-recommends \
    ca-certificates curl git jq unattended-upgrades \
    docker.io docker-compose-v2

systemctl enable --now docker

# Security updates apply themselves. An unpatched box is the likeliest way this
# deployment gets compromised, and nobody remembers to log in and run apt.
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF

# --- source ----------------------------------------------------------------
install -d -m 0755 /opt
if [ ! -d /opt/basivo/.git ]; then
    git clone --branch "${repo_ref}" --depth 1 "${repo_url}" /opt/basivo
fi
cd /opt/basivo

# --- configuration ---------------------------------------------------------
# Secrets are generated here, on the machine, and never leave it. They are not
# in Terraform state, not in user data, and not in the repository.
ENV_FILE=/opt/basivo/deploy/.env
if [ ! -f "$ENV_FILE" ]; then
    SECRET_KEY="$(openssl rand -base64 48 | tr -d '\n=' | tr '+/' '-_')"
    POSTGRES_PASSWORD="$(openssl rand -base64 24 | tr -d '\n=+/' )"

    cat > "$ENV_FILE" <<EOF
# Generated on first boot. Never commit this file.
ENVIRONMENT=production
DEBUG=false
APP_NAME=Basivo Orchestrator
LOG_JSON=true

SECRET_KEY=$SECRET_KEY
POSTGRES_PASSWORD=$POSTGRES_PASSWORD

PUBLIC_BASE_URL=https://${site_host}
FRONTEND_BASE_URL=https://${site_host}
CORS_ORIGINS=https://${site_host}

# Caddy terminates TLS and forwards one hop. Left at 0, every client IP would
# read as the proxy's container address — so rate limiting, lockout and the
# audit trail would all key on a single "user" and stop working entirely.
TRUSTED_PROXY_COUNT=1

EMAIL_FROM=no-reply@${mail_domain}
EMAIL_FROM_NAME=Basivo
TOTP_ISSUER=Basivo

# Email goes out through a webhook you own — an n8n workflow that sends it
# from a Gmail account this service holds no credentials for.
#
# The secret must match BASIVO_WEBHOOK_SECRET in n8n's environment, or the
# workflow rejects every request as unsigned.
EMAIL_WEBHOOK_URL=${email_webhook_url}
EMAIL_WEBHOOK_SECRET=${email_webhook_secret}
EMAIL_WEBHOOK_AUTH_HEADER=
EMAIL_WEBHOOK_TIMEOUT_SECONDS=10

# Caddy
SITE_ADDRESS=${site_host}
ACME_EMAIL=${acme_email}
EOF
    chmod 600 "$ENV_FILE"
fi

# --- build and start -------------------------------------------------------
# Built on the instance rather than pulled from a registry: it keeps the
# deployment to one moving part and costs nothing. It is also slow on this
# hardware — several minutes — which is the trade being made.
cd /opt/basivo/deploy
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml up -d

echo "=== bootstrap finished $(date -Is) ==="
