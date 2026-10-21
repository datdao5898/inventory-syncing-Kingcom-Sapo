# Hướng dẫn cài đặt trên VPS

Tài liệu này dành cho người hoặc trợ lý AI (ví dụ Hermes) cài web **Đồng bộ tồn kho Sapo → Haravan** lên VPS từ GitHub.
Làm lần lượt từng bước. **Mỗi bước có phần "Kết quả đúng"**: nếu kết quả khác, dừng lại và báo cho chủ dự án, đừng tự sửa code.

- Repo: `https://github.com/datdao5898/inventory-syncing-Kingcom-Sapo`
- Thư mục cài đặt: **thư mục clone repo**, app chạy ngay tại đó. Trên VPS KingCom dùng `/srv/inventory-syncing-Kingcom-Sapo` (cùng chỗ với các app khác).
  Trong tài liệu này, `$APP_DIR` là thư mục đó. Đặt biến trước khi chạy lệnh:
  ```bash
  APP_DIR=/srv/inventory-syncing-Kingcom-Sapo
  ```
- Dịch vụ systemd: `sync-tonkho`, chạy bằng user hệ thống `synctonkho` (không chạy bằng root)
- Không cần cài thư viện Python nào. Chỉ cần `python3` (3.8 trở lên) và `git`.

---

## ⚠️ Quy tắc bắt buộc

1. **KHÔNG chạy `python3 sync.py --apply`** và **KHÔNG bấm "Cập nhật lên Haravan" hoặc bật "Tự động cập nhật"** trên web. Các lệnh đó ghi thật lên cửa hàng Haravan. Chỉ chủ dự án được làm.
2. **KHÔNG commit, KHÔNG push** file `.env`, và không in nội dung `.env` ra log hay tin nhắn. File này chứa key Sapo, token Haravan và mật khẩu web.
3. **KHÔNG tự điền key/token.** Các giá trị bí mật do chủ dự án cung cấp. Nếu chưa có, dừng ở Bước 2 và hỏi.
4. Không sửa code trên VPS. Nếu gặp lỗi, báo lại nguyên văn thông báo lỗi.

---

## Bước 1. Tải code và cài đặt

Chạy bằng root:

```bash
APP_DIR=/srv/inventory-syncing-Kingcom-Sapo
[ -d "$APP_DIR/.git" ] || git clone https://github.com/datdao5898/inventory-syncing-Kingcom-Sapo.git "$APP_DIR"
sudo bash "$APP_DIR/deploy/install.sh"
```

Script `install.sh` sẽ:
- cài `git` và `python3`;
- tạo user `synctonkho`;
- tạo `$APP_DIR/.env` từ `.env.example` (nếu chưa có), đặt quyền 600;
- cài dịch vụ `sync-tonkho`.

**Kết quả đúng (lần đầu):** script dừng với thông báo `.env con thieu: ...`. Đây là bình thường, chuyển sang Bước 2.

> Chạy lại `install.sh` bao nhiêu lần cũng an toàn: không ghi đè `.env`, `reports/`, `settings.json`.

## Bước 2. Điền file cấu hình `.env`

```bash
sudo nano $APP_DIR/.env
```

| Biến | Giá trị | Ai cung cấp |
|---|---|---|
| `SAPO_STORE` | Tên cửa hàng Sapo, tức phần trước `.mysapo.net` | Chủ dự án |
| `SAPO_API_KEY`, `SAPO_API_SECRET` | Key của ứng dụng riêng trên Sapo | Chủ dự án (bí mật) |
| `HARAVAN_TOKEN` | Token ứng dụng riêng trên Haravan | Chủ dự án (bí mật) |
| `LOCATION_MAP` | Giữ nguyên: `821598:1245549,821504:1102762` | Có sẵn |
| `SAPO_QTY_FIELD` | Giữ nguyên: `available` | Có sẵn |
| `WEB_USER` | Tên đăng nhập web, ví dụ `admin` | Tự đặt |
| `WEB_PASSWORD` | Mật khẩu web mạnh, tạo bằng `openssl rand -base64 18` | Tự tạo, **báo lại cho chủ dự án** |
| `WEB_PORT` | Cổng web. **Trên VPS KingCom dùng `8081`** (8080 đã có nginx). Kiểm tra cổng trống bằng `ss -ltn` | Tự đặt |

Quy tắc ghi: mỗi dòng có dạng `TEN=giatri`, không có dấu cách quanh dấu `=`, không có dấu ngoặc kép.

Lưu xong thì chạy lại:

```bash
sudo bash $APP_DIR/deploy/install.sh
```

**Kết quả đúng:** dòng `Active: active (running)` và thông báo `Xong. Web: http://<IP-VPS>:8081`.
Nếu báo `Cong ... dang bi chuong trinh khac dung`: đổi `WEB_PORT` sang cổng trống rồi chạy lại.

## Bước 3. Kiểm tra kết nối Sapo/Haravan (chỉ đọc)

```bash
cd $APP_DIR && runuser -u synctonkho -- python3 sync.py check
```

**Kết quả đúng:**
```
Sapo chi nhanh:  id=821504  ten=KingCom Hồ Chí Minh
Sapo chi nhanh:  id=821598  ten=KingCom Hà Nội
Haravan kho:     id=1245549 ten=Kho Hà Nội
...
Ket noi OK
```

| Lỗi gặp | Nguyên nhân | Việc cần làm |
|---|---|---|
| `HTTP 403` + `error 1010` / `browser_signature_banned` | Cloudflare của Sapo chặn IP của VPS | Báo chủ dự án, kèm IP của VPS |
| `HTTP 403` + `access_denied` từ mysapo.net | Key Sapo thiếu quyền | Báo chủ dự án |
| `HTTP 401` | Key/token sai | Báo chủ dự án kiểm tra lại `.env` |
| `Thieu cau hinh ...` | `.env` thiếu biến | Quay lại Bước 2 |

## Bước 4. Chạy thử (chỉ đọc, không ghi gì lên Haravan)

```bash
cd $APP_DIR && runuser -u synctonkho -- python3 sync.py
```

**Kết quả đúng:** khoảng 30–60 giây sau thấy dòng `Sapo: ... SKU | Haravan: ... SKU | Khop: ... | Can cap nhat: N dong`, và cuối cùng là `Day la CHAY THU`.

## Bước 5. Kiểm tra web

```bash
PORT=$(grep ^WEB_PORT= $APP_DIR/.env | cut -d= -f2)
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:${PORT:-8081}/
```

**Kết quả đúng:** `401` (web đang chạy và đòi đăng nhập).

Sau đó chọn **một** trong hai cách đưa web ra ngoài:

### Cách A (khuyến nghị cho VPS KingCom): qua nginx, có HTTPS

Web chỉ nghe ở `127.0.0.1`, đi ra ngoài qua nginx sẵn có tại `https://ai.kingcom.com.vn/tonkho/`, không mở thêm cổng nào.

1. Trong `$APP_DIR/.env`, đặt `WEB_HOST=127.0.0.1` (thêm dòng nếu chưa có), rồi chạy `systemctl restart sync-tonkho`.
2. Tìm file cấu hình nginx có `server_name ai.kingcom.com.vn` và đang `listen 443`:
   ```bash
   grep -rl "ai.kingcom.com.vn" /etc/nginx/
   ```
   **Sao lưu file trước khi sửa:** `cp <file> <file>.bak-$(date +%F)`
3. Thêm vào **bên trong** block `server { ... }` của 443 (không đụng tới các `location` có sẵn):
   ```nginx
   location = /tonkho { return 301 /tonkho/; }
   location /tonkho/ {
       proxy_pass http://127.0.0.1:8081/;   # dau "/" cuoi de bo tien to /tonkho
       proxy_set_header Host $host;
       proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
       proxy_set_header X-Forwarded-Proto $scheme;
       proxy_read_timeout 180s;
   }
   ```
4. Kiểm tra rồi nạp lại. **Chỉ reload khi `nginx -t` báo OK**, nếu lỗi thì khôi phục file `.bak`:
   ```bash
   sudo nginx -t && sudo systemctl reload nginx
   curl -s -o /dev/null -w "%{http_code}
" https://ai.kingcom.com.vn/tonkho/
   ```
   **Kết quả đúng:** `401`. Các app khác trên `ai.kingcom.com.vn` vẫn chạy bình thường.
5. Không cần mở cổng tường lửa. Địa chỉ web là `https://ai.kingcom.com.vn/tonkho/` (nhớ có dấu `/` ở cuối).

### Cách B: mở cổng trực tiếp (không mã hoá)

```bash
sudo ufw allow ${PORT:-8081}/tcp
```

Địa chỉ web là `http://<IP-VPS>:8081`. Mật khẩu đăng nhập đi qua mạng không được mã hoá.

## Bước 6. Báo cáo lại cho chủ dự án

Gửi các thông tin sau. **Không gửi kèm key/token.**
- Địa chỉ web: `https://ai.kingcom.com.vn/tonkho/` (Cách A) hoặc `http://<IP-VPS>:<PORT>` (Cách B)
- `WEB_USER` và `WEB_PASSWORD` (gửi qua kênh riêng)
- Kết quả Bước 3 (copy nguyên văn)
- Dòng `Sapo: ... | Can cap nhat: N dong` ở Bước 4

---

## Cập nhật khi có code mới

> **Dành cho trợ lý AI/người vận hành:** khi chủ dự án yêu cầu "pull git", "cập nhật", "lấy code mới" cho app này,
> **luôn chạy `update.sh` bên dưới, không chạy `git pull` trực tiếp.** `git pull` bằng root làm sai quyền sở hữu file
> (user chạy app là `synctonkho`) và không khởi động lại web, nên thay đổi trong `web.py`/`sync.py` chưa có hiệu lực.

```bash
sudo bash $APP_DIR/deploy/update.sh
```

**Kết quả đúng:** `OK: <commit cũ> -> <commit mới>, web dang chay`. Các file `.env`, lịch sử và báo cáo được giữ nguyên.

## Lệnh thường dùng

```bash
systemctl status sync-tonkho          # xem dịch vụ có đang chạy không
systemctl restart sync-tonkho         # khởi động lại (sau khi sửa .env)
journalctl -u sync-tonkho -n 100      # xem 100 dòng log gần nhất
journalctl -u sync-tonkho -f          # xem log trực tiếp
```

## Dữ liệu trên VPS (không có trên GitHub)

| Đường dẫn | Nội dung |
|---|---|
| `$APP_DIR/.env` | Key, token, mật khẩu web |
| `$APP_DIR/settings.json` | Cài đặt tự động cập nhật |
| `$APP_DIR/reports/` | Báo cáo CSV, lịch sử chạy, dữ liệu so sánh |
| `$APP_DIR/logs/sync.log` | Nhật ký chi tiết |
