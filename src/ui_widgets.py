# -*- coding: utf-8 -*-
"""界面小部件：细滚动条与 Canvas 滚动绑定。"""
import tkinter as tk

TRACK = "#E4EAF2"
THUMB = "#8E9AAB"
THUMB_HOVER = "#6F7C8E"
THUMB_ACTIVE = "#556274"


class SlimScrollbar(tk.Canvas):
    """细轨自定义滚动条：无箭头、轻量重绘，避免 ttk 粗条与滚动鬼影。"""

    WIDTH = 9

    def __init__(self, master, command=None, bg="#F4F6F9", **kw):
        kw.setdefault("width", self.WIDTH)
        kw.setdefault("highlightthickness", 0)
        kw.setdefault("bd", 0)
        kw.setdefault("relief", "flat")
        kw.setdefault("cursor", "arrow")
        super().__init__(master, bg=bg, **kw)
        self.command = command
        self._bg = bg
        self._first = 0.0
        self._last = 1.0
        self._drag_off = None
        self._color = THUMB
        self._thumb_id = None
        self._rail_id = None
        self._pending = None

        self.bind("<Configure>", self._on_configure)
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<MouseWheel>", self._on_wheel)

    def set(self, first, last):
        try:
            first = float(first)
            last = float(last)
        except (TypeError, ValueError):
            return
        self._first = max(0.0, min(1.0, first))
        self._last = max(self._first, min(1.0, last))
        # 滚动过程中合并重绘，减少卡顿
        if self._pending is None:
            self._pending = self.after_idle(self._flush_redraw)

    def _flush_redraw(self):
        self._pending = None
        self._redraw()

    def _on_configure(self, _event=None):
        self._redraw(force=True)

    def _on_enter(self, _event=None):
        self._color = THUMB_HOVER
        self._redraw(force=True)

    def _on_leave(self, _event=None):
        if self._drag_off is None:
            self._color = THUMB
            self._redraw(force=True)

    def _metrics(self):
        h = max(int(self.winfo_height()), 1)
        span = max(0.0, self._last - self._first)
        if span >= 0.995:
            return None
        thumb_h = max(28, int(round(h * max(span, 0.05))))
        thumb_h = min(thumb_h, h)
        travel = max(h - thumb_h, 1)
        # 用 first 映射到可滑动区间
        denom = max(1.0 - span, 1e-6)
        ratio = max(0.0, min(1.0, self._first / denom))
        y0 = int(round(travel * ratio))
        return y0, y0 + thumb_h, travel, thumb_h, span

    def _redraw(self, force=False):
        w = max(int(self.winfo_width()), self.WIDTH)
        h = max(int(self.winfo_height()), 1)
        metrics = self._metrics()

        if self._rail_id is None or force:
            self.delete("all")
            self._thumb_id = None
            pad = 2
            self._rail_id = self.create_rectangle(
                pad, 0, w - pad, h, fill=TRACK, outline="", tags="rail",
            )
        else:
            self.coords(self._rail_id, 2, 0, w - 2, h)
            self.itemconfigure(self._rail_id, fill=TRACK)

        if not metrics:
            if self._thumb_id is not None:
                self.delete(self._thumb_id)
                self._thumb_id = None
            return

        y0, y1, _travel, _th, _span = metrics
        x0, x1 = 1, w - 1
        if self._thumb_id is None:
            self._thumb_id = self.create_rectangle(
                x0, y0, x1, y1, fill=self._color, outline="", tags="thumb",
            )
        else:
            self.coords(self._thumb_id, x0, y0, x1, y1)
            self.itemconfigure(self._thumb_id, fill=self._color)

    def _move_fraction(self, first):
        if not callable(self.command):
            return
        metrics = self._metrics()
        span = metrics[4] if metrics else max(self._last - self._first, 0.05)
        first = max(0.0, min(max(0.0, 1.0 - span), float(first)))
        self.command("moveto", first)

    def _on_press(self, event):
        metrics = self._metrics()
        if not metrics:
            return
        y0, y1, travel, thumb_h, span = metrics
        self._color = THUMB_ACTIVE
        if y0 <= event.y <= y1:
            self._drag_off = event.y - y0
            self._redraw(force=True)
            return
        # 点击空白：拇指中心跳到点击处
        if travel <= 0:
            return
        top = max(0, min(travel, int(event.y - thumb_h / 2)))
        self._move_fraction((top / float(travel)) * max(1.0 - span, 1e-6))
        self._drag_off = event.y - top
        self._redraw(force=True)

    def _on_drag(self, event):
        if self._drag_off is None:
            return
        metrics = self._metrics()
        if not metrics:
            return
        _y0, _y1, travel, _th, span = metrics
        if travel <= 0:
            return
        top = max(0, min(travel, int(event.y - self._drag_off)))
        self._move_fraction((top / float(travel)) * max(1.0 - span, 1e-6))

    def _on_release(self, _event):
        self._drag_off = None
        self._color = THUMB_HOVER
        self._redraw(force=True)

    def _on_wheel(self, event):
        if not callable(self.command):
            return
        steps = _wheel_steps(event)
        if steps:
            self.command("scroll", steps, "units")


def _wheel_steps(event):
    try:
        delta = int(event.delta)
    except Exception:
        return 0
    if delta == 0:
        return 0
    if abs(delta) >= 120:
        return int(-delta / 120)
    return -1 if delta > 0 else 1


def attach_canvas_scroll(canvas, scrollbar, inner, window_id, root, state):
    """绑定 Canvas 滚动。

    注意：内嵌 Frame 上移动鼠标会触发 Canvas <Leave>，因此不能靠
    Enter/Leave + unbind_all，否则滚到卡片上就失灵、手感发粘。
    """
    canvas.configure(yscrollcommand=scrollbar.set, yscrollincrement=28)
    scrollbar.command = canvas.yview
    state.setdefault("region_job", None)
    state["canvas"] = canvas
    state["inner"] = inner

    def sync_region():
        state["region_job"] = None
        try:
            bbox = canvas.bbox("all")
            canvas.configure(scrollregion=bbox if bbox else (0, 0, 1, 1))
        except Exception:
            pass

    def on_inner(_event=None):
        job = state.get("region_job")
        if job is not None:
            try:
                root.after_cancel(job)
            except Exception:
                pass
        state["region_job"] = root.after(16, sync_region)

    def on_canvas(event):
        try:
            canvas.itemconfigure(window_id, width=event.width)
        except Exception:
            pass

    def hit_test(x_root, y_root):
        try:
            w = root.winfo_containing(x_root, y_root)
        except Exception:
            return False
        while w is not None:
            if w in (canvas, inner, scrollbar):
                return True
            try:
                w = w.master
            except Exception:
                break
        return False

    def on_wheel(event):
        if not hit_test(event.x_root, event.y_root):
            return
        steps = _wheel_steps(event)
        if not steps:
            return
        try:
            canvas.yview_scroll(steps, "units")
        except Exception:
            pass
        return "break"

    # 全局一只滚轮路由：按指针落点决定滚哪个 Canvas，互不抢绑
    key = "wheel_handler"
    old = state.get(key)
    if old is not None:
        try:
            root.unbind_all("<MouseWheel>", old)
        except Exception:
            pass
    # bind_all 不能按 handler 卸载，改用共享分发器挂在 root 上
    dispatch = getattr(root, "_slim_wheel_dispatch", None)
    if dispatch is None:
        handlers = []

        def dispatch(event):
            for fn in list(handlers):
                try:
                    result = fn(event)
                except Exception:
                    continue
                if result == "break":
                    return "break"
            return None

        dispatch._handlers = handlers
        root._slim_wheel_dispatch = dispatch
        root.bind_all("<MouseWheel>", dispatch)

    handlers = root._slim_wheel_dispatch._handlers
    if on_wheel not in handlers:
        handlers.append(on_wheel)
    state[key] = on_wheel
    state["remove_wheel"] = lambda: (
        handlers.remove(on_wheel) if on_wheel in handlers else None
    )

    inner.bind("<Configure>", on_inner)
    canvas.bind("<Configure>", on_canvas)
    state["sync_region"] = sync_region
    state["on_inner"] = on_inner
    return state
