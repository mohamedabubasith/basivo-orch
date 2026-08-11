#!/usr/bin/env bash
#
# One entry point for the beta deployment.
#
#   ./deploy.sh up          provision the infrastructure (first time only)
#   ./deploy.sh deploy      ship the current main to the instance
#   ./deploy.sh status      instance and container health
#   ./deploy.sh logs [svc]  follow logs
#   ./deploy.sh ssh         shell on the box
#   ./deploy.sh smoke       run the post-deploy smoke test
#   ./deploy.sh backup      dump the database to ./backups/
#   ./deploy.sh destroy     tear everything down
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TF_DIR="$ROOT/deploy/terraform"
COMPOSE="docker compose -f docker-compose.prod.yml"
REMOTE_DIR="/opt/basivo"

export AWS_PROFILE="${AWS_PROFILE:-default}"

# --- output ----------------------------------------------------------------
if [ -t 1 ]; then
    BOLD=$'\033[1m'; RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; DIM=$'\033[2m'; OFF=$'\033[0m'
else
    BOLD=""; RED=""; GREEN=""; YELLOW=""; DIM=""; OFF=""
fi

say()  { printf '%s\n' "${BOLD}$*${OFF}"; }
note() { printf '%s\n' "${DIM}  $*${OFF}"; }
warn() { printf '%s\n' "${YELLOW}$*${OFF}" >&2; }
die()  { printf '%s\n' "${RED}error: $*${OFF}" >&2; exit 1; }

# --- tools -----------------------------------------------------------------
# Resolved explicitly rather than trusted to PATH: a non-interactive shell (a
# CI runner, an editor's terminal) often has a narrower PATH than the one these
# were installed onto, and the failure is otherwise a bare "command not found".
find_tool() {
    local name="$1" found
    found="$(command -v "$name" 2>/dev/null || true)"
    [ -n "$found" ] && { printf '%s' "$found"; return; }
    for dir in /opt/homebrew/bin /usr/local/bin "$HOME/.local/bin"; do
        [ -x "$dir/$name" ] && { printf '%s' "$dir/$name"; return; }
    done
    die "$name is not installed, or not on PATH."
}

TERRAFORM="$(find_tool terraform)"

tf() { "$TERRAFORM" -chdir="$TF_DIR" "$@"; }

tf_output() {
    tf output -raw "$1" 2>/dev/null || return 1
}

require_state() {
    [ -f "$TF_DIR/terraform.tfstate" ] \
        || die "No Terraform state in deploy/terraform. Run './deploy.sh up' first."
}

instance_ip() {
    require_state
    local ip
    ip="$(tf_output static_ip)" || die "Could not read the instance address from Terraform."
    [ -n "$ip" ] || die "Terraform reported an empty address."
    printf '%s' "$ip"
}

on_box() {
    # -n matters. Without it ssh reads this script's stdin and forwards it to
    # the remote command, so a scripted `echo "..." | ./deploy.sh destroy`
    # loses its confirmation phrase to the backup's ssh before the prompt is
    # ever reached. Nothing here needs stdin.
    ssh -n -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 "ubuntu@$(instance_ip)" "$@"
}

# --- commands --------------------------------------------------------------

cmd_up() {
    say "Provisioning infrastructure"
    [ -f "$TF_DIR/terraform.tfvars" ] \
        || die "deploy/terraform/terraform.tfvars is missing. Copy terraform.tfvars.example and fill it in."

    tf init -input=false
    tf apply -input=false

    echo
    say "DNS records to add"
    tf output -raw dns_records_to_add
    echo
    note "Caddy cannot obtain a certificate until the A record resolves."
    note "Watch the first boot with:  ./deploy.sh logs-boot"
}

cmd_deploy() {
    local ip; ip="$(instance_ip)"
    say "Deploying to $ip"

    # The instance builds from GitHub, so anything not pushed is not deployed.
    # Silently shipping the last push while you look at newer local code is a
    # confusing hour.
    local branch; branch="$(git -C "$ROOT" rev-parse --abbrev-ref HEAD)"
    if [ -n "$(git -C "$ROOT" status --porcelain)" ]; then
        warn "You have uncommitted changes. They will NOT be deployed."
    fi
    git -C "$ROOT" fetch -q origin "$branch" 2>/dev/null || true
    local ahead; ahead="$(git -C "$ROOT" rev-list --count "origin/$branch..$branch" 2>/dev/null || echo 0)"
    if [ "$ahead" != "0" ]; then
        warn "$ahead local commit(s) are not pushed. They will NOT be deployed."
        printf 'Push them now? [y/N] '
        read -r reply
        [ "${reply:-n}" = "y" ] && git -C "$ROOT" push origin "$branch"
    fi

    note "pulling and rebuilding on the instance (several minutes on 1GB)"
    on_box "set -e
        cd $REMOTE_DIR
        sudo git fetch --all -q
        sudo git reset --hard -q origin/$branch
        cd $REMOTE_DIR/deploy
        sudo $COMPOSE up -d --build"

    echo
    cmd_status
    echo
    note "Migrations run automatically as the API container starts."
    note "Verify with:  ./deploy.sh smoke"
}

cmd_status() {
    local ip; ip="$(instance_ip)"
    say "Instance"
    "$(find_tool aws)" lightsail get-instance --instance-name basivo-beta \
        --query 'instance.{state:state.name,bundle:bundleId,ip:publicIpAddress}' \
        --output table 2>/dev/null || note "(could not read Lightsail state)"

    say "Containers"
    on_box "cd $REMOTE_DIR/deploy && sudo $COMPOSE ps --format 'table {{.Service}}\t{{.Status}}'" \
        || warn "could not reach the instance"

    say "Site"
    local url; url="$(tf_output site_url || echo '')"
    if [ -n "$url" ]; then
        local code
        code="$(python3 -c "
import ssl, urllib.request
try:
    with urllib.request.urlopen('$url/health', timeout=10) as r:
        print(r.status)
except Exception as exc:
    print(type(exc).__name__)
" 2>/dev/null)"
        note "$url/health -> $code"
    fi
}

cmd_logs() {
    local service="${1:-}"
    on_box "cd $REMOTE_DIR/deploy && sudo $COMPOSE logs -f --tail 100 $service"
}

cmd_logs_boot() {
    on_box "sudo tail -f /var/log/basivo-bootstrap.log"
}

cmd_ssh() {
    exec ssh -o StrictHostKeyChecking=accept-new "ubuntu@$(instance_ip)"
}

cmd_smoke() {
    python3 "$ROOT/deploy/smoke-test.py"
}

cmd_backup() {
    local dir="$ROOT/backups"
    mkdir -p "$dir"
    local file="$dir/basivo-$(date +%Y%m%d-%H%M%S).sql.gz"

    say "Dumping the database"
    # Streamed straight to a local file: the instance has 40GB but no reason to
    # accumulate dumps on the box, and a backup stored only on the machine it
    # protects is not a backup.
    on_box "cd $REMOTE_DIR/deploy && sudo $COMPOSE exec -T postgres pg_dump -U basivo basivo_orch | gzip" > "$file"

    [ -s "$file" ] || { rm -f "$file"; die "The dump was empty. Nothing was saved."; }
    note "wrote $file ($(du -h "$file" | cut -f1))"
}

cmd_destroy() {
    require_state
    local ip url
    ip="$(tf_output static_ip || echo 'unknown')"
    url="$(tf_output site_url || echo 'unknown')"

    say "This will permanently destroy the beta deployment"
    echo
    printf '%s\n' "  ${RED}The database goes with it.${OFF} Every account, flow and run log."
    printf '%s\n' "  The static IP ${BOLD}$ip${OFF} is released — a rebuild gets a different"
    printf '%s\n' "  address, and the GoDaddy A record for ${BOLD}$url${OFF} will need updating."
    printf '%s\n' "  The SES identity and its SMTP user are removed too."
    echo

    say "Terraform will destroy"
    tf plan -destroy -input=false -no-color 2>/dev/null \
        | grep -E '^  # ' | sed 's/^  # /  /' | sed 's/ will be destroyed//' \
        || note "(could not render the plan; run 'terraform plan -destroy' in deploy/terraform)"
    echo

    # A backup first, unless explicitly waived. Losing a database because a
    # teardown was one keystroke easier than a dump is a bad afternoon.
    if [ "${1:-}" != "--no-backup" ]; then
        if cmd_backup; then
            echo
        else
            warn "The backup failed."
            printf 'Continue without one? [y/N] '
            read -r reply
            [ "${reply:-n}" = "y" ] || die "Stopped. Nothing was destroyed."
        fi
    else
        warn "Skipping the backup (--no-backup)."
    fi

    # Typed confirmation, not [y/N]. A single keystroke is too small a gesture
    # for something this irreversible, and 'y' is muscle memory.
    printf '%s' "Type ${BOLD}destroy basivo-beta${OFF} to confirm: "
    read -r typed
    [ "$typed" = "destroy basivo-beta" ] || die "Did not match. Nothing was destroyed."

    say "Destroying"
    tf destroy -input=false -auto-approve

    echo
    say "Gone."
    note "Backups, if any, are in ./backups/"
    note "Remove the A record for $url at GoDaddy — it now points nowhere."
    note "Billing stops once the instance is deleted; check the Lightsail console to confirm."
}

usage() {
    cat <<'USAGE'
One entry point for the beta deployment.

  ./deploy.sh up            provision the infrastructure (first time only)
  ./deploy.sh deploy        ship the current main to the instance
  ./deploy.sh status        instance and container health
  ./deploy.sh logs [svc]    follow logs (api, web, postgres, redis)
  ./deploy.sh logs-boot     follow the first-boot provisioning log
  ./deploy.sh ssh           shell on the box
  ./deploy.sh smoke         run the post-deploy smoke test
  ./deploy.sh backup        dump the database to ./backups/
  ./deploy.sh destroy       tear everything down (backs up first, asks twice)

destroy takes --no-backup to skip the dump. It still requires the confirmation
phrase, because it releases the static IP and deletes the database.
USAGE
}

case "${1:-}" in
    up)         shift; cmd_up "$@" ;;
    deploy)     shift; cmd_deploy "$@" ;;
    status)     shift; cmd_status "$@" ;;
    logs)       shift; cmd_logs "$@" ;;
    logs-boot)  shift; cmd_logs_boot "$@" ;;
    ssh)        shift; cmd_ssh "$@" ;;
    smoke)      shift; cmd_smoke "$@" ;;
    backup)     shift; cmd_backup "$@" ;;
    destroy)    shift; cmd_destroy "$@" ;;
    ""|-h|--help|help) usage ;;
    *)          die "Unknown command '$1'. Run './deploy.sh --help'." ;;
esac
