#!/usr/bin/env bash
# Cai dat / cai lai web dong bo ton kho tren VPS (Ubuntu/Debian). Chay bang root:
#   sudo bash deploy/install.sh
# Chay lai nhieu lan van an toan: khong ghi de .env, reports/, settings.json.
set -euo pipefail

APP_DIR=/opt/sapo_haravan_sync
APP_USER=synctonkho
REPO_URL=${REPO_URL:-https://github.com/datdao5898/inventory-syncing-Kingcom-Sapo.git}

[ "$(id -u)" -eq 0 ] || { echo "Can chay bang root (sudo)."; exit 1; }

echo "==> Cai goi can thiet"
apt-get update -qq
apt-get install -y -qq git python3 ca-certificates >/dev/null
python3 -c 'import sys; assert sys.version_info >= (3, 8), "Can Python 3.8+"'

echo "==> Tao user he thong $APP_USER"
id "$APP_USER" >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"

echo "==> Lay code tu $REPO_URL"
if [ -d "$APP_DIR/.git" ]; then
  # chay git bang user so huu thu muc (tranh loi "dubious ownership")
  chown -R "$APP_USER:$APP_USER" "$APP_DIR"
  runuser -u "$APP_USER" -- git -C "$APP_DIR" pull --ff-only
else
  git clone "$REPO_URL" "$APP_DIR"
fi
mkdir -p "$APP_DIR/reports" "$APP_DIR/logs"

if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  echo "==> Da tao $APP_DIR/.env tu .env.example"
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 600 "$APP_DIR/.env"

echo "==> Cai dich vu systemd"
cp "$APP_DIR/deploy/sync-tonkho.service" /etc/systemd/system/sync-tonkho.service
systemctl daemon-reload
systemctl enable sync-tonkho >/dev/null

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

systemctl restart sync-tonkho
sleep 2
systemctl --no-pager --lines=5 status sync-tonkho || true
PORT=$(grep -E '^WEB_PORT=' "$APP_DIR/.env" | cut -d= -f2); PORT=${PORT:-8080}
echo
echo "==> Xong. Web: http://<IP-VPS>:$PORT  (dang nhap bang WEB_USER/WEB_PASSWORD trong .env)"
