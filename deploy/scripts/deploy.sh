#!/usr/bin/env bash
# /var/www/dominio/deploy/scripts/deploy.sh
#
# Runs ON THE SERVER. Called by GitHub Actions over SSH.
# Pulls latest code, installs deps, collects static, restarts Gunicorn.

set -euo pipefail

APP_DIR="/var/www/dominio"
VENV="$APP_DIR/venv"
SERVICE="gunicorn-dominio"

cd "$APP_DIR"

echo "==> Pulling latest from main"
git fetch --quiet origin main
git reset --hard origin/main

echo "==> Activating venv"
# shellcheck disable=SC1091
source "$VENV/bin/activate"

echo "==> Installing/updating dependencies"
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

echo "==> Running migrations (no-op if none)"
python manage.py migrate --noinput

echo "==> Collecting static files"
python manage.py collectstatic --noinput

echo "==> Restarting Gunicorn"
sudo systemctl restart "$SERVICE"

echo "==> Done. $(date)"
