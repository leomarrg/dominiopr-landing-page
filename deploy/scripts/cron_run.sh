#!/usr/bin/env bash
# /var/www/dominio/deploy/scripts/cron_run.sh NAME [--timeout SECONDS] -- command [args...]
#
# Wrapper for every cron line. It:
#   - runs the command from the app directory, optionally under `timeout`
#   - appends its output to the cron log, each line prefixed with the time and
#     NAME (a run that succeeds silently writes nothing, so the every-minute
#     jobs do not flood the log)
#   - if the command exits non-zero (or is killed by the timeout), emails
#     ADMINS through Django's mail_admins: subject with NAME and exit code,
#     body with the last 40 lines of output. At most ONE email per NAME per
#     hour (marker file in /tmp), so a job that fails every minute does not
#     bury the inbox.
#   - exits with the command's own exit code.
#
# Example (crontab of the app user):
#   */5 * * * * /var/www/dominio/deploy/scripts/cron_run.sh publish_due -- venv/bin/python manage.py publish_due
#   * * * * * /var/www/dominio/deploy/scripts/cron_run.sh process_knowledge --timeout 3600 -- venv/bin/python manage.py process_knowledge --limit 1
#
# Environment (optional):
#   APP_DIR          default /var/www/dominio
#   CRON_LOG         default $HOME/dominio-cron.log
#   CRON_ALERT_EVERY minutes between two alerts for the same NAME (default 60)
#
# The commands must not print secrets: their last lines travel by email.

set -uo pipefail   # no -e: a failing command is what we handle, not a reason to stop

APP_DIR="${APP_DIR:-/var/www/dominio}"
CRON_LOG="${CRON_LOG:-$HOME/dominio-cron.log}"
ALERT_EVERY="${CRON_ALERT_EVERY:-60}"
PYTHON="${PYTHON:-$APP_DIR/venv/bin/python}"

usage() {
    echo "uso: cron_run.sh NOMBRE [--timeout SEGUNDOS] -- comando [args...]" >&2
    exit 2
}

[ $# -ge 1 ] || usage
NAME="$1"; shift
# The name ends up in a file path and an email subject: keep it boring.
[[ "$NAME" =~ ^[A-Za-z0-9_.-]{1,64}$ ]] || { echo "cron_run: nombre invalido: $NAME" >&2; exit 2; }

TIMEOUT=""
while [ $# -gt 0 ]; do
    case "$1" in
        --timeout) [ $# -ge 2 ] || usage; TIMEOUT="$2"; shift 2 ;;
        --timeout=*) TIMEOUT="${1#--timeout=}"; shift ;;
        --) shift; break ;;
        *) usage ;;
    esac
done
[ $# -ge 1 ] || usage
if [ -n "$TIMEOUT" ] && ! [[ "$TIMEOUT" =~ ^[0-9]+$ ]]; then
    echo "cron_run: --timeout debe ser un numero de segundos" >&2; exit 2
fi

cd "$APP_DIR" || { echo "cron_run: no existe $APP_DIR" >&2; exit 1; }

OUT="$(mktemp "/tmp/dominio-cron-$NAME.XXXXXX")"
trap 'rm -f "$OUT"' EXIT

# Prefix every line with time and name while it is produced (long jobs show
# up in the log as they run), and keep a raw copy for the alert body.
prefix() {
    local line
    while IFS= read -r line || [ -n "$line" ]; do
        printf '%(%Y-%m-%d %H:%M:%S)T [%s] %s\n' -1 "$NAME" "$line"
    done
}

if [ -n "$TIMEOUT" ]; then
    # --kill-after: a job that ignores SIGTERM still dies a minute later.
    timeout --kill-after=60 "$TIMEOUT" "$@" 2>&1 | tee "$OUT" | prefix >> "$CRON_LOG"
else
    "$@" 2>&1 | tee "$OUT" | prefix >> "$CRON_LOG"
fi
RC=${PIPESTATUS[0]}

[ "$RC" -eq 0 ] && exit 0

if [ "$RC" -eq 124 ] || [ "$RC" -eq 137 ]; then
    REASON="cortado por timeout (${TIMEOUT}s)"
else
    REASON="salio con codigo $RC"
fi
printf '%(%Y-%m-%d %H:%M:%S)T [%s] FALLO: %s\n' -1 "$NAME" "$REASON" >> "$CRON_LOG"

# ---- alert, at most one per NAME per ALERT_EVERY minutes ----
MARK="/tmp/dominio-cron-alert-$NAME"
if [ -f "$MARK" ] && [ -n "$(find "$MARK" -mmin -"$ALERT_EVERY" 2>/dev/null)" ]; then
    printf '%(%Y-%m-%d %H:%M:%S)T [%s] alerta por correo omitida (ya se envio una hace menos de %s min)\n' \
        -1 "$NAME" "$ALERT_EVERY" >> "$CRON_LOG"
    exit "$RC"
fi
touch "$MARK"

# Subject and body travel in environment variables, never interpolated into
# the Python source, so nothing in the output can change the code that runs.
CRON_ALERT_SUBJECT="Cron $NAME: $REASON en $(hostname)" \
CRON_ALERT_BODY="$(printf 'Cron: %s\nComando: %s\nResultado: %s\nHora: %s\nLog completo: %s\n\nUltimas 40 lineas:\n%s\n' \
    "$NAME" "$*" "$REASON" "$(date '+%Y-%m-%d %H:%M:%S %Z')" "$CRON_LOG" "$(tail -n 40 "$OUT")")" \
"$PYTHON" manage.py shell -v 0 -c "
import os
from django.core.mail import mail_admins
mail_admins(os.environ['CRON_ALERT_SUBJECT'], os.environ['CRON_ALERT_BODY'], fail_silently=False)
" >> "$CRON_LOG" 2>&1
MAIL_RC=$?
if [ "$MAIL_RC" -ne 0 ]; then
    # Let the next failure try again instead of staying silent for an hour.
    rm -f "$MARK"
    printf '%(%Y-%m-%d %H:%M:%S)T [%s] no se pudo enviar la alerta por correo (codigo %s)\n' \
        -1 "$NAME" "$MAIL_RC" >> "$CRON_LOG"
fi

exit "$RC"
