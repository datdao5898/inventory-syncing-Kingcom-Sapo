#!/usr/bin/env bash
# Cai dat / cai lai web dong bo ton kho tren VPS (Ubuntu/Debian).
# Clone repo vao thu muc tuy chon (vd /srv/inventory-syncing-Kingcom-Sapo) roi chay bang root:
#   sudo bash <thu-muc-repo>/deploy/install.sh
# App chay ngay trong thu muc repo do. Chay lai nhieu lan van an toan:
# khong ghi de .env, reports/, settings.json.
set -euo pipefail

APP_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
APP_USER=synctonkho
SERVICE=sync-tonkho

[ "$(id -u)" -eq 0 ] || { echo "Can chay bang root (sudo)."; exit 1; }
[ -f "$APP_DIR/sync.py" ] && [ -f "$APP_DIR/web.py" ] || { echo "Khong thay sync.py/web.py trong $APP_DIR"; exit 1; }
echo "==> Thu muc ung dung: $APP_DIR"

echo "==> Cai goi can thiet"
apt-get update -qq
apt-get install -y -qq git python3 ca-certificates >/dev/null
python3 -c 'import sys; assert sys.version_info >= (3, 8), "Can Python 3.8+"'

echo "==> Tao user he thong $APP_USER"
id "$APP_USER" >/dev/null 2>&1 || useradd --system --home-dir "$APP_DIR" --no-create-home --shell /usr/sbin/nologin "$APP_USER"

mkdir -p "$APP_DIR/reports" "$APP_DIR/logs"
if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  echo "==> Da tao $APP_DIR/.env tu .env.example"
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 600 "$APP_DIR/.env"

echo "==> Cai dich vu systemd $SERVICE"
sed "s#/opt/sapo_haravan_sync#$APP_DIR#g" "$APP_DIR/deploy/sync-tonkho.service" > /etc/systemd/system/$SERVICE.service
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null

# Chi khoi dong khi .env da dien du thong tin bat buoc
missing=$(for k in SAPO_STORE HARAVAN_TOKEN LOCATION_MAP WEB_USER WEB_PASSWORD; do
  grep -Eq "^$k=.+" "$APP_DIR/.env" || echo "$k"; done)
if ! grep -Eq '^(SAPO_ACCESS_TOKEN=.+|SAPO_API_SECRET=.+)' "$APP_DIR/.env"; then missing="$missing SAPO_API_KEY/SAPO_API_SECRET"; fi
if [ -n "$(echo $missing)" ]; then
  echo
  echo "!! .env con thieu: $(echo $missing)"
  echo "   Sua file: nano $APP_DIR/.env   roi chay lai: sudo bash $APP_DIR/deploy/install.sh"
  exit 2
fi

# Cong web khong duoc trung voi chuong trinh khac (vd nginx dang dung 8080)
PORT=$(grep -E '^WEB_PORT=' "$APP_DIR/.env" | cut -d= -f2); PORT=${PORT:-8080}
if ! systemctl is-active --quiet "$SERVICE" && ss -ltnH "sport = :$PORT" | grep -q .; then
  echo "!! Cong $PORT dang bi chuong trinh khac dung:"
  ss -ltnp "sport = :$PORT" || true
  echo "   Doi WEB_PORT trong $APP_DIR/.env (vd 8081) roi chay lai."
  exit 3
fi

systemctl restart "$SERVICE"
sleep 2
systemctl --no-pager --lines=5 status "$SERVICE" || true
echo
echo "==> Xong. Web: http://<IP-VPS>:$PORT  (dang nhap bang WEB_USER/WEB_PASSWORD trong .env)"
