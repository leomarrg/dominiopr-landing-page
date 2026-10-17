#!/usr/bin/env bash
# Apply the infrastructure files in this repo to the server they run on.
#
#   ssh ubuntu@<server> 'bash /var/www/dominio/deploy/scripts/apply_infra.sh'
#
# deploy.sh (what GitHub Actions runs) deliberately touches only the app:
# code, deps, migrations, static, gunicorn restart. nginx and the systemd unit
# are system files that need sudo and can take the site down if wrong, so
# they are applied here, on purpose, with a validation gate before each
# reload. Idempotent: safe to re-run.
set -euo pipefail
APP=/var/www/dominio
cd "$APP"

echo "==> nginx: rate-limit zones + hardened server block"
sudo install -m 644 deploy/nginx/dominio-limits.conf /etc/nginx/conf.d/dominio-limits.conf
sudo install -m 644 deploy/nginx/dominio.conf        /etc/nginx/sites-available/dominio.conf
# nginx -t validates the WHOLE config before anything is reloaded. If this
# fails, the running nginx keeps serving the old config untouched.
sudo nginx -t
sudo systemctl reload nginx
echo "    nginx reloaded"

echo "==> gunicorn: threaded workers"
sudo install -m 644 deploy/systemd/gunicorn-dominio.service /etc/systemd/system/gunicorn-dominio.service
sudo systemctl daemon-reload
sudo systemctl restart gunicorn-dominio
sleep 2
sudo systemctl is-active --quiet gunicorn-dominio && echo "    gunicorn active" || {
  echo "    gunicorn FAILED to start:"; sudo journalctl -u gunicorn-dominio -n 30 --no-pager; exit 1; }

echo "==> logrotate: cron log written by deploy/scripts/cron_run.sh"
sudo install -m 644 -o root -g root deploy/logrotate/dominio-cron /etc/logrotate.d/dominio-cron
# -d = debug/dry run: parses the file and shows what it would do, rotates nothing.
if sudo logrotate -d /etc/logrotate.d/dominio-cron >/dev/null 2>&1; then
  echo "    logrotate config OK"
else
  echo "    logrotate config INVALID:"; sudo logrotate -d /etc/logrotate.d/dominio-cron; exit 1
fi

echo "==> backup dirs for deploy.sh / rollback.sh"
sudo install -d -m 700 -o ubuntu -g ubuntu /var/backups/dominio /var/backups/dominio/predeploy /var/backups/dominio/prerollback

echo "==> swap (the box has 512MB and no swap; it has already been OOM-killed once)"
if swapon --show | grep -q '/swapfile'; then
  echo "    swap already present"
else
  sudo fallocate -l 1G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile >/dev/null
  sudo swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
  # Swap is a safety net, not working memory: only page out under real pressure.
  echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/90-swappiness.conf >/dev/null
  sudo sysctl -q vm.swappiness=10
  echo "    1G swapfile created"
fi

echo "==> state"
free -m | sed 's/^/    /'
printf "    site: "; curl -s -o /dev/null -w "HTTP %{http_code}\n" --max-time 15 https://dominiopr.com/
printf "    rate limit test (30 fast hits on /api/demo/, expect some 429): "
codes=$(for i in $(seq 1 30); do curl -s -o /dev/null -w "%{http_code} " --max-time 5 -X POST -H 'Content-Type: application/json' -d '{}' https://dominiopr.com/api/demo/; done)
echo "$codes" | tr ' ' '\n' | sort | uniq -c | tr '\n' ' '; echo
