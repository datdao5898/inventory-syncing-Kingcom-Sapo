#!/usr/bin/env bash
# Cap nhat code moi nhat tu GitHub va khoi dong lai web. Chay bang root:
#   sudo bash <thu-muc-repo>/deploy/update.sh
# Giu nguyen .env, reports/ (lich su, bao cao), settings.json.
set -euo pipefail
APP_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
APP_USER=synctonkho
SERVICE=sync-tonkho
[ "$(id -u)" -eq 0 ] || { echo "Can chay bang root (sudo)."; exit 1; }

# Tra lai quyen so huu (phong khi truoc do co nguoi chay git pull bang root),
# roi chay git bang user so huu thu muc (tranh loi "dubious ownership")
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
g() { runuser -u "$APP_USER" -- git -C "$APP_DIR" "$@"; }
if [ "${1:-}" != "--after-pull" ]; then
  before=$(g rev-parse --short HEAD)
  g pull --ff-only
  # Chay lai bang ban update.sh vua tai ve: bash dang doc ban cu trong bo nho,
  # neu khong cac buoc moi them (vd cai thu vien) se bi bo qua o lan cap nhat nay
  exec bash "$APP_DIR/deploy/update.sh" --after-pull "$before"
fi
before=${2:-?}
after=$(g rev-parse --short HEAD)
# Thu vien xuat Excel (trang Doanh thu san); chi cai neu chua co
if ! python3 -c 'import openpyxl' 2>/dev/null; then
  echo "==> Cai thu vien xuat Excel (python3-openpyxl)"
  apt-get update -qq && apt-get install -y -qq python3-openpyxl >/dev/null
  python3 -c 'import openpyxl' || { echo "LOI: khong cai duoc python3-openpyxl"; exit 1; }
fi
sed "s#/opt/sapo_haravan_sync#$APP_DIR#g" "$APP_DIR/deploy/sync-tonkho.service" > /etc/systemd/system/$SERVICE.service
systemctl daemon-reload
systemctl restart "$SERVICE"
sleep 2
systemctl is-active --quiet "$SERVICE" && echo "OK: $before -> $after, web dang chay" \
  || { echo "LOI: web khong chay. Xem: journalctl -u $SERVICE -n 50"; exit 1; }
