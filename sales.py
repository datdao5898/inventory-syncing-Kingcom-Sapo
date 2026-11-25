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

def to_xlsx(report, ads_analysis=None):
    """Excel theo file yeu cau BAO_CAO_SHOPEE_DEMO_AI + tra loi cua Yu (24/11/2026). Chi gian Shopee.
    1. Theo doi tuan (Shopee Ads Weekly Tracker): moi gian 1 dong, du 4 nhom
    2. Quang cao Shopee: chi tiet QC San pham / QC Shop / tu khoa (khi co file Shopee Ads cung ky)
    3. Top san pham: tung gian, xep theo so luong, danh dau SP ban >= TOP_QTY cai
    4. Thuong hieu: thuong hieu ban chay tung gian + tat ca gian (theo so luong)"""
    import ads
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    head_font = Font(bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor="3F3F46")
    thin = Side(style="thin", color="D4D4D8")
    box = Border(left=thin, right=thin, top=thin, bottom=thin)
    d1, d2 = (datetime.strptime(report[k], "%Y-%m-%d") for k in ("from", "to"))
    period = f"Từ {d1:%d/%m/%Y} đến {d2:%d/%m/%Y}"
    note = "Chỉ gian Shopee. Không tính đơn đã huỷ; doanh thu đã trừ tiền hoàn (trả hàng). Nguồn: đơn hàng Sapo + file Quảng cáo Shopee."

    def sheet(ws, title, headers, rows, formats, widths, top=1):
        ws.cell(top, 1, title).font = Font(bold=True, size=14)
        ws.cell(top + 1, 1, period)
        ws.cell(top + 2, 1, note).font = Font(italic=True, color="71717A")
        hr = top + 4
        for i, h in enumerate(headers, 1):
            c = ws.cell(hr, i, h)
            c.font, c.fill, c.border = head_font, head_fill, box
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        for k, r in enumerate(rows, hr + 1):
            for i, v in enumerate(r, 1):
                ws.cell(k, i, v)
            for i, f in enumerate(formats, 1):
                c = ws.cell(k, i)
                c.border = box
                if f:
                    c.number_format = f
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.row_dimensions[hr].height = 44
        ws.freeze_panes = ws.cell(hr + 1, 1)
        return hr

    wb = Workbook()
    ad_shops = ads.by_shop(ads_analysis) if ads_analysis and ads_analysis.get("has_sales") else {}
    ws = wb.active
    ws.title = "Theo dõi tuần"
    tracker_sheet(ws, report, ad_shops, sheet)
    if ad_shops:
        ads_sheet(wb.create_sheet("Quảng cáo Shopee"), ads_analysis, sheet, period)
    names = brand_names(report["all_products"])
    top_sheet(wb.create_sheet("Top sản phẩm"), report, sheet, names)
    brand_sheet(wb.create_sheet("Thương hiệu"), report, sheet, names)
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


def top_sheet(ws, report, sheet, names):
    """Nhom 1: top san pham tung gian theo so luong; luon co it nhat TOP_N SP dau (file mau: Top 10 ban chay)."""
    import ads
    from openpyxl.styles import Font
    sm, rows, best_rows = ads.salesmen(), [], []
    for r, prods in _shopee_shops(report):
        known = {k for k in (brand_key(p["name"]) for p in prods) if k}
        ranked = sorted((p for p in prods if p["qty"] > 0), key=lambda p: (-p["qty"], -p["revenue"]))
        n = max(TOP_N, sum(1 for p in ranked if p["qty"] >= TOP_QTY))
        for i, p in enumerate(ranked[:n], 1):
            if i == 1:
                best_rows.append(len(rows))
            rows.append([r["shop"], ads.salesman_of(r["shop"], sm), i, p["sku"], p["name"], names.get(brand_key(p["name"], known), brand_key(p["name"], known)) or "Khác",
                         p["qty"], round(p["revenue"]), "✓" if p["qty"] >= TOP_QTY else ""])
    hr = sheet(ws, f"Top sản phẩm bán chạy từng gian (theo số lượng; ✓ = bán ≥ {TOP_QTY} cái)",
               ["Gian hàng", "Saleman", "Hạng", "SKU", "Tên sản phẩm", "Thương hiệu", "Số lượng", "Doanh thu (đ)", f"≥ {TOP_QTY} cái"],
               rows, [None, None, "0", None, None, None, "#,##0", "#,##0", None], [24, 11, 6, 12, 60, 14, 10, 16, 9])
    bold = Font(bold=True)
    for k in best_rows:  # hang 1 = bestseller
        for c in ws[hr + 1 + k]:
            c.font = bold
    if rows:
        ws.auto_filter.ref = f"A{hr}:I{hr + len(rows)}"


def brand_sheet(ws, report, sheet, names):
    """Nhom 1: thuong hieu ban chay (theo so luong) - tat ca gian Shopee + tung gian."""
    import ads
    from openpyxl.styles import Font, PatternFill
    sm, rows, totals = ads.salesmen(), [], []
    shops = _shopee_shops(report)
    merged = {}
    for _, prods in shops:  # gop SP cua cac gian theo SKU de dem so SP khong trung
        for p in prods:
            m = merged.setdefault(p["sku"].upper() or p["name"], {"name": p["name"], "qty": 0, "revenue": 0.0})
            m["qty"] += p["qty"]; m["revenue"] += p["revenue"]
    for label, saleman, prods in [("TẤT CẢ GIAN SHOPEE", "", list(merged.values()))] + [(r["shop"], ads.salesman_of(r["shop"], sm), ps) for r, ps in shops]:
        stats = brand_stats(prods, 10 if not saleman and label.startswith("TẤT CẢ") else 5, names)
        totals.append(len(rows))
        for i, b in enumerate(stats, 1):
            rows.append([label, saleman, i, b["brand"], b["qty"], round(b["revenue"]), b["products"]])
    hr = sheet(ws, "Thương hiệu bán chạy (theo số lượng; thương hiệu = chữ đầu tên sản phẩm)",
               ["Gian hàng", "Saleman", "Hạng", "Thương hiệu", "Số lượng", "Doanh thu (đ)", "Số sản phẩm"],
               rows, [None, None, "0", None, "#,##0", "#,##0", "#,##0"], [24, 11, 6, 18, 11, 16, 11])
    fill = PatternFill("solid", fgColor="F4F4F5")
    for k in range(totals[1] if len(totals) > 1 else len(rows)):  # khoi tong tat ca gian
        for c in ws[hr + 1 + k]:
            c.fill = fill
    for k in totals:
        ws.cell(hr + 1 + k, 4).font = Font(bold=True)


def tracker_sheet(ws, report, ad_shops, sheet):
    """Bang theo doi (Shopee Ads Weekly Tracker): moi gian Shopee 1 dong, du 4 nhom cua file yeu cau."""
    import ads
    from openpyxl.styles import Font
    d1, d2 = (datetime.strptime(report[k], "%Y-%m-%d") for k in ("from", "to"))
    ky = f"{d1:%d/%m} – {d2:%d/%m/%Y}"
    sm = ads.salesmen()
    vn = lambda x: f"{round(x):,}".replace(",", ".")
    div = lambda a, b: a / b if a is not None and b else None  # ty le (Excel dinh dang %), None neu khong tinh duoc
    rows = []
    tot = {"orders": 0, "revenue": 0.0, "sp_cost": 0.0, "shop_cost": 0.0, "cost": 0.0, "conv": 0.0, "gmv": 0.0,
           "mapped_cost": 0.0, "sapo_rev": 0.0, "impr": 0.0, "clicks": 0.0, "ad_orders_base": 0, "ad_rev_base": 0.0}
    has_any_ads = has_any_sp = has_any_shop = False
    aov = lambda rev, n: round(rev / n) if n else None

    def ad_cols(orders, revenue, sp_cost, shop_cost, cost, conv, gmv, sapo_rev, mapped_cost, impr, clicks):
        """Cot nhom QC theo file mau FORM_REPORT_SHOPEE_ADS_MAU (dung chung cho dong gian va dong Tong)."""
        return [sp_cost, shop_cost, cost, conv, gmv,
                div(cost, conv),                                   # CPO = chi phi QC / don tu QC
                div(conv, orders),                                 # ty trong don tu QC / tong don gian
                div(gmv, revenue),                                 # ty trong DT QC / DT gian
                div(gmv, cost),                                    # ROAS
                div(cost, gmv),                                    # ACOS = chi phi / doanh so QC
                div(cost, revenue),                                # TACOS (file mau) = chi phi QC / DT ca gian
                sapo_rev, div(mapped_cost, sapo_rev),              # TACOS SP chay QC (cong thuc Yu)
                impr, clicks, div(clicks, impr), div(conv, clicks)]

    for r in report["shops"]:
        if r["channel"] != "Shopee":
            continue
        a = ad_shops.get(ads.norm(r["shop"]))
        best = r.get("bestseller")
        brands = ", ".join(f"{b['brand']} ({vn(b['qty'])})" for b in r.get("brands", []))
        orders, revenue = r["orders"], round(r["revenue"])
        tot["orders"] += orders
        tot["revenue"] += revenue
        row = [ky, r["shop"], ads.salesman_of(r["shop"], sm), "Shopee", orders, revenue, aov(revenue, orders),
               f"{best['name']} ({vn(best['qty'])} cái)" if best else None, brands or None, len(r.get("top_qty", []))]
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
            row += ad_cols(orders, revenue, sp_cost, shop_cost, round(a["cost"]), a["conversions"], round(a["gmv"]),
                           sapo_rev, mapped, a["impressions"], a["clicks"])
            row.append("; ".join(f["text"] for f in a["flags"] + [dict(f, text="QC Shop: " + f["text"]) for f in sa.get("flags", [])]))
            for k, v in (("sp_cost", sp_cost), ("shop_cost", shop_cost), ("cost", round(a["cost"])), ("conv", a["conversions"]),
                         ("gmv", round(a["gmv"])), ("sapo_rev", sapo_rev), ("mapped_cost", mapped),
                         ("impr", a["impressions"]), ("clicks", a["clicks"])):
                tot[k] += v or 0
            tot["ad_orders_base"] += orders
            tot["ad_rev_base"] += revenue
        else:
            row += [None] * 17 + ["Chưa có file Quảng cáo Shopee cùng kỳ"]
        rows.append(row)
    if rows:  # dong Tong (nhu file mau); ty le QC chi tinh tren cac gian co file QC
        t = tot
        total = ["", "TỔNG", "", "Shopee", t["orders"], t["revenue"], aov(t["revenue"], t["orders"]), None, None, None]
        if has_any_ads:
            total += ad_cols(t["ad_orders_base"], t["ad_rev_base"], t["sp_cost"] if has_any_sp else None,
                             t["shop_cost"] if has_any_shop else None, t["cost"],
                             t["conv"], t["gmv"], t["sapo_rev"] if has_any_sp else None, t["mapped_cost"] if has_any_sp else None,
                             t["impr"], t["clicks"]) + [None]
        else:
            total += [None] * 18
        rows.append(total)
    money, pc = "#,##0", "0.0%"
    hr = sheet(ws, "Shopee Ads Weekly Tracker – theo dõi quảng cáo theo gian hàng",
               ["Kỳ", "Gian hàng", "Saleman", "Nền tảng", "Tổng số đơn", "Doanh thu gian hàng (đ)", "AOV (đ)",
                "Bán chạy nhất (số lượng)", "Thương hiệu bán chạy (số lượng)", f"Số SP bán ≥ {TOP_QTY} cái",
                "Chi phí QC Sản phẩm (đ)", "Chi phí QC Shop (đ)", "Tổng chi phí QC (đ)", "Đơn hàng từ QC", "Doanh số từ QC (đ)",
                "CPO (đ)", "Tỷ trọng đơn từ QC", "Tỷ trọng DT QC", "ROAS (lần)", "ACOS", "TACOS (QC / DT cả gian)",
                "Doanh thu Sapo SP chạy QC (đ)", "TACOS SP chạy QC", "Lượt hiển thị", "Lượt click", "CTR", "CR", "Đánh giá tự động"],
               rows, [None, None, None, None, money, money, money, None, None, money,
                      money, money, money, money, money, money, pc, pc, "0.0", "0.0%", "0.0%",
                      money, "0.0%", money, money, "0.00%", "0.00%", None],
               [16, 24, 11, 9, 9, 16, 13, 40, 34, 10, 14, 13, 14, 9, 15, 12, 10, 10, 7, 8, 10, 16, 9, 11, 9, 8, 8, 70])
    if rows:
        for c in ws[hr + len(rows)]:  # dong Tong in dam
            c.font = Font(bold=True)
    notes = [
        "AOV = doanh thu gian hàng / tổng số đơn.  CPO = chi phí QC / đơn hàng từ QC.",
        "Tỷ trọng đơn từ QC = đơn hàng từ QC / tổng số đơn.  Tỷ trọng DT QC = doanh số từ QC / doanh thu gian hàng.",
        "ROAS = doanh số từ QC / chi phí QC (càng cao càng tốt).  ACOS = chi phí QC / doanh số từ QC (càng thấp càng tốt).",
        "TACOS (QC / DT cả gian) = tổng chi phí QC / doanh thu gian hàng (công thức file mẫu).",
        "TACOS SP chạy QC = chi phí QC Sản phẩm / doanh thu Sapo của các SP chạy QC (công thức Yu, tốt 8–12%; dùng cho đánh giá tự động).",
        "Tổng số đơn, doanh thu: đơn Sapo, không tính đơn huỷ, đã trừ tiền hoàn. Số QC theo file Quảng cáo Shopee cùng kỳ (chưa trừ huỷ, hoàn).",
        "Bán chạy và thương hiệu xếp theo số lượng; thương hiệu lấy theo chữ đầu tên sản phẩm. Dòng TỔNG: tỷ lệ QC chỉ tính trên các gian có file QC.",
    ]
    for i, text in enumerate(notes):
        ws.cell(hr + len(rows) + 2 + i, 1, text).font = Font(italic=True, color="71717A")
    if rows:
        ws.auto_filter.ref = f"A{hr}:AB{hr + len(rows) - 1}"


def ads_sheet(ws, a, sheet, period):
    """Sheet Quang cao Shopee (= sheet "Chi Tiet" file mau): moi dong 1 san pham / chien dich chay QC, kem dong tong tung gian."""
    import ads
    from openpyxl.styles import Font, PatternFill
    T = a["thresholds"]
    pct = lambda v: None if v is None else v / 100
    rnd = lambda v: None if v is None else round(v)

    def line(shop, kind, name, sku, m, sapo=True):
        return [shop, kind, name, sku, m["sold"], round(m["gmv"]), round(m["cost"]), m["impressions"], m["clicks"], pct(m["ctr"]),
                m["add_to_cart"], pct(m.get("atc_rate")), m["conversions"], pct(m["cr"]),
                m["conversions_direct"], pct(m.get("cr_direct")), rnd(m.get("cpo_direct")),
                m["roas"], m["roas_direct"], pct(m.get("acos")), pct(m.get("acos_direct")),
                rnd(m.get("sapo_revenue")) if sapo else None, pct(m.get("tacos")) if sapo else None,
                "; ".join(f["text"] for f in m["flags"])]

    rows, shop_rows = [], []
    for s in a["shops"]:
        shop_rows.append(len(rows))
        rows.append(line(s["shop"], "TỔNG GIAN" + (f" · {s['saleman']}" if s.get("saleman") else ""), "", "",
                         dict(s, sapo_revenue=s.get("sapo_revenue") or 0)))
        sa = s.get("shop_ads")
        if sa:
            rows.append(line(s["shop"], ads.KINDS["shop"], ", ".join(sa["campaigns"]), "(cả gian)", sa))
            for k in sa.get("keywords", []):
                rows.append(line(s["shop"], ads.KINDS["shop"], f"   Từ khóa: {k['keyword']}" + (f" ({k['keyword_type']})" if k["keyword_type"] else ""),
                                 "", k, sapo=False))
        for p in s["products"]:
            rows.append(line(s["shop"], ads.KINDS["product"], p["name"], ", ".join(p["skus"]), p))
    money = "#,##0"
    hr = sheet(ws, "Chi tiết chiến dịch quảng cáo Shopee (theo file Shopee Ads + doanh thu Sapo)",
               ["Gian hàng", "Loại hình quảng cáo", "Tên chiến dịch / sản phẩm", "SKU Sapo", "Sản phẩm đã bán", "Doanh số từ QC (đ)",
                "Chi phí QC (đ)", "Lượt xem", "Lượt click", "Tỷ lệ click (CTR)", "Thêm vào giỏ hàng", "Tỷ lệ thêm vào giỏ",
                "Đơn từ QC", "CR", "Lượt chuyển đổi trực tiếp", "Tỷ lệ chuyển đổi trực tiếp", "Chi phí mỗi chuyển đổi trực tiếp (đ)",
                "ROAS", "ROAS trực tiếp", "ACOS", "ACOS trực tiếp", "Doanh thu Sapo SP chạy QC (đ)", "TACOS", "Đánh giá tự động"],
               rows, [None, None, None, None, money, money, money, money, money, "0.00%", money, "0.00%", money, "0.00%",
                      money, "0.00%", money, "0.0", "0.0", "0.0%", "0.0%", money, "0.0%", None],
               [22, 20, 50, 14, 9, 15, 13, 10, 9, 9, 9, 9, 9, 8, 10, 10, 13, 7, 8, 8, 8, 16, 8, 70])
    bold, fill = Font(bold=True), PatternFill("solid", fgColor="F4F4F5")
    for k in shop_rows:
        for c in ws[hr + 1 + k]:
            c.font, c.fill = bold, fill
    notes = [
        "CTR = click / lượt xem.  Tỷ lệ thêm vào giỏ = thêm vào giỏ / click.  CR = đơn từ QC / click.  Tỷ lệ chuyển đổi trực tiếp = chuyển đổi trực tiếp / click.",
        "Chi phí mỗi chuyển đổi trực tiếp = chi phí / lượt chuyển đổi trực tiếp.  ROAS = doanh số QC / chi phí.  ACOS = chi phí / doanh số QC "
        "(“trực tiếp” = chỉ tính doanh số, đơn của chính sản phẩm được quảng cáo).",
        f"TACOS = chi phí QC / doanh thu Sapo của SKU đã gán (tốt: {T['tacos_low']:g}–{T['tacos_high']:g}%); riêng Quảng cáo Shop chia cho doanh thu cả gian.",
        "Doanh số QC lấy theo Shopee (chưa trừ huỷ, hoàn); doanh thu Sapo đã trừ.",
    ]
    for i, text in enumerate(notes):
        ws.cell(hr + len(rows) + 2 + i, 1, text).font = Font(italic=True, color="71717A")
    ws.auto_filter.ref = f"A{hr}:X{hr + len(rows)}"


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
