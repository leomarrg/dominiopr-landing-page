#!/usr/bin/env bash
# /var/www/dominio/deploy/scripts/rollback.sh [--yes] [--no-db] [SHA]
#
# Runs ON THE SERVER, by hand (never from CI):
#   ssh ubuntu@dominiopr.com 'bash /var/www/dominio/deploy/scripts/rollback.sh'
#
# Goes back to the commit recorded in .deploy_prev by deploy.sh (or to SHA).
#   - If the commit being undone added migrations that are APPLIED in the
#     database, the old code cannot run on the new schema: the database is
#     restored from the backup deploy.sh took right before those migrations
#     (.deploy_prev_backup). Everything written since that deploy (leads,
#     chats, bookings) is lost from the live DB; a safety copy of the current
#     DB is taken first under /var/backups/dominio/prerollback/. This asks for
#     confirmation unless --yes.
#   - --no-db skips the restore (code only). Use it when the new migrations
#     are additive and the old code tolerates them, or after reversing them by
#     hand with the CURRENT code: venv/bin/python manage.py migrate landing 00NN
#   - Without new migrations it is code only: checkout, pip, collectstatic,
#     restart, smoke test.
#
# Idempotent: when HEAD is already the target it only re-runs the smoke test.
# It does not move .deploy_prev, so a second run can never "roll back" to the
# bad commit. The next deploy.sh records the rollback point again.
#
# The whole body lives in main(), called on the last line: bash parses it in
# full before `git reset` rewrites this file.

set -euo pipefail

SERVICE_STOPPED=0

APP_DIR="${APP_DIR:-/var/www/dominio}"
VENV="$APP_DIR/venv"
SERVICE="gunicorn-dominio"
PREV_FILE="$APP_DIR/.deploy_prev"
PREV_BACKUP_FILE="$APP_DIR/.deploy_prev_backup"
DB_PATH="${DJANGO_DB_PATH:-$APP_DIR/db.sqlite3}"
PREROLLBACK_DIR="${PREROLLBACK_DIR:-/var/backups/dominio/prerollback}"

log()  { echo "==> $*"; }
fail() { echo "ERROR: $*" >&2; exit 1; }

usage() { sed -n '2,25p' "$0"; }

# Same check as deploy.sh, kept here on purpose: after `git reset` to an older
# commit, that commit's deploy.sh may not have a --smoke mode at all.
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
            if [ "$code" = "404" ] && [ "$path" = "/healthz/" ]; then
                echo "    /healthz/ -> 404 (no existe en este commit); uso / como respaldo"
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

# Prints "app name" for every migration file added between $1 and $2 that is
# recorded as applied in the database (asked through Django, so it reads the
# same DB the app uses).
applied_new_migrations() {
    local from="$1" to="$2" files
    files="$(git diff --name-only --diff-filter=A "$from" "$to" -- '*/migrations/[0-9]*.py')"
    [ -z "$files" ] && return 0
    MIGRATION_FILES="$files" python manage.py shell -v 0 -c "
import os
from django.db.migrations.recorder import MigrationRecorder
done = set(MigrationRecorder.Migration.objects.values_list('app', 'name'))
for path in os.environ['MIGRATION_FILES'].split():
    parts = path.split('/')
    app, name = parts[-3], parts[-1][:-3]
    if (app, name) in done:
        print(app, name)
"
}

restore_db() {
    local backup="$1"
    local tmp="$DB_PATH.restore-tmp"
    log "Copia de seguridad de la BD actual -> $PREROLLBACK_DIR"
    BACKUP_DIR="$PREROLLBACK_DIR" STAMP="$(date +%Y%m%d-%H%M%S)-pre-rollback" KEEP_DAYS=30 \
        BACKUP_S3_URI="" bash "$APP_DIR/deploy/backup_db.sh"

    log "Deteniendo $SERVICE para restaurar"
    sudo systemctl stop "$SERVICE"
    # From here until the restart in main(), any failure (restore, checkout,
    # pip...) must not leave the site down: on_exit starts it again.
    SERVICE_STOPPED=1

    log "Restaurando $backup"
    rm -f "$tmp"
    gunzip -c "$backup" > "$tmp"
    chmod 600 "$tmp"
    # sqlite3 backup API INTO the live file: it takes the write lock, works
    # with WAL and never leaves a half-copied file, unlike cp/mv over a DB
    # that a cron job may have open.
    python - "$tmp" "$DB_PATH" <<'PYRESTORE'
import sqlite3
import sys

src_path, dst_path = sys.argv[1], sys.argv[2]
src = sqlite3.connect(src_path)
status = src.execute('PRAGMA integrity_check').fetchone()[0]
if status != 'ok':
    raise SystemExit(f'la copia no paso integrity_check: {status}')
dst = sqlite3.connect(dst_path, timeout=60)
src.backup(dst)
dst.execute('PRAGMA wal_checkpoint(TRUNCATE)')
status = dst.execute('PRAGMA integrity_check').fetchone()[0]
dst.close()
src.close()
if status != 'ok':
    raise SystemExit(f'la BD restaurada no paso integrity_check: {status}')
print('    BD restaurada: integridad ok')
PYRESTORE
    rm -f "$tmp"
}

on_exit() {
    if [ "$SERVICE_STOPPED" = 1 ]; then
        echo "Rollback interrumpido con $SERVICE detenido; arrancandolo de nuevo" >&2
        sudo systemctl start "$SERVICE" || true
    fi
}

main() {
    trap on_exit EXIT
    local assume_yes=0 skip_db=0 target=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --yes|-y)  assume_yes=1 ;;
            --no-db)   skip_db=1 ;;
            -h|--help) usage; exit 0 ;;
            -*)        fail "opcion desconocida: $1 (usa --help)" ;;
            *)         target="$1" ;;
        esac
        shift
    done

    cd "$APP_DIR"
    if [ -z "$target" ]; then
        [ -s "$PREV_FILE" ] || fail "no existe $PREV_FILE (aun no hubo un deploy con deploy.sh nuevo). Pasa el SHA: rollback.sh <sha>"
        target="$(cat "$PREV_FILE")"
    fi
    target="$(git rev-parse --verify "${target}^{commit}" 2>/dev/null)" || fail "commit desconocido: $target"

    local cur
    cur="$(git rev-parse HEAD)"

    # shellcheck disable=SC1091
    source "$VENV/bin/activate"

    if [ "$cur" = "$target" ]; then
        log "Ya estas en ${target:0:12}: nada que revertir. Verificando el sitio."
        smoke || fail "el sitio no responde; revisa: sudo journalctl -u $SERVICE -n 80 --no-pager"
        log "Sitio OK"
        exit 0
    fi

    log "Rollback: ${cur:0:12} -> ${target:0:12}"
    git log --oneline -n 20 "$target..$cur" | sed 's/^/    deshace: /'

    local applied need_restore=0 backup=""
    applied="$(applied_new_migrations "$target" "$cur")"
    if [ -n "$applied" ]; then
        echo "    migraciones nuevas ya aplicadas en la BD:"
        echo "$applied" | sed 's/^/      /'
        need_restore=1
    else
        echo "    sin migraciones nuevas aplicadas: rollback solo de codigo"
    fi

    if [ "$need_restore" = 1 ] && [ "$skip_db" = 1 ]; then
        echo "    --no-db: NO se restaura la BD; el codigo viejo correra con el esquema nuevo."
        need_restore=0
    fi

    if [ "$need_restore" = 1 ]; then
        [ -s "$PREV_BACKUP_FILE" ] || fail "no hay backup predeploy registrado ($PREV_BACKUP_FILE).
  Opciones: revertir las migraciones con el codigo ACTUAL
    (venv/bin/python manage.py migrate <app> <migracion_anterior>) y luego rollback.sh --no-db,
  o restaurar a mano un backup de /var/backups/dominio/."
        backup="$(cat "$PREV_BACKUP_FILE")"
        [ -f "$backup" ] || fail "el backup registrado no existe: $backup"
        [[ "$backup" == *"-${cur:0:7}.sqlite3.gz" ]] || fail "el backup registrado ($backup) no es el del deploy de ${cur:0:7}; restauralo a mano si es el correcto"
        gzip -t "$backup" || fail "el backup $backup esta corrupto"
        echo ""
        echo "    Se restaurara la BD desde: $backup"
        echo "    (tomado $(date -r "$backup" '+%Y-%m-%d %H:%M:%S'), antes del deploy de ${cur:0:7})."
        echo "    Se PIERDE todo lo escrito despues (leads, chats, citas, pagos registrados)."
        echo "    Antes se guarda una copia de la BD actual en $PREROLLBACK_DIR."
        if [ "$assume_yes" != 1 ]; then
            local answer=""
            read -r -p "    Escribe RESTAURAR para continuar: " answer || true
            [ "$answer" = "RESTAURAR" ] || fail "cancelado; no se cambio nada"
        fi
        restore_db "$backup"
    fi

    log "Checkout ${target:0:12}"
    git reset --hard --quiet "$target"

    log "Dependencias"
    pip install --quiet -r requirements.txt

    log "Cache table y estaticos"
    python manage.py createcachetable
    python manage.py collectstatic --noinput

    log "Reiniciando $SERVICE"
    sudo systemctl restart "$SERVICE"
    SERVICE_STOPPED=0

    echo "$cur" > "$APP_DIR/.deploy_rolled_back_from"

    log "Smoke test"
    if ! smoke; then
        fail "el sitio no responde tras el rollback a ${target:0:12}.
  Logs: sudo journalctl -u $SERVICE -n 80 --no-pager"
    fi
    log "Rollback completo: ${target:0:12} en produccion (deshecho: ${cur:0:12}). $(date)"
    echo "    Ojo: el proximo push a main vuelve a desplegar. Revierte el commit malo (git revert) antes."
}

main "$@"
