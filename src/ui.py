# -*- coding: utf-8 -*-
"""
workbuddy-codebuddy-chat-sync 管理窗口

纯标准库 tkinter 实现，只负责展示和按钮调用。
账号发现、读写数据库、合并文件全部复用 sync_core，界面不直接碰数据。
"""
import datetime
import json
import os
import queue
import re
import threading
import tkinter as tk
import tkinter.font as tkfont
import traceback
from tkinter import messagebox, simpledialog, ttk

import accounts as acct
import sync_core as core

# 同步页的「最后状态」跟着账号数据一起放在便携目录里；
# 同步配置与日志仍由 sync_core 管理，位置和格式都不变。
LAST_SYNC_PATH = os.path.join(str(acct.DATA_DIR), "last-sync.json")
LEGACY_LAST_SYNC_PATH = os.path.join(core.DATA_DIR, "last-sync.json")

# 手动同步、界面切换、托盘切换、定时同步共用同一把锁，
# 避免两个写操作同时进 SQLite。
SYNC_LOCK = threading.Lock()
RECORD_LOCK = threading.Lock()

FAMILY = "Microsoft YaHei UI"
BG = "#F4F6F9"
CARD = "#FFFFFF"
BORDER = "#E1E7F0"
TEXT = "#1F2937"
MUTED = "#6B7280"
ACCENT = "#2F6FED"
CURRENT_BG = "#EDF4FF"
OK_GREEN = "#15803D"
OK_BG = "#E8F7EE"
WARN = "#B45309"
WARN_BG = "#FFF4E2"
DANGER = "#C2410C"
DANGER_BG = "#FDECEC"
SOFT_BG = "#F7F9FC"

PRODUCTS = (("wb", "WorkBuddy"), ("cb", "CodeBuddy"))
PRODUCT_TITLES = dict(PRODUCTS)

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)


# ---------------------------------------------------------------- 上次同步记录

def record_last_sync(result, source):
    """把一次同步的结果落盘，窗口和下次启动都能看到。"""
    payload = {
        "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": source,
        "result": result,
    }
    try:
        os.makedirs(os.path.dirname(LAST_SYNC_PATH), exist_ok=True)
        with RECORD_LOCK:
            with open(LAST_SYNC_PATH, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception:
        core.log("写入最后同步记录失败：" + traceback.format_exc(limit=1))
    return payload


def load_last_sync():
    for path in (LAST_SYNC_PATH, LEGACY_LAST_SYNC_PATH):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            continue
    return {}


# ---------------------------------------------------------------- 账号快照

def _uuid_dirs(base):
    out = []
    try:
        for name in os.listdir(base):
            if UUID_RE.match(name) and os.path.isdir(os.path.join(base, name)):
                out.append(name)
    except Exception:
        pass
    return out


def _product_snapshot(key, cfg):
    """
    每个软件各自识别账号：
      数据库里出现过的 user_id + 该软件数据目录下的 UUID 目录名。
    只读，不修改任何东西。
    """
    if key == "wb":
        counts = core.wb_counts()
        current = core.wb_current()
        available = core.wb_available()
        path = core.WB_DB
        base = os.path.join(core.HOME, ".workbuddy")
    else:
        counts = core.cb_counts()
        current = core.cb_current()
        available = core.cb_available()
        path = core.CB_DB
        base = core.CB_DATA

    uids = []
    for uid in list(counts.keys()) + _uuid_dirs(base):
        if uid and UUID_RE.match(uid) and uid not in uids:
            uids.append(uid)
    # 当前账号不是标准 UUID 时也要显示出来，否则用户看不到自己正在用的号
    if current and current not in uids:
        uids.append(current)

    rows = []
    for uid in uids:
        rows.append(
            {
                "uid": uid,
                "label": core.account_label(uid, cfg),
                "count": int(counts.get(uid, 0) or 0),
                "current": uid == current,
            }
        )
    rows.sort(key=lambda r: (0 if r["current"] else 1, -r["count"], r["label"]))

    return {
        "title": PRODUCT_TITLES[key],
        "rows": rows,
        "current": current,
        "current_label": core.account_label(current, cfg) if current else "",
        "available": available,
        "path": path,
        "total": sum(r["count"] for r in rows),
    }


def build_snapshot():
    cfg = core.load_config()
    # 第一次要扫日志拿昵称，放在后台线程里做，避免窗口卡住
    try:
        core.detected_names()
    except Exception:
        pass
    return {
        "cfg": cfg,
        "products": {key: _product_snapshot(key, cfg) for key, _ in PRODUCTS},
    }


# ---------------------------------------------------------------- 窗口

class App:
    def __init__(self, root, icon_image=None, auto_enabled=True):
        self.root = root
        self.auto_enabled = bool(auto_enabled)
        self.on_minimized = None     # 收起到托盘时弹一次气泡
        self._queue = queue.Queue()
        self._busy = False
        self._gen = 0
        self._snapshot = None
        self._log_win = None
        self._icon_photo = None
        self._action_buttons = []
        self._hint_shown = False
        self._acct_busy = False
        self._acct_groups = []
        self._quota_labels = {}

        self._setup_window(icon_image)
        self._build_ui()
        self._load_last_sync()
        self.root.after(80, self._pump)

    # ------------------------------------------------------------ 基础

    def _setup_window(self, icon_image):
        self.root.title("账号记录同步")
        self.root.configure(bg=BG)
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        width = min(920, max(760, screen_w - 80))
        height = min(720, max(500, screen_h - 90))
        self.root.minsize(min(760, width), min(520, height))
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 3)
        self.root.geometry("%dx%d+%d+%d" % (width, height, x, y))
        self.root.protocol("WM_DELETE_WINDOW", self.hide)

        for name in (
            "TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont",
            "TkCaptionFont", "TkSmallCaptionFont", "TkIconFont", "TkTooltipFont",
        ):
            try:
                tkfont.nametofont(name).configure(family=FAMILY, size=9)
            except Exception:
                pass

        if icon_image is not None:
            try:
                from PIL import ImageTk

                self._icon_photo = ImageTk.PhotoImage(icon_image)
                self.root.iconphoto(True, self._icon_photo)
            except Exception:
                pass

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(
            ".", background=BG, foreground=TEXT, font=(FAMILY, 9),
            bordercolor=BORDER, focuscolor=BG,
        )
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=TEXT, font=(FAMILY, 9))
        style.configure(
            "TButton", background="#EDF1F7", foreground=TEXT, borderwidth=0,
            focusthickness=0, padding=(12, 6), font=(FAMILY, 9), relief="flat",
        )
        style.map(
            "TButton",
            background=[("active", "#E1E7F1"), ("pressed", "#D6DEEA"), ("disabled", "#F2F4F8")],
            foreground=[("disabled", "#A6AEBB")],
        )
        style.configure(
            "Primary.TButton", background=ACCENT, foreground="#FFFFFF",
            padding=(14, 6), font=(FAMILY, 9, "bold"), borderwidth=0,
        )
        style.map(
            "Primary.TButton",
            background=[("active", "#255CCB"), ("pressed", "#1F4FB3"), ("disabled", "#A9C2F5")],
            foreground=[("disabled", "#F3F7FF")],
        )
        style.configure(
            "Small.TButton", padding=(9, 3), font=(FAMILY, 8), background="#EFF3F9",
        )
        style.map("Small.TButton", background=[("active", "#E2E9F3"), ("pressed", "#D7E0EE")])
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure(
            "TNotebook.Tab", background=BG, foreground=MUTED,
            padding=(18, 8), font=(FAMILY, 10), borderwidth=0,
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", CARD)],
            foreground=[("selected", ACCENT)],
            expand=[("selected", (0, 0, 0, 1))],
        )
        style.configure(
            "TScrollbar", background="#D8DFE9", troughcolor=BG,
            borderwidth=0, arrowsize=12,
        )

    def _build_ui(self):
        header = tk.Frame(self.root, bg=BG)
        header.pack(fill="x", padx=20, pady=(12, 6))
        title_box = tk.Frame(header, bg=BG)
        title_box.pack(side="left", fill="x", expand=True)
        tk.Label(
            title_box, text="账号记录同步", bg=BG, fg=TEXT,
            font=(FAMILY, 16, "bold"), anchor="w",
        ).pack(anchor="w")
        tk.Label(
            title_box,
            text="把本地聊天记录归到指定账号；「账号」页查看 WorkBuddy / Trae / Qoder 的登录态与额度。",
            bg=BG, fg=MUTED, font=(FAMILY, 9), anchor="w",
        ).pack(anchor="w", pady=(2, 0))
        self.refresh_btn = ttk.Button(header, text="刷新", command=self.refresh_current)
        self.refresh_btn.pack(side="right", padx=(12, 0))

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=20, pady=(4, 12))
        self.sync_page = tk.Frame(self.notebook, bg=BG)
        self.accounts_page = tk.Frame(self.notebook, bg=BG)
        self.notebook.add(self.sync_page, text="  聊天记录同步  ")
        self.notebook.add(self.accounts_page, text="  账号  ")
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        self._build_sync_page(self.sync_page)
        self._build_accounts_page(self.accounts_page)

        self._action_buttons = [self.refresh_btn, self.sync_btn]

    def _build_sync_page(self, parent):
        footer = tk.Frame(
            parent, bg=CARD, highlightbackground=BORDER, highlightthickness=1
        )
        footer.pack(side="bottom", fill="x", pady=(6, 0))
        self._build_footer(footer)

        body = tk.Frame(parent, bg=BG)
        body.pack(fill="both", expand=True, pady=(4, 0))

        self.canvas = tk.Canvas(body, bg=BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.inner = tk.Frame(self.canvas, bg=BG)
        self._canvas_window = self.canvas.create_window(
            (0, 0), window=self.inner, anchor="nw"
        )
        self.inner.bind("<Configure>", self._on_inner_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<Enter>", self._bind_wheel)
        self.canvas.bind("<Leave>", self._unbind_wheel)

        self.loading_label = tk.Label(
            self.inner, text="正在读取账号信息…", bg=BG, fg=MUTED,
            font=(FAMILY, 10),
        )
        self.loading_label.pack(pady=48)

    def _build_footer(self, footer):
        line1 = tk.Frame(footer, bg=CARD)
        line1.pack(fill="x", padx=16, pady=(10, 3))
        tk.Label(
            line1, text="上次同步", bg=CARD, fg=TEXT, font=(FAMILY, 10, "bold")
        ).pack(side="left")
        self.last_time = tk.Label(
            line1, text="尚无记录", bg=CARD, fg=MUTED, font=(FAMILY, 9)
        )
        self.last_time.pack(side="left", padx=(10, 0))
        self.auto_label = tk.Label(
            line1, text="", bg=CARD, fg=MUTED, font=(FAMILY, 9)
        )
        self.auto_label.pack(side="right")
        self.set_auto_state(self.auto_enabled)

        self.last_result = tk.Label(
            footer, text="运行一次同步后，这里会显示结果。", bg=CARD, fg=TEXT,
            font=(FAMILY, 9), justify="left", anchor="w", wraplength=820,
        )
        self.last_result.pack(fill="x", padx=16)

        actions = tk.Frame(footer, bg=CARD)
        actions.pack(fill="x", padx=16, pady=(8, 4))
        self.sync_btn = ttk.Button(
            actions, text="立即同步", style="Primary.TButton", command=self.sync_now
        )
        self.sync_btn.pack(side="left")
        self.log_btn = ttk.Button(actions, text="查看日志", command=self.show_log)
        self.log_btn.pack(side="left", padx=(10, 0))
        self.feedback_label = tk.Label(
            actions, text="", bg=CARD, fg=OK_GREEN, font=(FAMILY, 9)
        )
        self.feedback_label.pack(side="left", padx=(14, 0))

        tk.Label(
            footer,
            text="切换会把该软件的全部历史会话归到所选账号；切换后请重启对应软件。"
                 "关闭窗口只会最小化到托盘，后台同步继续运行。",
            bg=CARD, fg=MUTED, font=(FAMILY, 8), justify="left", anchor="w",
            wraplength=820,
        ).pack(fill="x", padx=16, pady=(0, 10))

    # ------------------------------------------------------------ 账号页

    def _build_accounts_page(self, parent):
        top = tk.Frame(parent, bg=BG)
        top.pack(fill="x", pady=(2, 8))
        titles = tk.Frame(top, bg=BG)
        titles.pack(side="left", fill="x", expand=True)
        tk.Label(
            titles, text="账号与额度", bg=BG, fg=TEXT,
            font=(FAMILY, 15, "bold"), anchor="w",
        ).pack(anchor="w")
        tk.Label(
            titles,
            text="WorkBuddy · Trae · Qoder；打开页面只读本地文件，不会自动联网。",
            bg=BG, fg=MUTED, font=(FAMILY, 9), anchor="w",
        ).pack(anchor="w", pady=(2, 0))

        self.acct_new_btn = ttk.Button(
            top, text="新建 WorkBuddy 账号", style="Primary.TButton",
            command=self.new_account,
        )
        self.acct_new_btn.pack(side="right")
        self.acct_refresh_btn = ttk.Button(
            top, text="刷新列表", command=self.refresh_accounts
        )
        self.acct_refresh_btn.pack(side="right", padx=(0, 8))

        strip = tk.Frame(
            parent, bg=CARD, highlightbackground=BORDER, highlightthickness=1
        )
        strip.pack(fill="x", pady=(0, 10))
        strip_inner = tk.Frame(strip, bg=CARD)
        strip_inner.pack(fill="x", padx=14, pady=8)
        tk.Label(
            strip_inner, text="工具数据目录", bg=CARD, fg=MUTED,
            font=(FAMILY, 8),
        ).pack(side="left")
        self.data_dir_label = tk.Label(
            strip_inner, text=acct.data_dir_text(), bg=CARD, fg=TEXT,
            font=(FAMILY, 9), anchor="w",
        )
        self.data_dir_label.pack(side="left", padx=(8, 0), fill="x", expand=True)
        ttk.Button(
            strip_inner, text="打开", style="Small.TButton",
            command=self.open_tool_data_dir,
        ).pack(side="right")

        body = tk.Frame(parent, bg=BG)
        body.pack(fill="both", expand=True)
        self.acct_canvas = tk.Canvas(body, bg=BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(
            body, orient="vertical", command=self.acct_canvas.yview
        )
        self.acct_canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.acct_canvas.pack(side="left", fill="both", expand=True)

        self.acct_inner = tk.Frame(self.acct_canvas, bg=BG)
        self._acct_window = self.acct_canvas.create_window(
            (0, 0), window=self.acct_inner, anchor="nw"
        )
        self.acct_inner.bind(
            "<Configure>",
            lambda _e: self.acct_canvas.configure(
                scrollregion=self.acct_canvas.bbox("all")
            ),
        )
        self.acct_canvas.bind(
            "<Configure>",
            lambda e: self.acct_canvas.itemconfigure(
                self._acct_window, width=e.width
            ),
        )
        self.acct_canvas.bind("<Enter>", self._bind_acct_wheel)
        self.acct_canvas.bind("<Leave>", self._unbind_acct_wheel)

        self.acct_loading = tk.Label(
            self.acct_inner, text="正在读取本地账号…", bg=BG, fg=MUTED,
            font=(FAMILY, 10),
        )
        self.acct_loading.pack(pady=40)

        self.acct_status = tk.Label(
            parent, text="", bg=BG, fg=MUTED, font=(FAMILY, 8),
            justify="left", anchor="w", wraplength=860,
        )
        self.acct_status.pack(fill="x", pady=(6, 0))

        self.acct_buttons = [self.acct_new_btn, self.acct_refresh_btn]

    # ------------------------------------------------------------ 账号页滚动

    def _bind_acct_wheel(self, _event):
        self.acct_canvas.bind_all("<MouseWheel>", self._on_acct_wheel)

    def _unbind_acct_wheel(self, _event):
        self.acct_canvas.unbind_all("<MouseWheel>")

    def _on_acct_wheel(self, event):
        try:
            self.acct_canvas.yview_scroll(int(-event.delta / 120), "units")
        except Exception:
            pass

    def _chip(self, parent, text, bg, fg):
        return tk.Label(
            parent, text=" %s " % text, bg=bg, fg=fg,
            font=(FAMILY, 8), padx=2, pady=1,
        )

    def _render_acct_group(self, group, first):
        head = tk.Frame(self.acct_inner, bg=BG)
        head.pack(fill="x", padx=2, pady=(0 if first else 12, 6))
        tk.Label(
            head, text=group["title"], bg=BG, fg=TEXT,
            font=(FAMILY, 12, "bold"),
        ).pack(side="left")
        tk.Label(
            head, text=" %d " % len(group["accounts"]), bg="#E7ECF5",
            fg=MUTED, font=(FAMILY, 8), padx=2, pady=1,
        ).pack(side="left", padx=(8, 0))
        tk.Label(
            head, text=group["hint"], bg=BG, fg=MUTED, font=(FAMILY, 8)
        ).pack(side="right")

        if not group["accounts"]:
            card = tk.Frame(
                self.acct_inner, bg=CARD,
                highlightbackground=BORDER, highlightthickness=1,
            )
            card.pack(fill="x", pady=(0, 2))
            tk.Label(
                card, text="未在本机发现该产品的账号数据。",
                bg=CARD, fg=MUTED, font=(FAMILY, 9),
            ).pack(anchor="w", padx=14, pady=14)
            return

        for row in group["accounts"]:
            self._render_acct_row(group["product"], row)

    def _render_acct_row(self, product, row):
        card = tk.Frame(
            self.acct_inner, bg=CARD,
            highlightbackground=BORDER, highlightthickness=1,
        )
        card.pack(fill="x", pady=(0, 6))
        inner = tk.Frame(card, bg=CARD)
        inner.pack(fill="x", padx=14, pady=10)

        head = tk.Frame(inner, bg=CARD)
        head.pack(fill="x")
        tk.Label(
            head, text=row["label"], bg=CARD, fg=TEXT,
            font=(FAMILY, 11, "bold"),
        ).pack(side="left")
        if row.get("isDefault"):
            self._chip(head, "默认", "#EEF3FF", ACCENT).pack(
                side="left", padx=(8, 0)
            )
        if row.get("active"):
            self._chip(head, "当前", OK_BG, OK_GREEN).pack(
                side="left", padx=(6, 0)
            )
        uid = row.get("uid") or ""
        uid_text = (uid[:8] + "…") if len(uid) > 8 else (uid or "未知")
        tk.Label(
            head, text="UID %s" % uid_text, bg=CARD, fg=MUTED,
            font=(FAMILY, 8),
        ).pack(side="left", padx=(10, 0))

        if row["running"]:
            status_text, status_fg, status_bg = "● 运行中", OK_GREEN, OK_BG
        else:
            status_text, status_fg, status_bg = "○ 未运行", MUTED, "#F1F3F6"
        self._chip(head, status_text, status_bg, status_fg).pack(side="right")

        expiry = row.get("expiry") or "未知"
        days = row.get("daysLeft")
        if days is not None and days < 0:
            text, fg, bg = "登录态已过期", DANGER, DANGER_BG
        elif days is not None and days <= 7:
            text, fg, bg = "登录态 %s（%d 天）" % (expiry, days), WARN, WARN_BG
        else:
            text, fg, bg = "登录态 %s" % expiry, MUTED, "#F1F3F6"
        self._chip(head, text, bg, fg).pack(side="right", padx=(0, 6))

        meta = tk.Frame(inner, bg=CARD)
        meta.pack(fill="x", pady=(6, 0))
        path_text = self._shorten(row.get("configDir") or "", 74)
        tk.Label(
            meta, text=path_text, bg=CARD, fg=MUTED,
            font=(FAMILY, 8), anchor="w",
        ).pack(side="left")
        note = row.get("note") or ""
        if note:
            tk.Label(
                meta, text=note, bg=CARD,
                fg=WARN if "限制" in note else MUTED,
                font=(FAMILY, 8),
            ).pack(side="right", padx=(10, 0))

        foot = tk.Frame(inner, bg=CARD)
        foot.pack(fill="x", pady=(8, 0))
        qbox = tk.Frame(foot, bg=SOFT_BG)
        qbox.pack(side="left", fill="x", expand=True)
        tk.Label(
            qbox, text="额度", bg=SOFT_BG, fg=MUTED, font=(FAMILY, 8)
        ).pack(side="left", padx=(10, 6), pady=6)
        quota = row.get("quota") or {}
        if quota:
            quota_text = self._quota_text(quota)
            quota_fg = ACCENT if quota.get("mode") == "api" else WARN
        else:
            quota_text = "未查询，点右侧「刷新额度」"
            quota_fg = MUTED
        quota_label = tk.Label(
            qbox, text=quota_text, bg=SOFT_BG, fg=quota_fg,
            font=(FAMILY, 9), anchor="w",
        )
        quota_label.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self._quota_labels[row["key"]] = quota_label

        actions = tk.Frame(foot, bg=CARD)
        actions.pack(side="right", padx=(10, 0))
        ttk.Button(
            actions, text="刷新额度", style="Small.TButton",
            command=lambda: self._refresh_quota(product, row),
        ).pack(side="left")
        if row.get("canSwitch"):
            ttk.Button(
                actions, text="置前", style="Small.TButton",
                command=lambda: self._switch_row(row),
            ).pack(side="left", padx=(6, 0))
        if row.get("canStop"):
            ttk.Button(
                actions, text="停止", style="Small.TButton",
                command=lambda: self._stop_row(row),
            ).pack(side="left", padx=(6, 0))
        ttk.Button(
            actions, text="打开目录", style="Small.TButton",
            command=lambda: self._open_row_dir(row),
        ).pack(side="left", padx=(6, 0))

    @staticmethod
    def _shorten(text, limit):
        text = str(text or "")
        if len(text) <= limit:
            return text
        return text[: max(0, limit // 2) - 1] + "…" + text[-(limit // 2):]

    # ------------------------------------------------------------ 滚动

    def _on_inner_configure(self, _event):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        self.canvas.itemconfigure(self._canvas_window, width=event.width)

    def _bind_wheel(self, _event):
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)

    def _unbind_wheel(self, _event):
        self.canvas.unbind_all("<MouseWheel>")

    def _on_wheel(self, event):
        try:
            self.canvas.yview_scroll(int(-event.delta / 120), "units")
        except Exception:
            pass

    # ------------------------------------------------------------ 线程桥

    def post(self, fn):
        """后台线程统一通过队列切回 Tk 线程执行。"""
        self._queue.put(fn)

    def _pump(self):
        while True:
            try:
                fn = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:
                core.log("界面回调异常：" + traceback.format_exc(limit=2))
        try:
            self.root.after(80, self._pump)
        except Exception:
            pass

    # ------------------------------------------------------------ 展示与刷新

    def show(self):
        try:
            self.root.deiconify()
            self.root.state("normal")
            self.root.lift()
            self.root.focus_force()
        except Exception:
            pass
        self.refresh_current()

    def hide(self):
        if self._log_win is not None:
            try:
                if self._log_win.winfo_exists():
                    self._log_win.withdraw()
            except Exception:
                pass
        self.root.withdraw()
        if not self._hint_shown:
            self._hint_shown = True
            if self.on_minimized:
                try:
                    self.on_minimized()
                except Exception:
                    pass

    def set_auto_state(self, enabled, seconds=None):
        self.auto_enabled = bool(enabled)
        if seconds is None:
            try:
                seconds = int(core.load_config().get("interval_seconds") or 120)
            except Exception:
                seconds = 120
        try:
            self.auto_label.config(
                text="自动同步：%s · 每 %d 秒"
                % ("开启" if self.auto_enabled else "关闭", max(30, seconds))
            )
        except Exception:
            pass

    def refresh(self):
        if self._busy:
            return
        self._gen += 1
        gen = self._gen
        threading.Thread(target=self._load_worker, args=(gen,), daemon=True).start()

    def refresh_current(self):
        """顶栏「刷新」跟随当前页。"""
        try:
            if self.notebook.index(self.notebook.select()) == 1:
                self.refresh_accounts()
                return
        except Exception:
            pass
        self.refresh()

    def _on_tab_changed(self, _event):
        try:
            if self.notebook.index(self.notebook.select()) == 1:
                self.refresh_accounts()
        except Exception:
            pass

    def _load_worker(self, gen):
        try:
            snap = build_snapshot()
            err = None
        except Exception:
            snap = None
            err = traceback.format_exc(limit=2)
        self.post(lambda: self._apply_snapshot(gen, snap, err))

    def _apply_snapshot(self, gen, snap, err):
        if gen != self._gen:
            return
        if err or snap is None:
            core.log("读取账号信息失败：" + (err or "未知错误"))
            self.feedback_label.config(text="读取账号信息失败，详见日志", fg=WARN)
            return

        self._snapshot = snap
        for child in self.inner.winfo_children():
            child.destroy()
        self._action_buttons = [self.refresh_btn, self.sync_btn]

        for key, _title in PRODUCTS:
            self._render_product(self.inner, key, snap["products"][key])

        self.canvas.yview_moveto(0)
        self.set_auto_state(
            self.auto_enabled, int(snap["cfg"].get("interval_seconds") or 120)
        )
        self._apply_busy_state()

    def _render_product(self, parent, key, snap):
        card = tk.Frame(
            parent, bg=CARD, highlightbackground=BORDER, highlightthickness=1
        )
        card.pack(fill="x", pady=(0, 12))

        head = tk.Frame(card, bg=CARD)
        head.pack(fill="x", padx=16, pady=(9, 2))
        tk.Label(
            head, text=snap["title"], bg=CARD, fg=TEXT,
            font=(FAMILY, 11, "bold"),
        ).pack(side="left")
        if snap["current"]:
            current_text = "当前生效：%s" % snap["current_label"]
            current_fg = ACCENT
        else:
            current_text = "未发现当前账号"
            current_fg = MUTED
        tk.Label(
            head, text=current_text, bg=CARD, fg=current_fg, font=(FAMILY, 9)
        ).pack(side="right")

        sub = tk.Frame(card, bg=CARD)
        sub.pack(fill="x", padx=16)
        tk.Label(
            sub,
            text="识别到 %d 个账号，共 %d 条会话"
            % (len(snap["rows"]), snap["total"]),
            bg=CARD, fg=MUTED, font=(FAMILY, 8),
        ).pack(side="left")

        if not snap["available"]:
            tk.Label(
                card,
                text="未发现数据文件：%s" % snap["path"],
                bg=CARD, fg=WARN, font=(FAMILY, 8), justify="left",
                anchor="w", wraplength=800,
            ).pack(fill="x", padx=16, pady=(8, 14))
            return

        if not snap["rows"]:
            tk.Label(
                card,
                text="还没有识别到账号。登录该软件并产生一次会话后会自动出现。",
                bg=CARD, fg=MUTED, font=(FAMILY, 9), anchor="w",
            ).pack(fill="x", padx=16, pady=(12, 14))
            return

        rows_box = tk.Frame(card, bg=CARD)
        rows_box.pack(fill="x", padx=10, pady=(4, 8))
        for row in snap["rows"]:
            self._render_account_row(rows_box, key, snap, row)

    def _render_account_row(self, parent, key, snap, row):
        is_current = row["current"]
        row_bg = CURRENT_BG if is_current else CARD
        outer = tk.Frame(parent, bg=row_bg)
        outer.pack(fill="x", pady=1)

        bar = tk.Frame(outer, bg=ACCENT if is_current else row_bg, width=3)
        bar.pack(side="left", fill="y")

        inner = tk.Frame(outer, bg=row_bg)
        inner.pack(side="left", fill="x", expand=True, padx=12, pady=5)

        tk.Label(
            inner, text="●" if is_current else "○", bg=row_bg,
            fg=ACCENT if is_current else "#9CA3AF", font=(FAMILY, 11),
        ).pack(side="left", padx=(0, 8))

        left = tk.Frame(inner, bg=row_bg)
        left.pack(side="left", fill="x", expand=True)
        tk.Label(
            left, text=row["label"], bg=row_bg, fg=TEXT,
            font=(FAMILY, 10, "bold" if is_current else "normal"), anchor="w",
        ).pack(anchor="w")
        tk.Label(
            left, text="ID %s" % (row["uid"][:8] if row["uid"] else "未知"),
            bg=row_bg, fg=MUTED, font=(FAMILY, 8), anchor="w",
        ).pack(anchor="w")

        right = tk.Frame(inner, bg=row_bg)
        right.pack(side="right")
        tk.Label(
            right, text="%d 条会话" % row["count"], bg=row_bg, fg=MUTED,
            font=(FAMILY, 9),
        ).pack(side="left", padx=(0, 12))

        if is_current:
            tk.Label(
                right, text="当前生效", bg="#DCEAFE", fg="#1D4ED8",
                font=(FAMILY, 8, "bold"), padx=8, pady=3,
            ).pack(side="left")
        else:
            btn = ttk.Button(
                right,
                text="切换到此账号",
                command=lambda k=key, u=row["uid"], n=row["label"]: self.switch_account(k, u, n),
            )
            btn.pack(side="left")
            self._action_buttons.append(btn)

    def _load_last_sync(self):
        data = load_last_sync()
        if not data:
            self.last_time.config(text="尚无记录")
            self.last_result.config(
                text="运行一次同步后，这里会显示时间和结果。", fg=TEXT
            )
            return
        when = data.get("time") or "未知时间"
        source = data.get("source") or ""
        self.last_time.config(text=when + (" · " + source if source else ""))
        result = data.get("result") or "（无返回内容）"
        bad = ("失败" in result) or ("异常" in result) or ("被占用" in result)
        self.last_result.config(
            text=result, fg=WARN if bad else TEXT
        )

    # ------------------------------------------------------------ 忙碌状态

    def _set_busy(self, busy, text=""):
        self._busy = busy
        try:
            if busy:
                self.feedback_label.config(text=text or "处理中…", fg=ACCENT)
            elif text:
                self.feedback_label.config(text=text, fg=OK_GREEN)
        except Exception:
            pass
        self._apply_busy_state()

    def _apply_busy_state(self):
        state = "disabled" if self._busy else "normal"
        for btn in self._action_buttons:
            try:
                if btn.winfo_exists():
                    btn.configure(state=state)
            except Exception:
                pass

    # ------------------------------------------------------------ 动作

    def sync_now(self):
        if self._busy:
            return
        self._set_busy(True, "正在同步…")
        threading.Thread(target=self._sync_worker, daemon=True).start()

    def _sync_worker(self):
        try:
            with SYNC_LOCK:
                msg = core.sync()
            core.log("手动同步：" + msg)
        except Exception as e:
            msg = "同步失败：%s" % e
            core.log("手动同步失败：" + traceback.format_exc())
        record_last_sync(msg, "手动同步")
        self.post(lambda: self._sync_done(msg))

    def _sync_done(self, msg):
        self._set_busy(False, "同步完成")
        self._load_last_sync()
        self.refresh()

    def switch_account(self, product, uid, label):
        if self._busy:
            return
        title = PRODUCT_TITLES.get(product, product)
        self._set_busy(True, "正在把 %s 的会话归到「%s」…" % (title, label))
        threading.Thread(
            target=self._switch_worker, args=(product, title, uid, label), daemon=True
        ).start()

    def _switch_worker(self, product, title, uid, label):
        try:
            with SYNC_LOCK:
                msg = core.sync(product=product, target=uid)
            core.log("界面切换 %s → %s：%s" % (title, label, msg))
        except Exception:
            msg = "切换失败：%s" % traceback.format_exc(limit=1)
            core.log("界面切换失败：" + traceback.format_exc())
        record_last_sync(msg, "手动切换")
        self.post(lambda: self._switch_done(title, label, msg))

    def _switch_done(self, title, label, msg):
        self._set_busy(False, "已切换到「%s」" % label)
        self._load_last_sync()
        self.refresh()
        bad = ("失败" in msg) or ("异常" in msg)
        if bad:
            messagebox.showerror("切换失败", msg, parent=self.root)
        else:
            messagebox.showinfo(
                "切换完成",
                "%s\n\n已把 %s 的全部会话归到「%s」。\n"
                "请重启 %s 后查看聊天列表。" % (msg, title, label, title),
                parent=self.root,
            )

    def external_sync_done(self, msg=None):
        """托盘菜单完成操作后同步窗口状态。"""
        self._load_last_sync()
        if not self._busy:
            self.refresh()
            if msg:
                self.feedback_label.config(text="托盘操作完成", fg=OK_GREEN)

    def on_auto_sync(self):
        self._load_last_sync()
        try:
            visible = bool(self.root.winfo_viewable())
        except Exception:
            visible = False
        if visible and not self._busy:
            self.refresh()

    # ------------------------------------------------------------ 账号页动作

    @staticmethod
    def _short_time(value):
        if not value:
            return ""
        try:
            return datetime.datetime.fromisoformat(value).strftime("%m-%d %H:%M")
        except Exception:
            return str(value)[:16]

    def _quota_text(self, quota):
        if not quota:
            return "—"
        text = quota.get("text") or "—"
        when = self._short_time(quota.get("checkedAt"))
        return "%s · %s" % (text, when) if when else text

    def _set_acct_busy(self, busy, text=""):
        self._acct_busy = busy
        try:
            self.acct_status.config(text=text, fg=ACCENT if busy else MUTED)
        except Exception:
            pass
        self._apply_acct_buttons()

    def _apply_acct_buttons(self):
        for btn in getattr(self, "acct_buttons", []):
            try:
                if not btn.winfo_exists():
                    continue
                btn.state(["disabled"] if self._acct_busy else ["!disabled"])
            except Exception:
                pass

    def refresh_accounts(self):
        if self._acct_busy or not hasattr(self, "acct_canvas"):
            return
        self._set_acct_busy(True, "正在读取本地账号…")
        threading.Thread(target=self._accounts_worker, daemon=True).start()

    def _accounts_worker(self):
        try:
            groups = acct.scan_all_products()
            err = None
        except Exception:
            groups = []
            err = traceback.format_exc(limit=2)
        self.post(lambda: self._apply_accounts(groups, err))

    def _apply_accounts(self, groups, err):
        self._acct_busy = False
        self._apply_acct_buttons()
        if err:
            core.log("读取账号列表失败：" + err)
            self.acct_status.config(text="读取账号列表失败，详见日志", fg=WARN)
            return

        self._acct_groups = groups
        self._quota_labels = {}
        for child in self.acct_inner.winfo_children():
            child.destroy()
        total = 0
        for index, group in enumerate(groups):
            total += len(group["accounts"])
            self._render_acct_group(group, index == 0)

        try:
            self.data_dir_label.config(text=acct.data_dir_text())
        except Exception:
            pass
        self.acct_canvas.yview_moveto(0)
        self.acct_status.config(
            text="共 %d 个账号 · %s" % (
                total, datetime.datetime.now().strftime("%H:%M:%S")
            ),
            fg=MUTED,
        )

    def _switch_row(self, row):
        if self._acct_busy:
            return
        self._set_acct_busy(True, "正在切换到「%s」…" % row["label"])
        threading.Thread(
            target=self._switch_account_worker, args=(row["uid"],), daemon=True
        ).start()

    def _switch_account_worker(self, uid):
        try:
            result = acct.switch_account(uid)
        except Exception as exc:
            result = {"ok": False, "message": "切换失败：%s" % exc}
        self.post(lambda: self._account_op_done(result["ok"], result["message"]))

    def _stop_row(self, row):
        if self._acct_busy:
            return
        if row.get("isDefault"):
            if not messagebox.askyesno(
                "停止主实例",
                "「%s」是默认 profile 的主实例，确认关闭？\n\n"
                "关闭后日常在用的 WorkBuddy 窗口也会退出。" % row["label"],
                parent=self.root,
            ):
                return
        self._set_acct_busy(True, "正在停止「%s」…" % row["label"])
        threading.Thread(
            target=self._stop_account_worker,
            args=(row["uid"], bool(row.get("isDefault"))),
            daemon=True,
        ).start()

    def _stop_account_worker(self, uid, allow_default):
        try:
            result = acct.stop_account(uid, allow_default=allow_default)
        except Exception as exc:
            result = {"ok": False, "message": "停止失败：%s" % exc}
        self.post(lambda: self._account_op_done(result["ok"], result["message"]))

    def _account_op_done(self, ok, message):
        self._set_acct_busy(False)
        self.refresh_accounts()
        if ok:
            messagebox.showinfo("完成", message, parent=self.root)
        else:
            messagebox.showerror("未完成", message, parent=self.root)

    def _refresh_quota(self, product, row):
        if self._acct_busy:
            return
        self._set_acct_busy(True, "正在查询「%s」的额度…" % row["label"])
        threading.Thread(
            target=self._quota_worker,
            args=(product, row["key"], row["label"]),
            daemon=True,
        ).start()

    def _quota_worker(self, product, key, label):
        try:
            result = acct.refresh_quota_any(product, key)
        except Exception as exc:
            result = {
                "mode": "manual", "text": "需手动查看",
                "detail": "查询失败：%s" % exc, "checkedAt": acct.now_iso(),
            }
        self.post(lambda: self._quota_done(key, result))

    def _quota_done(self, key, result):
        self._set_acct_busy(False)
        widget = self._quota_labels.get(key)
        try:
            if widget is not None and widget.winfo_exists():
                widget.config(
                    text=self._quota_text(result),
                    fg=ACCENT if result.get("mode") == "api" else WARN,
                )
        except Exception:
            pass
        for group in self._acct_groups:
            for row in group["accounts"]:
                if row["key"] == key:
                    row["quota"] = result
        detail = result.get("detail") or result.get("text") or ""
        self.acct_status.config(
            text=detail, fg=WARN if result.get("mode") == "manual" else MUTED
        )

    def _open_row_dir(self, row):
        path = row.get("configDir") or ""
        if not os.path.isdir(path):
            messagebox.showwarning("目录不存在", path, parent=self.root)
            return
        try:
            os.startfile(path)
        except Exception as exc:
            messagebox.showwarning("打开失败", "%s\n\n%s" % (path, exc), parent=self.root)

    def new_account(self):
        if self._acct_busy:
            return
        label = simpledialog.askstring(
            "新建账号", "显示名（可留空，登录后自动取昵称）：", parent=self.root
        )
        if label is None:
            return
        self._set_acct_busy(True, "正在创建实例…")
        threading.Thread(
            target=self._new_account_worker, args=(label.strip(),), daemon=True
        ).start()

    def _new_account_worker(self, label):
        try:
            info = acct.prepare_new_account(label or None)
        except Exception as exc:
            msg = "新建失败：%s" % exc
            core.log("新建账号失败：" + traceback.format_exc(limit=2))
            self.post(lambda: self._account_op_done(False, msg))
            return
        self.post(lambda: self._set_acct_busy(
            True, "已拉起新实例，请在 WorkBuddy 窗口完成登录…"
        ))
        try:
            result = acct.wait_new_login(info)
        except Exception as exc:
            result = {"ok": False, "message": "等待登录失败：%s" % exc}
        self.post(lambda: self._account_op_done(result["ok"], result["message"]))

    def open_tool_data_dir(self):
        try:
            os.makedirs(str(acct.DATA_DIR), exist_ok=True)
            os.startfile(str(acct.DATA_DIR))
        except Exception as exc:
            messagebox.showwarning("打开失败", "%s" % exc, parent=self.root)

    def show_log(self):
        if self._log_win is not None:
            try:
                if self._log_win.winfo_exists():
                    self._log_win.deiconify()
                    self._log_win.lift()
                    self._reload_log()
                    return
            except Exception:
                pass

        win = tk.Toplevel(self.root)
        win.title("同步日志")
        win.geometry("860x540")
        win.configure(bg=BG)
        win.transient(self.root)
        self._log_win = win

        head = tk.Frame(win, bg=BG)
        head.pack(fill="x", padx=14, pady=(12, 6))
        tk.Label(
            head, text="同步日志", bg=BG, fg=TEXT, font=(FAMILY, 12, "bold")
        ).pack(side="left")
        tk.Label(
            head, text=core.LOG_PATH, bg=BG, fg=MUTED, font=(FAMILY, 8)
        ).pack(side="left", padx=(10, 0))

        text_box = tk.Frame(win, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        text_box.pack(fill="both", expand=True, padx=14, pady=(0, 8))
        self._log_text = tk.Text(
            text_box, wrap="word", bg="#FBFCFE", fg=TEXT, relief="flat",
            font=("Consolas", 9), padx=10, pady=8, state="disabled",
        )
        log_scroll = ttk.Scrollbar(
            text_box, orient="vertical", command=self._log_text.yview
        )
        self._log_text.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side="right", fill="y")
        self._log_text.pack(side="left", fill="both", expand=True)

        actions = tk.Frame(win, bg=BG)
        actions.pack(fill="x", padx=14, pady=(0, 12))
        ttk.Button(actions, text="刷新", command=self._reload_log).pack(side="left")
        ttk.Button(actions, text="用记事本打开", command=self._open_log_file).pack(
            side="left", padx=(10, 0)
        )
        ttk.Button(actions, text="关闭", command=win.destroy).pack(side="right")

        win.protocol("WM_DELETE_WINDOW", win.destroy)
        self._reload_log()

    def _reload_log(self):
        try:
            if not os.path.exists(core.LOG_PATH):
                content = "（还没有日志）"
            else:
                with open(core.LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
                    content = "".join(f.readlines()[-500:])
        except Exception as e:
            content = "读取日志失败：%s" % e
        try:
            self._log_text.config(state="normal")
            self._log_text.delete("1.0", "end")
            self._log_text.insert("1.0", content)
            self._log_text.see("end")
            self._log_text.config(state="disabled")
        except Exception:
            pass

    def _open_log_file(self):
        try:
            if os.path.exists(core.LOG_PATH):
                os.startfile(core.LOG_PATH)
        except Exception:
            pass

    def shutdown(self):
        try:
            self.root.quit()
            self.root.destroy()
        except Exception:
            pass
