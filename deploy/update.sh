#!/usr/bin/env bash
# Cap nhat code moi nhat tu GitHub va khoi dong lai web. Chay bang root:
#   sudo bash <thu-muc-repo>/deploy/update.sh
# Giu nguyen .env, reports/ (lich su, bao cao), settings.json.
set -euo pipefail
APP_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
APP_USER=synctonkho
SERVICE=sync-tonkho
[ "$(id -u)" -eq 0 ] || { echo "Can chay bang root (sudo)."; exit 1; }

# chay git bang user so huu thu muc (tranh loi "dubious ownership")
g() { runuser -u "$APP_USER" -- git -C "$APP_DIR" "$@"; }
before=$(g rev-parse --short HEAD)
g pull --ff-only
after=$(g rev-parse --short HEAD)
sed "s#/opt/sapo_haravan_sync#$APP_DIR#g" "$APP_DIR/deploy/sync-tonkho.service" > /etc/systemd/system/$SERVICE.service
systemctl daemon-reload
systemctl restart "$SERVICE"
sleep 2
systemctl is-active --quiet "$SERVICE" && echo "OK: $before -> $after, web dang chay" \
  || { echo "LOI: web khong chay. Xem: journalctl -u $SERVICE -n 50"; exit 1; }
