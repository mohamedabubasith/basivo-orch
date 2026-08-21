#!/usr/bin/env bash
#
# Runs ON the server. Clone the repo, run this, that is the whole deployment.
#
#   ./deploy/server.sh bootstrap    first time only: Docker, swap, firewall, .env, backups
#   ./deploy/server.sh deploy       pull the branch, rebuild, migrate, restart   (default)
#   ./deploy/server.sh status       what is running, and how much it is using
#   ./deploy/server.sh logs [svc]   follow logs
#   ./deploy/server.sh backup       dump the database now
#   ./deploy/server.sh clean        reclaim disk: build cache, dead images, old logs
#   ./deploy/server.sh rollback     go back to the previous commit and redeploy
#
# Deliberately not the same script as ./deploy.sh at the root: that one drives
# Terraform and talks to a box over SSH. This one *is* on the box.
set -euo pipefail

BRANCH="${BASIVO_BRANCH:-main}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE=(docker compose -f "$ROOT/deploy/docker-compose.prod.yml")
ENV_FILE="$ROOT/deploy/.env"
BACKUP_DIR="${BASIVO_BACKUP_DIR:-/var/backups/basivo}"

if [ -t 1 ]; then
    BOLD=$'\033[1m'; RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; DIM=$'\033[2m'; OFF=$'\033[0m'
else
    BOLD=""; RED=""; GREEN=""; YELLOW=""; DIM=""; OFF=""
fi
say()  { printf '%s\n' "${BOLD}==> $*${OFF}"; }
note() { printf '%s\n' "${DIM}    $*${OFF}"; }
ok()   { printf '%s\n' "${GREEN}    $*${OFF}"; }
warn() { printf '%s\n' "${YELLOW}    $*${OFF}" >&2; }
die()  { printf '%s\n' "${RED}error: $*${OFF}" >&2; exit 1; }

need_env() {
    [ -f "$ENV_FILE" ] || die "no $ENV_FILE — run './deploy/server.sh bootstrap' first."
}

# ---------------------------------------------------------------------------
# bootstrap
# ---------------------------------------------------------------------------

cmd_bootstrap() {
    [ "$(id -u)" -eq 0 ] || die "bootstrap needs root (sudo)."

    say "Docker"
    if command -v docker >/dev/null 2>&1; then
        ok "already installed: $(docker --version)"
    else
        curl -fsSL https://get.docker.com | sh
        systemctl enable --now docker
        ok "installed"
    fi

    say "Swap"
    # Even with 16GB. A render spikes, and swap turns a would-be OOM kill into
    # a slow render — which is the trade you want for an asynchronous job.
    if swapon --show | grep -q .; then
        ok "already present: $(swapon --show=SIZE --noheadings | tr -d ' ' | paste -sd, -)"
    else
        fallocate -l 4G /swapfile
        chmod 600 /swapfile
        mkswap /swapfile >/dev/null
        swapon /swapfile
        grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
        # Prefer RAM heavily; swap is the safety net, not a working surface.
        sysctl -w vm.swappiness=10 >/dev/null
        grep -q '^vm.swappiness' /etc/sysctl.conf || echo 'vm.swappiness=10' >> /etc/sysctl.conf
        ok "4G swapfile created"
    fi

    say "Firewall"
    if command -v ufw >/dev/null 2>&1; then
        ufw allow 22/tcp  >/dev/null
        ufw allow 80/tcp  >/dev/null
        ufw allow 443/tcp >/dev/null
        yes | ufw enable   >/dev/null 2>&1 || true
        ok "22, 80, 443 open; everything else closed"
        # Postgres and Redis are not published to the host at all (see the
        # compose file), so there is nothing to close for them.
    else
        warn "ufw not installed; skipping. Postgres and Redis are not exposed regardless."
    fi

    say "Configuration"
    if [ -f "$ENV_FILE" ]; then
        ok "$ENV_FILE already exists — left alone"
    else
        cp "$ROOT/deploy/.env.example" "$ENV_FILE"
        # Generated here rather than left as a placeholder: SECRET_KEY also
        # derives the key that encrypts every stored credential, so a shared
        # or guessable value is not a login problem, it is a key-disclosure one.
        secret="$(openssl rand -base64 48 | tr -d '\n')"
        postgres_password="$(openssl rand -base64 24 | tr -d '\n=+/' )"
        sed -i "s|^SECRET_KEY=.*|SECRET_KEY=${secret}|" "$ENV_FILE"
        sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=${postgres_password}|" "$ENV_FILE"
        chmod 600 "$ENV_FILE"
        ok "$ENV_FILE created with generated secrets"
        warn "now set ACME_EMAIL, CONSOLE_ADDRESS, LANDING_ADDRESS and the email webhook in it"
    fi

    say "Nightly database backup"
    mkdir -p "$BACKUP_DIR"
    cat > /etc/cron.daily/basivo-backup <<CRON
#!/bin/sh
# The provider's weekly image is a disaster fallback, not a backup: a week of
# runs, flows and credentials is too much to lose to a bad afternoon.
exec ${ROOT}/deploy/server.sh backup
CRON
    chmod +x /etc/cron.daily/basivo-backup
    ok "/etc/cron.daily/basivo-backup → $BACKUP_DIR (14 days kept)"

    say "Done"
    note "Edit $ENV_FILE, then: ./deploy/server.sh deploy"
}

# ---------------------------------------------------------------------------
# deploy
# ---------------------------------------------------------------------------

cmd_deploy() {
    need_env
    cd "$ROOT"

    say "Fetching $BRANCH"
    local before after
    before="$(git rev-parse --short HEAD)"
    git fetch --prune origin "$BRANCH"
    # Reset rather than pull: a merge conflict on a server at 2am is not a
    # thing anyone wants to resolve over SSH. The box mirrors the branch.
    git reset --hard "origin/$BRANCH"
    after="$(git rev-parse --short HEAD)"
    if [ "$before" = "$after" ]; then
        note "already at $after"
    else
        ok "$before → $after"
        git --no-pager log --oneline "$before..$after" | sed 's/^/    /'
    fi
    echo "$before" > "$ROOT/deploy/.last-deploy"

    # A backup before the deploy, not after: the thing you want to restore is
    # the state *before* a migration you now regret.
    cmd_backup || warn "backup failed; continuing"

    say "Building"
    # No --pull. It re-downloaded python:3.12-slim, the uv image and the Caddy
    # image on every deploy — bandwidth and a minute, to almost never find a
    # change. `./deploy/server.sh deploy --pull` when you actually want fresh
    # base images, which is worth doing about monthly for security updates.
    if [ "${2:-}" = "--pull" ] || [ "${BASIVO_PULL_BASE:-}" = "1" ]; then
        "${COMPOSE[@]}" build --pull
    else
        "${COMPOSE[@]}" build
    fi

    say "Starting"
    # The API container applies migrations on boot (see docker-entrypoint.sh),
    # so ordering is: api first and alone, then everything else. Two containers
    # racing for the Alembic lock is a coin toss over which one crashes.
    "${COMPOSE[@]}" up -d --remove-orphans postgres redis
    "${COMPOSE[@]}" up -d --no-deps api
    say "Waiting for the API"
    local waited=0
    until "${COMPOSE[@]}" exec -T api curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1; do
        waited=$((waited + 2)); sleep 2
        if [ "$waited" -ge 120 ]; then
            "${COMPOSE[@]}" logs --tail 40 api
            die "the API did not become healthy in 120s. Nothing else was restarted."
        fi
    done
    ok "healthy after ${waited}s"

    "${COMPOSE[@]}" up -d
    ok "all services up"

    say "Housekeeping"
    # Dangling images from the previous build, which on a 50GB disk is not
    # optional. `-a` is deliberately avoided: it would delete the base layers
    # every rebuild has to pull again, on a connection that meters inbound.
    docker image prune -f >/dev/null
    # Build cache older than a fortnight. Recent cache is what makes the next
    # deploy take a minute instead of fifteen, so it is kept.
    docker builder prune -f --filter 'until=336h' >/dev/null 2>&1 || true
    ok "pruned — $(df -h "$ROOT" | awk 'NR==2 {print $4}') free"

    cmd_status
}

cmd_rollback() {
    need_env
    cd "$ROOT"
    [ -f "$ROOT/deploy/.last-deploy" ] || die "no record of a previous deploy."
    local target
    target="$(cat "$ROOT/deploy/.last-deploy")"
    say "Rolling back to $target"
    git reset --hard "$target"
    "${COMPOSE[@]}" build
    "${COMPOSE[@]}" up -d
    warn "the database was NOT migrated down. If the bad deploy added a migration, restore a dump."
    cmd_status
}

# ---------------------------------------------------------------------------
# operations
# ---------------------------------------------------------------------------

cmd_status() {
    need_env
    say "Containers"
    "${COMPOSE[@]}" ps --format 'table {{.Service}}\t{{.Status}}\t{{.Ports}}'
    say "Resources"
    docker stats --no-stream --format 'table {{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.CPUPerc}}'
    say "Disk"
    df -h "$ROOT" | awk 'NR==1 || NR==2'
    docker system df
    # The two that grow on their own: artifacts in the database, and dumps.
    local artifacts
    artifacts="$("${COMPOSE[@]}" exec -T postgres psql -qtAX -U basivo -d basivo_orch \
        -c "select pg_size_pretty(pg_total_relation_size('artifact'))" 2>/dev/null || echo "?")"
    note "artifacts table: ${artifacts}    backups: $(du -sh "$BACKUP_DIR" 2>/dev/null | cut -f1 || echo '-')"
}

cmd_logs() { need_env; "${COMPOSE[@]}" logs -f --tail 100 "${1:-}"; }

cmd_clean() {
    say "Before"
    df -h "$ROOT" | awk 'NR==1 || NR==2'
    docker system df

    # NOTHING here touches volumes, and that is the whole point of having this
    # as a command rather than a remembered incantation. `docker system prune
    # -a --volumes` — the one every search result suggests — deletes
    # postgres_data. That is the database: every flow, run, credential and
    # artifact, gone, with a cheerful summary of the space reclaimed.
    say "Stopped containers"
    docker container prune -f
    say "Dangling images"
    docker image prune -f
    say "Build cache older than 24h"
    docker builder prune -f --filter 'until=24h'
    say "Unused networks"
    docker network prune -f

    # Proof the data is still there, because "it reclaimed 8GB" and "it deleted
    # your database" look identical in the output above.
    say "Volumes (must still be listed)"
    docker volume ls --filter name=basivo --format '  {{.Name}}'

    say "After"
    df -h "$ROOT" | awk 'NR==1 || NR==2'
    note "Logs are capped at 10MB x 3 per service by the compose file, so they"
    note "do not need clearing. Database dumps live in $BACKUP_DIR (14 days)."
}

cmd_backup() {
    need_env
    mkdir -p "$BACKUP_DIR"
    local stamp file
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    file="$BACKUP_DIR/basivo-$stamp.sql.gz"
    # Through the container, so the password never appears in a shell history
    # or a process list on the host.
    if "${COMPOSE[@]}" exec -T postgres pg_dump -U basivo basivo_orch | gzip > "$file"; then
        ok "$file ($(du -h "$file" | cut -f1))"
    else
        rm -f "$file"
        return 1
    fi
    # Keep two weeks. Artifacts make these dumps large, and the disk is 50GB.
    find "$BACKUP_DIR" -name 'basivo-*.sql.gz' -mtime +14 -delete
}

case "${1:-deploy}" in
    bootstrap) cmd_bootstrap ;;
    deploy)    cmd_deploy ;;
    rollback)  cmd_rollback ;;
    status)    cmd_status ;;
    logs)      shift; cmd_logs "${1:-}" ;;
    clean)     cmd_clean ;;
    backup)    cmd_backup ;;
    *)         die "unknown command '$1'. Try: bootstrap, deploy, status, logs, clean, backup, rollback" ;;
esac
