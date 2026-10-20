# Đưa web đồng bộ tồn kho lên VPS (Ubuntu/Debian)

Không cần cài thư viện nào, chỉ cần Python 3.8 trở lên (Ubuntu có sẵn).

## 1. Copy code lên VPS
Mở PowerShell trên máy Windows, thay `IP_VPS` bằng IP của VPS:
```
ssh root@IP_VPS "mkdir -p /opt/sapo_haravan_sync/deploy"
cd C:\Users\datda\OneDrive\Desktop\Codex\sapo_haravan_sync
scp sync.py web.py .env root@IP_VPS:/opt/sapo_haravan_sync/
scp deploy\sync-tonkho.service root@IP_VPS:/opt/sapo_haravan_sync/deploy/
```

## 2. Cài thành dịch vụ (tự chạy khi VPS khởi động lại)
```
ssh root@IP_VPS
chmod 600 /opt/sapo_haravan_sync/.env
cp /opt/sapo_haravan_sync/deploy/sync-tonkho.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now sync-tonkho
ufw allow 8080/tcp        # neu VPS co bat tuong lua ufw
```

## 3. Dùng
Mở trình duyệt: `http://IP_VPS:8080`, rồi đăng nhập bằng `WEB_USER` / `WEB_PASSWORD` trong `.env`.

## Lệnh hay dùng trên VPS
```
systemctl status sync-tonkho      # xem dang chay khong
systemctl restart sync-tonkho     # khoi dong lai (sau khi sua .env hoac code)
journalctl -u sync-tonkho -f      # xem log truc tiep
```

## Cập nhật code mới
Chạy lại các lệnh `scp` ở bước 1, sau đó chạy `systemctl restart sync-tonkho`.

## Bảo mật
- Đặt `WEB_PASSWORD` mạnh.
- Địa chỉ `http://` không mã hoá mật khẩu. Nếu có tên miền, nên đặt Caddy hoặc nginx phía trước để có `https://`. Khi đó đặt `WEB_HOST=127.0.0.1` trong `.env` và đóng cổng 8080.
