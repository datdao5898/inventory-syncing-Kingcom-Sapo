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
- Trang **Doanh thu sàn** (`/doanh-thu`, file `sales.html` + `sales.py`) tách riêng khỏi phần đồng bộ, không phụ thuộc web Haravan đang chọn: lấy đơn Shopee/TikTok Shop từ Sapo theo khoảng ngày, tổng hợp theo gian hàng và theo sản phẩm, biểu đồ tròn (mặc định, 10 gian lớn nhất + "các gian khác") hoặc cột; chọn từng gian hàng để xem biểu đồ doanh thu theo tháng + tỷ trọng sản phẩm; xuất Excel theo mẫu Report_Yu (sheet Tổng Quan + mỗi gian hàng 1 sheet: doanh thu/đơn theo tháng, cột quảng cáo để trống cho Yu, 10 sản phẩm bán chạy nhất từng tháng kèm biểu đồ %). Giao diện và file Excel chỉ dùng tiếng Việt.
- **Quảng cáo Shopee** (mục 3 trang Doanh thu sàn, file `ads.py`): tải lên file CSV xuất từ Quảng cáo Shopee (Dịch vụ Hiển thị; mỗi file 1 gian hàng, 1 khoảng thời gian, kéo thả nhiều file cùng lúc). Tool tự nhận gian hàng theo tên trong file, gộp các chiến dịch cùng sản phẩm và tính theo file yêu cầu `BAO_CAO_SHOPEE_DEMO_AI`: chi phí, doanh số từ QC, lượt xem, click, đơn từ QC; ROAS = doanh số QC / chi phí; **TACOS = chi phí QC / doanh thu Sapo của SKU đã gán** (cần lấy doanh thu Sapo đúng cùng kỳ với file); CTR; CR; đánh giá tự động theo ngưỡng `THRESHOLDS` trong `ads.py` (TACOS tốt 8–12%). Mã sản phẩm Shopee khác SKU Sapo nên cần gán 1 lần (tool gợi ý theo tên, nhiều phân loại thì nhập nhiều SKU), lưu ở `reports/ads/map.json`; file đã tải lên lưu ở `reports/ads/uploads.json`. Khi có file cùng kỳ, Excel tự điền 3 cột quảng cáo ở sheet Tổng Quan và thêm sheet "Quảng cáo Shopee".
  - 2 loại file: **Quảng cáo Sản phẩm** (có cột Mã sản phẩm) và **Quảng cáo Shop** (không gắn sản phẩm); cùng gian + cùng kỳ + cùng loại thì file mới thay file cũ. QC Shop hiện thành dòng riêng, TACOS của nó chia cho doanh thu cả gian; dòng tổng gian cộng cả 2 loại nhưng TACOS chỉ tính QC Sản phẩm (công thức Yu chọn).
  - Ngưỡng đánh giá theo trả lời của Yu (24/11/2026): TACOS tốt 8–12%, ROAS < 8, CTR < 6%, CR < 3%, tốn tiền mà 0 đơn → nên dừng chiến dịch.
- **Theo dõi tuần** (nhóm 1 file yêu cầu): nút "Tuần trước" (thứ 2 – chủ nhật); bảng gian hàng có cột **Saleman** (gán ngay trên bảng, lưu `reports/saleman.json`), **bán chạy nhất** và **thương hiệu bán chạy** (xếp theo số lượng; thương hiệu = chữ đầu tên sản phẩm, gộp tên viết sai trong `BRAND_ALIASES`); lọc **SP bán ≥ 30 cái**. Excel có sheet **"Theo dõi tuần"** = Shopee Ads Weekly Tracker (mỗi gian Shopee 1 dòng: saleman, bán chạy, thương hiệu, chi phí QC SP/Shop, doanh số QC, ROAS, TACOS, hiển thị, click, CTR, đơn, CR, đánh giá). Không tính đơn huỷ; doanh thu đã trừ tiền hoàn. Cần quyền đọc Đơn hàng trên Sapo và thư viện `openpyxl` (`update.sh` tự cài). Chạy tay: `python sales.py 2026-09-01 2026-11-30`.
