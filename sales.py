"""Bao cao doanh thu cac gian hang San (Shopee, TikTok Shop...) lay tu don hang Sapo.

Don San duoc Sapo dong bo ve qua kenh ban hang; moi don co channel_definition.branch_name = ten gian hang.
Quy uoc so lieu:
- Ngay = ngay tao don tren Sapo, gio Viet Nam.
- Bo don da huy. Doanh thu = tong tien don sau giam gia, tru phan da hoan tien (tra hang).
- Doanh thu san pham = don gia sau giam gia x so luong con lai (sau tra hang).

Chay tay:  python sales.py 2026-09-01 2026-11-30   -> reports/doanhthu_san_<tu>_<den>.xlsx
"""
import io
import re
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
CACHE_SECONDS = 600
TOP_QTY = 30  # "Top san pham ban hon 30 luot" (Yu: 30 cai, tinh theo tuan)
TOP_N = 10    # sheet Top san pham: it nhat 10 SP moi gian (file FORM_REPORT_SHOPEE_ADS_MAU: "Top 10 san pham ban chay")
# Thuong hieu = chu dau ten san pham (Sapo khong luu thuong hieu). Chu chung chung o dau -> tim tiep trong ten.
GENERIC_WORDS = {"TRIPOD", "DOUBLE", "COMBO", "BO", "SET", "KIT", "DEN", "GIA", "TAY", "CHAN", "MIC", "HANG", "NEW", "KINGCOM"}
BRAND_ALIASES = {"ULANZL": "ULANZI", "BOYAMIC": "BOYA", "BOYAMINI": "BOYA", "BOYALINK": "BOYA"}
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
        # Nhom 1 file yeu cau: SP ban chay nhat, SP >= 30 cai, thuong hieu ban chay (theo so luong)
        per_shop = defaultdict(list)
        for r in prod_rows:
            per_shop[(r["channel"], r["shop"])].append(r)
        names = brand_names(all_rows)
        for r in shop_rows:
            rows = per_shop.get((r["channel"], r["shop"]), [])
            r["bestseller"], r["top_qty"] = bestsellers(rows)
            r["brands"] = brand_stats(rows, 3, names)
        total_best, total_top = bestsellers(all_rows)
        return {
            "top_qty_min": TOP_QTY,
            "from": f"{self.start:%Y-%m-%d}", "to": f"{self.end:%Y-%m-%d}",
            "generated_at": datetime.now(VN).strftime("%Y-%m-%d %H:%M"),
            "total": {"orders": sum(r["orders"] for r in shop_rows), "revenue": sum(r["revenue"] for r in shop_rows),
                      "cancelled": self.cancelled, "shops": len(shop_rows), "months": total_months,
                      "bestseller": total_best, "top_qty": total_top, "brands": brand_stats(all_rows, 10, names)},
            "shops": shop_rows, "products": prod_rows, "all_products": all_rows,
        }


def _plain(s):
    import unicodedata
    s = unicodedata.normalize("NFD", s).replace("đ", "d").replace("Đ", "D")
    return "".join(ch for ch in s if unicodedata.category(ch) != "Mn").upper()


def _words(name):
    name = re.sub(r"^\s*([\[(【][^\])】]*[\])】]\s*)+", "", name or "")  # bo [KINGCOM], (Hang moi)... o dau
    return re.findall(r"[^\W_]+", name)


def brand_key(name, known=None):
    """Thuong hieu cua san pham (chu in hoa, da gop ten viet sai) hoac '' neu khong doan duoc."""
    words = _words(name)
    for i, w in enumerate(words[:6]):
        k = BRAND_ALIASES.get(_plain(w), _plain(w))
        if k in GENERIC_WORDS or k.isdigit() or len(k) < 2:
            continue
        if i == 0 or (known is not None and k in known):
            return k
        if known is None:
            return ""  # chi lay chu dau khi chua co danh sach thuong hieu
    return ""


def brand_names(rows):
    """{thuong hieu: cach viet pho bien nhat} tinh tren toan bo san pham -> moi gian, moi sheet viet giong nhau."""
    return {brand_key(b["brand"]) or b["brand"]: b["brand"] for b in brand_stats(rows, 10 ** 6)}


def brand_stats(rows, top=5, names=None):
    """Xep hang thuong hieu theo so luong ban ra (Yu: bestseller theo so luong). rows: [{name, qty, revenue}].
    names: ket qua brand_names() de thong nhat cach viet."""
    known = {k for k in (brand_key(r["name"]) for r in rows) if k}
    agg, spell = {}, {}
    for r in rows:
        k = brand_key(r["name"], known) or "Khác"
        a = agg.setdefault(k, [0, 0.0, 0])
        a[0] += r["qty"]; a[1] += r["revenue"]; a[2] += 1
        w = next((x for x in _words(r["name"]) if BRAND_ALIASES.get(_plain(x), _plain(x)) == k), None)
        if w:
            spell.setdefault(k, {}).setdefault(w, 0)
            spell[k][w] += 1
    show = lambda k: (names or {}).get(k) or (max(spell[k].items(), key=lambda kv: kv[1])[0] if k in spell else k)
    out = sorted(({"brand": show(k), "qty": v[0], "revenue": v[1], "products": v[2]} for k, v in agg.items()),
                 key=lambda b: (-b["qty"], -b["revenue"]))
    return out[:top]


def bestsellers(rows):
    """Ban chay nhat (theo so luong) + cac SP ban >= TOP_QTY cai trong ky."""
    rows = sorted((r for r in rows if r["qty"] > 0), key=lambda r: (-r["qty"], -r["revenue"]))
    pick = lambda r: {"sku": r["sku"], "name": r["name"], "qty": r["qty"], "revenue": r["revenue"]}
    return (pick(rows[0]) if rows else None), [pick(r) for r in rows if r["qty"] >= TOP_QTY]


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
# Trinh bay theo file mau sep gui FORM_REPORT_SHOPEE_ADS_MAU.xlsx (25/11/2026): sheet Tong Quan / Chi Tiet / Top Sell,
# font Aptos, thanh tieu de xanh dam, hang ky bao cao, nhom cot mau, dong xen ke, dong Tong, khoi chu thich cong thuc.

NAVY, BLUE, TEAL, SLATE = "172B4D", "285D91", "147D98", "526781"
ZEBRA, TOTAL_BG, TEXT, MUTED, YELLOW, LINE = "F3F7FB", "EAF1F7", "203044", "66788D", "FFF1D2", "C9D3DF"
MONEY, PCT, RATIO = "#,##0", "0.0%", "0.00"
EXCEL_KINDS = {"product": "Hiển thị sản phẩm", "shop": "Quảng cáo Shop"}  # cach goi trong file mau


class XL:
    """Bo style dung chung cho cac sheet (theo file mau)."""

    def __init__(self):
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        from openpyxl.utils import get_column_letter
        self.A, self.Font, self.col = Alignment, Font, get_column_letter
        self.fill = lambda c: PatternFill("solid", fgColor=c)
        side = Side(style="thin", color=LINE)
        self.box = Border(left=side, right=side, top=side, bottom=side)

    def font(self, size=10, bold=False, color=TEXT, italic=False):
        return self.Font(name="Aptos", size=size, bold=bold, color=color, italic=italic)

    def bar(self, ws, row, text, ncols, fill=NAVY, size=16, height=40, start=1):
        """Thanh tieu de (gop o, nen mau, chu trang dam)."""
        if ncols > 1:
            ws.merge_cells(start_row=row, start_column=start, end_row=row, end_column=start + ncols - 1)
        for i in range(start, start + ncols):
            c = ws.cell(row, i)
            c.fill, c.font = self.fill(fill), self.font(size, True, "FFFFFF")
            c.alignment = self.A(horizontal="left", vertical="center", indent=1)
        ws.cell(row, start, text)
        ws.row_dimensions[row].height = height

    def info(self, ws, row, ncols, label, value, extra=None):
        """Hang 'Ky bao cao' nen xanh nhat."""
        for i in range(1, ncols + 1):
            c = ws.cell(row, i)
            c.fill, c.font = self.fill(ZEBRA), self.font(color=SLATE)
            c.alignment = self.A(vertical="center")
        a, b = ws.cell(row, 1, label), ws.cell(row, 2, value)
        a.font, b.font = self.font(bold=True, color=BLUE), self.font(bold=True)
        b.border = self.box
        if extra:
            e = ws.cell(row, 4, extra)
            e.font = self.font(9.5, color=MUTED, italic=True)
        ws.row_dimensions[row].height = 29

    def groups(self, ws, row, groups):
        """Hang nhom cot: [(nhan, so cot, mau)]."""
        col = 1
        for label, n, color in groups:
            if n > 1:
                ws.merge_cells(start_row=row, start_column=col, end_row=row, end_column=col + n - 1)
            for i in range(col, col + n):
                c = ws.cell(row, i)
                c.fill, c.font, c.border = self.fill(color), self.font(bold=True, color="FFFFFF"), self.box
                c.alignment = self.A(horizontal="center", vertical="center", wrap_text=True)
            ws.cell(row, col, label)
            col += n
        ws.row_dimensions[row].height = 29

    def header(self, ws, row, cols, height=62, size=10):
        """cols: [(tieu de, dinh dang, rong, can le, mau nen)]."""
        for i, (h, _, w, _, color) in enumerate(cols, 1):
            c = ws.cell(row, i, h)
            c.fill, c.font, c.border = self.fill(color), self.font(size, True, "FFFFFF"), self.box
            c.alignment = self.A(horizontal="center", vertical="center", wrap_text=True)
            ws.column_dimensions[self.col(i)].width = w
        ws.row_dimensions[row].height = height

    def row(self, ws, row, cols, values, zebra=False, total=False, height=28, styles=None):
        """Ghi 1 dong du lieu. styles: {chi so cot: dict(font=..., fill=...)} de doi rieng tung o."""
        bg = TOTAL_BG if total else (ZEBRA if zebra else "FFFFFF")
        for i, ((_, fmt, _, align, _), v) in enumerate(zip(cols, values), 1):
            c = ws.cell(row, i, v)
            c.fill, c.border = self.fill(bg), self.box
            c.font = self.font(bold=True, color=NAVY) if total else self.font()
            c.alignment = self.A(horizontal=align, vertical="center", wrap_text=align == "left")
            if fmt:
                c.number_format = fmt
            for k, val in ((styles or {}).get(i - 1) or {}).items():
                setattr(c, k, val)
        ws.row_dimensions[row].height = height

    def notes(self, ws, row, ncols, lines, title="CHÚ THÍCH CÔNG THỨC & CHỈ SỐ:"):
        self.bar(ws, row, title, ncols, size=11, height=28)
        for k, text in enumerate(lines, row + 1):
            ws.merge_cells(start_row=k, start_column=1, end_row=k, end_column=ncols)
            c = ws.cell(k, 1, "• " + text)
            c.fill, c.font = self.fill(ZEBRA), self.font(9.5, color=SLATE)
            c.alignment = self.A(horizontal="left", vertical="center", wrap_text=True, indent=1)
            ws.row_dimensions[k].height = 26


def to_xlsx(report, ads_analysis=None):
    """Excel chi gian Shopee, trinh bay theo file mau FORM_REPORT_SHOPEE_ADS_MAU:
    1. Tong Quan: moi gian 1 dong (thong tin gian / hieu qua kinh doanh / hieu qua Shopee Ads / chi tiet bo sung) + dong Tong
    2. Chi Tiet: tung san pham, QC Shop, tu khoa (khi co file Quang cao Shopee cung ky)
    3. Top Sell: moi gian 1 khoi Top 10 san pham ban chay (theo so luong)
    4. Thuong hieu: thuong hieu ban chay tat ca gian + tung gian"""
    import ads
    from openpyxl import Workbook
    x = XL()
    d1, d2 = (datetime.strptime(report[k], "%Y-%m-%d") for k in ("from", "to"))
    period = f"{d1:%Y/%m/%d} - {d2:%Y/%m/%d}"
    wb = Workbook()
    ad_shops = ads.by_shop(ads_analysis) if ads_analysis and ads_analysis.get("has_sales") else {}
    ws = wb.active
    ws.title = "Tổng Quan"
    tracker_sheet(ws, report, ad_shops, x, period)
    if ad_shops:
        ads_sheet(wb.create_sheet("Chi Tiết"), ads_analysis, x, period)
    names = brand_names(report["all_products"])
    top_sheet(wb.create_sheet("Top Sell"), report, x, names, period)
    brand_sheet(wb.create_sheet("Thương hiệu"), report, x, names, period)
    buf = io.BytesIO()
    wb.save(buf)
    return fix_core_dates(buf.getvalue())


def _shopee_shops(report):
    """[(gian, [san pham cua gian])] cac gian Shopee, gian doanh thu lon truoc."""
    prods = defaultdict(list)
    for p in report["products"]:
        if p["channel"] == "Shopee":
            prods[p["shop"]].append(p)
    return [(r, prods.get(r["shop"], [])) for r in report["shops"] if r["channel"] == "Shopee"]


def tracker_sheet(ws, report, ad_shops, x, period):
    """Sheet Tong Quan (file mau) + cac cot bo sung cua tool o nhom cuoi."""
    import ads
    sm = ads.salesmen()
    vn = lambda v: f"{round(v):,}".replace(",", ".")
    div = lambda a, b: a / b if a is not None and b else None  # ty le (Excel dinh dang %), None neu khong tinh duoc
    aov = lambda rev, n: round(rev / n) if n else None
    tot = {"orders": 0, "revenue": 0.0, "sp_cost": 0.0, "shop_cost": 0.0, "cost": 0.0, "conv": 0.0, "gmv": 0.0,
           "mapped_cost": 0.0, "sapo_rev": 0.0, "impr": 0.0, "clicks": 0.0, "ad_orders_base": 0, "ad_rev_base": 0.0}
    has_any_ads = has_any_sp = has_any_shop = False

    def ad_cols(orders, revenue, cost, conv, gmv):
        """Nhom HIEU QUA SHOPEE ADS dung thu tu file mau."""
        return [cost, conv, gmv, div(cost, conv), div(conv, orders), div(gmv, revenue), div(gmv, cost), div(cost, gmv), div(cost, revenue)]

    def extra_cols(sp_cost, shop_cost, impr, clicks, conv, sapo_rev, mapped_cost):
        return [sp_cost, shop_cost, impr, clicks, div(clicks, impr), div(conv, clicks), sapo_rev, div(mapped_cost, sapo_rev)]

    rows = []
    for r in report["shops"]:
        if r["channel"] != "Shopee":
            continue
        a = ad_shops.get(ads.norm(r["shop"]))
        best = r.get("bestseller")
        brands = ", ".join(f"{b['brand']} ({vn(b['qty'])})" for b in r.get("brands", []))
        orders, revenue = r["orders"], round(r["revenue"])
        tot["orders"] += orders
        tot["revenue"] += revenue
        row = [r["shop"], ads.salesman_of(r["shop"], sm), "Shopee", orders, revenue, aov(revenue, orders)]
        sell = [f"{best['name']} ({vn(best['qty'])} cái)" if best else None, brands or None, len(r.get("top_qty", []))]
        if a:
            has_any_ads = True
            sa = a.get("shop_ads") or {}
            has_sp = bool(a.get("product_ads"))  # chua tai file QC San pham -> de trong, khong ghi 0
            has_any_sp |= has_sp
            has_any_shop |= bool(sa)
            sp_cost = round(a["cost"] - sa.get("cost", 0)) if has_sp else None
            shop_cost = round(sa.get("cost", 0)) if sa else None
            sapo_rev = round(a.get("sapo_revenue") or 0) if has_sp else None
            mapped = a.get("mapped_cost", 0.0) if has_sp else None
            row += ad_cols(orders, revenue, round(a["cost"]), a["conversions"], round(a["gmv"]))
            row += extra_cols(sp_cost, shop_cost, a["impressions"], a["clicks"], a["conversions"], sapo_rev, mapped) + sell
            row.append("; ".join(f["text"] for f in a["flags"] + [dict(f, text="QC Shop: " + f["text"]) for f in sa.get("flags", [])]))
            for k, v in (("sp_cost", sp_cost), ("shop_cost", shop_cost), ("cost", round(a["cost"])), ("conv", a["conversions"]),
                         ("gmv", round(a["gmv"])), ("sapo_rev", sapo_rev), ("mapped_cost", mapped),
                         ("impr", a["impressions"]), ("clicks", a["clicks"])):
                tot[k] += v or 0
            tot["ad_orders_base"] += orders
            tot["ad_rev_base"] += revenue
        else:
            row += [None] * 9 + [None] * 8 + sell + ["Chưa có file Quảng cáo Shopee cùng kỳ"]
        rows.append(row)
    t = tot
    total = ["Tổng", "", "Shopee", t["orders"], t["revenue"], aov(t["revenue"], t["orders"])]
    if has_any_ads:  # ty le QC chi tinh tren cac gian co file QC
        total += ad_cols(t["ad_orders_base"], t["ad_rev_base"], t["cost"], t["conv"], t["gmv"])
        total += extra_cols(t["sp_cost"] if has_any_sp else None, t["shop_cost"] if has_any_shop else None, t["impr"], t["clicks"],
                            t["conv"], t["sapo_rev"] if has_any_sp else None, t["mapped_cost"] if has_any_sp else None)
    else:
        total += [None] * 17
    total += [None, None, None, None]

    L, C, R = "left", "center", "right"
    cols = [("Gian hàng", None, 26, L, BLUE), ("Saleman", None, 14, L, BLUE), ("Nền tảng", None, 11, C, BLUE),
            ("Tổng số đơn", MONEY, 11, C, BLUE), ("Doanh thu gian hàng (đ)", MONEY, 18, R, BLUE), ("AOV (gian hàng)", MONEY, 15, R, BLUE),
            ("Chi phí quảng cáo (đ)", MONEY, 16, R, TEAL), ("Đơn hàng từ Ads", MONEY, 11, C, TEAL),
            ("Doanh thu từ Shopee Ads (VNĐ)", MONEY, 18, R, TEAL), ("CPO", MONEY, 12, R, TEAL),
            ("Tỷ trọng đơn hàng từ Ads", PCT, 12, R, TEAL), ("Tỷ trọng DT Ads (%)", PCT, 12, R, TEAL),
            ("ROAS (Lần)", RATIO, 10, R, TEAL), ("ACOS (%)", PCT, 10, R, TEAL), ("TACOS (%)", PCT, 10, R, TEAL),
            ("Chi phí QC Sản phẩm (đ)", MONEY, 15, R, SLATE), ("Chi phí QC Shop (đ)", MONEY, 14, R, SLATE),
            ("Lượt hiển thị", MONEY, 12, R, SLATE), ("Lượt click", MONEY, 10, R, SLATE), ("CTR", "0.00%", 9, R, SLATE),
            ("CR", "0.00%", 9, R, SLATE), ("Doanh thu Sapo SP chạy QC (đ)", MONEY, 16, R, SLATE),
            ("TACOS SP chạy QC", PCT, 11, R, SLATE), ("Bán chạy nhất (số lượng)", None, 38, L, SLATE),
            ("Thương hiệu bán chạy (số lượng)", None, 32, L, SLATE), (f"Số SP bán ≥ {TOP_QTY} cái", MONEY, 11, C, SLATE),
            ("Đánh giá tự động", None, 60, L, SLATE)]
    n = len(cols)
    x.bar(ws, 1, "TỔNG DOANH THU CÁC SÀN THƯƠNG MẠI ĐIỆN TỬ", n, size=17, height=39)
    x.info(ws, 2, n, "Kỳ báo cáo (Tuần):", period,
           "Nguồn: đơn hàng Sapo (không tính đơn huỷ, đã trừ tiền hoàn) + file Quảng cáo Shopee cùng kỳ. Chỉ gian Shopee.")
    x.groups(ws, 4, [("THÔNG TIN GIAN HÀNG", 3, SLATE), ("HIỆU QUẢ KINH DOANH TỔNG", 3, BLUE),
                     ("HIỆU QUẢ SHOPEE ADS (QUẢNG CÁO)", 9, TEAL), ("CHI TIẾT BỔ SUNG (TOOL)", n - 15, SLATE)])
    x.header(ws, 5, cols)
    shop_style = {0: {"font": x.font(bold=True, color=BLUE)}, 1: {"font": x.font(color=MUTED)}}
    for k, v in enumerate(rows):
        x.row(ws, 6 + k, cols, v, zebra=k % 2 == 1, height=34, styles=shop_style)
    x.row(ws, 6 + len(rows), cols, total, total=True)
    x.notes(ws, 8 + len(rows), 15, [
        "Giá trị đơn hàng bình quân (AOV) = Tổng doanh thu / Số lượng đơn hàng.",
        "ROAS (Return on Ad Spend) = Doanh thu từ Shopee Ads / Ngân sách quảng cáo (chỉ số càng cao càng tốt).",
        "ACOS (Advertising Cost of Sales) = Chi phí quảng cáo / Doanh thu từ Shopee Ads (chỉ số càng thấp càng tốt).",
        "Tỷ trọng DT Ads (%) = Doanh thu từ Shopee Ads / Tổng doanh thu toàn gian hàng.  Tỷ trọng đơn hàng từ Ads = Đơn hàng từ Ads / Tổng số đơn.",
        "CPO (Cost Per Order) – chi phí quảng cáo trên mỗi đơn hàng = Tổng chi phí Shopee Ads / Số đơn hàng từ Ads.",
        "TACOS (Total Advertising Cost Of Sales) = Chi phí Shopee Ads / Tổng doanh thu gian hàng.",
        "TACOS SP chạy QC = chi phí QC Sản phẩm / doanh thu Sapo của các SP chạy QC (công thức Yu, tốt 8–12%; dùng cho đánh giá tự động).",
        "Doanh thu, số đơn: đơn Sapo, không tính đơn huỷ, đã trừ tiền hoàn. Số liệu quảng cáo theo file Shopee (chưa trừ huỷ, hoàn).",
        "Dòng Tổng: các tỷ lệ quảng cáo chỉ tính trên những gian đã có file Quảng cáo Shopee cùng kỳ.",
    ])
    ws.freeze_panes = "B6"
    ws.sheet_view.zoomScale = 110


def ads_sheet(ws, a, x, period):
    """Sheet Chi Tiet (file mau): tung san pham / QC Shop / tu khoa, moi gian ket thuc bang dong Tong gian."""
    T = a["thresholds"]
    pct = lambda v: None if v is None else v / 100
    rnd = lambda v: None if v is None else round(v)

    def line(shop, kind, name, sku, m, sapo=True):
        return [shop, kind, name, sku, m["sold"], round(m["gmv"]), round(m["cost"]), m["impressions"], m["clicks"], pct(m["ctr"]),
                m["add_to_cart"], pct(m.get("atc_rate")), m["conversions_direct"], pct(m.get("cr_direct")), rnd(m.get("cpo_direct")),
                m["roas_direct"], pct(m.get("acos_direct")),
                m["conversions"], pct(m["cr"]), m["roas"], pct(m.get("acos")),
                rnd(m.get("sapo_revenue")) if sapo else None, pct(m.get("tacos")) if sapo else None,
                "; ".join(f["text"] for f in m["flags"])]

    L, C, R = "left", "center", "right"
    cols = [("Gian hàng", None, 20, L, BLUE), ("Loại hình quảng cáo", None, 19, L, BLUE), ("Tên chiến dịch", None, 42, L, BLUE),
            ("SKU", None, 15, C, BLUE), ("Sản phẩm đã bán", MONEY, 11, C, BLUE), ("Doanh số", MONEY, 15, R, BLUE),
            ("Chi phí", MONEY, 13, R, BLUE), ("Số lượt xem", MONEY, 11, R, BLUE), ("Số lượt click", MONEY, 10, R, BLUE),
            ("Tỷ Lệ Click", "0.00%", 10, R, BLUE), ("Thêm vào giỏ hàng", MONEY, 11, R, BLUE),
            ("Tỷ lệ Thêm vào giỏ hàng", "0.00%", 12, R, BLUE), ("Lượt chuyển đổi trực tiếp", MONEY, 12, C, BLUE),
            ("Tỷ lệ chuyển đổi trực tiếp", "0.00%", 12, R, BLUE), ("Chi phí cho mỗi lượt chuyển đổi trực tiếp", MONEY, 15, R, BLUE),
            ("ROAS trực tiếp", RATIO, 10, R, BLUE), ("ACOS trực tiếp", PCT, 10, R, BLUE),
            ("Đơn từ QC (tổng)", MONEY, 10, C, TEAL), ("CR", "0.00%", 9, R, TEAL), ("ROAS", RATIO, 9, R, TEAL),
            ("ACOS", PCT, 9, R, TEAL), ("Doanh thu Sapo SP chạy QC (đ)", MONEY, 15, R, TEAL), ("TACOS", PCT, 9, R, TEAL),
            ("Đánh giá tự động", None, 60, L, TEAL)]
    n = len(cols)
    x.bar(ws, 1, "CHI TIẾT CHIẾN DỊCH QUẢNG CÁO", n, size=16, height=41)
    x.info(ws, 2, n, "Thời gian:", period, "Cột tiêu đề xanh ngọc (từ “Đơn từ QC” trở đi): chỉ số bổ sung của tool (tính cả chuyển đổi gián tiếp, ghép doanh thu Sapo).")
    x.header(ws, 4, cols, height=67)
    kind_style = {1: {"fill": x.fill(YELLOW)}}
    k, r = 0, 5
    for s in a["shops"]:
        lines = [line(s["shop"], EXCEL_KINDS["product"], p["name"], ", ".join(p["skus"]), p) for p in s["products"]]
        sa = s.get("shop_ads")
        if sa:
            lines.append(line(s["shop"], EXCEL_KINDS["shop"], ", ".join(sa["campaigns"]) or EXCEL_KINDS["shop"], "(cả gian)", sa))
            for kw in sa.get("keywords", []):
                lines.append(line(s["shop"], EXCEL_KINDS["shop"], f"   Từ khóa: {kw['keyword']}" + (f" ({kw['keyword_type']})" if kw["keyword_type"] else ""),
                                  "", kw, sapo=False))
        for i, v in enumerate(lines):
            if i:
                v[0] = None  # file mau: ten gian chi ghi o dong dau cua gian
            x.row(ws, r, cols, v, zebra=k % 2 == 1, height=40, styles={**kind_style, 0: {"font": x.font(bold=True, color=BLUE)}})
            r += 1
            k += 1
        x.row(ws, r, cols, line(f"Tổng {s['shop']}", "Tổng gian hàng" + (f" · {s['saleman']}" if s.get("saleman") else ""), "", "",
                                dict(s, sapo_revenue=s.get("sapo_revenue") or 0)), total=True, height=40)
        r += 1
        k = 0
    x.notes(ws, r + 1, 17, [
        "Tỷ Lệ Click = số lượt click / số lượt xem.  Tỷ lệ thêm vào giỏ hàng = thêm vào giỏ / số lượt click.",
        "Lượt chuyển đổi trực tiếp = đơn của chính sản phẩm được quảng cáo.  Tỷ lệ chuyển đổi trực tiếp = lượt chuyển đổi trực tiếp / số lượt click.",
        "Chi phí cho mỗi lượt chuyển đổi trực tiếp = chi phí / lượt chuyển đổi trực tiếp.  ROAS trực tiếp = doanh số trực tiếp / chi phí.  ACOS trực tiếp = chi phí / doanh số trực tiếp.",
        "Đơn từ QC (tổng), CR, ROAS, ACOS: tính cả chuyển đổi gián tiếp (khách bấm QC rồi mua sản phẩm khác của gian).",
        f"TACOS = chi phí QC / doanh thu Sapo của SKU đã gán (tốt: {T['tacos_low']:g}–{T['tacos_high']:g}%); riêng Quảng cáo Shop chia cho doanh thu cả gian.",
        "Doanh số QC lấy theo Shopee (chưa trừ huỷ, hoàn); doanh thu Sapo đã trừ.",
    ])
    ws.freeze_panes = "D5"


def top_sheet(ws, report, x, names, period):
    """Sheet Top Sell (file mau): moi gian 1 khoi Top san pham ban chay theo so luong (it nhat TOP_N, du cac SP >= TOP_QTY)."""
    import ads
    sm = ads.salesmen()
    C, L, R = "center", "left", "right"
    cols = [("SKU", None, 20, C, TEAL), ("Tên Sản Phẩm", None, 54, L, TEAL), ("Số Lượng", MONEY, 18, C, TEAL),
            ("Doanh Thu", MONEY, 24, R, TEAL), ("Thương hiệu", None, 16, C, TEAL)]
    r = 2
    for shop, prods in _shopee_shops(report):
        known = {k for k in (brand_key(p["name"]) for p in prods) if k}
        ranked = sorted((p for p in prods if p["qty"] > 0), key=lambda p: (-p["qty"], -p["revenue"]))
        n = max(TOP_N, sum(1 for p in ranked if p["qty"] >= TOP_QTY))
        saleman = ads.salesman_of(shop["shop"], sm)
        x.bar(ws, r, shop["shop"].upper(), len(cols), size=16, height=42)
        x.bar(ws, r + 1, f"TOP {min(n, len(ranked)) or TOP_N} SẢN PHẨM BÁN CHẠY" + (f"  ·  Saleman: {saleman}" if saleman else "")
              + f"  ·  {period}", len(cols), fill=BLUE, size=11, height=32)
        x.header(ws, r + 3, cols, height=39, size=11)
        for i, p in enumerate(ranked[:n]):
            brand = names.get(brand_key(p["name"], known), brand_key(p["name"], known)) or "Khác"
            bold = {j: {"font": x.font(bold=True)} for j in range(len(cols))} if p["qty"] >= TOP_QTY else None
            x.row(ws, r + 4 + i, cols, [p["sku"], p["name"], p["qty"], round(p["revenue"]), brand], zebra=i % 2 == 1, height=26, styles=bold)
        if not ranked:
            ws.cell(r + 4, 1, "Không có sản phẩm bán ra trong kỳ").font = x.font(italic=True, color=MUTED)
        r += 4 + max(len(ranked[:n]), 1) + 2
    ws.cell(r, 1, f"In đậm: sản phẩm bán ≥ {TOP_QTY} cái trong kỳ. Xếp theo số lượng bán (không tính đơn huỷ, đã trừ hoàn).").font = \
        x.font(9.5, color=MUTED, italic=True)


def brand_sheet(ws, report, x, names, period):
    """Thuong hieu ban chay (theo so luong) - tat ca gian Shopee + tung gian."""
    import ads
    sm, shops = ads.salesmen(), _shopee_shops(report)
    merged = {}
    for _, prods in shops:  # gop SP cua cac gian theo SKU de dem so SP khong trung
        for p in prods:
            m = merged.setdefault(p["sku"].upper() or p["name"], {"name": p["name"], "qty": 0, "revenue": 0.0})
            m["qty"] += p["qty"]; m["revenue"] += p["revenue"]
    L, C, R = "left", "center", "right"
    cols = [("Gian hàng", None, 26, L, BLUE), ("Saleman", None, 14, L, BLUE), ("Hạng", None, 7, C, BLUE),
            ("Thương hiệu", None, 20, L, BLUE), ("Số lượng", MONEY, 12, C, BLUE), ("Doanh thu (đ)", MONEY, 18, R, BLUE),
            ("Số sản phẩm", MONEY, 12, C, BLUE)]
    x.bar(ws, 1, "THƯƠNG HIỆU BÁN CHẠY", len(cols), size=16, height=40)
    x.info(ws, 2, len(cols), "Kỳ báo cáo:", period)
    x.header(ws, 4, cols, height=40)
    r = 5
    blocks = [("TẤT CẢ GIAN SHOPEE", "", list(merged.values()), 10)] + [(s["shop"], ads.salesman_of(s["shop"], sm), ps, 5) for s, ps in shops]
    for label, saleman, prods, top in blocks:
        for i, b in enumerate(brand_stats(prods, top, names)):
            x.row(ws, r, cols, [label if i == 0 else None, saleman if i == 0 else None, i + 1, b["brand"], b["qty"], round(b["revenue"]), b["products"]],
                  zebra=i % 2 == 1, height=24,
                  styles={0: {"font": x.font(bold=True, color=BLUE)}, 1: {"font": x.font(color=MUTED)}})
            r += 1
    x.notes(ws, r + 1, len(cols), ["Xếp theo số lượng bán. Thương hiệu lấy theo chữ đầu tên sản phẩm (gộp các cách viết sai)."], title="GHI CHÚ:")
    ws.freeze_panes = "A5"


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






def xlsx_name(report):
    return f"bao_cao_shopee_ads_{report['from']}_{report['to']}.xlsx"


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
