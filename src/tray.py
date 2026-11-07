# -*- coding: utf-8 -*-
"""
workbuddy-codebuddy-chat-sync 托盘程序

常驻右下角通知区域，右键出菜单：
- 实时显示 WorkBuddy / CodeBuddy 各自有哪些账号、各多少条
- 两个软件分别指定归到哪个账号（允许登录不同号）
- 每 interval_seconds 秒自动同步一次
- 所有操作都会弹气泡反馈

启动：pythonw src/tray.py
"""
import msvcrt
import os
import sys
import threading
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sync_core as core  # noqa: E402

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:
    # pythonw.exe 没有控制台，直接 raise 的话用户双击后什么都看不到，
    # 只会以为程序坏了。这里弹个窗，把要执行的命令写清楚。
    # 注意：机器上可能装了多个 Python，pip install 容易装错版本，
    # 所以直接给出本进程对应的那个解释器的完整路径，让用户照着复制。
    _py = sys.executable
    if _py.lower().endswith("pythonw.exe"):
        _py = _py[:-len("pythonw.exe")] + "python.exe"
    _msg = (
        "缺少运行依赖：pystray、pillow\n\n"
        "请按 Win+R 输入 cmd 回车，把下面整行粘贴进去执行：\n\n"
        '"%s" -m pip install pystray pillow\n\n'
        "装完再双击启动即可。\n\n"
        "（用这条命令而不是直接写 pip install，是因为电脑上可能装了\n"
        "多个 Python，直接 pip 容易装到别的版本上去。）" % _py
    )
    core.log("缺少依赖：" + _py + " -m pip install pystray pillow")
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, _msg, "账号记录同步", 0x30)
    except Exception:
        pass
    sys.exit(1)

LOCK_PATH = os.path.join(core.DATA_DIR, "tray.lock")

state = {"auto": True}


def make_image():
    """任务栏图标：蓝色圆底 + 上下两点 + 连接线"""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((3, 3, 61, 61), fill=(56, 132, 255, 255))
    d.ellipse((17, 13, 33, 29), fill=(255, 255, 255, 255))
    d.ellipse((31, 35, 47, 51), fill=(255, 255, 255, 255))
    d.line((25, 29, 25, 42), fill=(255, 255, 255, 255), width=4)
    d.line((39, 22, 39, 35), fill=(255, 255, 255, 255), width=4)
    return img


# ---------------------------------------------------------------- 菜单内容

def status_text(product, key):
    cfg = core.load_config()
    data = core.stats().get(key, {})
    title = "WorkBuddy" if key == "wb" else "CodeBuddy"
    if not data:
        return "%s: 未发现数据" % title
    parts = [
        "%s %d 条" % (core.account_label(uid, cfg), n) for uid, n in data.items()
    ]
    return "%s: %s" % (title, "，".join(parts))


# ---------------------------------------------------------------- 开机自启
# 用 HKCU 的 Run 键，只影响当前用户，不需要管理员权限，也不往启动文件夹丢东西。

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "workbuddy-codebuddy-chat-sync"


def launch_command():
    """开机自启时应该执行的命令。

    打包成 exe 后被调用的就是自己；从源码跑则拼 pythonw + tray.py。
    """
    if getattr(sys, "frozen", False):
        return '"%s"' % sys.executable
    pyw = sys.executable
    if pyw.lower().endswith("python.exe"):
        cand = pyw[: -len("python.exe")] + "pythonw.exe"
        if os.path.exists(cand):
            pyw = cand
    return '"%s" "%s"' % (pyw, os.path.abspath(__file__))


def _read_run_value():
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as k:
            try:
                val, _ = winreg.QueryValueEx(k, RUN_VALUE)
                return val or ""
            except FileNotFoundError:
                return ""
    except Exception:
        return ""


def autostart_enabled():
    return bool(_read_run_value())


def set_autostart(enable):
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if enable:
                winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, launch_command())
            else:
                try:
                    winreg.DeleteValue(k, RUN_VALUE)
                except FileNotFoundError:
                    pass
        return True
    except Exception:
        core.log("设置开机自启失败：" + traceback.format_exc())
        return False


def toggle_autostart(icon, item):
    want = not autostart_enabled()
    if set_autostart(want):
        _notify(icon, "已开启开机自动启动" if want else "已关闭开机自动启动")
    else:
        _notify(icon, "设置失败，详情见同步日志")
    _refresh(icon)


def fix_autostart_path():
    """整个文件夹被移动过之后，注册表里还是旧路径。启动时顺手校正一次。"""
    cur = _read_run_value()
    if cur and cur != launch_command():
        if set_autostart(True):
            core.log("开机自启路径已校正为：" + launch_command())


def build_account_menu(product):
    """为一个软件生成"归到某账号"的子菜单；账号从数据库现场扫描"""
    cfg = core.load_config()
    uids = core.discover_accounts()
    if not uids:
        return pystray.Menu(
            pystray.MenuItem("（未发现账号）", None, enabled=False)
        )
    items = []
    for uid in uids:
        label = core.account_label(uid, cfg)

        def make_action(u):
            def action(icon, item):
                do_force(icon, product, u)
            return action

        items.append(pystray.MenuItem("归到 %s" % label, make_action(uid)))
    return pystray.Menu(*items)


def build_menu():
    return pystray.Menu(
        pystray.MenuItem(lambda i: status_text("WorkBuddy", "wb"), None, enabled=False),
        pystray.MenuItem(lambda i: status_text("CodeBuddy", "cb"), None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("WorkBuddy  ▸", build_account_menu("wb")),
        pystray.MenuItem("CodeBuddy  ▸", build_account_menu("cb")),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("立即同步一次", do_sync_once),
        pystray.MenuItem(
            "自动同步",
            toggle_auto,
            checked=lambda item: state["auto"],
        ),
        pystray.MenuItem(
            "开机自动启动",
            toggle_autostart,
            checked=lambda item: autostart_enabled(),
        ),
        pystray.MenuItem("打开同步日志", open_log),
        pystray.MenuItem("打开配置文件", open_config),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", quit_app),
    )


# ---------------------------------------------------------------- 动作

def _notify(icon, msg):
    try:
        icon.notify(msg, "账号记录同步")
    except Exception:
        pass


def _refresh(icon):
    """重建整个菜单，这样新登录的账号也能立刻出现在子菜单里"""
    try:
        icon.menu = build_menu()
    except Exception:
        pass
    try:
        icon.update_menu()
    except Exception:
        pass


def do_force(icon, product, uid):
    try:
        msg = core.sync(product=product, target=uid)
        core.log(msg)
    except Exception:
        msg = "操作失败：%s" % traceback.format_exc(limit=1)
        core.log(msg)
    _notify(icon, msg)
    _refresh(icon)


def do_sync_once(icon, item):
    try:
        msg = core.sync()
    except Exception as e:
        msg = "同步失败：%s" % e
    _notify(icon, msg)
    _refresh(icon)


def toggle_auto(icon, item):
    state["auto"] = not state["auto"]
    _notify(icon, "自动同步已%s" % ("开启" if state["auto"] else "关闭"))
    _refresh(icon)


def open_log(icon, item):
    try:
        if os.path.exists(core.LOG_PATH):
            os.startfile(core.LOG_PATH)
    except Exception:
        pass


def open_config(icon, item):
    try:
        os.makedirs(core.DATA_DIR, exist_ok=True)
        if not os.path.exists(core.CONFIG_PATH):
            with open(core.CONFIG_PATH, "w", encoding="utf-8") as f:
                f.write('{\n  "accounts": {},\n  "auto_sync": true,\n'
                        '  "interval_seconds": 120,\n  "merge_history_files": true\n}\n')
        os.startfile(core.CONFIG_PATH)
    except Exception:
        pass


def quit_app(icon, item):
    icon.stop()


# ---------------------------------------------------------------- 后台循环

def ticker():
    # 预热：第一次要扫一遍日志才能拿到账号昵称，别让它在渲染菜单时卡住
    try:
        core.detected_names()
    except Exception:
        pass
    while True:
        if state["auto"]:
            try:
                core.sync()
            except Exception:
                core.log("定时同步异常：" + traceback.format_exc())
        interval = int(core.load_config().get("interval_seconds") or 120)
        time.sleep(max(30, interval))


def acquire_lock():
    os.makedirs(core.DATA_DIR, exist_ok=True)
    fh = open(LOCK_PATH, "a+")
    try:
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        return fh
    except OSError:
        fh.close()
        return None


def already_running_hint():
    """双击启动但已有实例时，给个明确反馈，避免用户以为程序坏了"""
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            0,
            "同步托盘已经在运行了。\n\n"
            "请看屏幕右下角的通知区域，\n"
            "如果没直接看到，点一下那里的 ∧ 箭头展开，\n"
            "找到一个蓝色的圆形图标，右键它就能切换账号。",
            "账号记录同步",
            0x40,
        )
    except Exception:
        pass


def main():
    lock = acquire_lock()
    if lock is None:
        already_running_hint()
        return
    state["auto"] = bool(core.load_config().get("auto_sync", True))
    core.log("托盘程序启动")
    fix_autostart_path()

    icon = pystray.Icon(
        "workbuddy_codebuddy_chat_sync", make_image(), "账号记录同步", build_menu()
    )

    def setup(i):
        # 关键：pystray 传入自定义 setup 时，必须自己把图标设为可见，
        # 否则图标不会出现在通知区域（默认的 setup 才会自动设 True）
        try:
            i.visible = True
            core.log("图标已显示 visible=%s" % i.visible)
        except Exception:
            core.log("设置图标可见失败：" + traceback.format_exc())
        threading.Thread(target=ticker, daemon=True).start()

    icon.run(setup=setup)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        core.log("托盘启动失败：" + traceback.format_exc())
