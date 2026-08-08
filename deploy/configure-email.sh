#!/usr/bin/env bash
# Push the SES SMTP settings to the instance and restart the API.
#
# Separate from Terraform's user data on purpose: user data is retrievable from
# the Lightsail API for as long as the instance exists, so a credential written
# there is a credential stored in clear, indefinitely. This sends it over SSH
# into a 0600 file instead.
set -euo pipefail

cd "$(dirname "$0")/terraform"

if ! terraform output -raw static_ip >/dev/null 2>&1; then
    echo "No Terraform state here. Run 'terraform apply' first." >&2
    exit 1
fi

IP="$(terraform output -raw static_ip)"
HOST="$(terraform output -raw smtp_host)"
USER="$(terraform output -raw smtp_username)"
PASS="$(terraform output -raw smtp_password)"
DOMAIN="$(terraform output -raw site_url | sed 's|https://||')"

echo "checking that SES has verified the domain…"
STATUS="$(aws ses get-identity-verification-attributes \
    --identities "${DOMAIN#*.}" \
    --query "VerificationAttributes.*.VerificationStatus" --output text 2>/dev/null || echo Unknown)"

if [ "$STATUS" != "Success" ]; then
    echo
    echo "  SES reports: $STATUS"
    echo "  The DKIM CNAMEs have not verified yet. Add the records from"
    echo "  'terraform output dns_records_to_add' and try again in a few minutes."
    echo "  (DNS changes at GoDaddy usually take 10-30 minutes.)"
    echo
    exit 1
fi

echo "verified. writing settings to the instance…"

# Rewritten in place rather than appended: running this twice should leave one
# copy of each setting, not two, and the last-wins behaviour of .env files
# makes duplicates silently confusing rather than obviously broken.
ssh -o StrictHostKeyChecking=accept-new "ubuntu@$IP" \
    "sudo python3 - '$HOST' '$USER' '$PASS'" <<'PY'
import sys, pathlib

host, user, password = sys.argv[1:4]
path = pathlib.Path("/opt/basivo/deploy/.env")
settings = {
    "SMTP_HOST": host,
    "SMTP_PORT": "587",
    "SMTP_USER": user,
    "SMTP_PASSWORD": password,
    "SMTP_TLS": "true",
}

lines, seen = [], set()
for line in path.read_text().splitlines():
    key = line.split("=", 1)[0].strip() if "=" in line else None
    if key in settings:
        lines.append(f"{key}={settings[key]}")
        seen.add(key)
    else:
        lines.append(line)
for key, value in settings.items():
    if key not in seen:
        lines.append(f"{key}={value}")

path.write_text("\n".join(lines) + "\n")
path.chmod(0o600)
print("wrote", path)
PY

echo "restarting the api…"
ssh "ubuntu@$IP" \
    'cd /opt/basivo/deploy && sudo docker compose -f docker-compose.prod.yml up -d api'

echo
echo "Done. Note SES is in the sandbox until you request production access:"
echo "mail will only reach addresses you have verified in the SES console."
