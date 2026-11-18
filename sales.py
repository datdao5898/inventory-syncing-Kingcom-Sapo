"""Bao cao doanh thu cac gian hang San (Shopee, TikTok Shop...) lay tu don hang Sapo.

Don San duoc Sapo dong bo ve qua kenh ban hang; moi don co channel_definition.branch_name = ten gian hang.
Quy uoc so lieu:
- Ngay = ngay tao don tren Sapo, gio Viet Nam.
- Bo don da huy. Doanh thu = tong tien don sau giam gia, tru phan da hoan tien (tra hang).
- Doanh thu san pham = don gia sau giam gia x so luong con lai (sau tra hang).

Chay tay:  python sales.py 2026-09-01 2026-11-30   -> reports/doanhthu_san_<tu>_<den>.xlsx
"""
import io
import os
import sys
import time
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import sync

log = sync.log
VN = timezone(timedelta(hours=7))
CHANNEL_NAMES = {"shopee": "Shopee", "tiktokshop": "TikTok Shop", "lazada": "Lazada", "tiki": "Tiki"}
PIE_TOP = 10  # bieu do tron: 10 muc lon nhat + "Cac ... khac"
# 10 mau phan loai (da kiem tra phan biet duoc ca voi nguoi mu mau) + xam cho "khac"; giong trang web
PIE_COLORS = ["2A78D6", "EB6834", "1BAF7A", "EDA100", "E87BA4", "008300", "6250D6", "E34948", "0094C6", "B8650A", "B4B4BB"]
TOP_MONTH = 10  # sheet tung gian: top 10 san pham ban chay moi thang
PRODUCT_ROW_PT = 34  # chieu cao dong san pham: du cho ten 2 dong + chu thich 10 muc cua bieu do
AD_COLUMNS = ["Ngân sách quảng cáo\n广告预算", "Tổng doanh thu từ Shopee ads\nShopee 广告销售额", "Tổng đơn hàng\n总订单数"]
CACHE_SECONDS = 600
_cache = {}


def parse_day(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


def iter_order_pages(sapo, start, end, progress=None):
    """Tung trang don (moi trang thai) tao trong [start, end] theo gio VN.
    Tra ve tung trang de nguoi goi cong don roi bo di - khong giu ca nghin don trong RAM
    (moi don day du ~20 KB, 1 nam ~30.000 don se ton hon 1 GB)."""
    params = {"created_on_min": f"{start:%Y-%m-%d}T00:00:00+07:00",
              "created_on_max": f"{end:%Y-%m-%d}T23:59:59+07:00"}
    total = sapo.get("orders/count.json", params).get("count", 0)
    done, page = 0, 1
    while True:
        rows = sapo.get("orders.json", {**params, "limit": 250, "page": page}).get("orders", [])
        yield rows
        done += len(rows)
        if progress:
            progress(done, total)
        if len(rows) < 250:
            return
        page += 1


def shop_of(order):
    """(kenh, gian hang) neu la don San, nguoc lai None."""
    ch = order.get("channel_definition") or {}
    branch = (ch.get("branch_name") or "").strip()
    alias = (ch.get("alias") or order.get("source_name") or "").lower()
    if not branch or alias not in CHANNEL_NAMES:
        return None
    channel = CHANNEL_NAMES[alias]
    suffix = f" - {ch.get('main_name') or ''}"
    shop = branch[: -len(suffix)] if suffix.strip(" -") and branch.endswith(suffix) else branch
    return channel, shop.strip()


def is_cancelled(order):
    return bool(order.get("cancelled_on")) or order.get("status") == "cancelled"


def order_revenue(order):
    return float(order.get("total_price") or 0) - float(order.get("total_refunded") or 0)


def line_qty(li):
    q = li.get("current_quantity")
    return int(q if q is not None else li.get("quantity") or 0)


def line_revenue(li):
    unit = li.get("discounted_unit_price")
    if unit is None:
        unit = float(li.get("price") or 0) - float(li.get("total_discount") or 0) / max(int(li.get("quantity") or 1), 1)
    return float(unit) * line_qty(li)


def month_of(order):
    """Thang (YYYY-MM) theo gio VN cua ngay tao don."""
    raw = (order.get("created_on") or "").replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(raw).astimezone(VN).strftime("%Y-%m")
    except ValueError:
        return raw[:7]


def months_between(start, end):
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


class Aggregator:
    """Cong don tung don hang, chi giu cac con so can cho bao cao (RAM khong tang theo so don)."""

    def __init__(self, start, end):
        self.start, self.end = start, end
        self.months = months_between(start, end)
        self.shops = {}                                   # (kenh, gian) -> tong
        self.products = defaultdict(lambda: [0, 0.0])    # (kenh, gian, sku) -> [so luong, doanh thu]
        self.all_products = defaultdict(lambda: [0, 0.0, set()])  # sku -> [so luong, doanh thu, {gian}]
        self.names = {}                                   # sku -> (sku goc, ten dau tien gap)
        self.monthly = defaultdict(lambda: defaultdict(lambda: [0, 0.0]))  # (kenh, gian, thang) -> sku -> [sl, dt]
        self.cancelled = 0

    def add(self, o):
        key = shop_of(o)
        if not key:
            return
        s = self.shops.get(key)
        if s is None:
            s = self.shops[key] = {"channel": key[0], "shop": key[1], "orders": 0, "revenue": 0.0, "cancelled": 0,
                                   "months": {m: [0, 0.0] for m in self.months}}
        if is_cancelled(o):
            s["cancelled"] += 1
            self.cancelled += 1
            return
        revenue = order_revenue(o)
        s["orders"] += 1
        s["revenue"] += revenue
        month = month_of(o)
        mo = s["months"].setdefault(month, [0, 0.0])
        mo[0] += 1
        mo[1] += revenue
        lines = [li for li in o.get("line_items", []) if line_qty(li) > 0]
        # Giam gia cap don (voucher shop...) khong nam trong don gia tung dong -> chia theo ty le
        # de tong doanh thu san pham khop tong doanh thu gian hang
        gross = sum(line_revenue(li) for li in lines)
        ratio = revenue / gross if gross > 0 else 0
        monthly = self.monthly[(key[0], key[1], month)]
        for li in lines:
            qty, rev = line_qty(li), line_revenue(li) * ratio
            sku = (li.get("sku") or "").strip()
            name = (li.get("name") or li.get("title") or "").strip()
            pkey = sku.upper() or name
            if pkey not in self.names:
                self.names[pkey] = (sku, name)
            p = self.products[(key[0], key[1], pkey)]
            p[0] += qty
            p[1] += rev
            a = self.all_products[pkey]
            a[0] += qty
            a[1] += rev
            a[2].add(key)
            mp = monthly[pkey]
            mp[0] += qty
            mp[1] += rev

    def result(self):
        shops, names = self.shops, self.names
        shop_rows = sorted(({**r, "months": [{"month": m, "orders": v[0], "revenue": v[1]} for m, v in sorted(r["months"].items())]}
                            for r in shops.values()), key=lambda r: -r["revenue"])
        total_months = [{"month": m,
                         "orders": sum(r["months"][m][0] for r in shops.values() if m in r["months"]),
                         "revenue": sum(r["months"][m][1] for r in shops.values() if m in r["months"])}
                        for m in self.months]
        prod_rows = sorted(({"channel": c, "shop": sh, "sku": names[k][0], "name": names[k][1], "qty": v[0], "revenue": v[1]}
                            for (c, sh, k), v in self.products.items()),
                           key=lambda r: (-shops[(r["channel"], r["shop"])]["revenue"], -r["revenue"]))
        all_rows = sorted(({"sku": names[k][0], "name": names[k][1], "qty": v[0], "revenue": v[1], "shops": len(v[2])}
                           for k, v in self.all_products.items()), key=lambda r: -r["revenue"])
        # Top san pham tung thang cua tung gian (chi giu TOP_MONTH muc dau)
        monthly_top = [{"channel": c, "shop": sh, "month": m,
                        "top": [{"sku": names[k][0], "name": names[k][1], "qty": v[0], "revenue": v[1]}
                                for k, v in sorted(prods.items(), key=lambda kv: -kv[1][1])[:TOP_MONTH]]}
                       for (c, sh, m), prods in self.monthly.items()]
        return {
            "from": f"{self.start:%Y-%m-%d}", "to": f"{self.end:%Y-%m-%d}",
            "generated_at": datetime.now(VN).strftime("%Y-%m-%d %H:%M"),
            "total": {"orders": sum(r["orders"] for r in shop_rows), "revenue": sum(r["revenue"] for r in shop_rows),
                      "cancelled": self.cancelled, "shops": len(shop_rows), "months": total_months},
            "shops": shop_rows, "products": prod_rows, "all_products": all_rows, "monthly_top": monthly_top,
        }


def build_report(orders, start, end):
    agg = Aggregator(start, end)
    for o in orders:
        agg.add(o)
    return agg.result()


def get_report(start, end, progress=None, fresh=False):
    """Bao cao co cache 10 phut. Lay don theo tung trang 250 don, cong don xong bo ngay."""
    key = (start, end)
    hit = _cache.get(key)
    if hit and not fresh and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    agg = Aggregator(start, end)
    for page in iter_order_pages(sync.Sapo(), start, end, progress):
        for o in page:
            agg.add(o)
        del page
    report = agg.result()
    _cache.clear()
    _cache[key] = (time.time(), report)
    return report


# ---------------------------------------------------------------- excel

def to_xlsx(report):
    from openpyxl import Workbook
    from openpyxl.chart import BarChart, PieChart, Reference
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    head_font, bold = Font(bold=True, color="FFFFFF"), Font(bold=True)
    head_fill = PatternFill("solid", fgColor="3F3F46")
    yu_fill = PatternFill("solid", fgColor="FFF4CC")
    thin = Side(style="thin", color="D4D4D8")
    box = Border(left=thin, right=thin, top=thin, bottom=thin)
    money, count = "#,##0", "#,##0"
    d1, d2 = (datetime.strptime(report[k], "%Y-%m-%d") for k in ("from", "to"))
    period = f"Từ {d1:%d/%m/%Y} đến {d2:%d/%m/%Y} · 期间：{d1:%Y年%m月%d日} – {d2:%Y年%m月%d日}"
    note = ("Không tính đơn đã huỷ; doanh thu đã trừ tiền hoàn (trả hàng). Nguồn: đơn hàng Sapo. · "
            "不含已取消订单；销售额已扣除退货退款。数据来源：Sapo 订单。")
    vnd_h = "Tổng doanh thu (đ)\n销售额（越南盾）"
    rev_h = "Doanh thu (đ)\n销售额（越南盾）"

    def head(c):
        c.font, c.fill, c.border = head_font, head_fill, box
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    def sheet(ws, title, headers, rows, formats, widths, yu_cols=0, top=1):
        ws.cell(top, 1, title).font = Font(bold=True, size=14)
        ws.cell(top + 1, 1, period)
        ws.cell(top + 2, 1, note).font = Font(italic=True, color="71717A")
        hr = top + 4
        for i, h in enumerate(headers, 1):
            ws.cell(hr, i, h)
        for i, _ in enumerate(headers, 1):
            c = ws.cell(hr, i)
            c.font, c.fill, c.border = head_font, head_fill, box
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            if i > len(headers) - yu_cols:
                c.fill, c.font = PatternFill("solid", fgColor="A16207"), head_font
        for k, r in enumerate(rows, hr + 1):
            for i, v in enumerate(r, 1):
                ws.cell(k, i, v)
            for i, f in enumerate(formats, 1):
                c = ws.cell(k, i)
                c.border = box
                if f:
                    c.number_format = f
                if i > len(headers) - yu_cols:
                    c.fill = yu_fill
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.row_dimensions[hr].height = 44
        ws.freeze_panes = ws.cell(hr + 1, 1)
        return hr

    wb = Workbook()

    # ======== Tong Quan (theo file mau Report_Yu): bang gian hang + 3 cot de trong cho Yu, bieu do ben duoi
    ws = wb.active
    ws.title = "Tổng Quan"
    rows = [[r["shop"], r["channel"], r["orders"], round(r["revenue"]), None, None, None] for r in report["shops"]]
    hr = sheet(ws, "Tổng doanh thu theo gian hàng Sàn · 各电商平台店铺销售额",
               ["Gian hàng\n店铺", "Kênh bán hàng\n销售平台", "Số lượng đơn hàng\n订单数量", vnd_h] + AD_COLUMNS,
               rows, [None, None, count, money, money, money, count], [27, 22, 18, 20, 20, 22, 16], yu_cols=3, top=2)
    last = hr + len(rows)
    t = report["total"]
    total_row = last + 1
    for i, v in enumerate(["TỔNG CỘNG · 合计", "", t["orders"], round(t["revenue"])] +
                          [f"=SUM({get_column_letter(i)}{hr + 1}:{get_column_letter(i)}{last})" for i in (5, 6, 7)], 1):
        ws.cell(total_row, i, v)
    for i, f in enumerate([None, None, count, money, money, money, count], 1):
        c = ws.cell(total_row, i)
        c.font, c.border = bold, box
        if f:
            c.number_format = f
    ws.cell(total_row + 1, 1, "Các cột màu vàng để trống cho Yu điền số liệu quảng cáo. · 黄色列留空，由 Yu 填写广告数据。").font = Font(italic=True, color="A16207")
    chart_row = total_row + 3
    ws.cell(chart_row, 2, "BIỂU ĐỒ · 图表").font = bold
    shops = [r for r in report["shops"] if r["revenue"] > 0]
    if shops:
        # Bang ty trong cho bieu do tron: 10 gian lon nhat + "Cac gian khac" (cot I-K, nhu file mau)
        top, rest = shops[:PIE_TOP], shops[PIE_TOP:]
        pie_rows = [(r["shop"] if r["channel"] == "Shopee" else f'{r["shop"]} ({r["channel"]})', r["revenue"]) for r in top]
        if rest:
            pie_rows.append((f"Các gian khác · 其他店铺 ({len(rest)})", sum(r["revenue"] for r in rest)))
        total = sum(v for _, v in pie_rows)
        ws.cell(hr, 9, "Tỷ trọng doanh thu · 销售额占比").font = bold
        for j, h in enumerate(["Gian hàng\n店铺", rev_h, "Tỷ trọng\n占比"], 9):
            head(ws.cell(hr + 1, j, h))
        for k, (name, v) in enumerate(pie_rows, hr + 2):
            for j, (val, f) in enumerate([(name, None), (round(v), money), (v / total, "0.0%")], 9):
                c = ws.cell(k, j, val)
                c.border = box
                if f:
                    c.number_format = f
        pie_last = hr + 1 + len(pie_rows)
        for j, w in zip("IJK", (32, 18, 10)):
            ws.column_dimensions[j].width = w
        pie = PieChart()
        pie.title = "Tỷ trọng doanh thu theo gian hàng · 各店铺销售额占比"
        pie.add_data(Reference(ws, min_col=10, min_row=hr + 1, max_row=pie_last), titles_from_data=True)
        pie.set_categories(Reference(ws, min_col=9, min_row=hr + 2, max_row=pie_last))
        pie.dataLabels = only_percent()
        color_points(pie, len(pie_rows))
        pie.height, pie.width = 12, 24
        ws.add_chart(pie, f"A{chart_row + 1}")

        chart = BarChart()
        chart.type = "bar"
        chart.title = "Doanh thu theo gian hàng · 各店铺销售额"
        chart.y_axis.title = "Doanh thu (đ) · 销售额（越南盾）"
        chart.y_axis.numFmt = "#,##0"
        chart.y_axis.majorGridlines = None
        chart.x_axis.scaling.orientation = "maxMin"  # gian hang lon nhat o tren
        chart.legend = None
        chart.x_axis.delete = chart.y_axis.delete = False  # openpyxl 3.1 mac dinh an truc -> mat ten gian hang
        chart.y_axis.crosses = "max"  # truc so o duoi khi dao thu tu gian hang
        chart.add_data(Reference(ws, min_col=4, min_row=hr, max_row=last), titles_from_data=True)
        chart.set_categories(Reference(ws, min_col=1, min_row=hr + 1, max_row=last))
        chart.series[0].graphicalProperties.solidFill = "3F3F46"
        chart.height, chart.width = max(12, 0.75 * len(report["shops"]) + 3), 22
        ws.add_chart(chart, f"I{chart_row + 1}")

    # ======== Moi gian hang 1 sheet (theo sheet "Ulanzi HN" cua file mau)
    tops = {(m["channel"], m["shop"], m["month"]): m["top"] for m in report.get("monthly_top", [])}
    one_year = len({m["month"][:4] for m in t["months"]}) <= 1
    month_cell = lambda m: int(m[5:]) if one_year else f"{int(m[5:])}/{m[:4]}"
    used_names = {"Tổng Quan"}
    yellow = PatternFill("solid", fgColor="FFEB9C")    # kieu "Neutral" cua Excel nhu file mau
    yellow_font = Font(color="9C5700")
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)

    def shop_head(c, text):
        c.value, c.font, c.fill, c.border, c.alignment = text, yellow_font, yellow, box, center

    def shop_cell(c, value, fmt=None, size=12):
        c.value, c.border, c.alignment, c.font = value, box, center, Font(size=size)
        if fmt:
            c.number_format = fmt

    for r in report["shops"]:
        if r["orders"] <= 0:
            continue
        channel_ads = "Shopee" if r["channel"] == "Shopee" else r["channel"].replace(" Shop", "")
        name = sheet_name(r["shop"], r["channel"], used_names)
        ws = wb.create_sheet(name)
        for col, w in zip("ABCDEFG", (14, 62, 18, 20, 24, 24, 4)):
            ws.column_dimensions[col].width = w
        # tieu de
        ws.merge_cells("A2:F2")
        c = ws["A2"]
        c.value = (f"REPORT DOANH THU VÀ CHI PHÍ {channel_ads.upper()} ADS {r['shop'].upper()}\n"
                   f"{r['shop']} 销售额与 {channel_ads} 广告费用报告")
        c.font, c.alignment = Font(bold=True, size=12), Alignment(horizontal="left", vertical="center", wrap_text=True)
        ws.row_dimensions[2].height = 36
        ws["A3"] = f"{r['shop']} · {r['channel']} · {period}"
        ws["A3"].font = Font(italic=True, color="71717A")
        # bang theo thang
        heads = ["Tháng\n月份", "Doanh Thu\n销售额", "Tổng Đơn Hàng\n订单总数", f"Chi phí {channel_ads} ads\n{channel_ads} 广告费用",
                 f"Doanh Thu Từ {channel_ads} Ads\n{channel_ads} 广告销售额", "Tổng Đơn Hàng Từ Ads\n广告订单数"]
        for j, h in enumerate(heads, 1):
            shop_head(ws.cell(4, j), h)
        ws.row_dimensions[4].height = 34
        row = 5
        for m in r["months"]:
            shop_cell(ws.cell(row, 1), month_cell(m["month"]))
            shop_cell(ws.cell(row, 2), round(m["revenue"]), "#,##0")
            shop_cell(ws.cell(row, 3), m["orders"], "#,##0")
            for j in (4, 5, 6):  # Yu dien
                shop_cell(ws.cell(row, j), None, '_(* #,##0_);_(* \\(#,##0\\);_(* "-"??_);_(@_)')
            row += 1
        # top 10 san pham ban chay tung thang
        row += 1
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=3)
        c = ws.cell(row, 1, f"TOP {TOP_MONTH} SẢN PHẨM BÁN CHẠY TRONG THÁNG · 每月畅销产品前 {TOP_MONTH} 名")
        c.font, c.alignment = Font(bold=True, size=12), Alignment(horizontal="left", vertical="center")
        ws.row_dimensions[row].height = 30
        row += 2
        for j, h in enumerate(["Tháng\n月份", "Tên sản phẩm\n产品名称", "Số lượng\n数量", "Doanh thu\n销售额"], 1):
            shop_head(ws.cell(row, j), h)
        ws.row_dimensions[row].height = 34
        ws.cell(row, 6, "BIỂU ĐỒ % CỦA TOP SP · 热销产品占比").font = bold
        row += 1
        for m in r["months"]:
            items = tops.get((r["channel"], r["shop"], m["month"]), [])
            first = row
            n = TOP_MONTH  # moi thang dung du 10 dong de bieu do khong chong len nhau
            for k in range(n):
                it = items[k] if k < len(items) else None
                shop_cell(ws.cell(row, 2), it["name"] if it else ("Không có đơn · 无订单" if k == 0 and not items else None))
                shop_cell(ws.cell(row, 3), it["qty"] if it else None, "#,##0")
                shop_cell(ws.cell(row, 4), round(it["revenue"]) if it else None, "#,##0")
                ws.row_dimensions[row].height = PRODUCT_ROW_PT
                row += 1
            ws.merge_cells(start_row=first, start_column=1, end_row=row - 1, end_column=1)
            shop_cell(ws.cell(first, 1), month_cell(m["month"]))
            if items:
                pie = PieChart()
                pie.title = f"Tháng {month_cell(m['month'])} · {m['month'][:4]}年{int(m['month'][5:])}月"
                pie.add_data(Reference(ws, min_col=4, min_row=first, max_row=first + len(items) - 1))
                pie.set_categories(Reference(ws, min_col=2, min_row=first, max_row=first + len(items) - 1))
                pie.dataLabels = only_percent()
                color_points(pie, len(items))
                pie.legend.txPr = small_text(800)  # chu 8pt de chu thich hien du 10 ten san pham dai
                pie.height, pie.width = PRODUCT_ROW_PT * n * 2.54 / 72 * 0.97, 27  # cm, vua khit 10 dong cua thang
                ws.add_chart(pie, f"F{first}")

    # ======== Phu luc: danh sach day du san pham
    ws = wb.create_sheet("PL1. SP theo gian hàng")
    sheet(ws, "Phụ lục: doanh thu theo sản phẩm — chi tiết từng gian hàng · 附录：各店铺产品销售额",
          ["Gian hàng\n店铺", "Kênh bán hàng\n销售平台", "SKU", "Tên sản phẩm\n产品名称", "Số lượng\n数量", vnd_h],
          [[r["shop"], r["channel"], r["sku"], r["name"], r["qty"], round(r["revenue"])] for r in report["products"]],
          [None, None, None, None, count, money], [30, 14, 14, 60, 10, 20])
    ws.auto_filter.ref = f"A5:F{max(ws.max_row, 5)}"

    ws = wb.create_sheet("PL2. SP tất cả gian")
    sheet(ws, "Phụ lục: doanh thu theo sản phẩm — tổng tất cả các gian hàng · 附录：全部店铺产品销售额",
          ["SKU", "Tên sản phẩm\n产品名称", "Số lượng\n数量", vnd_h, "Số gian hàng có bán\n销售店铺数"],
          [[r["sku"], r["name"], r["qty"], round(r["revenue"]), r["shops"]] for r in report["all_products"]],
          [None, None, count, money, count], [14, 64, 10, 20, 14])
    ws.auto_filter.ref = f"A5:E{ws.max_row}"

    buf = io.BytesIO()
    wb.save(buf)
    return fix_core_dates(buf.getvalue())


def fix_core_dates(data):
    """openpyxl ban cu (vd python3-openpyxl cua Ubuntu) ghi ngay tao file dang '...+00:00Z' - sai dinh dang,
    Excel bao 'We found a problem with some content' va doi sua file. Sua lai thanh '...Z'."""
    src = zipfile.ZipFile(io.BytesIO(data))
    core = src.read("docProps/core.xml")
    if b"+00:00Z" not in core:
        return data
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            body = core.replace(b"+00:00Z", b"Z") if item.filename == "docProps/core.xml" else src.read(item.filename)
            dst.writestr(item, body)
    return out.getvalue()


def sheet_name(shop, channel, used):
    """Ten sheet hop le cua Excel (<= 31 ky tu, khong []:*?/\\), khong trung."""
    base = "".join("_" if ch in '[]:*?/\\' else ch for ch in shop).strip(" '") or "Gian"
    for cand in (base, f"{base} ({channel.replace(' Shop', '')})"):
        cand = cand[:31]
        if cand.lower() not in {u.lower() for u in used}:
            used.add(cand)
            return cand
    n = 2
    while f"{base[:27]} ({n})".lower() in {u.lower() for u in used}:
        n += 1
    used.add(f"{base[:27]} ({n})")
    return f"{base[:27]} ({n})"


def only_percent():
    """Nhan bieu do tron chi hien %. Phai tat ro cac muc khac, neu khong Excel hien ca 'Series1; ten; gia tri'."""
    from openpyxl.chart.label import DataLabelList
    return DataLabelList(showPercent=True, showVal=False, showCatName=False, showSerName=False, showLegendKey=False,
                         showLeaderLines=False)


def small_text(size):
    """Co chu (1/100 pt) cho chu thich bieu do."""
    from openpyxl.chart.text import RichText
    from openpyxl.drawing.text import CharacterProperties, Paragraph, ParagraphProperties
    return RichText(p=[Paragraph(pPr=ParagraphProperties(defRPr=CharacterProperties(sz=size)),
                                 endParaRPr=CharacterProperties(sz=size))])


def color_points(pie, n):
    """To mau co dinh theo thu tu cho bieu do tron Excel (giong trang web)."""
    from openpyxl.chart.marker import DataPoint
    for k, color in enumerate(PIE_COLORS[:n]):
        pt = DataPoint(idx=k)
        pt.graphicalProperties.solidFill = color
        pt.graphicalProperties.line.solidFill = "FFFFFF"
        pie.series[0].dPt.append(pt)


def xlsx_name(report):
    return f"doanhthu_san_{report['from']}_{report['to']}.xlsx"


def main():
    sync.load_env(os.path.join(sync.BASE_DIR, ".env"))
    start = parse_day(sys.argv[1]) if len(sys.argv) > 1 else date(2026, 9, 1)
    end = parse_day(sys.argv[2]) if len(sys.argv) > 2 else date(2026, 11, 30)
    report = get_report(start, end, lambda n, t: print(f"\rDa lay {n}/{t} don", end="", flush=True))
    print()
    path = os.path.join(sync.BASE_DIR, "reports", xlsx_name(report))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(to_xlsx(report))
    t = report["total"]
    print(f"{t['shops']} gian hang, {t['orders']} don, doanh thu {t['revenue']:,.0f} d (bo {t['cancelled']} don huy)")
    print("Da ghi", path)


if __name__ == "__main__":
    main()
