# Đồng bộ tồn kho Sapo → Haravan

Lấy số tồn của từng SKU trên Sapo và ghi đè sang Haravan. Hai bên ghép với nhau bằng **SKU**. Chỉ dùng Python có sẵn, không cần cài thêm thư viện.

## Cài đặt
1. Copy `.env.example` thành `.env`, rồi điền key Sapo và token Haravan.
2. Token Haravan: vào Haravan Admin → Ứng dụng → Ứng dụng riêng (Private app) → tạo app với các quyền
   `com.read_products`, `com.read_inventories`, `com.write_inventories`.

## Dùng bằng app (giao diện)
Bấm đúp lối tắt **Dong bo ton kho** trên Desktop (hoặc chạy `pythonw app.py`).

## Dùng bằng dòng lệnh
```
python sync.py check              # kiểm tra kết nối, xem danh sách kho Haravan
python sync.py                    # CHẠY THỬ: chỉ in chênh lệch + xuất báo cáo CSV
python sync.py --apply            # cập nhật thật sang Haravan
python sync.py --apply --loop 15  # tự đồng bộ mỗi 15 phút
```

Báo cáo nằm trong `reports/`, chỉ gồm các sản phẩm cần/đã cập nhật tồn (tên, SKU, kho, tồn trước, tồn mới, chênh lệch). Danh sách SKU lệch nhau giữa 2 bên xem ở trang "Sản phẩm lệch nhau" trên web. Log ghi vào `logs/sync.log`.

## Lưu ý
- Sapo là nguồn chuẩn: tool không bao giờ ghi ngược về Sapo.
- SKU phải trùng nhau ở hai bên. Variant không có SKU sẽ bị bỏ qua.
- Đồng bộ theo từng chi nhánh qua `LOCATION_MAP` (Sapo Hà Nội → Haravan Hà Nội, Sapo HCM → Haravan HCM).
- Nhiều web Haravan dùng chung 1 Sapo: khai báo `SITES=kingcom,newlite` và `NEWLITE_HARAVAN_TOKEN`, `NEWLITE_LOCATION_MAP` (xem `.env.example`). Web đầu tiên vẫn dùng `HARAVAN_TOKEN` / `LOCATION_MAP`. Mỗi lần chạy tải Sapo 1 lần rồi cập nhật từng web; web lỗi không làm hỏng web khác. Báo cáo mỗi web nằm ở `reports/<web>/`. Chạy riêng 1 web: `python sync.py --site newlite`.
- Tồn âm trên Sapo được đẩy sang Haravan là 0.
- Sapo app chỉ cần quyền đọc Tồn kho + Chi nhánh (không cần quyền Sản phẩm).
