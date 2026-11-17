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
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import sync

log = sync.log
VN = timezone(timedelta(hours=7))
CHANNEL_NAMES = {"shopee": "Shopee", "tiktokshop": "TikTok Shop", "lazada": "Lazada", "tiki": "Tiki"}
AD_COLUMNS = ["Ngân sách quảng cáo", "Tổng doanh thu từ Shopee ads", "Tổng đơn hàng"]
CACHE_SECONDS = 600
_cache = {}


def parse_day(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


def fetch_orders(sapo, start, end, progress=None):
    """Tat ca don (moi trang thai) tao trong [start, end] theo gio VN."""
    params = {"created_on_min": f"{start:%Y-%m-%d}T00:00:00+07:00",
              "created_on_max": f"{end:%Y-%m-%d}T23:59:59+07:00"}
    total = sapo.get("orders/count.json", params).get("count", 0)
    out, page = [], 1
    while True:
        rows = sapo.get("orders.json", {**params, "limit": 250, "page": page}).get("orders", [])
        out += rows
        if progress:
            progress(len(out), total)
        if len(rows) < 250:
            return out
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


def build_report(orders, start, end):
    shops = {}                                  # (kenh, gian) -> tong
    products = defaultdict(lambda: {"qty": 0, "revenue": 0.0})   # (kenh, gian, sku, ten)
    all_products = defaultdict(lambda: {"qty": 0, "revenue": 0.0, "shops": set()})  # (sku, ten)
    cancelled = 0
    names = {}                                   # sku -> ten dau tien gap (gop ten khac nhau cua cung SKU)
    for o in orders:
        key = shop_of(o)
        if not key:
            continue
        s = shops.setdefault(key, {"channel": key[0], "shop": key[1], "orders": 0, "revenue": 0.0, "cancelled": 0})
        if is_cancelled(o):
            s["cancelled"] += 1
            cancelled += 1
            continue
        revenue = order_revenue(o)
        s["orders"] += 1
        s["revenue"] += revenue
        lines = [li for li in o.get("line_items", []) if line_qty(li) > 0]
        # Giam gia cap don (voucher shop...) khong nam trong don gia tung dong -> chia theo ty le
        # de tong doanh thu san pham khop tong doanh thu gian hang
        gross = sum(line_revenue(li) for li in lines)
        ratio = revenue / gross if gross > 0 else 0
        for li in lines:
            qty, rev = line_qty(li), line_revenue(li) * ratio
            sku = (li.get("sku") or "").strip()
            name = (li.get("name") or li.get("title") or "").strip()
            pkey = sku.upper() or name
            names.setdefault(pkey, (sku, name))
            p = products[(key[0], key[1], pkey)]
            p["qty"] += qty
            p["revenue"] += rev
            a = all_products[pkey]
            a["qty"] += qty
            a["revenue"] += rev
            a["shops"].add(key[1])
    shop_rows = sorted(shops.values(), key=lambda r: -r["revenue"])
    prod_rows = sorted(({"channel": c, "shop": sh, "sku": names[k][0], "name": names[k][1], **v}
                        for (c, sh, k), v in products.items()),
                       key=lambda r: (-shops[(r["channel"], r["shop"])]["revenue"], -r["revenue"]))
    all_rows = sorted(({"sku": names[k][0], "name": names[k][1], "qty": v["qty"], "revenue": v["revenue"],
                        "shops": len(v["shops"])} for k, v in all_products.items()),
                      key=lambda r: -r["revenue"])
    return {
        "from": f"{start:%Y-%m-%d}", "to": f"{end:%Y-%m-%d}",
        "generated_at": datetime.now(VN).strftime("%Y-%m-%d %H:%M"),
        "total": {"orders": sum(r["orders"] for r in shop_rows), "revenue": sum(r["revenue"] for r in shop_rows),
                  "cancelled": cancelled, "shops": len(shop_rows)},
        "shops": shop_rows, "products": prod_rows, "all_products": all_rows,
    }


def get_report(start, end, progress=None, fresh=False):
    """Bao cao co cache 10 phut (lay ~30 trang don mat 1-2 phut)."""
    key = (start, end)
    hit = _cache.get(key)
    if hit and not fresh and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    orders = fetch_orders(sync.Sapo(), start, end, progress)
    report = build_report(orders, start, end)
    _cache.clear()
    _cache[key] = (time.time(), report)
    return report


# ---------------------------------------------------------------- excel

def to_xlsx(report):
    from openpyxl import Workbook
    from openpyxl.chart import BarChart, Reference
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    head_font, bold = Font(bold=True, color="FFFFFF"), Font(bold=True)
    head_fill = PatternFill("solid", fgColor="3F3F46")
    yu_fill = PatternFill("solid", fgColor="FFF4CC")
    thin = Side(style="thin", color="D4D4D8")
    box = Border(left=thin, right=thin, top=thin, bottom=thin)
    money, count = "#,##0", "#,##0"
    period = f"Từ {datetime.strptime(report['from'], '%Y-%m-%d'):%d/%m/%Y} đến {datetime.strptime(report['to'], '%Y-%m-%d'):%d/%m/%Y}"
    note = "Không tính đơn đã huỷ; doanh thu đã trừ tiền hoàn (trả hàng). Nguồn: đơn hàng Sapo."

    def sheet(ws, title, headers, rows, formats, widths, yu_cols=0):
        ws.append([title]); ws["A1"].font = Font(bold=True, size=14)
        ws.append([period]); ws.append([note]); ws["A3"].font = Font(italic=True, color="71717A")
        ws.append([])
        ws.append(headers)
        hr = ws.max_row
        for i, _ in enumerate(headers, 1):
            c = ws.cell(hr, i)
            c.font, c.fill, c.border = head_font, head_fill, box
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            if i > len(headers) - yu_cols:
                c.fill, c.font = PatternFill("solid", fgColor="A16207"), head_font
        for r in rows:
            ws.append(r)
            for i, f in enumerate(formats, 1):
                c = ws.cell(ws.max_row, i)
                c.border = box
                if f:
                    c.number_format = f
                if i > len(headers) - yu_cols:
                    c.fill = yu_fill
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.row_dimensions[hr].height = 32
        ws.freeze_panes = ws.cell(hr + 1, 1)
        return hr

    wb = Workbook()

    # 1. Tong theo gian hang + 3 cot de trong cho Yu
    ws = wb.active
    ws.title = "1. Theo gian hàng"
    rows = [[r["shop"], r["channel"], r["orders"], round(r["revenue"]), None, None, None] for r in report["shops"]]
    hr = sheet(ws, "Tổng doanh thu theo gian hàng Sàn",
               ["Gian hàng", "Kênh bán hàng", "Số lượng đơn hàng", "Tổng doanh thu (đ)"] + AD_COLUMNS,
               rows, [None, None, count, money, money, money, count], [34, 16, 14, 20, 20, 22, 16], yu_cols=3)
    last = ws.max_row
    t = report["total"]
    ws.append(["TỔNG CỘNG", "", t["orders"], round(t["revenue"])] +
              [f"=SUM({get_column_letter(i)}{hr + 1}:{get_column_letter(i)}{last})" for i in (5, 6, 7)])
    for i, f in enumerate([None, None, count, money, money, money, count], 1):
        c = ws.cell(ws.max_row, i)
        c.font, c.border = bold, box
        if f:
            c.number_format = f
    ws.cell(ws.max_row + 2, 1, "Các cột màu vàng để trống cho Yu điền số liệu quảng cáo.").font = Font(italic=True, color="A16207")
    if report["shops"]:
        chart = BarChart()
        chart.type = "bar"
        chart.title = "Doanh thu theo gian hàng"
        chart.y_axis.title = "Doanh thu (đ)"
        chart.y_axis.numFmt = "#,##0"
        chart.y_axis.majorGridlines = None
        chart.x_axis.scaling.orientation = "maxMin"  # gian hang lon nhat o tren
        chart.legend = None
        chart.add_data(Reference(ws, min_col=4, min_row=hr, max_row=last), titles_from_data=True)
        chart.set_categories(Reference(ws, min_col=1, min_row=hr + 1, max_row=last))
        chart.series[0].graphicalProperties.solidFill = "3F3F46"
        chart.height, chart.width = max(8, 0.75 * len(report["shops"]) + 3), 22
        ws.add_chart(chart, "I5")

    # 2a. San pham theo tung gian hang
    ws = wb.create_sheet("2a. SP theo gian hàng")
    sheet(ws, "Doanh thu theo sản phẩm — chi tiết từng gian hàng",
          ["Gian hàng", "Kênh bán hàng", "SKU", "Tên sản phẩm", "Số lượng", "Tổng doanh thu (đ)"],
          [[r["shop"], r["channel"], r["sku"], r["name"], r["qty"], round(r["revenue"])] for r in report["products"]],
          [None, None, None, None, count, money], [30, 14, 14, 60, 10, 20])
    ws.auto_filter.ref = f"A5:F{ws.max_row}"

    # 2b. San pham tong tat ca gian
    ws = wb.create_sheet("2b. SP tổng tất cả gian")
    sheet(ws, "Doanh thu theo sản phẩm — tổng tất cả các gian hàng",
          ["SKU", "Tên sản phẩm", "Số lượng", "Tổng doanh thu (đ)", "Số gian hàng có bán"],
          [[r["sku"], r["name"], r["qty"], round(r["revenue"]), r["shops"]] for r in report["all_products"]],
          [None, None, count, money, count], [14, 64, 10, 20, 14])
    ws.auto_filter.ref = f"A5:E{ws.max_row}"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


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
