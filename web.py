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
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

import sync

SETTINGS_FILE = os.path.join(sync.BASE_DIR, "settings.json")
REPORT_DIR = os.path.join(sync.BASE_DIR, "reports")
KEEP_REPORTS = 50


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = None  # ten viec dang chay
        self.last_result = ""
        self.last_auto_run = None
        self.next_auto_run = None
        self.logs = collections.deque(maxlen=1000)
        self.settings = {"auto": False, "minutes": 15}
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                self.settings.update(json.load(f))

    def save_settings(self):
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(self.settings, f)


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


# ---------------------------------------------------------------- jobs

JOBS = {
    "check": ("Kiểm tra kết nối", lambda: sync.check()),
    "preview": ("Chạy thử", lambda: sync.run_sync(apply=False)),
    "apply": ("Đồng bộ ngay", lambda: sync.run_sync(apply=True)),
    "auto": ("Tự động đồng bộ", lambda: sync.run_sync(apply=True)),
}


def start_job(name):
    """Chay viec trong luong nen. Tra ve False neu dang co viec khac chay."""
    with state.lock:
        if state.running:
            return False
        state.running = name
    label, fn = JOBS[name]

    def worker():
        sync.log.info("===== %s =====", label)
        try:
            result = fn()
            state.last_result = f"{label}: xong" + (f" ({result} dòng)" if isinstance(result, int) else "")
        except BaseException as e:  # sync.require() dung sys.exit khi thieu cau hinh
            sync.log.error("LỖI: %s", e)
            state.last_result = f"{label}: LỖI - {e}"
        finally:
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
            self.send(200, PAGE, "text/html; charset=utf-8")
        elif path == "/api/status":
            reports = sorted((f for f in os.listdir(REPORT_DIR) if f.endswith(".csv")), reverse=True) \
                if os.path.isdir(REPORT_DIR) else []
            self.send(200, json.dumps({
                "running": JOBS[state.running][0] if state.running else None,
                "last_result": state.last_result,
                "settings": state.settings,
                "next_auto_run": state.next_auto_run.strftime("%H:%M %d/%m") if state.next_auto_run else None,
                "logs": list(state.logs)[-400:],
                "reports": reports[:20],
            }, ensure_ascii=False))
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
            self.send(200 if ok else 409, json.dumps({"ok": ok}))
        elif path == "/api/settings":
            minutes = max(float(body.get("minutes", state.settings["minutes"])), 5)
            turning_on = bool(body.get("auto")) and not state.settings["auto"]
            state.settings = {"auto": bool(body.get("auto")), "minutes": minutes}
            state.save_settings()
            if turning_on:
                state.last_auto_run = None  # chay ngay khi vua bat
            sync.log.info("Cài đặt: tự động %s, mỗi %g phút", "BẬT" if state.settings["auto"] else "TẮT", minutes)
            wake_scheduler.set()
            self.send(200, json.dumps({"ok": True}))
        else:
            self.send(404, json.dumps({"ok": False}))


PAGE = """<!doctype html>
<html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Đồng bộ tồn kho</title>
<style>
:root { --bg:#f6f7f9; --card:#fff; --text:#1d2330; --muted:#667085; --line:#e4e7ec;
        --primary:#2563eb; --danger:#dc2626; --ok:#16a34a; --log:#0f172a; --logtext:#e2e8f0; }
* { box-sizing:border-box; }
body { margin:0; font:15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; background:var(--bg); color:var(--text); }
main { max-width:1000px; margin:0 auto; padding:20px 16px; }
h1 { font-size:22px; margin:0 0 4px; }
.sub { color:var(--muted); margin:0 0 20px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px; margin-bottom:16px; }
.row { display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
button { font:inherit; padding:9px 14px; border-radius:8px; border:1px solid var(--line); background:#fff; cursor:pointer; }
button.primary { background:var(--primary); border-color:var(--primary); color:#fff; }
button:disabled { opacity:.5; cursor:not-allowed; }
.status { margin-top:12px; color:var(--muted); }
.status b.run { color:var(--primary); } .status b.idle { color:var(--ok); }
input[type=number] { width:70px; font:inherit; padding:6px; border:1px solid var(--line); border-radius:6px; }
pre { background:var(--log); color:var(--logtext); border-radius:8px; padding:12px; height:420px; overflow:auto;
      font:12.5px/1.5 Consolas, Menlo, monospace; white-space:pre; margin:0; }
ul { margin:0; padding-left:18px; } a { color:var(--primary); }
h2 { font-size:16px; margin:0 0 10px; }
</style></head><body><main>
<h1>Đồng bộ tồn kho Sapo → Haravan</h1>
<p class="sub">Sapo là nguồn chuẩn. Hà Nội → Kho Hà Nội, HCM → Kho Hồ Chí Minh, ghép theo SKU.</p>

<div class="card">
  <div class="row">
    <button data-job="check">Kiểm tra kết nối</button>
    <button data-job="preview">Chạy thử (xem chênh lệch)</button>
    <button data-job="apply" class="primary">Đồng bộ ngay</button>
  </div>
  <div class="status" id="status">Đang tải...</div>
</div>

<div class="card">
  <h2>Tự động đồng bộ</h2>
  <div class="row">
    <label><input type="checkbox" id="auto"> Bật, chạy mỗi</label>
    <input type="number" id="minutes" min="5" step="1"> phút
    <button id="save">Lưu</button>
    <span id="next" class="status" style="margin:0"></span>
  </div>
</div>

<div class="card"><h2>Nhật ký</h2><pre id="log"></pre></div>
<div class="card"><h2>Báo cáo (CSV, mở bằng Excel)</h2><ul id="reports"></ul></div>
</main>
<script>
const $ = s => document.querySelector(s);
let loaded = false, stick = true;
$('#log').addEventListener('scroll', e => { const el = e.target; stick = el.scrollTop + el.clientHeight >= el.scrollHeight - 20; });

async function refresh() {
  try {
    const r = await fetch('/api/status'); const d = await r.json();
    document.querySelectorAll('[data-job]').forEach(b => b.disabled = !!d.running);
    $('#status').innerHTML = d.running ? '<b class="run">Đang chạy: ' + d.running + '...</b>'
      : '<b class="idle">Sẵn sàng</b>' + (d.last_result ? ' — lần gần nhất: ' + escapeHtml(d.last_result) : '');
    if (!loaded) { $('#auto').checked = d.settings.auto; $('#minutes').value = d.settings.minutes; loaded = true; }
    $('#next').textContent = d.settings.auto ? (d.next_auto_run ? 'Lần tới: ' + d.next_auto_run : '') : 'Đang tắt';
    const log = $('#log'); log.textContent = d.logs.join('\\n'); if (stick) log.scrollTop = log.scrollHeight;
    $('#reports').innerHTML = d.reports.map(f => '<li><a href="/reports/' + encodeURIComponent(f) + '">' + f + '</a></li>').join('') || '<li>Chưa có</li>';
  } catch (e) { $('#status').textContent = 'Mất kết nối tới máy chủ...'; }
}
function escapeHtml(s) { return s.replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

document.querySelectorAll('[data-job]').forEach(b => b.onclick = async () => {
  if (b.dataset.job === 'apply' && !confirm('Cập nhật THẬT tồn kho lên Haravan?\\nNên bấm "Chạy thử" và xem báo cáo trước.')) return;
  const r = await fetch('/api/run/' + b.dataset.job, {method: 'POST'});
  if (r.status === 409) alert('Đang có việc khác chạy, đợi xong rồi bấm lại.');
  refresh();
});
$('#save').onclick = async () => {
  const auto = $('#auto').checked;
  if (auto && !confirm('Bật tự động: hệ thống sẽ cập nhật THẬT lên Haravan theo chu kỳ, không hỏi lại. Bật?')) return;
  await fetch('/api/settings', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({auto, minutes: Number($('#minutes').value) || 15})});
  refresh();
};
refresh(); setInterval(refresh, 2000);
</script></body></html>"""


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
