"""App dong bo ton kho Sapo -> Haravan (giao dien cua so).

Mo bang: bam dup loi tat "Dong bo ton kho" tren Desktop, hoac: pythonw app.py
"""

import glob
import logging
import os
import queue
import threading
import tkinter as tk
from datetime import datetime, timedelta
from tkinter import messagebox, scrolledtext, ttk

import sync

APP_TITLE = "Đồng bộ tồn kho Sapo → Haravan"


class QueueHandler(logging.Handler):
    """Day log tu luong chay nen sang giao dien."""

    def __init__(self, q):
        super().__init__()
        self.q = q

    def emit(self, record):
        self.q.put(self.format(record))


class App:
    def __init__(self, root):
        self.root = root
        self.busy = False
        self.auto_job = None
        self.log_queue = queue.Queue()

        root.title(APP_TITLE)
        root.geometry("900x560")
        root.minsize(640, 400)

        bar = ttk.Frame(root, padding=8)
        bar.pack(fill="x")
        self.buttons = [
            ttk.Button(bar, text="Kiểm tra kết nối", command=lambda: self.start(self.do_check)),
            ttk.Button(bar, text="Chạy thử (xem chênh lệch)", command=lambda: self.start(self.do_preview)),
            ttk.Button(bar, text="Đồng bộ ngay", command=lambda: self.start(self.do_apply)),
        ]
        for b in self.buttons:
            b.pack(side="left", padx=(0, 6))
        ttk.Button(bar, text="Mở báo cáo mới nhất", command=self.open_report).pack(side="left", padx=(0, 6))

        auto = ttk.Frame(root, padding=(8, 0, 8, 8))
        auto.pack(fill="x")
        self.auto_on = tk.BooleanVar(value=False)
        self.minutes = tk.StringVar(value="15")
        ttk.Checkbutton(auto, text="Tự động đồng bộ mỗi", variable=self.auto_on,
                        command=self.toggle_auto).pack(side="left")
        ttk.Spinbox(auto, from_=5, to=1440, width=5, textvariable=self.minutes).pack(side="left", padx=4)
        ttk.Label(auto, text="phút").pack(side="left")
        self.status = ttk.Label(auto, text="Sẵn sàng", foreground="#555")
        self.status.pack(side="right")

        self.text = scrolledtext.ScrolledText(root, font=("Consolas", 10), state="disabled", wrap="none")
        self.text.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        handler = QueueHandler(self.log_queue)
        handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
        sync.log.addHandler(handler)
        sync.log.setLevel(logging.INFO)
        os.makedirs(os.path.join(sync.BASE_DIR, "logs"), exist_ok=True)
        file_handler = logging.FileHandler(os.path.join(sync.BASE_DIR, "logs", "sync.log"), encoding="utf-8")
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        sync.log.addHandler(file_handler)
        sync.load_env(os.path.join(sync.BASE_DIR, ".env"))

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.poll_log()

    # ------------------------------------------------------------ helpers

    def poll_log(self):
        while not self.log_queue.empty():
            self.text.configure(state="normal")
            self.text.insert("end", self.log_queue.get() + "\n")
            self.text.see("end")
            self.text.configure(state="disabled")
        self.root.after(200, self.poll_log)

    def set_busy(self, busy, status=None):
        self.busy = busy
        for b in self.buttons:
            b.configure(state="disabled" if busy else "normal")
        if status:
            self.status.configure(text=status)

    def start(self, job):
        if self.busy:
            return
        self.set_busy(True, "Đang chạy...")

        def worker():
            try:
                job()
            except SystemExit as e:  # sync.require() bao thieu cau hinh bang sys.exit
                sync.log.error("%s", e)
            except Exception as e:
                sync.log.error("LỖI: %s", e)
            finally:
                self.root.after(0, self.finish)

        threading.Thread(target=worker, daemon=True).start()

    def finish(self):
        self.set_busy(False, "Sẵn sàng")
        if self.auto_on.get():
            self.schedule_auto()

    def ask_main_thread(self, title, message):
        """Hien hop thoai xac nhan tu luong chay nen (tkinter chi cho goi o luong chinh)."""
        answer, done = {}, threading.Event()

        def ask():
            answer["ok"] = messagebox.askyesno(title, message, parent=self.root)
            done.set()

        self.root.after(0, ask)
        done.wait()
        return answer["ok"]

    # ------------------------------------------------------------ actions

    def do_check(self):
        sync.check()

    def do_preview(self):
        sync.run_sync(apply=False)

    def do_apply(self):
        sync.run_sync(apply=True, confirm=lambda n: self.ask_main_thread(
            "Xác nhận đồng bộ",
            f"Sẽ cập nhật {n} dòng tồn kho lên Haravan.\n\nTiếp tục?"))

    def do_auto(self):
        sync.log.info("--- Tự động đồng bộ ---")
        sync.run_sync(apply=True)

    def open_report(self):
        reports = sorted(glob.glob(os.path.join(sync.BASE_DIR, "reports", "*.csv")))
        if not reports:
            messagebox.showinfo(APP_TITLE, "Chưa có báo cáo. Bấm 'Chạy thử' trước.")
            return
        os.startfile(reports[-1])

    # ------------------------------------------------------------ auto mode

    def toggle_auto(self):
        if self.auto_on.get():
            if not messagebox.askyesno(APP_TITLE, "Bật tự động: app sẽ cập nhật thật lên Haravan theo chu kỳ, "
                                                  "không hỏi lại.\nCần để máy và app luôn mở.\n\nBật?"):
                self.auto_on.set(False)
                return
            sync.log.info("Đã bật tự động đồng bộ mỗi %s phút", self.minutes.get())
            self.start(self.do_auto)
        else:
            if self.auto_job:
                self.root.after_cancel(self.auto_job)
                self.auto_job = None
            sync.log.info("Đã tắt tự động đồng bộ")
            self.status.configure(text="Sẵn sàng")

    def schedule_auto(self):
        try:
            minutes = max(float(self.minutes.get()), 5)
        except ValueError:
            minutes = 15
        if self.auto_job:
            self.root.after_cancel(self.auto_job)
        self.auto_job = self.root.after(int(minutes * 60_000), lambda: self.start(self.do_auto))
        next_run = datetime.now() + timedelta(minutes=minutes)
        self.status.configure(text=f"Tự động: lần tới lúc {next_run:%H:%M}")

    def on_close(self):
        if self.busy and not messagebox.askyesno(APP_TITLE, "Đang đồng bộ dở. Vẫn đóng app?"):
            return
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
