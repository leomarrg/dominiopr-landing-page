#!/usr/bin/env bash
# Run stripe_bootstrap (or preflight) against the LIVE Stripe account.
#
# Why a wrapper instead of just editing .env: the live key must never sit in the
# main .env. That file is what `runserver` and every local manage.py command
# read, so a stray test checkout on the laptop would charge a real card. Here
# the key lives in .env.live (gitignored) and only decorates this one process.
#
#   bash tools/stripe_live.sh dry      # show what would be created, write nothing
#   bash tools/stripe_live.sh apply    # create catalog + IVU 11.5% + coupon
#   bash tools/stripe_live.sh preflight
set -euo pipefail
cd "$(dirname "$0")/.."

KEY=$(grep '^STRIPE_SECRET_KEY_LIVE=' .env.live 2>/dev/null | cut -d= -f2- | tr -d '\r' || true)
if [ -z "$KEY" ]; then
  echo "Falta STRIPE_SECRET_KEY_LIVE en .env.live" >&2
  exit 1
fi
case "$KEY" in
  sk_live_*) ;;
  # A test key here would quietly build the catalog in the wrong account and
  # everything downstream would look fine until nobody could pay.
  *) echo "La llave en .env.live no empieza con sk_live_ — abortando." >&2; exit 1 ;;
esac

PY=venv/Scripts/python.exe
[ -x "$PY" ] || PY=python

# Export every STRIPE_PRICE_*/STRIPE_TAX_RATE_ID stored in .env.live too.
# preflight compares Stripe against the site's advertised prices, so pointing a
# live key at the sandbox price ids in .env would fail all nine for the wrong
# reason. Real env vars win over .env (load_dotenv does not override).
while IFS='=' read -r name value; do
  case "$name" in
    STRIPE_PRICE_*|STRIPE_TAX_RATE_ID|STRIPE_WEBHOOK_SECRET) export "$name=${value%$'\r'}" ;;
  esac
done < .env.live

case "${1:-}" in
  dry)
    STRIPE_SECRET_KEY="$KEY" "$PY" manage.py stripe_bootstrap --dry-run \
      --tax-rate 11.5 --tax-name IVU --coupon LANZAMIENTO
    ;;
  apply)
    # No --webhook-url on purpose: creating the endpoint is the only moment the
    # signing secret is ever shown, and it should be read off the dashboard by a
    # human, not scrolled through a terminal. The webhook is step 1 of
    # docs/lanzamiento/08-prompt-stripe-live.md.
    STRIPE_SECRET_KEY="$KEY" "$PY" manage.py stripe_bootstrap --yes \
      --tax-rate 11.5 --tax-name IVU --coupon LANZAMIENTO
    ;;
  preflight)
    STRIPE_SECRET_KEY="$KEY" "$PY" manage.py preflight
    ;;
  *)
    echo "uso: bash tools/stripe_live.sh {dry|apply|preflight}" >&2
    exit 2
    ;;
esac
