"""Web app dong bo ton kho Sapo -> Haravan (chay tren VPS).

Chay:  python3 web.py           (mac dinh cong 8080, doi bang WEB_PORT trong .env)
Dang nhap bang WEB_USER / WEB_PASSWORD trong .env.
"""

import base64
import collections
import hmac
import json
import logging
import os
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

import sync

SETTINGS_FILE = os.path.join(sync.BASE_DIR, "settings.json")
REPORT_DIR = os.path.join(sync.BASE_DIR, "reports")
HISTORY_FILE = os.path.join(REPORT_DIR, "history.json")
KEEP_REPORTS = 50
KEEP_HISTORY = 200


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = None  # ten viec dang chay
        self.last_error = None  # loi de hieu cua lan chay gan nhat (None neu thanh cong)
        self.last_auto_run = None
        self.next_auto_run = None
        self.logs = collections.deque(maxlen=1000)
        self.settings = {"auto": False, "minutes": 30, **load_json(SETTINGS_FILE, {})}
        self.history = load_json(HISTORY_FILE, [])

    def add_history(self, entry):
        self.history.insert(0, entry)
        del self.history[KEEP_HISTORY:]
        save_json(HISTORY_FILE, self.history)


state = State()
wake_scheduler = threading.Event()


class MemoryHandler(logging.Handler):
    def emit(self, record):
        state.logs.append(self.format(record))


def setup_logging():
    os.makedirs(os.path.join(sync.BASE_DIR, "logs"), exist_ok=True)
    mem = MemoryHandler()
    mem.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%d/%m %H:%M:%S"))
    file_handler = logging.FileHandler(os.path.join(sync.BASE_DIR, "logs", "sync.log"), encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    for h in (mem, file_handler, console):
        sync.log.addHandler(h)
    sync.log.setLevel(logging.INFO)


def cleanup_reports():
    if not os.path.isdir(REPORT_DIR):
        return
    files = sorted(f for f in os.listdir(REPORT_DIR) if f.endswith(".csv"))
    for f in files[:-KEEP_REPORTS]:
        os.remove(os.path.join(REPORT_DIR, f))


def friendly_error(e):
    """Doi loi ky thuat thanh cau de hieu cho nguoi dung."""
    msg = str(e)
    if "Thieu cau hinh" in msg or "LOCATION_MAP" in msg:
        return "Hệ thống chưa được cấu hình đủ. Hãy báo David Dao."
    if "mysapo.net" in msg and ("HTTP 401" in msg or "HTTP 403" in msg):
        return "Sapo từ chối truy cập (key sai, hết hạn hoặc thiếu quyền). Hãy báo David Dao."
    if "haravan.com" in msg and ("HTTP 401" in msg or "HTTP 403" in msg):
        return "Haravan từ chối truy cập (token sai, hết hạn hoặc thiếu quyền). Hãy báo David Dao."
    if "that bai sau" in msg or "loi mang" in msg:
        return "Không kết nối được tới Sapo/Haravan. Hãy thử lại sau ít phút."
    return "Có lỗi xảy ra. Hãy thử lại; nếu vẫn lỗi, báo David Dao (chi tiết ở mục Thông tin kỹ thuật)."


# ---------------------------------------------------------------- jobs

JOBS = {
    "check": ("Kiểm tra kết nối", lambda: sync.check()),
    "preview": ("Kiểm tra tồn kho", lambda: sync.run_sync(apply=False)),
    "apply": ("Cập nhật lên Haravan", lambda: sync.run_sync(apply=True)),
    "auto": ("Tự động cập nhật", lambda: sync.run_sync(apply=True)),
}


def result_message(name, total):
    if name == "check":
        return "Kết nối Sapo và Haravan bình thường"
    if not total:
        return "Tồn kho đã khớp, không có gì thay đổi"
    if name == "preview":
        return f"Phát hiện {total} thay đổi (chưa cập nhật)"
    return f"Đã cập nhật {total} thay đổi lên Haravan"


def start_job(name):
    """Chay viec trong luong nen. Tra ve False neu dang co viec khac chay."""
    with state.lock:
        if state.running:
            return False
        state.running = name
    label, fn = JOBS[name]

    def worker():
        sync.log.info("===== %s =====", label)
        entry = {"time": datetime.now().strftime("%H:%M %d/%m/%Y"), "action": label, "ok": True,
                 "report": None}
        try:
            total = fn()
            state.last_error = None
            entry["message"] = result_message(name, total)
            if isinstance(total, int):
                entry.update(total=total, applied=name in ("apply", "auto"))
            if name != "check":
                entry["report"] = load_json(sync.LAST_RUN_FILE, {}).get("report")
        except BaseException as e:  # sync.require() dung sys.exit khi thieu cau hinh
            sync.log.error("LỖI: %s", e)
            state.last_error = friendly_error(e)
            entry.update(ok=False, message=state.last_error)
        finally:
            entry["time"] = datetime.now().strftime("%H:%M %d/%m/%Y")
            state.add_history(entry)
            cleanup_reports()
            with state.lock:
                state.running = None

    threading.Thread(target=worker, daemon=True).start()
    return True


def scheduler():
    while True:
        s = state.settings
        if s["auto"]:
            minutes = max(float(s["minutes"]), 5)
            due = (state.last_auto_run or datetime.min) + timedelta(minutes=minutes)
            if datetime.now() >= due and start_job("auto"):
                state.last_auto_run = datetime.now()
                due = state.last_auto_run + timedelta(minutes=minutes)
            state.next_auto_run = due
        else:
            state.next_auto_run = None
        wake_scheduler.wait(20)
        wake_scheduler.clear()


# ---------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    server_version = "SyncTonKho"

    def log_message(self, fmt, *args):
        pass  # khong ghi log moi request

    def authorized(self):
        user, password = os.environ.get("WEB_USER", ""), os.environ.get("WEB_PASSWORD", "")
        expected = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        return hmac.compare_digest(self.headers.get("Authorization", ""), expected)

    def send(self, code, body, content_type="application/json; charset=utf-8", extra=None):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, data, code=200):
        self.send(code, json.dumps(data, ensure_ascii=False))

    def guard(self):
        if self.authorized():
            return True
        self.send(401, "Can dang nhap", "text/plain; charset=utf-8",
                  {"WWW-Authenticate": 'Basic realm="Dong bo ton kho", charset="UTF-8"'})
        return False

    def do_GET(self):
        if not self.guard():
            return
        path = urlparse(self.path).path
        if path == "/":
            with open(UI_FILE, "rb") as f:  # doc moi lan de sua giao dien khong can khoi dong lai
                self.send(200, f.read(), "text/html; charset=utf-8")
        elif path == "/api/status":
            last = load_json(sync.LAST_RUN_FILE, None)
            if last:
                last.pop("changes", None)
            self.send_json({
                "running": JOBS[state.running][0] if state.running else None,
                "error": state.last_error,
                "last_run": last,
                "settings": state.settings,
                "next_auto_run": state.next_auto_run.strftime("%H:%M") if state.next_auto_run else None,
                "logs": list(state.logs)[-300:],
            })
        elif path == "/api/last_run":
            self.send_json(load_json(sync.LAST_RUN_FILE, {}))
        elif path == "/api/compare":
            self.send_json(load_json(sync.COMPARE_FILE, {}))
        elif path == "/api/history":
            self.send_json(state.history)
        elif path.startswith("/reports/"):
            name = os.path.basename(unquote(path))
            full = os.path.join(REPORT_DIR, name)
            if not name.endswith(".csv") or not os.path.isfile(full):
                self.send(404, "Khong tim thay", "text/plain; charset=utf-8")
                return
            with open(full, "rb") as f:
                self.send(200, f.read(), "text/csv; charset=utf-8",
                          {"Content-Disposition": f'attachment; filename="{name}"'})
        else:
            self.send(404, "Khong tim thay", "text/plain; charset=utf-8")

    def do_POST(self):
        if not self.guard():
            return
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if path.startswith("/api/run/") and path.rsplit("/", 1)[1] in ("check", "preview", "apply"):
            ok = start_job(path.rsplit("/", 1)[1])
            self.send_json({"ok": ok}, 200 if ok else 409)
        elif path == "/api/settings":
            minutes = max(float(body.get("minutes", state.settings["minutes"])), 5)
            turning_on = bool(body.get("auto")) and not state.settings["auto"]
            state.settings = {"auto": bool(body.get("auto")), "minutes": minutes}
            save_json(SETTINGS_FILE, state.settings)
            if turning_on:
                state.last_auto_run = None  # chay ngay khi vua bat
            sync.log.info("Cài đặt: tự động %s, mỗi %g phút", "BẬT" if state.settings["auto"] else "TẮT", minutes)
            wake_scheduler.set()
            self.send_json({"ok": True})
        else:
            self.send_json({"ok": False}, 404)


UI_FILE = os.path.join(sync.BASE_DIR, "ui.html")


def main():
    sync.load_env(os.path.join(sync.BASE_DIR, ".env"))
    if not os.environ.get("WEB_USER") or not os.environ.get("WEB_PASSWORD"):
        raise SystemExit("Can dat WEB_USER va WEB_PASSWORD trong .env truoc khi chay web")
    setup_logging()
    threading.Thread(target=scheduler, daemon=True).start()
    host = os.environ.get("WEB_HOST", "0.0.0.0")
    port = int(os.environ.get("WEB_PORT", "8080"))
    sync.log.info("Web chay tai http://%s:%s", host, port)
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
