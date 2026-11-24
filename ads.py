"""Quang cao Shopee (Shopee Ads / "Dich vu Hien thi"): doc file CSV xuat tu Shopee, ghep voi doanh thu Sapo.

Theo file yeu cau "BAO_CAO_SHOPEE_DEMO_AI":
- Nhom 2 (tu file Shopee): chi phi, doanh so tu QC, luot xem, luot click, don tu QC, ROAS.
- Nhom 3 (tu tinh):  ROAS = doanh so QC / chi phi; TACOS = chi phi / doanh thu Sapo cua san pham chay QC x 100;
                     CTR = click / luot xem; CR = don tu QC / click.
- Nhom 4: danh gia tu dong theo nguong (THRESHOLDS).

Moi file Shopee = 1 gian hang, 1 khoang thoi gian (tong ca ky, khong chia theo ngay), moi dong 1 chien dich.
San pham Shopee (Ma san pham) khac SKU Sapo -> bang gan reports/ads/map.json (gan 1 lan, nho mai).
"""
import csv
import io
import json
import os
import re
import threading
import unicodedata
from datetime import datetime

import sync

ADS_DIR = os.path.join(sync.REPORT_DIR, "ads")
UPLOADS_FILE = os.path.join(ADS_DIR, "uploads.json")
MAP_FILE = os.path.join(ADS_DIR, "map.json")
_lock = threading.Lock()

# Nguong danh gia. TACOS 8-12% = tot (theo file yeu cau); cac nguong con lai la mac dinh, sua o day neu can.
THRESHOLDS = {
    "tacos_low": 8.0,     # TACOS duoi muc nay: hieu qua cao, con du dia tang ngan sach
    "tacos_high": 12.0,   # TACOS tren muc nay: chi phi QC cao so voi doanh thu
    "roas_min": 8.0,      # ROAS duoi muc nay: kem (ROAS 8 ~ ACOS 12.5%)
    "ctr_min": 1.0,       # %: it nguoi bam -> xem lai anh bia / tieu de / gia
    "cr_min": 2.0,        # %: nhieu click it mua -> xem lai gia / mo ta / danh gia
    "min_clicks": 100,    # chi danh gia CR / "chua ra don" khi du so click
    "min_impressions": 1000,  # chi danh gia CTR khi du luot xem
}

# Cot trong file Shopee -> ten truong. Chi can 4 cot dau; cac cot khac co thi lay.
COLUMNS = {
    "Chi phí": "cost", "Số lượt xem": "impressions", "Số lượt click": "clicks", "Doanh số": "gmv",
    "Doanh số trực tiếp": "gmv_direct", "Lượt chuyển đổi": "conversions", "Lượt chuyển đổi trực tiếp": "conversions_direct",
    "Sản phẩm đã bán": "sold", "Sản phẩm đã bán trực tiếp": "sold_direct", "Thêm vào giỏ hàng": "add_to_cart",
}
REQUIRED = ("Chi phí", "Số lượt xem", "Số lượt click", "Doanh số")
SUM_FIELDS = list(COLUMNS.values())


# ---------------------------------------------------------------- doc file

def _num(v):
    v = (v or "").strip().replace("%", "")
    if v in ("", "-"):
        return 0.0
    try:
        return float(v.replace(",", ""))
    except ValueError:
        return 0.0


def _day(s):
    return datetime.strptime(s.strip(), "%d/%m/%Y").strftime("%Y-%m-%d")


def parse_csv(text):
    """Noi dung file CSV Shopee -> {shop, username, seller_id, from, to, created, campaigns}. Loi -> ValueError (tieng Viet)."""
    lines = text.lstrip("﻿").splitlines()
    head = next((i for i, l in enumerate(lines) if l.startswith("Thứ tự,")), None)
    if head is None:
        raise ValueError("Không phải file báo cáo Quảng cáo Shopee (không thấy dòng tiêu đề 'Thứ tự,...')")
    meta = {}
    for row in csv.reader(lines[:head]):
        if len(row) >= 2:
            meta[row[0].strip()] = row[1].strip()
    shop = meta.get("Tên gian hàng")
    period = meta.get("Khoảng thời gian", "")
    m = re.match(r"\s*(\d{2}/\d{2}/\d{4})\s*-\s*(\d{2}/\d{2}/\d{4})", period)
    if not shop or not m:
        raise ValueError("File thiếu 'Tên gian hàng' hoặc 'Khoảng thời gian' ở phần đầu")
    reader = csv.DictReader(io.StringIO("\n".join(lines[head:])))
    missing = [c for c in REQUIRED if c not in (reader.fieldnames or [])]
    if missing:
        raise ValueError("File thiếu cột: " + ", ".join(missing))
    campaigns = []
    for r in reader:
        if not (r.get("Thứ tự") or "").strip():
            continue
        c = {f: _num(r.get(col)) for col, f in COLUMNS.items()}
        c.update(name=(r.get("Tên Dịch vụ Hiển thị") or "").strip(), status=(r.get("Trạng thái") or "").strip(),
                 type=(r.get("Loại Dịch vụ Hiển thị") or "").strip(), bidding=(r.get("Phương thức đấu thầu") or "").strip(),
                 item_id=re.sub(r"\D", "", r.get("Mã sản phẩm") or ""))
        campaigns.append(c)
    return {"shop": shop, "username": meta.get("Tên đăng nhập", ""), "seller_id": meta.get("Mã Người bán", ""),
            "from": _day(m.group(1)), "to": _day(m.group(2)), "created": meta.get("Thời gian tạo báo cáo", ""),
            "uploaded_at": datetime.now().strftime("%Y-%m-%d %H:%M"), "campaigns": campaigns}


# ---------------------------------------------------------------- luu tru

def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save(path, data):
    os.makedirs(ADS_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def uploads():
    return _load(UPLOADS_FILE, [])


def get_map():
    return _load(MAP_FILE, {})


def add_upload(parsed):
    """Luu file da doc; cung gian + cung ky thi thay ban cu. Tra ve True neu la thay the."""
    with _lock:
        items = uploads()
        same = lambda u: _norm(u["shop"]) == _norm(parsed["shop"]) and (u["from"], u["to"]) == (parsed["from"], parsed["to"])
        replaced = any(same(u) for u in items)
        items = [u for u in items if not same(u)] + [parsed]
        _save(UPLOADS_FILE, items)
        return replaced


def delete_upload(shop, start, end):
    with _lock:
        items = uploads()
        keep = [u for u in items if not (u["shop"] == shop and u["from"] == start and u["to"] == end)]
        _save(UPLOADS_FILE, keep)
        return len(keep) != len(items)


def set_map(item_id, skus):
    item_id = re.sub(r"\D", "", str(item_id))
    skus = [s.strip() for s in skus if s and s.strip()]
    with _lock:
        mp = get_map()
        if skus:
            mp[item_id] = skus
        else:
            mp.pop(item_id, None)
        _save(MAP_FILE, mp)


def periods():
    """Cac ky da tai len: [{from, to, shops: [...]}], moi nhat truoc."""
    out = {}
    for u in uploads():
        out.setdefault((u["from"], u["to"]), []).append(u["shop"])
    return [{"from": f, "to": t, "shops": sorted(s)} for (f, t), s in sorted(out.items(), key=lambda kv: kv[0][::-1], reverse=True)]


# ---------------------------------------------------------------- tinh toan

def _norm(s):
    s = unicodedata.normalize("NFD", str(s or "")).casefold().replace("đ", "d")
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    return re.sub(r"[\s_\-]+", " ", s).strip()


# Tu chi phien ban san pham: phan biet "M40 SE" voi "M40", "Pocket 3 Pro" voi "Pocket 3"...
MIN_SCORE = 0.1  # goi y chi de tham khao; nguoi dung luon xac nhan truoc khi luu
VARIANT_WORDS = {"se", "pro", "max", "mini", "plus", "lite", "ultra", "air", "neo", "ii", "iii", "iv", "v2", "v3"}


def _tokens(s):
    raw = [t for t in re.split(r"[^0-9a-z]+", _norm(s)) if t]
    out = {t for t in raw if len(t) > 1}
    # ghep 2-3 tu lien nhau co chu so: "MT-44 M" va "MT-44M" deu ra "mt44m"
    for k in (2, 3):
        for i in range(len(raw) - k + 1):
            j = "".join(raw[i:i + k])
            if re.search(r"\d", j) and len(j) <= 12:
                out.add(j)
    return out


def _weight(t):
    return 3.0 if re.search(r"\d", t) or t in VARIANT_WORDS else 1.0


def suggest(name, products, n=3):
    """Goi y SKU Sapo cho ten san pham Shopee. Diem = F1 cua tu trung (co trong so: ma model, tu phien ban x3)."""
    want = _tokens(name)
    if not want:
        return []
    w_want = sum(_weight(t) for t in want)
    scored = []
    for p in products:
        have = _tokens(p["name"])
        common = want & have
        if not common or not any(_weight(t) > 1 for t in common):
            continue  # phai trung it nhat 1 ma model / tu phien ban
        c = sum(_weight(t) for t in common)
        prec, rec = c / sum(_weight(t) for t in have), c / w_want
        scored.append((2 * prec * rec / (prec + rec), p))
    scored.sort(key=lambda x: -x[0])
    return [{"sku": p["sku"], "name": p["name"], "score": round(s, 2)} for s, p in scored[:n] if s >= MIN_SCORE]


def _metrics(m, sapo_rev=None, shop_rev=None):
    cost, clicks, impr = m["cost"], m["clicks"], m["impressions"]
    m["ctr"] = 100 * clicks / impr if impr else None
    m["cr"] = 100 * m["conversions"] / clicks if clicks else None
    m["roas"] = m["gmv"] / cost if cost else None
    m["roas_direct"] = m["gmv_direct"] / cost if cost else None
    m["cpc"] = cost / clicks if clicks else None
    if sapo_rev is not None:
        m["sapo_revenue"] = sapo_rev
        m["tacos"] = 100 * m["mapped_cost"] / sapo_rev if sapo_rev > 0 else None
    if shop_rev is not None:
        m["shop_revenue"] = shop_rev
        m["cost_share"] = 100 * cost / shop_rev if shop_rev > 0 else None
    m["flags"] = evaluate(m)
    return m


def evaluate(m):
    """Nhom 4: danh gia tu dong -> [{level: bad|warn|good|info, text}]."""
    T, out = THRESHOLDS, []
    cost, tacos, roas = m["cost"], m.get("tacos"), m.get("roas")
    vnd = lambda x: f"{round(x):,}".replace(",", ".") + " đ"
    vn = lambda x, d=1: f"{x:.{d}f}".replace(".", ",")  # so kieu Viet: 2,5
    if cost <= 0:
        return [{"level": "info", "text": "Không phát sinh chi phí trong kỳ"}]
    if m["conversions"] == 0 and m["clicks"] >= T["min_clicks"]:
        out.append({"level": "bad", "text": f"Tốn {vnd(cost)} nhưng chưa ra đơn → nên tạm dừng hoặc đổi ảnh, giá"})
    if tacos is not None:
        if tacos > T["tacos_high"]:
            out.append({"level": "bad", "text": f"TACOS {vn(tacos, 1)}% > {vn(T['tacos_high'], 0)}%: chi phí QC cao so với doanh thu → giảm giá thầu / ngân sách"})
        elif tacos >= T["tacos_low"]:
            out.append({"level": "good", "text": f"TACOS {vn(tacos, 1)}% trong ngưỡng tốt {vn(T['tacos_low'], 0)}–{vn(T['tacos_high'], 0)}%"})
        elif roas is not None and roas >= T["roas_min"]:
            out.append({"level": "good", "text": f"TACOS {vn(tacos, 1)}% thấp, ROAS {vn(roas, 1)}: hiệu quả cao → có thể tăng ngân sách"})
    elif m.get("sapo_revenue") == 0 and m.get("mapped_cost"):
        out.append({"level": "warn", "text": "Sapo không có doanh thu của SKU đã gán trong kỳ → kiểm tra lại SKU"})
    elif m.get("unmapped"):
        out.append({"level": "info", "text": "Chưa gán SKU Sapo → chưa tính được TACOS"})
    if roas is not None and roas < T["roas_min"] and m["conversions"] > 0:
        out.append({"level": "warn", "text": f"ROAS {vn(roas, 1)} < {vn(T['roas_min'], 0)}: doanh số QC thấp so với chi phí"})
    if m.get("ctr") is not None and m["impressions"] >= T["min_impressions"] and m["ctr"] < T["ctr_min"]:
        out.append({"level": "warn", "text": f"CTR {vn(m['ctr'], 2)}% < {vn(T['ctr_min'], 0)}%: ít người bấm → xem lại ảnh bìa, tiêu đề, giá"})
    if m.get("cr") is not None and m["clicks"] >= T["min_clicks"] and 0 < m["cr"] < T["cr_min"]:
        out.append({"level": "warn", "text": f"CR {vn(m['cr'], 2)}% < {vn(T['cr_min'], 0)}%: nhiều click ít mua → xem lại giá, mô tả, đánh giá"})
    return out


def _blank():
    return {f: 0.0 for f in SUM_FIELDS} | {"mapped_cost": 0.0}


def _add(dst, src):
    for f in SUM_FIELDS + ["mapped_cost"]:
        dst[f] += src.get(f, 0.0)


def analyze(start, end, report=None):
    """Phan tich ky [start, end]. report = bao cao doanh thu Sapo (sales.get_report); chi dung neu dung cung ky."""
    has_sales = bool(report and report.get("from") == start and report.get("to") == end)
    mp = get_map()
    sapo_shops, sapo_products = {}, {}
    if has_sales:
        for r in report["shops"]:
            if r["channel"] == "Shopee":
                sapo_shops[_norm(r["shop"])] = r
        for p in report["products"]:
            if p["channel"] == "Shopee":
                sapo_products.setdefault(_norm(p["shop"]), []).append(p)
    shops, total = [], _blank()
    total_rev = total_shop_rev = 0.0
    unmapped_total = 0
    for u in sorted((u for u in uploads() if (u["from"], u["to"]) == (start, end)), key=lambda u: u["shop"]):
        key = _norm(u["shop"])
        sapo_shop = sapo_shops.get(key)
        prods = sapo_products.get(key, [])
        by_sku = {}
        for p in prods:
            by_sku.setdefault(p["sku"].upper(), []).append(p)
        # gop cac chien dich cung 1 san pham
        items = {}
        for c in u["campaigns"]:
            k = c["item_id"] or ("chien-dich:" + c["name"])
            it = items.get(k)
            if it is None:
                it = items[k] = _blank() | {"item_id": c["item_id"], "name": c["name"], "status": [], "campaigns": 0}
            _add(it, c)
            it["campaigns"] += 1
            if c["status"] and c["status"] not in it["status"]:
                it["status"].append(c["status"])
        shop_m = _blank()
        shop_mapped_rev = 0.0
        rows = []
        for it in items.values():
            skus = mp.get(it["item_id"], []) if it["item_id"] else []
            sapo_rev = None
            if has_sales and skus:
                matched = [p for s in skus for p in by_sku.get(s.upper(), [])]
                sapo_rev = sum(p["revenue"] for p in matched)
                it["sapo_qty"] = sum(p["qty"] for p in matched)
                it["sapo_names"] = sorted({p["name"] for p in matched})
                it["mapped_cost"] = it["cost"]
                shop_mapped_rev += sapo_rev
            it["skus"] = skus
            it["unmapped"] = bool(it["item_id"]) and not skus and it["cost"] > 0
            if it["unmapped"]:
                unmapped_total += 1
                if has_sales:
                    it["suggestions"] = suggest(it["name"], prods)
            _add(shop_m, it)
            rows.append(_metrics(it, sapo_rev if has_sales and skus else None))
        rows.sort(key=lambda r: -r["cost"])
        shop_rev = sapo_shop["revenue"] if sapo_shop else None
        shop_m.update(shop=u["shop"], username=u["username"], created=u["created"], uploaded_at=u["uploaded_at"],
                      found_in_sapo=bool(sapo_shop), products=rows,
                      unmapped=sum(1 for r in rows if r["unmapped"]),
                      sapo_skus=[{"sku": p["sku"], "name": p["name"]} for p in sorted(prods, key=lambda p: -p["revenue"])])
        _metrics(shop_m, shop_mapped_rev if has_sales else None, shop_rev if has_sales else None)
        shops.append(shop_m)
        _add(total, shop_m)
        total_rev += shop_mapped_rev
        total_shop_rev += shop_rev or 0.0
    total["unmapped"] = unmapped_total
    _metrics(total, total_rev if has_sales else None, total_shop_rev if has_sales else None)
    shops.sort(key=lambda s: -s["cost"])
    return {"from": start, "to": end, "has_sales": has_sales, "thresholds": THRESHOLDS, "total": total, "shops": shops}


def by_shop(analysis):
    """{ten gian (chuan hoa): so lieu QC} de dien vao Excel."""
    return {_norm(s["shop"]): s for s in (analysis or {}).get("shops", [])}


def norm(s):
    return _norm(s)
