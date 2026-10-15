#!/usr/bin/env bash
# M-10: daily SQLite backup (the app runs on SQLite — see DATABASES in
# dominio_website/settings.py). Uses the sqlite3 online-backup API through
# Python, so the copy is consistent even while Gunicorn is writing.
#
# The script does not just make a copy — it opens the copy and runs an
# integrity check, because a truncated or corrupt backup looks exactly like a
# good one right up until the day you need it.
#
# Cron (as the app user, e.g. ubuntu):
#   15 4 * * * /var/www/dominio/deploy/backup_db.sh >> /home/ubuntu/dominio-cron.log 2>&1
#
# Restore (the drill, run monthly and write down the date):
#   sudo systemctl stop gunicorn-dominio
#   # La BD corre en modo WAL: un cp a secas deja fuera lo que aun esta en db.sqlite3-wal.
#   sqlite3 /var/www/dominio/db.sqlite3 'PRAGMA wal_checkpoint(TRUNCATE);'
#   cp /var/www/dominio/db.sqlite3 /home/ubuntu/db-antes-de-restaurar-$(date +%F-%H%M).sqlite3
#   gunzip -c /var/backups/dominio/db-YYYYMMDD.sqlite3.gz > /var/www/dominio/db.sqlite3
#   sudo systemctl start gunicorn-dominio
set -euo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
DB_PATH="${DJANGO_DB_PATH:-$APP_DIR/db.sqlite3}"
PYTHON="${PYTHON:-$APP_DIR/venv/bin/python}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/dominio}"
KEEP_DAYS="${KEEP_DAYS:-14}"
STAMP="$(date +%Y%m%d)"
OUT="$BACKUP_DIR/db-$STAMP.sqlite3"

[ -f "$DB_PATH" ] || { echo "Backup FAILED: no database at $DB_PATH" >&2; exit 1; }

# 700/600: these files hold every tenant's leads, chat transcripts and contact
# details. The default 755/644 would let any local account read them.
install -d -m 700 "$BACKUP_DIR"

"$PYTHON" - "$DB_PATH" "$OUT" <<'PYBACKUP'
import sqlite3
import sys

src, dst = sys.argv[1], sys.argv[2]
s = sqlite3.connect(src)
d = sqlite3.connect(dst)
with d:
    s.backup(d)
d.close()
s.close()
PYBACKUP

chmod 600 "$OUT"
gzip -f "$OUT"
chmod 600 "$OUT.gz"

# ---- Verify the backup is restorable, not merely present ----
if ! gzip -t "$OUT.gz"; then
    echo "Backup FAILED: $OUT.gz is not a valid gzip file" >&2
    exit 1
fi

VERIFY="$(mktemp)"
trap 'rm -f "$VERIFY"' EXIT
gunzip -c "$OUT.gz" > "$VERIFY"

if ! "$PYTHON" - "$VERIFY" <<'PYVERIFY'
import sqlite3
import sys

path = sys.argv[1]
con = sqlite3.connect(path)
status = con.execute('PRAGMA integrity_check').fetchone()[0]
if status != 'ok':
    print(f'  integrity_check said: {status}', file=sys.stderr)
    raise SystemExit(1)
# An empty tenant table means we backed up the wrong file, or an empty one.
clients = con.execute('SELECT COUNT(*) FROM landing_client').fetchone()[0]
leads = con.execute('SELECT COUNT(*) FROM landing_contactsubmission').fetchone()[0]
con.close()
print(f'  verificado: integridad ok, {clients} clientes y {leads} leads en la copia')
PYVERIFY
then
    echo "Backup FAILED: the copy did not open as a valid database" >&2
    exit 1
fi

find "$BACKUP_DIR" -name 'db-*.sqlite3.gz' -mtime +"$KEEP_DAYS" -delete

# ---- Off-server copy (optional, encrypted) ----
# Lightsail automatic snapshots also cover this, but a backup that only exists
# on the box it protects is not a backup. The copy leaves the server encrypted
# (AES256, symmetric) so the bucket never holds readable tenant PII.
#   BACKUP_S3_URI           e.g. s3://mi-bucket/dominio-db  (no trailing slash)
#   BACKUP_PASSPHRASE_FILE  file with the passphrase, chmod 600, owned by the
#                           cron user. Keep a copy OFF the server too (password
#                           manager): without it the off-site backups are useless.
#
# Restore from S3 (on any box with aws cli + gpg + python3):
#   aws s3 cp "s3://mi-bucket/dominio-db/db-YYYYMMDD.sqlite3.gz.gpg" .
#   gpg --batch --pinentry-mode loopback --passphrase-file /ruta/passphrase -o db-YYYYMMDD.sqlite3.gz -d db-YYYYMMDD.sqlite3.gz.gpg
#   gunzip db-YYYYMMDD.sqlite3.gz
#   python3 -c "import sqlite3; print(sqlite3.connect('db-YYYYMMDD.sqlite3').execute('PRAGMA integrity_check').fetchone()[0])"
#   # Must print "ok"; then follow the restore drill at the top of this file.
if [ -n "${BACKUP_S3_URI:-}" ] && [ -n "${BACKUP_PASSPHRASE_FILE:-}" ]; then
    ENC="$OUT.gz.gpg"
    # The local $OUT.gz is already verified and kept no matter what happens here.
    if ! gpg --batch --yes --pinentry-mode loopback --symmetric --cipher-algo AES256 \
            --passphrase-file "$BACKUP_PASSPHRASE_FILE" -o "$ENC" "$OUT.gz"; then
        rm -f "$ENC"
        echo "Backup local OK ($OUT.gz) pero la copia externa FALLÓ: no se pudo cifrar" >&2
        exit 1
    fi
    chmod 600 "$ENC"
    if ! aws s3 cp "$ENC" "$BACKUP_S3_URI/"; then
        rm -f "$ENC"
        echo "Backup local OK ($OUT.gz) pero la copia externa FALLÓ: aws s3 cp a $BACKUP_S3_URI" >&2
        exit 1
    fi
    rm -f "$ENC"
    echo "Copia externa: OK ($BACKUP_S3_URI/$(basename "$ENC"))"
elif [ -n "${BACKUP_S3_URI:-}" ]; then
    # Misconfiguration must be loud: never upload unencrypted, never skip silently.
    echo "Backup local OK ($OUT.gz) pero la copia externa FALLÓ: falta BACKUP_PASSPHRASE_FILE" >&2
    exit 1
else
    echo "Copia externa: omitida (BACKUP_S3_URI no configurado)"
fi

echo "Backup OK: $OUT.gz"
