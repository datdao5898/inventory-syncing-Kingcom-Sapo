"""Dong bo ton kho Sapo -> Haravan theo tung chi nhanh, ghep san pham theo SKU.

Sapo la nguon chuan (master): ton cua moi SKU o mot chi nhanh Sapo se duoc
ghi de (set) sang kho Haravan tuong ung (cau hinh LOCATION_MAP trong .env).

Cach dung:
    python sync.py check              # kiem tra ket noi + liet ke chi nhanh/kho 2 ben
    python sync.py                    # chay thu (dry-run), chi in ra chenh lech
    python sync.py --apply            # cap nhat that sang Haravan
    python sync.py --apply --loop 15  # tu dong dong bo moi 15 phut
"""

import argparse
import base64
import csv
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HARAVAN_API = "https://apis.haravan.com/com"
HARAVAN_BATCH_SIZE = 100  # Haravan khuyen nghi toi da 100 dong / request
HARAVAN_INV_LOOKUP_SIZE = 50  # inventory_locations.json chi nhan toi da 50 variant

log = logging.getLogger("sync")


def norm_sku(sku):
    """Khoa ghep SKU: bo khoang trang dau/cuoi, khong phan biet hoa/thuong."""
    return (sku or "").strip().upper()


# ---------------------------------------------------------------- config

def load_env(path):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def require(name):
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"Thieu cau hinh {name} trong file .env")
    return value


def location_map():
    """LOCATION_MAP=sapo_id:haravan_id,sapo_id:haravan_id -> {sapo_id: haravan_id}"""
    mapping = {}
    for pair in require("LOCATION_MAP").split(","):
        sapo_id, _, haravan_id = pair.strip().partition(":")
        if not sapo_id or not haravan_id:
            sys.exit(f"LOCATION_MAP sai dinh dang o '{pair}', dung dang sapo_id:haravan_id")
        mapping[int(sapo_id)] = int(haravan_id)
    return mapping


# ---------------------------------------------------------------- http

def http_json(method, url, headers, body=None, retries=5):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    # Sapo dung Cloudflare, chan User-Agent mac dinh cua Python (loi 1010)
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) sapo-haravan-sync/1.0",
               **headers}
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                wait = float(e.headers.get("Retry-After") or 2 ** attempt)
                log.warning("%s %s -> HTTP %s, thu lai sau %.0fs", method, url, e.code, wait)
                time.sleep(wait)
                continue
            detail = e.read().decode("utf-8", "replace")[:500]
            raise RuntimeError(f"{method} {url} -> HTTP {e.code}: {detail}") from None
        except urllib.error.URLError as e:
            wait = 2 ** attempt
            log.warning("%s %s -> loi mang (%s), thu lai sau %ss", method, url, e.reason, wait)
            time.sleep(wait)
    raise RuntimeError(f"{method} {url} that bai sau {retries} lan thu")


# ---------------------------------------------------------------- sapo

class Sapo:
    def __init__(self):
        store = require("SAPO_STORE").replace("https://", "").split(".")[0]
        self.base = f"https://{store}.mysapo.net/admin"
        self.items = {}  # sku chuan hoa -> inventory_item dau tien (de lay product_id, sku goc)
        token = os.environ.get("SAPO_ACCESS_TOKEN", "").strip()
        if token:
            self.headers = {"X-Sapo-Access-Token": token}
        else:
            key, secret = require("SAPO_API_KEY"), require("SAPO_API_SECRET")
            cred = base64.b64encode(f"{key}:{secret}".encode()).decode()
            self.headers = {"Authorization": f"Basic {cred}"}
        # available = co the ban (da tru hang dang giu cho don); on_hand = ton thuc te
        self.qty_field = os.environ.get("SAPO_QTY_FIELD", "available").strip() or "available"

    def get(self, path, params=None):
        url = f"{self.base}/{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return http_json("GET", url, self.headers)

    def get_all(self, path, key, params=None):
        out, page = [], 1
        while True:
            rows = self.get(path, {**(params or {}), "limit": 250, "page": page}).get(key, [])
            out += rows
            if len(rows) < 250:
                return out
            page += 1

    def locations(self):
        return self.get("locations.json").get("locations", [])

    def stock_by_sku(self, location_ids):
        """{location_id: {sku: qty}} (sku da chuan hoa). Dung inventory_items + inventory_levels nen chi can
        quyen ton kho. Ghi lai self.duplicate_skus: {sku: [sku goc...]} cho SKU bi trung tren Sapo."""
        sku_of, originals = {}, {}
        for item in self.get_all("inventory_items.json", "inventory_items"):
            sku = norm_sku(item.get("sku"))
            if sku and item.get("tracked", True):
                sku_of[item["variant_id"]] = sku
                originals.setdefault(sku, []).append(item["sku"].strip())
                self.items.setdefault(sku, item)
        self.duplicate_skus = {k: v for k, v in originals.items() if len(v) > 1}
        stock = {}
        for loc in location_ids:
            per_sku = stock.setdefault(loc, {})
            for lv in self.get_all("inventory_levels.json", "inventory_levels", {"location_ids": loc}):
                sku = sku_of.get(lv["variant_id"])
                if sku:
                    per_sku[sku] = per_sku.get(sku, 0) + int(lv.get(self.qty_field) or 0)
        return stock

    def variant_titles(self):
        """{variant_id: ten}. Can quyen doc San pham; neu chua co quyen thi tra ve {}."""
        try:
            products = self.get_all("products.json", "products", {"fields": "id,title,variants"})
        except RuntimeError as e:
            if "HTTP 403" not in str(e):
                raise
            log.info("Sapo chua cap quyen doc San pham -> danh sach so sanh se khong co ten san pham Sapo")
            return {}
        return {v["id"]: f'{p.get("title")} - {v.get("title")}' for p in products for v in p.get("variants", [])}


# ---------------------------------------------------------------- haravan

class Haravan:
    def __init__(self):
        self.headers = {"Authorization": f"Bearer {require('HARAVAN_TOKEN')}"}

    def get(self, path, params=None):
        url = f"{HARAVAN_API}/{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return http_json("GET", url, self.headers)

    def locations(self):
        return self.get("locations.json").get("locations", [])

    def variants(self):
        """List tat ca variant: {sku (da chuan hoa, "" neu khong co), sku_raw, product_id, variant_id, title, tracked}."""
        out, page = [], 1
        while True:
            data = self.get("products.json", {"limit": 50, "page": page, "fields": "id,title,variants"})
            products = data.get("products", [])
            for p in products:
                for v in p.get("variants", []):
                    out.append({
                        "sku": norm_sku(v.get("sku")),
                        "sku_raw": (v.get("sku") or "").strip(),
                        "product_id": p["id"],
                        "variant_id": v["id"],
                        "title": f'{p.get("title")} - {v.get("title")}',
                        "tracked": bool(v.get("inventory_management")),
                    })
            if len(products) < 50:
                return out
            page += 1

    def stock(self, variant_ids, location_ids):
        """{(location_id, variant_id): qty_onhand}. Variant chua co dong ton o kho nao thi coi la 0."""
        out = {}
        locs = ",".join(map(str, location_ids))
        for i in range(0, len(variant_ids), HARAVAN_INV_LOOKUP_SIZE):
            batch = variant_ids[i:i + HARAVAN_INV_LOOKUP_SIZE]
            rows = self.get("inventory_locations.json", {
                "location_ids": locs, "variant_ids": ",".join(map(str, batch)),
            }).get("inventory_locations", [])
            for r in rows:
                out[(r["loc_id"], r["variant_id"])] = int(r.get("qty_onhand") or 0)
        return out

    def set_inventory(self, location_id, items):
        for i in range(0, len(items), HARAVAN_BATCH_SIZE):
            batch = items[i:i + HARAVAN_BATCH_SIZE]
            body = {"inventory": {
                "location_id": int(location_id),
                "type": "set",
                "reason": "shrinkage",
                "note": f"Dong bo tu Sapo {datetime.now():%Y-%m-%d %H:%M}",
                "line_items": [{
                    "product_id": it["product_id"],
                    "product_variant_id": it["variant_id"],
                    "quantity": it["new_qty"],
                } for it in batch],
            }}
            http_json("POST", f"{HARAVAN_API}/inventories/adjustorset.json", self.headers, body)
            log.info("  kho %s: da cap nhat %d/%d variant",
                     location_id, min(i + HARAVAN_BATCH_SIZE, len(items)), len(items))


# ---------------------------------------------------------------- sync

def write_report(updates, loc_names):
    """Bao cao Excel: chi cac san pham can/da cap nhat ton (cac SKU lech nhau xem o trang web)."""
    os.makedirs(os.path.join(BASE_DIR, "reports"), exist_ok=True)
    path = os.path.join(BASE_DIR, "reports", f"sync_{datetime.now():%Y%m%d_%H%M%S}.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Sản phẩm", "SKU", "Kho", "Tồn Haravan trước", "Tồn mới (theo Sapo)", "Chênh lệch"])
        for loc, loc_updates in updates.items():
            for u in sorted(loc_updates, key=lambda u: u["title"]):
                d = u["new_qty"] - u["old_qty"]
                w.writerow([u["title"], u["sku_raw"], loc_names.get(loc, str(loc)),
                            u["old_qty"], u["new_qty"], f"+{d}" if d > 0 else d])
    return path


COMPARE_FILE = os.path.join(BASE_DIR, "reports", "compare_latest.json")
LAST_RUN_FILE = os.path.join(BASE_DIR, "reports", "last_run.json")


def save_last_run(data):
    """Ket qua lan chay gan nhat (danh sach thay doi) cho giao dien web."""
    with open(LAST_RUN_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def write_comparison(sapo, haravan, mapping, sapo_stock, hrv_stock, by_sku, no_sku, hrv_dupes):
    """Luu danh sach so sanh 2 ben de trang web loc/tim kiem."""
    haravan_admin = haravan.get("shop.json").get("shop", {}).get("myharavan_domain", "").rstrip("/")
    haravan_admin = f"{haravan_admin}/admin" if haravan_admin else ""
    sapo_skus = set().union(*(s.keys() for s in sapo_stock.values()))
    titles = sapo.variant_titles()
    sapo_locs, hrv_locs = list(mapping), list(mapping.values())
    loc_names = {l["id"]: l.get("name", "") for l in sapo.locations()}

    def hrv_row(v):
        return {"sku": v["sku_raw"], "title": v["title"], "product_id": v["product_id"],
                "stock": [hrv_stock.get((loc, v["variant_id"]), 0) for loc in hrv_locs]}

    data = {
        "generated_at": datetime.now().strftime("%H:%M %d/%m/%Y"),
        "sapo_base": sapo.base,
        "haravan_admin": haravan_admin,
        "locations": [loc_names.get(loc, str(loc)) for loc in sapo_locs],
        "sapo_only": [{
            "sku": sapo.items[sku]["sku"].strip(),
            "title": titles.get(sapo.items[sku]["variant_id"], ""),
            "product_id": sapo.items[sku]["product_id"],
            "stock": [sapo_stock[loc].get(sku, 0) for loc in sapo_locs],
        } for sku in sorted(sapo_skus - set(by_sku))],
        "haravan_only": [hrv_row(v) for sku in sorted(set(by_sku) - sapo_skus) for v in by_sku[sku]],
        "haravan_no_sku": [{"sku": "", "title": v["title"], "product_id": v["product_id"], "stock": []}
                           for v in no_sku],
        "haravan_dupes": [hrv_row(v) for sku in sorted(hrv_dupes) for v in hrv_dupes[sku]],
    }
    with open(COMPARE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def run_sync(apply, confirm=None, only_sku=None):
    """confirm(so_dong) -> bool: neu co, hoi truoc khi cap nhat that. only_sku: chi cap nhat 1 SKU (de thu).
    Tra ve so dong da/can cap nhat."""
    mapping = location_map()
    sapo, haravan = Sapo(), Haravan()

    log.info("Dang tai ton kho Sapo (%s)...", sapo.qty_field)
    sapo_stock = sapo.stock_by_sku(list(mapping))
    log.info("Dang tai san pham Haravan...")
    all_variants = haravan.variants()
    no_sku = [v for v in all_variants if not v["sku"]]
    hrv_variants = [v for v in all_variants if v["sku"]]
    log.info("Dang tai ton kho Haravan (%d variant)...", len(hrv_variants))
    hrv_stock = haravan.stock([v["variant_id"] for v in hrv_variants], list(mapping.values()))

    by_sku = {}
    for v in hrv_variants:
        by_sku.setdefault(v["sku"], []).append(v)
    hrv_dupes = {sku: vs for sku, vs in by_sku.items() if len(vs) > 1}
    hrv_skus = set(by_sku)
    sapo_skus = set().union(*(s.keys() for s in sapo_stock.values()))
    updates = {}
    for sapo_loc, hrv_loc in mapping.items():
        per_sku = sapo_stock[sapo_loc]
        loc_updates = updates.setdefault(hrv_loc, [])
        for v in hrv_variants:
            if v["sku"] not in sapo_skus or (only_sku and v["sku"] != norm_sku(only_sku)):
                continue
            # SKU chua co ton o chi nhanh nay -> 0; ton am tren Sapo (ban vuot ton) -> 0
            new_qty = max(per_sku.get(v["sku"], 0), 0)
            old_qty = hrv_stock.get((hrv_loc, v["variant_id"]), 0)
            if new_qty == old_qty:
                continue
            loc_updates.append({**v, "new_qty": new_qty, "old_qty": old_qty})

    total = sum(len(u) for u in updates.values())
    log.info("Sapo: %d SKU | Haravan: %d SKU | Khop: %d SKU | Can cap nhat: %d dong",
             len(sapo_skus), len(hrv_skus), len(sapo_skus & hrv_skus), total)
    if no_sku:
        log.warning("Haravan: %d variant KHONG CO SKU (bo qua, xem trang San pham lech nhau)", len(no_sku))
    if hrv_dupes:
        log.warning("Haravan: %d SKU bi trung tren %d variant - moi variant deu nhan cung so ton (xem trang San pham lech nhau)",
                    len(hrv_dupes), sum(len(vs) for vs in hrv_dupes.values()))
    if sapo.duplicate_skus:
        log.warning("Sapo: %d SKU bi trung (khac hoa/thuong) - ton duoc cong don",
                    len(sapo.duplicate_skus))
    for hrv_loc, loc_updates in updates.items():
        log.info("Kho Haravan %s: %d variant can cap nhat", hrv_loc, len(loc_updates))
        for u in loc_updates[:15]:
            old = hrv_stock.get((hrv_loc, u["variant_id"]), 0)
            log.info("  %-25s %5s -> %5s  %s", u["sku_raw"], old, u["new_qty"], u["title"])
        if len(loc_updates) > 15:
            log.info("  ... va %d dong khac (xem file bao cao)", len(loc_updates) - 15)
    untracked = sum(1 for u in updates.values() for v in u if not v["tracked"])
    if untracked:
        log.warning("%d dong thuoc variant Haravan chua bat 'quan ly ton kho'", untracked)
    hrv_names = {l["id"]: l.get("name", "") for l in haravan.locations()}
    report = write_report(updates, hrv_names)
    log.info("Bao cao: %s", report)
    write_comparison(sapo, haravan, mapping, sapo_stock, hrv_stock, by_sku, no_sku, hrv_dupes)
    last_run = {
        "time": datetime.now().strftime("%H:%M %d/%m/%Y"),
        "applied": False,
        "total": total,
        "report": os.path.basename(report),
        "changes": [{"sku": u["sku_raw"], "title": u["title"], "location": hrv_names.get(loc, str(loc)),
                     "old": u["old_qty"], "new": u["new_qty"]}
                    for loc, loc_updates in updates.items() for u in loc_updates],
        "counts": {"matched": len(sapo_skus & hrv_skus), "sapo_only": len(sapo_skus - hrv_skus),
                   # dem theo san pham (variant) cho khop voi trang "San pham lech nhau"
                   "haravan_only": sum(len(by_sku[s]) for s in hrv_skus - sapo_skus),
                   "no_sku": len(no_sku), "dupes": len(hrv_dupes)},
    }
    save_last_run(last_run)

    if not total:
        log.info("Ton kho da khop, khong can cap nhat.")
    elif apply and confirm and not confirm(total):
        log.info("Da huy, khong cap nhat gi.")
        return 0
    elif apply:
        for hrv_loc, loc_updates in updates.items():
            if loc_updates:
                haravan.set_inventory(hrv_loc, loc_updates)
        last_run["applied"] = True
        save_last_run(last_run)
        log.info("Hoan tat dong bo %d dong sang Haravan.", total)
    else:
        log.info("Day la CHAY THU. Them --apply de cap nhat that.")
    return total


def check():
    sapo, haravan = Sapo(), Haravan()
    for l in sapo.locations():
        log.info("Sapo chi nhanh:  id=%s  ten=%s", l["id"], l.get("name"))
    for l in haravan.locations():
        log.info("Haravan kho:     id=%s  ten=%s", l["id"], l.get("name"))
    log.info("Ket noi OK")


def main():
    parser = argparse.ArgumentParser(description="Dong bo ton kho Sapo -> Haravan theo SKU")
    parser.add_argument("command", nargs="?", default="sync", choices=["sync", "check"])
    parser.add_argument("--apply", action="store_true", help="cap nhat that (mac dinh chi chay thu)")
    parser.add_argument("--loop", type=float, metavar="PHUT", help="lap lai moi N phut")
    parser.add_argument("--sku", help="chi cap nhat 1 SKU (de chay thu tren 1 san pham)")
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")  # console Windows mac dinh khong in duoc tieng Viet
    os.makedirs(os.path.join(BASE_DIR, "logs"), exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(os.path.join(BASE_DIR, "logs", "sync.log"), encoding="utf-8")],
    )
    load_env(os.path.join(BASE_DIR, ".env"))

    if args.command == "check":
        check()
        return
    while True:
        try:
            run_sync(args.apply, only_sku=args.sku)
        except Exception as e:
            if not args.loop:
                raise
            log.error("Loi khi dong bo: %s", e)
        if not args.loop:
            break
        log.info("Cho %s phut...", args.loop)
        time.sleep(args.loop * 60)


if __name__ == "__main__":
    main()
