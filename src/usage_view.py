# -*- coding: utf-8 -*-
"""用量看板页签：总览 + Qoder / WorkBuddy / CodeBuddy / Trae。

金额一律按真实 token × 公开 API 单价折算；厂商积分只做展示，不参与计价。
"""
import datetime
import threading
import traceback
import tkinter as tk
from tkinter import ttk

import usage
from ui_widgets import SlimScrollbar, attach_canvas_scroll

FAMILY = "Microsoft YaHei UI"
BG = "#F4F6F9"
CARD = "#FFFFFF"
BORDER = "#E4EAF2"
SOFT = "#F7F9FC"
SOFT2 = "#EEF3FA"
TEXT = "#1B2430"
MUTED = "#667085"
FAINT = "#98A2B3"
ACCENT = "#2F6FED"
GREEN = "#15803D"
YELLOW = "#B45309"
RED = "#B91C1C"

PRODUCT_COLORS = {
    "qoder": "#6D5DF6",
    "workbuddy": "#12A150",
    "codebuddy": "#2F6FED",
    "trae": "#E8590C",
}
PRODUCT_TITLES = {
    "qoder": "Qoder",
    "workbuddy": "WorkBuddy",
    "codebuddy": "CodeBuddy",
    "trae": "Trae",
}
TABS = (("overview", "总览"), ("qoder", "Qoder"),
        ("workbuddy", "WorkBuddy"), ("codebuddy", "CodeBuddy"),
        ("trae", "Trae"))


def F(size, bold=False):
    return (FAMILY, size, "bold" if bold else "normal")


def fmt_tokens(n):
    n = float(n or 0)
    if n >= 1e9:
        return "%.2fB" % (n / 1e9)
    if n >= 1e6:
        return "%.1fM" % (n / 1e6)
    if n >= 1e3:
        return "%.1fk" % (n / 1e3)
    return "%d" % round(n)


def fmt_money(value):
    return "¥{:,.2f}".format(float(value or 0))


def fmt_int(value):
    return "{:,}".format(int(value or 0))


def fmt_pct(value):
    return "%.1f%%" % (float(value or 0) * 100)


def short(text, limit=44):
    text = str(text or "")
    return text if len(text) <= limit else text[: max(1, limit - 1)] + "…"


def clock(iso):
    text = str(iso or "")
    return text[11:19] if len(text) >= 19 else (text or "—")


class StackChart(tk.Canvas):
    """按天堆叠柱状图；数据变化或窗口宽度变化时重绘。"""

    def __init__(self, parent, height=158, **kw):
        super().__init__(parent, height=height, bg=CARD,
                         highlightthickness=0, bd=0, **kw)
        self._labels = []
        self._series = []
        self.bind("<Configure>", lambda _e: self._draw())

    def set_data(self, labels, series):
        self._labels = list(labels or [])
        self._series = list(series or [])
        self._draw()

    def _draw(self):
        self.delete("all")
        width = self.winfo_width()
        height = self.winfo_height()
        if width < 40 or height < 40:
            return
        if not self._labels or not self._series:
            self.create_text(width / 2, height / 2, text="暂无数据",
                             font=F(9), fill=FAINT)
            return
        left, right, top, bottom = 10, width - 10, 10, height - 24
        totals = []
        for i in range(len(self._labels)):
            totals.append(sum((vals[i] if i < len(vals) else 0)
                              for _name, _color, vals in self._series))
        peak = max(totals) if totals else 0
        if peak <= 0:
            self.create_text(width / 2, height / 2, text="暂无数据",
                             font=F(9), fill=FAINT)
            return
        for step in (0.25, 0.5, 0.75, 1.0):
            y = bottom - (bottom - top) * step
            self.create_line(left, y, right, y, fill="#EEF1F6")
        slot = (right - left) / max(1, len(self._labels))
        bar_w = min(28.0, max(4.0, slot * 0.58))
        every = max(1, len(self._labels) // 7)
        for i, label in enumerate(self._labels):
            x = left + slot * i + slot / 2
            y = bottom
            for _name, color, vals in self._series:
                value = vals[i] if i < len(vals) else 0
                h = (value / peak) * (bottom - top)
                if h >= 0.6:
                    self.create_rectangle(x - bar_w / 2, y - h, x + bar_w / 2, y,
                                          fill=color, outline="")
                    y -= h
            if i % every == 0 or i == len(self._labels) - 1:
                text = label[5:] if len(label) >= 10 else label
                self.create_text(x, bottom + 5, text=text, anchor="n",
                                 font=F(8), fill=FAINT)


class MiniBar(tk.Canvas):
    def __init__(self, parent, ratio=0.0, color=ACCENT, height=8, **kw):
        super().__init__(parent, height=height, bg=SOFT2,
                         highlightthickness=0, bd=0, **kw)
        self._ratio = max(0.0, min(1.0, float(ratio or 0)))
        self._color = color
        self.bind("<Configure>", lambda _e: self._draw())

    def set(self, ratio, color=None):
        self._ratio = max(0.0, min(1.0, float(ratio or 0)))
        if color:
            self._color = color
        self._draw()

    def _draw(self):
        self.delete("all")
        width = self.winfo_width()
        height = self.winfo_height()
        if width <= 2:
            return
        self.create_rectangle(0, 0, width, height, fill=SOFT2, outline="")
        if self._ratio > 0:
            self.create_rectangle(0, 0, max(2, width * self._ratio), height,
                                  fill=self._color, outline="")


class UsagePage:
    def __init__(self, parent, app):
        self.parent = parent
        self.app = app
        self.product = "overview"
        self.snapshot = usage.load_snapshot() or {}
        self.busy = False
        self.error = ""
        self.elapsed = 0.0
        self._loaded_once = False
        self._pills = {}
        self._scroll = {"region_job": None}
        self._build()
        self._refresh_pills()
        self._render()

    # ------------------------------------------------------------ 骨架
    def _build(self):
        self.parent.configure(bg=BG)
        self.frame = tk.Frame(self.parent, bg=BG)
        self.frame.pack(fill="both", expand=True)

        head = tk.Frame(self.frame, bg=BG)
        head.pack(fill="x", padx=2, pady=(8, 0))
        title_box = tk.Frame(head, bg=BG)
        title_box.pack(side="left", fill="x", expand=True)
        tk.Label(title_box, text="用量与白嫖看板", bg=BG, fg=TEXT,
                 font=F(17, True), anchor="w").pack(anchor="w")
        tk.Label(
            title_box,
            text="金额 = 真实 token × 公开 API 单价；厂商积分只展示，不计价。",
            bg=BG, fg=MUTED, font=F(9), anchor="w",
        ).pack(anchor="w", pady=(2, 0))

        self.trae_btn = ttk.Button(head, text="刷新 Trae 额度",
                                   command=lambda: self.refresh(online_trae=True))
        self.trae_btn.pack(side="right", padx=(8, 0))
        self.refresh_btn = ttk.Button(head, text="重新采集", style="Primary.TButton",
                                      command=lambda: self.refresh(force_qoder=True))
        self.refresh_btn.pack(side="right")

        status_row = tk.Frame(self.frame, bg=BG)
        status_row.pack(fill="x", padx=2, pady=(6, 4))
        self.status_label = tk.Label(status_row, text="", bg=BG, fg=MUTED,
                                     font=F(9), anchor="w")
        self.status_label.pack(side="left")
        self.updated_label = tk.Label(status_row, text="", bg=BG, fg=FAINT,
                                      font=F(9), anchor="e")
        self.updated_label.pack(side="right")

        pill_row = tk.Frame(self.frame, bg=BG)
        pill_row.pack(fill="x", padx=2, pady=(2, 8))
        for key, title in TABS:
            label = tk.Label(pill_row, text=title, font=F(10), padx=14, pady=5,
                             bd=0, highlightthickness=1, cursor="hand2")
            label.pack(side="left", padx=(0, 6))
            label.bind("<Button-1>", lambda _e, k=key: self.select(k))
            self._pills[key] = label

        body = tk.Frame(self.frame, bg=BG)
        body.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(body, bg=BG, highlightthickness=0, bd=0)
        self.scrollbar = SlimScrollbar(body, bg=BG)
        self.scrollbar.pack(side="right", fill="y", padx=(4, 0))
        self.canvas.pack(side="left", fill="both", expand=True)

        self.inner = tk.Frame(self.canvas, bg=BG)
        self._window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        attach_canvas_scroll(
            self.canvas, self.scrollbar, self.inner, self._window,
            self.app.root, self._scroll,
        )

    # ------------------------------------------------------------ 状态
    def on_show(self):
        """切到用量页签时按需采集，避免程序在托盘后台就扫描大盘。"""
        if not self._loaded_once:
            self._loaded_once = True
            self.refresh()
            return
        stamp = str((self.snapshot or {}).get("generatedAt") or "")
        if not stamp:
            self.refresh()
            return
        try:
            then = datetime.datetime.fromisoformat(stamp).timestamp()
            if datetime.datetime.now().timestamp() - then > 300:
                self.refresh()
        except Exception:
            pass

    def refresh(self, force_qoder=False, online_trae=False):
        if self.busy:
            return
        self.busy = True
        self.error = ""
        self._set_busy_controls()
        self.status_label.config(
            text="正在查询 Trae 额度…" if online_trae else "正在采集本地用量…",
            fg=ACCENT)
        threading.Thread(target=self._worker,
                         args=(force_qoder, online_trae), daemon=True).start()

    def _worker(self, force_qoder, online_trae):
        started = datetime.datetime.now().timestamp()
        try:
            snap = usage.build_snapshot(force_qoder=force_qoder,
                                        online_trae=online_trae)
            err = ""
        except Exception:
            snap = None
            err = traceback.format_exc(limit=2)
        elapsed = max(0.0, datetime.datetime.now().timestamp() - started)
        self.app.post(lambda: self._apply(snap, err, elapsed))

    def _apply(self, snap, err, elapsed):
        self.busy = False
        self.elapsed = elapsed
        if err:
            self.error = err.strip().splitlines()[-1][:120]
        if snap:
            self.snapshot = snap
        self._set_busy_controls()
        self._render()

    def _set_busy_controls(self):
        state = "disabled" if self.busy else "normal"
        for widget in (self.refresh_btn, self.trae_btn):
            try:
                widget.configure(state=state)
            except Exception:
                pass

    # ------------------------------------------------------------ 页签
    def select(self, key):
        if key == self.product:
            return
        self.product = key
        self._refresh_pills()
        self.canvas.yview_moveto(0)
        self._render()

    def _refresh_pills(self):
        for key, label in self._pills.items():
            active = key == self.product
            label.configure(
                bg=CARD if active else BG,
                fg=ACCENT if active else MUTED,
                font=F(10, active),
                highlightbackground=BORDER if active else BG,
            )

    # ------------------------------------------------------------ 渲染入口
    def _render(self):
        try:
            self.inner.unbind("<Configure>")
        except Exception:
            pass
        self.canvas.configure(yscrollcommand="")
        for child in self.inner.winfo_children():
            child.destroy()
        snap = self.snapshot or {}
        products = snap.get("products") or {}
        combined = snap.get("combined") or {}

        if self.busy and not products:
            self._empty("正在采集本地用量记录…", "首次扫描 WorkBuddy / CodeBuddy 可能需要十几秒。")
            self._finish_render()
            return
        if not products:
            self._empty("还没有用量数据", "点右上角「重新采集」开始读取本地记录。")
            self._finish_render()
            return

        if self.error:
            self._banner("采集异常：" + self.error, RED, "#FDECEC")
        if self.product == "overview":
            self._render_overview(products, combined)
        elif self.product == "trae":
            self._render_trae(products.get("trae") or {})
        else:
            self._render_product(products.get(self.product) or {})
        self._finish_render()

    def _finish_render(self):
        on_inner = self._scroll.get("on_inner")
        if callable(on_inner):
            self.inner.bind("<Configure>", on_inner)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        sync = self._scroll.get("sync_region")
        if callable(sync):
            sync()
        else:
            try:
                self.canvas.configure(
                    scrollregion=self.canvas.bbox("all") or (0, 0, 0, 0)
                )
            except Exception:
                pass
        self._update_status()

    def _update_status(self):
        snap = self.snapshot or {}
        combined = snap.get("combined") or {}
        stamp = clock(snap.get("generatedAt"))
        if self.busy:
            text, color = "采集中…", ACCENT
        elif self.error:
            text, color = "采集异常", RED
        else:
            text, color = "快照 %s · 耗时 %.1fs" % (stamp, self.elapsed), MUTED
        self.status_label.config(text=text, fg=color)
        self.updated_label.config(
            text="等价费用 %s · Trae 积分另列" % fmt_money(combined.get("cost")))
        self._set_busy_controls()

    # ------------------------------------------------------------ 通用块
    def _card(self, parent, pady=(0, 10)):
        card = tk.Frame(parent, bg=CARD, highlightbackground=BORDER,
                        highlightthickness=1)
        card.pack(fill="x", pady=pady)
        return card

    def _card_title(self, card, title, subtitle=""):
        box = tk.Frame(card, bg=CARD)
        box.pack(fill="x", padx=14, pady=(11, 4))
        tk.Label(box, text=title, bg=CARD, fg=TEXT, font=F(11, True),
                 anchor="w").pack(side="left")
        if subtitle:
            tk.Label(box, text=subtitle, bg=CARD, fg=FAINT, font=F(8),
                     anchor="w").pack(side="left", padx=(8, 0))
        return box

    def _banner(self, text, fg, bg):
        box = tk.Frame(self.inner, bg=bg, highlightbackground=bg, highlightthickness=1)
        box.pack(fill="x", pady=(0, 8))
        tk.Label(box, text=text, bg=bg, fg=fg, font=F(9),
                 anchor="w", justify="left", wraplength=820).pack(
            fill="x", padx=12, pady=8)

    def _empty(self, title, hint):
        card = self._card(self.inner)
        tk.Label(card, text="○", bg=CARD, fg=FAINT, font=F(22)).pack(pady=(18, 2))
        tk.Label(card, text=title, bg=CARD, fg=TEXT, font=F(11, True)).pack()
        tk.Label(card, text=hint, bg=CARD, fg=MUTED, font=F(9)).pack(pady=(3, 18))

    def _stat_row(self, parent, stats):
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", pady=(0, 10))
        for index, (label, value, sub, color) in enumerate(stats):
            row.grid_columnconfigure(index, weight=1, uniform="stat")
            card = tk.Frame(row, bg=CARD, highlightbackground=BORDER,
                            highlightthickness=1)
            card.grid(row=0, column=index, sticky="nsew",
                      padx=(0 if index == 0 else 8, 0))
            inner = tk.Frame(card, bg=CARD)
            inner.pack(fill="x", padx=13, pady=10)
            tk.Label(inner, text=label, bg=CARD, fg=MUTED, font=F(8),
                     anchor="w").pack(anchor="w")
            tk.Label(inner, text=value, bg=CARD, fg=color, font=F(18, True),
                     anchor="w").pack(anchor="w", pady=(1, 0))
            if sub:
                tk.Label(inner, text=sub, bg=CARD, fg=FAINT, font=F(8),
                         anchor="w").pack(anchor="w")

    # ------------------------------------------------------------ 总览
    def _render_overview(self, products, combined):
        q = (products.get("qoder") or {}).get("totals") or {}
        w = (products.get("workbuddy") or {}).get("totals") or {}
        c = (products.get("codebuddy") or {}).get("totals") or {}
        self._stat_row(self.inner, (
            ("等价 API 总费用", fmt_money(combined.get("cost")),
             "Qoder / WB / CB", YELLOW),
            ("总处理 token", fmt_tokens(combined.get("processed")),
             "净输入 + 缓存读", ACCENT),
            ("总请求", fmt_int(combined.get("requests")),
             "%d 个会话" % int(combined.get("sessions") or 0), GREEN),
            ("缓存命中率", fmt_pct(combined.get("cacheHit")),
             "按 token 加权", ACCENT),
        ))

        card = self._card(self.inner)
        self._card_title(card, "产品对比", "按 token 折价，不按积分")
        rows_box = tk.Frame(card, bg=CARD)
        rows_box.pack(fill="x", padx=14, pady=(0, 12))
        total_cost = sum(float(x.get("cost") or 0) for x in (q, w, c)) or 1.0
        rows = (("qoder", q), ("workbuddy", w), ("codebuddy", c))
        for key, totals in rows:
            color = PRODUCT_COLORS[key]
            row = tk.Frame(rows_box, bg=CARD)
            row.pack(fill="x", pady=4)
            ratio = float(totals.get("cost") or 0) / total_cost
            right = tk.Frame(row, bg=CARD)
            right.pack(side="right")
            tk.Label(right, text="%d%%" % round(ratio * 100), bg=CARD,
                     fg=MUTED, font=F(8), width=4, anchor="e").pack(side="right")
            MiniBar(right, ratio=ratio, color=color, height=8,
                    width=150).pack(side="right", padx=(0, 8))
            name = tk.Frame(row, bg=CARD)
            name.pack(side="left")
            tk.Label(name, text="●", bg=CARD, fg=color,
                     font=F(9)).pack(side="left")
            tk.Label(name, text=" " + PRODUCT_TITLES[key], bg=CARD, fg=TEXT,
                     font=F(9, True)).pack(side="left")
            tk.Label(row, text=fmt_money(totals.get("cost")), bg=CARD,
                     fg=YELLOW, font=F(10, True)).pack(side="left", padx=(16, 0))
            tk.Label(
                row,
                text="%s tok · %s 请求 · %s 会话" % (
                    fmt_tokens(totals.get("processed")),
                    fmt_int(totals.get("requests")),
                    fmt_int(totals.get("sessions"))),
                bg=CARD, fg=MUTED, font=F(8),
            ).pack(side="left", padx=(14, 0))

        trae = products.get("trae") or {}
        row = tk.Frame(rows_box, bg=CARD)
        row.pack(fill="x", pady=(6, 0))
        tk.Label(row, text="●", bg=CARD, fg=PRODUCT_COLORS["trae"],
                 font=F(9)).pack(side="left")
        tk.Label(row, text=" Trae", bg=CARD, fg=TEXT,
                 font=F(9, True)).pack(side="left")
        if trae.get("available"):
            text = "积分口径（不计价）  已用 %s / %s · 剩余 %s · 到期 %s" % (
                fmt_int(trae.get("consumed")), fmt_int(trae.get("total")),
                fmt_int(trae.get("remaining")), trae.get("expire") or "未知")
            fg = MUTED
        else:
            text = "尚未查询积分（点右上角「刷新 Trae 额度」）"
            fg = FAINT
        tk.Label(row, text=text, bg=CARD, fg=fg,
                 font=F(8)).pack(side="left", padx=(14, 0))

        # 每日费用堆叠图
        chart = self._card(self.inner)
        self._card_title(chart, "每日等价费用", "最近 21 天 · 三产品堆叠")
        legend = tk.Frame(chart, bg=CARD)
        legend.pack(fill="x", padx=14)
        days = self._merged_days(products, 21)
        series = []
        for key in ("qoder", "workbuddy", "codebuddy"):
            vals = [row["cost"].get(key, 0.0) for row in days]
            series.append((PRODUCT_TITLES[key], PRODUCT_COLORS[key], vals))
            tk.Label(legend, text="■ " + PRODUCT_TITLES[key], bg=CARD,
                     fg=PRODUCT_COLORS[key], font=F(8)).pack(side="left", padx=(0, 10))
        canvas = StackChart(chart, height=168)
        canvas.pack(fill="x", padx=12, pady=(4, 12))
        canvas.set_data([row["date"] for row in days], series)

        self._notes_card(products)

    def _merged_days(self, products, limit):
        dates = set()
        per_product = {}
        for key in ("qoder", "workbuddy", "codebuddy"):
            rows = (products.get(key) or {}).get("days") or []
            mapping = {str(r.get("date")): float(r.get("cost") or 0) for r in rows}
            per_product[key] = mapping
            dates.update(mapping.keys())
        dates.discard("unknown")
        order = sorted(dates)[-limit:]
        out = []
        for date in order:
            out.append({
                "date": date,
                "cost": {key: per_product[key].get(date, 0.0) for key in per_product},
            })
        return out

    def _notes_card(self, products):
        card = self._card(self.inner, pady=(0, 4))
        self._card_title(card, "口径说明", "为什么不用积分")
        lines = [
            "金额只用真实 token × 公开 API 单价折算；credits / 积分只做展示，不参与计价。",
            "WorkBuddy、CodeBuddy 直接读取本地转录中的 input / cache / output token。",
            "Qoder 免费模型不返回 token，由 collect.py 按上下文比例还原后折算。",
            "Trae 不提供 token 明细，只展示厂商积分，未计入金额汇总。",
        ]
        for line in lines:
            tk.Label(card, text="· " + line, bg=CARD, fg=MUTED, font=F(9),
                     anchor="w", justify="left", wraplength=840).pack(
                fill="x", padx=14, pady=1)
        note = ((self.snapshot or {}).get("pricing") or {}).get("note") or ""
        if note:
            tk.Label(card, text=note, bg=CARD, fg=FAINT, font=F(8),
                     anchor="w", justify="left", wraplength=840).pack(
                fill="x", padx=14, pady=(5, 10))
        else:
            tk.Frame(card, bg=CARD, height=8).pack()

    # ------------------------------------------------------------ 产品页
    def _render_product(self, product):
        key = product.get("key") or self.product
        title = PRODUCT_TITLES.get(key, key)
        totals = product.get("totals") or {}
        if not product.get("available"):
            self._empty(title + " 暂无数据",
                        product.get("error") or "未发现本地数据，点「重新采集」重试。")
            self._source_card(product)
            return

        self._stat_row(self.inner, (
            ("等价 API 费用", fmt_money(totals.get("cost")), "按 token 折算", YELLOW),
            ("处理 token", fmt_tokens(totals.get("processed")), "净输入 + 缓存", ACCENT),
            ("输出 token", fmt_tokens(totals.get("out")), "assistant 输出", TEXT),
            ("请求", fmt_int(totals.get("requests")),
             "%d 个会话" % int(totals.get("sessions") or 0), GREEN),
            ("缓存命中率", fmt_pct(totals.get("cacheHit")), "按 token 加权", ACCENT),
        ))

        if key == "qoder" and product.get("credits"):
            box = tk.Frame(self.inner, bg=CARD, highlightbackground=BORDER,
                           highlightthickness=1)
            box.pack(fill="x", pady=(0, 10))
            tk.Label(box, text="服务器 credits：%s（仅展示，不计价）" % fmt_int(product["credits"]),
                     bg=CARD, fg=MUTED, font=F(9), anchor="w").pack(
                fill="x", padx=14, pady=8)

        # 每日费用
        chart = self._card(self.inner)
        self._card_title(chart, "每日等价费用", "最近 21 天")
        days = (product.get("days") or [])[-21:]
        canvas = StackChart(chart, height=150)
        canvas.pack(fill="x", padx=12, pady=(2, 12))
        canvas.set_data([str(r.get("date")) for r in days],
                        [(title, PRODUCT_COLORS.get(key, ACCENT),
                          [float(r.get("cost") or 0) for r in days])])

        # 模型表
        self._model_table(product)

        # 会话表
        self._session_table(product)
        self._source_card(product)

    def _model_table(self, product):
        rows = product.get("models") or []
        card = self._card(self.inner)
        self._card_title(card, "按模型", "按处理 token 排序")
        if not rows:
            tk.Label(card, text="暂无模型数据", bg=CARD, fg=FAINT,
                     font=F(9)).pack(padx=14, pady=14)
            return
        grid = tk.Frame(card, bg=CARD)
        grid.pack(fill="x", padx=14, pady=(0, 12))
        weights = (30, 9, 12, 12, 11, 9, 12)
        for index, weight in enumerate(weights):
            grid.grid_columnconfigure(index, weight=weight)
        headers = ("模型", "请求", "净输入", "缓存读", "输出", "命中", "等价费用")
        for col, text in enumerate(headers):
            tk.Label(grid, text=text, bg=CARD, fg=FAINT, font=F(8),
                     anchor="w" if col == 0 else "e").grid(
                row=0, column=col, sticky="ew", padx=(0, 8), pady=(0, 5))
        for r, row in enumerate(rows[:30], start=1):
            bg = CARD if r % 2 else SOFT
            name = str(row.get("name") or row.get("id") or "?")
            if row.get("estimated"):
                name += " · est"
            values = (
                name,
                fmt_int(row.get("requests")),
                fmt_tokens(row.get("newInput")),
                fmt_tokens(row.get("cacheRead")),
                fmt_tokens(row.get("out")),
                fmt_pct(row.get("cacheHit")),
                fmt_money(row.get("cost")),
            )
            for col, text in enumerate(values):
                tk.Label(grid, text=text, bg=bg,
                         fg=TEXT if col == 0 else (YELLOW if col == 6 else MUTED),
                         font=F(9, col == 0), anchor="w" if col == 0 else "e").grid(
                    row=r, column=col, sticky="ew", padx=(0, 8), pady=5)

    def _session_table(self, product):
        rows = product.get("sessions") or []
        card = self._card(self.inner)
        self._card_title(card, "会话", "按最后活跃倒序")
        if not rows:
            tk.Label(card, text="暂无会话", bg=CARD, fg=FAINT,
                     font=F(9)).pack(padx=14, pady=14)
            return
        grid = tk.Frame(card, bg=CARD)
        grid.pack(fill="x", padx=14, pady=(0, 12))
        weights = (42, 20, 20, 13, 8, 12, 11)
        for index, weight in enumerate(weights):
            grid.grid_columnconfigure(index, weight=weight)
        headers = ("标题", "工作区", "模型", "最后活跃", "请求", "处理 token", "费用")
        for col, text in enumerate(headers):
            tk.Label(grid, text=text, bg=CARD, fg=FAINT, font=F(8),
                     anchor="w" if col < 3 else "e").grid(
                row=0, column=col, sticky="ew", padx=(0, 8), pady=(0, 5))
        for r, row in enumerate(rows[:24], start=1):
            bg = CARD if r % 2 else SOFT
            values = (
                short(row.get("title") or "(无标题)", 42),
                short(row.get("workspace") or "-", 22),
                short(row.get("model") or "-", 24),
                row.get("lastSeen") or "—",
                fmt_int(row.get("requests")),
                fmt_tokens(row.get("processed")),
                fmt_money(row.get("cost")),
            )
            colors = (TEXT, MUTED, MUTED, FAINT, MUTED, MUTED, YELLOW)
            for col, text in enumerate(values):
                tk.Label(grid, text=text, bg=bg, fg=colors[col],
                         font=F(9, col == 0),
                         anchor="w" if col < 3 else "e").grid(
                    row=r, column=col, sticky="ew", padx=(0, 8), pady=5)

    def _source_card(self, product):
        text = product.get("note") or ""
        source = product.get("source") or ""
        if not text and not source:
            return
        box = tk.Frame(self.inner, bg=SOFT, highlightbackground=BORDER,
                       highlightthickness=1)
        box.pack(fill="x", pady=(0, 4))
        tk.Label(box, text=text, bg=SOFT, fg=MUTED, font=F(8), anchor="w",
                 justify="left", wraplength=840).pack(
            fill="x", padx=12, pady=(7, 2))
        if source:
            tk.Label(box, text="来源：" + source, bg=SOFT, fg=FAINT, font=F(8),
                     anchor="w", justify="left", wraplength=840).pack(
                fill="x", padx=12, pady=(0, 7))

    # ------------------------------------------------------------ Trae
    def _render_trae(self, trae):
        if not trae.get("available"):
            self._empty("Trae 积分尚未查询",
                        trae.get("error") or "点右上角「刷新 Trae 额度」。")
            return
        consumed = float(trae.get("consumed") or 0)
        total = float(trae.get("total") or 0)
        ratio = float(trae.get("ratio") or 0)

        card = self._card(self.inner)
        self._card_title(card, "Trae 额度", "厂商积分口径 · 未计入金额汇总")
        inner = tk.Frame(card, bg=CARD)
        inner.pack(fill="x", padx=14, pady=(2, 14))
        numbers = tk.Frame(inner, bg=CARD)
        numbers.pack(fill="x")
        for label, value, color in (
                ("已用积分", fmt_int(consumed), PRODUCT_COLORS["trae"]),
                ("总额度", fmt_int(total), TEXT),
                ("剩余", fmt_int(trae.get("remaining")), GREEN),
                ("使用率", fmt_pct(ratio), YELLOW)):
            cell = tk.Frame(numbers, bg=CARD)
            cell.pack(side="left", expand=True, fill="x")
            tk.Label(cell, text=label, bg=CARD, fg=MUTED,
                     font=F(8)).pack(anchor="w")
            tk.Label(cell, text=value, bg=CARD, fg=color,
                     font=F(17, True)).pack(anchor="w")
        bar = MiniBar(inner, ratio=ratio, color=PRODUCT_COLORS["trae"], height=13)
        bar.pack(fill="x", pady=(12, 4))
        tk.Label(inner, text="最近到期：%s   ·   查询时间：%s" % (
            trae.get("expire") or "未知", clock(trae.get("checkedAt"))),
            bg=CARD, fg=FAINT, font=F(8), anchor="w").pack(fill="x", pady=(4, 0))

        accounts = trae.get("accounts") or []
        if accounts:
            card2 = self._card(self.inner)
            self._card_title(card2, "实例明细", "多开实例分别统计")
            grid = tk.Frame(card2, bg=CARD)
            grid.pack(fill="x", padx=14, pady=(0, 12))
            for index, weight in enumerate((30, 14, 14, 14, 12)):
                grid.grid_columnconfigure(index, weight=weight)
            for col, text in enumerate(("账号", "已用", "总额", "剩余", "到期")):
                tk.Label(grid, text=text, bg=CARD, fg=FAINT, font=F(8),
                         anchor="w" if col == 0 else "e").grid(
                    row=0, column=col, sticky="ew", padx=(0, 8), pady=(0, 5))
            for r, row in enumerate(accounts, start=1):
                bg = CARD if r % 2 else SOFT
                values = (short(row.get("label") or "-", 34),
                          fmt_int(row.get("consumed")),
                          fmt_int(row.get("total")),
                          fmt_int(row.get("remaining")),
                          row.get("expire") or "—")
                for col, text in enumerate(values):
                    tk.Label(grid, text=text, bg=bg,
                             fg=TEXT if col == 0 else MUTED,
                             font=F(9, col == 0),
                             anchor="w" if col == 0 else "e").grid(
                        row=r, column=col, sticky="ew", padx=(0, 8), pady=5)

        note = self._card(self.inner, pady=(0, 4))
        tk.Label(note, text="Trae 不提供逐会话 token 明细，本页只展示厂商积分；"
                            "积分额度可能被厂商随时调整，因此不计入上方金额汇总。",
                 bg=CARD, fg=MUTED, font=F(9), anchor="w", justify="left",
                 wraplength=840).pack(fill="x", padx=14, pady=12)
