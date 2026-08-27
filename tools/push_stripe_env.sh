#!/usr/bin/env bash
# Copy the production Stripe block from .env.live into the server's .env.
#
#   bash tools/push_stripe_env.sh          # muestra que haria, no toca nada
#   bash tools/push_stripe_env.sh --apply  # lo hace y reinicia gunicorn
#
# Why a script: the block is 12 lines of near-identical ids (the sandbox and
# live `price_`/`txr_` differ only deep in the string), and hand-copying one
# line from the wrong world breaks checkout silently — the site keeps working,
# it just stops charging. This copies the file, so there is nothing to mistype.
#
# The secret never passes through a terminal you paste into: it goes from your
# .env.live straight into the server's .env over ssh.
set -euo pipefail
cd "$(dirname "$0")/.."

HOST=${DOMINIO_HOST:-ubuntu@52.54.98.28}
KEYFILE=${DOMINIO_SSH_KEY:-$HOME/.ssh/lightsail-dominio.pem}
REMOTE=/var/www/dominio
APPLY=${1:-}

[ -f .env.live ] || { echo "No encuentro .env.live" >&2; exit 1; }

# Rename the local-only _LIVE suffix and keep only filled-in STRIPE_ lines.
BLOCK=$(sed 's/^STRIPE_SECRET_KEY_LIVE=/STRIPE_SECRET_KEY=/' .env.live \
        | grep -E '^STRIPE_[A-Z_]+=.+' || true)

need() { echo "$BLOCK" | grep -q "^$1=" || { echo "Falta $1 en .env.live" >&2; exit 1; }; }
need STRIPE_SECRET_KEY
need STRIPE_WEBHOOK_SECRET
need STRIPE_TAX_RATE_ID
echo "$BLOCK" | grep -q '^STRIPE_SECRET_KEY=sk_live_' || {
  echo "La llave de .env.live no es sk_live_ — abortando." >&2; exit 1; }
PRICES=$(echo "$BLOCK" | grep -c '^STRIPE_PRICE_')
[ "$PRICES" -eq 9 ] || { echo "Esperaba 9 price ids, encontre $PRICES" >&2; exit 1; }

echo "Se enviaran estas variables a $HOST:$REMOTE/.env (valores ocultos):"
echo "$BLOCK" | sed 's/=.*/= <valor>/' | sed 's/^/   /'
echo
if [ "$APPLY" != "--apply" ]; then
  echo "Esto fue un ensayo. Para hacerlo de verdad:"
  echo "   bash tools/push_stripe_env.sh --apply"
  exit 0
fi

# Replace any existing STRIPE_ lines rather than appending: duplicated keys in a
# .env are decided by parse order, which is not something to leave to luck.
printf '%s\n' "$BLOCK" | ssh -i "$KEYFILE" "$HOST" "
  set -euo pipefail
  cd $REMOTE
  cp .env .env.bak.\$(date +%Y%m%d-%H%M%S)
  cat > /tmp/stripe.block
  grep -v '^STRIPE_' .env > .env.new || true
  cat /tmp/stripe.block >> .env.new
  mv .env.new .env
  chmod 600 .env
  shred -u /tmp/stripe.block 2>/dev/null || rm -f /tmp/stripe.block
  echo '--> .env actualizado (respaldo .env.bak.*)'
  sudo systemctl restart gunicorn-dominio
  echo '--> gunicorn reiniciado'
  venv/bin/python manage.py preflight
"
