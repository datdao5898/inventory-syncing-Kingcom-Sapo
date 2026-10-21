#!/usr/bin/env bash
# Cap nhat code moi nhat tu GitHub va khoi dong lai web. Chay bang root:
#   sudo bash /opt/sapo_haravan_sync/deploy/update.sh
# Giu nguyen .env, reports/ (lich su, bao cao), settings.json.
set -euo pipefail
APP_DIR=/opt/sapo_haravan_sync
APP_USER=synctonkho
[ "$(id -u)" -eq 0 ] || { echo "Can chay bang root (sudo)."; exit 1; }

# chay git bang user so huu thu muc (tranh loi "dubious ownership")
g() { runuser -u "$APP_USER" -- git -C "$APP_DIR" "$@"; }
before=$(g rev-parse --short HEAD)
g pull --ff-only
after=$(g rev-parse --short HEAD)
cp "$APP_DIR/deploy/sync-tonkho.service" /etc/systemd/system/sync-tonkho.service
systemctl daemon-reload
systemctl restart sync-tonkho
sleep 2
systemctl is-active --quiet sync-tonkho && echo "OK: $before -> $after, web dang chay" \
  || { echo "LOI: web khong chay. Xem: journalctl -u sync-tonkho -n 50"; exit 1; }
