#!/usr/bin/env bash
# /var/www/dominio/deploy/scripts/deploy.sh [SHA]
#
# Runs ON THE SERVER. Called by GitHub Actions over SSH with the commit that
# passed CI (github.sha); without an argument it deploys origin/main.
#
#   1. records the running commit in .deploy_prev (rollback target)
#   2. checks out the new commit and re-executes THIS script from it, so a
#      change to the deploy logic applies on the same deploy (and bash never
#      reads a file that git just rewrote under it)
#   3. pip install, backup of the DB, migrate, createcachetable, collectstatic
#   4. restarts gunicorn and smoke-tests /healthz/ (fallback /) and widget.js
#      through the local nginx; exits 1 if the site does not answer, which
#      turns the GitHub Actions job red.
#
# Rollback: bash /var/www/dominio/deploy/scripts/rollback.sh
#
# The whole body lives in main(), called on the last line: bash parses it in
# full before running anything, so rewriting this file mid-run is harmless.

set -euo pipefail

APP_DIR="${APP_DIR:-/var/www/dominio}"
VENV="$APP_DIR/venv"
SERVICE="gunicorn-dominio"
PREV_FILE="$APP_DIR/.deploy_prev"
PREV_BACKUP_FILE="$APP_DIR/.deploy_prev_backup"
PREDEPLOY_DIR="${PREDEPLOY_DIR:-/var/backups/dominio/predeploy}"
PREDEPLOY_KEEP="${PREDEPLOY_KEEP:-5}"

log() { echo "==> $*"; }

checkout() {
    local target="${1:-origin/main}"
    cd "$APP_DIR"

    log "Fetching origin/main"
    git fetch --quiet origin main

    local new_sha prev_sha
    new_sha="$(git rev-parse --verify "${target}^{commit}")" || {
        echo "ERROR: $target no es un commit conocido tras el fetch" >&2; exit 1; }
    # Only commits that are on main reach production (a manual run from
    # another branch, or a typo, must not deploy).
    if ! git merge-base --is-ancestor "$new_sha" origin/main; then
        echo "ERROR: $new_sha no esta en origin/main; no se despliega" >&2
        exit 1
    fi

    prev_sha="$(git rev-parse HEAD)"
    if [ "$prev_sha" != "$new_sha" ]; then
        # A re-run of the same commit must NOT overwrite the rollback target
        # with itself.
        echo "$prev_sha" > "$PREV_FILE"
        # The predeploy backup pointer belongs to the previous deploy; this
        # deploy writes its own right before migrate. Dropping it now means a
        # deploy that dies before its backup can never make rollback.sh
        # restore an older deploy's data.
        rm -f "$PREV_BACKUP_FILE"
        log "Commit anterior guardado en .deploy_prev: ${prev_sha:0:12}"
    else
        log "Ya en ${new_sha:0:12} (re-deploy); .deploy_prev se conserva"
    fi

    log "Checking out ${new_sha:0:12}"
    git reset --hard --quiet "$new_sha"

    exec bash "$APP_DIR/deploy/scripts/deploy.sh" --after-checkout
}

# 5 attempts per URL, 3 s apart. Goes to the local nginx (--resolve) so it
# exercises nginx + TLS + gunicorn on this box without depending on DNS or on
# the public IP routing back to itself.
smoke() {
    local base="${SMOKE_BASE:-https://dominiopr.com}"
    local resolve="${SMOKE_RESOLVE:-dominiopr.com:443:127.0.0.1}"
    local curl_opts=(-sS -o /dev/null -w '%{http_code}' --max-time 15)
    [ -n "$resolve" ] && curl_opts+=(--resolve "$resolve")

    check_url() {
        local path="$1" code i
        for i in 1 2 3 4 5; do
            code="$(curl "${curl_opts[@]}" "$base$path" 2>/dev/null || true)"
            if [ "$code" = "200" ]; then
                echo "    $path -> 200"
                return 0
            fi
            # /healthz/ arrives with the backend change; until it exists the
            # home page is the fallback. Any other code (503 = DB/cache down)
            # is a real failure.
            if [ "$code" = "404" ] && [ "$path" = "/healthz/" ]; then
                echo "    /healthz/ -> 404 (aun no existe); uso / como respaldo"
                check_url /
                return $?
            fi
            echo "    $path -> ${code:-sin respuesta} (intento $i/5)"
            sleep 3
        done
        return 1
    }

    local ok=0
    check_url /healthz/ || ok=1
    check_url '/widget.js?key=dominio' || ok=1
    return $ok
}

after_checkout() {
    cd "$APP_DIR"
    local sha
    sha="$(git rev-parse HEAD)"
    log "Desplegando ${sha:0:12} ($(git log -1 --format=%s | cut -c1-72))"

    # shellcheck disable=SC1091
    source "$VENV/bin/activate"
    log "Python $(python --version 2>&1 | cut -d' ' -f2)"

    log "Installing/updating dependencies"
    pip install --quiet --upgrade pip
    pip install --quiet -r requirements.txt

    # Pre-migrate backup: a migration that goes wrong (or a rollback of a
    # schema change) needs the data exactly as it was before this deploy.
    # The name carries date, time and commit, so two deploys on the same day
    # never overwrite each other; only the last $PREDEPLOY_KEEP are kept.
    log "Backup de la BD antes de migrate -> $PREDEPLOY_DIR"
    local stamp
    stamp="$(date +%Y%m%d-%H%M%S)-${sha:0:7}"
    BACKUP_DIR="$PREDEPLOY_DIR" STAMP="$stamp" KEEP_DAYS=30 \
        BACKUP_S3_URI="" bash "$APP_DIR/deploy/backup_db.sh"
    # The rollback point is the backup taken BEFORE this commit's migrations
    # ran the first time. A re-deploy of the same commit keeps it (a new
    # backup would already contain the migrated schema).
    local pointed=""
    [ -f "$PREV_BACKUP_FILE" ] && pointed="$(cat "$PREV_BACKUP_FILE")"
    if [ -n "$pointed" ] && [ -f "$pointed" ] && [[ "$pointed" == *"-${sha:0:7}.sqlite3.gz" ]]; then
        log "Punto de rollback de este commit ya existe: $pointed (se conserva)"
    else
        pointed="$PREDEPLOY_DIR/db-$stamp.sqlite3.gz"
        echo "$pointed" > "$PREV_BACKUP_FILE"
    fi
    # Keep the newest N predeploy backups (names sort by date), never the
    # current rollback point.
    # (find instead of ls: no error when the glob is empty; `|| true` because
    # grep exits 1 when nothing is left to delete, and pipefail is on.)
    find "$PREDEPLOY_DIR" -maxdepth 1 -name 'db-*.sqlite3.gz' | sort \
        | head -n -"$PREDEPLOY_KEEP" | { grep -vxF "$pointed" || true; } | xargs -r rm -f

    log "Running migrations (no-op if none)"
    python manage.py migrate --noinput

    log "Ensuring cache table exists (idempotent; rate limiting needs it)"
    python manage.py createcachetable

    log "Collecting static files"
    python manage.py collectstatic --noinput

    log "Restarting Gunicorn"
    sudo systemctl restart "$SERVICE"

    log "Smoke test"
    if ! smoke; then
        echo "" >&2
        echo "ERROR: el sitio no responde bien tras el deploy de ${sha:0:12}." >&2
        echo "  Logs:     sudo journalctl -u $SERVICE -n 80 --no-pager" >&2
        echo "  Rollback: bash $APP_DIR/deploy/scripts/rollback.sh" >&2
        exit 1
    fi

    log "Done: ${sha:0:12} en produccion. $(date)"
}

main() {
    case "${1:-}" in
        --after-checkout) after_checkout ;;
        --smoke)          cd "$APP_DIR"; smoke ;;
        -h|--help)        sed -n '2,20p' "$0" ;;
        *)                checkout "${1:-origin/main}" ;;
    esac
}

main "$@"
