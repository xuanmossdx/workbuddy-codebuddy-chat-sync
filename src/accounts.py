# -*- coding: utf-8 -*-
"""
WorkBuddy 多账号管理 · 数据层

只做本地操作：建目录、生成启动器、拉起实例、置前、停止、读登录态元数据、
按需查询额度。不发送任何签到/领取请求，不启动调试端口，不做自动化点击。

数据位置（便携）：
  exe（或源码运行时的项目根）同级 data\\；不可写时回退
  %APPDATA%\\workbuddy-codebuddy-chat-sync\\data，界面显示实际位置。
  data\\ 只放轻量内容：账号表、账号设置、界面/同步用的最后状态。
  不复制客户端数据库，不写登录令牌。

本模块被界面直接 import 调用，不依赖任何 CLI 输出。
"""
import base64
import ctypes
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest

import _aes

UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
ACCOUNT_DIR_RE = re.compile(r"^wb-(\d+)$")
LAUNCHER_NAME = "launch-workbuddy.ps1"

DEFAULT_SETTINGS = {
    "workbuddy": {
        "exePath": None,
        "accountsRoot": None,
    },
    "trae": {
        "instanceRoots": [],
    },
    "launch": {
        "loginTimeoutSec": 1200,
        "pollIntervalSec": 2,
        "switchTimeoutSec": 180,
    },
}


# ================================================================ 基础工具

def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def deep_merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# ================================================================ 便携数据目录

def app_base_dir():
    """exe 所在目录；源码运行时为项目根。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _try_writable(path):
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-test"
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        probe.unlink()
        return True
    except Exception:
        return False


_resolved = {}


def data_location():
    """返回 {path, portable, kind}。优先程序同级 data\\，不可写回退 %APPDATA%。"""
    if _resolved:
        return dict(_resolved)

    override = os.environ.get("WB_CB_ACCOUNTS_DATA")
    if override:
        p = Path(override).expanduser()
        if _try_writable(p):
            _resolved.update(path=str(p), portable=False, kind="自定义")
            return dict(_resolved)
        raise RuntimeError("WB_CB_ACCOUNTS_DATA 指向的目录不可写：%s" % p)

    portable = app_base_dir() / "data"
    if _try_writable(portable):
        _resolved.update(path=str(portable), portable=True, kind="便携")
        return dict(_resolved)

    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    fallback = Path(appdata) / "workbuddy-codebuddy-chat-sync" / "data"
    if _try_writable(fallback):
        _resolved.update(path=str(fallback), portable=False, kind="回退（程序目录不可写）")
        return dict(_resolved)

    raise RuntimeError("没有可写的数据目录：已尝试 %s 与 %s" % (portable, fallback))


DATA_DIR = Path(data_location()["path"])
ACCOUNTS_PATH = DATA_DIR / "accounts.json"
SETTINGS_PATH = DATA_DIR / "accounts-config.json"
STATE_PATH = DATA_DIR / "account-state.json"
SHORTCUTS_PATH = DATA_DIR / "shortcuts.json"

_STORE_LOCK = threading.Lock()
_STATE_LOCK = threading.Lock()


def data_dir_text():
    info = data_location()
    return "%s（%s）" % (info["path"], info["kind"])


def ensure_store():
    """首次运行：没有账号表就按空表初始化。"""
    with _STORE_LOCK:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if not ACCOUNTS_PATH.exists():
            write_json(ACCOUNTS_PATH, {"version": 1, "accounts": []})


ensure_store()


# ================================================================ 设置

def default_root():
    env = os.environ.get("WB_ACCOUNTS_ROOT")
    if env:
        return Path(env).expanduser()
    return app_base_dir() / "accounts"


def detect_exe():
    env = os.environ.get("WORKBUDDY_EXE")
    if env and Path(env).is_file():
        return str(Path(env))

    try:
        import winreg

        keys = [
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\WorkBuddy.exe"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\WorkBuddy.exe"),
        ]
        for hive, sub in keys:
            try:
                with winreg.OpenKey(hive, sub) as key:
                    value = str(winreg.QueryValue(key, None)).strip().strip('"')
                    if Path(value).is_file():
                        return value
            except OSError:
                pass
    except Exception:
        pass

    try:
        out = subprocess.check_output(
            [
                "powershell", "-NoProfile", "-NonInteractive", "-Command",
                "Get-Process WorkBuddy -ErrorAction SilentlyContinue | "
                "Select-Object -First 1 -ExpandProperty Path",
            ],
            text=True, timeout=20, stderr=subprocess.DEVNULL,
            creationflags=0x08000000,
        ).strip()
        if out and Path(out).is_file():
            return out
    except Exception:
        pass

    candidates = []
    local = os.environ.get("LOCALAPPDATA")
    programs = os.environ.get("ProgramFiles")
    programs86 = os.environ.get("ProgramFiles(x86)")
    if local:
        candidates.append(Path(local) / "Programs" / "WorkBuddy" / "WorkBuddy.exe")
    if programs:
        candidates.append(Path(programs) / "WorkBuddy" / "WorkBuddy.exe")
    if programs86:
        candidates.append(Path(programs86) / "WorkBuddy" / "WorkBuddy.exe")
    for p in candidates:
        if p.is_file():
            return str(p)
    return None


def ensure_settings():
    with _STORE_LOCK:
        cfg = deep_merge(DEFAULT_SETTINGS, read_json(SETTINGS_PATH, {}))
        changed = not SETTINGS_PATH.exists()
        if not cfg["workbuddy"].get("exePath"):
            exe = detect_exe()
            if exe:
                cfg["workbuddy"]["exePath"] = exe
                changed = True
        if not cfg["workbuddy"].get("accountsRoot"):
            cfg["workbuddy"]["accountsRoot"] = str(default_root())
            changed = True
        if changed:
            write_json(SETTINGS_PATH, cfg)
        return cfg


def save_settings(cfg):
    with _STORE_LOCK:
        write_json(SETTINGS_PATH, cfg)


def exe_path():
    cfg = ensure_settings()
    value = cfg["workbuddy"].get("exePath")
    return Path(value) if value else None


# ================================================================ 账号表

def load_registry():
    data = read_json(ACCOUNTS_PATH, {})
    if isinstance(data, list):
        return {"version": 1, "accounts": data}
    if not isinstance(data, dict):
        return {"version": 1, "accounts": []}
    data.setdefault("version", 1)
    data.setdefault("accounts", [])
    return data


def save_registry(reg):
    with _STORE_LOCK:
        write_json(ACCOUNTS_PATH, reg)


def set_active(uid):
    reg = load_registry()
    reg["activeUid"] = uid
    save_registry(reg)


# ================================================================ 目录与启动器

def next_account_dir(root):
    used = []
    if root.exists():
        for p in root.iterdir():
            m = ACCOUNT_DIR_RE.match(p.name)
            if m and p.is_dir():
                used.append(int(m.group(1)))
    n = (max(used) + 1) if used else 1
    while True:
        candidate = root / ("wb-%02d" % n)
        if not candidate.exists():
            return candidate
        n += 1


def build_launcher(exe, config_dir):
    """在账号目录生成持久化启动器，固定该账号的配置目录与 profile。"""
    home = config_dir / "home"
    home.mkdir(parents=True, exist_ok=True)
    local = config_dir / "local"
    local.mkdir(parents=True, exist_ok=True)
    roaming = config_dir / "roaming"
    roaming.mkdir(parents=True, exist_ok=True)
    launcher = config_dir / LAUNCHER_NAME

    def q(p):
        return "'" + str(p).replace("'", "''") + "'"

    lines = [
        "# 由账号管理生成：固定该账号的配置目录与 profile，客户端自我重启后仍然生效",
        "$env:WORKBUDDY_CONFIG_DIR = %s" % q(config_dir),
        "$env:WORKBUDDY_USER_DATA_DIR = %s" % q(config_dir / "app"),
        "$env:WORKBUDDY_DATA_DIR = %s" % q(config_dir),
        "$env:USERPROFILE = %s" % q(home),
        "$env:HOME = %s" % q(home),
        "$env:LOCALAPPDATA = %s" % q(local),
        "$env:APPDATA = %s" % q(roaming),
        "$env:HOMEDRIVE = %s" % q(home.drive),
        "$env:HOMEPATH = %s" % q(str(home)[len(home.drive):]),
        "$env:WORKBUDDY_DISABLE_APP_LIFETIME_JOB = '1'",
        "'ELECTRON_RUN_AS_NODE', 'WORKBUDDY_INSTANCE_NUMBER', 'WORKBUDDY_ORIGINAL_USERPROFILE',",
        "'WORKBUDDY_FALLBACK_HOME', 'WORKBUDDY_STALE_FALLBACK_CONFIG_DIR' |",
        "  ForEach-Object { Remove-Item \"Env:$_\" -ErrorAction SilentlyContinue }",
        "Start-Process -FilePath %s -WorkingDirectory %s" % (q(exe), q(exe.parent)),
    ]
    launcher.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8-sig")
    return launcher


def launch_workbuddy(exe, config_dir):
    """通过独立启动器拉起实例，并用 WMI 创建进程使其脱离当前 Job。"""
    launcher = build_launcher(exe, config_dir)
    cmdline = (
        "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass "
        '-WindowStyle Hidden -File "%s"' % launcher
    )
    env = os.environ.copy()
    env["WB_LAUNCH_CMDLINE"] = cmdline
    env["WB_LAUNCH_CWD"] = str(config_dir)
    ps = (
        "$ErrorActionPreference='Stop';"
        "$r=Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
        "-Arguments @{CommandLine=$env:WB_LAUNCH_CMDLINE;CurrentDirectory=$env:WB_LAUNCH_CWD};"
        "if($r.ReturnValue -ne 0){exit 9};$r.ProcessId"
    )
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            env=env, text=True, timeout=120, stderr=subprocess.STDOUT,
            creationflags=0x08000000,
        ).strip()
        pid = int(out.splitlines()[-1]) if out else None
        return True, pid
    except Exception:
        env2 = os.environ.copy()
        env2["WORKBUDDY_CONFIG_DIR"] = str(config_dir)
        env2["WORKBUDDY_USER_DATA_DIR"] = str(config_dir / "app")
        env2["WORKBUDDY_DATA_DIR"] = str(config_dir)
        env2["USERPROFILE"] = str(config_dir / "home")
        env2["HOME"] = str(config_dir / "home")
        env2["WORKBUDDY_DISABLE_APP_LIFETIME_JOB"] = "1"
        proc = subprocess.Popen(
            [str(exe)], cwd=str(exe.parent), env=env2,
            creationflags=0x00000008 | 0x00000200,
            close_fds=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return False, proc.pid


# ================================================================ 桌面入口

def desktop_dir():
    """当前用户的桌面目录（跟随 OneDrive/系统重定向，不写死用户目录）。"""
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        )
        with key:
            value = winreg.QueryValueEx(key, "Desktop")[0]
        path = Path(os.path.expandvars(str(value)))
        if path.is_dir():
            return path
    except Exception:
        pass
    home = os.environ.get("USERPROFILE") or str(Path.home())
    return Path(home) / "Desktop"


def _safe_shortcut_name(label):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(label or "")).strip().strip(".")
    return name or "未命名账号"


def _load_shortcuts():
    data = read_json(SHORTCUTS_PATH, {})
    return data if isinstance(data, dict) else {}


def _save_shortcuts(data):
    with _STORE_LOCK:
        write_json(SHORTCUTS_PATH, data)


def _powershell_exe():
    root = os.environ.get("SystemRoot")
    if root:
        candidate = Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        if candidate.is_file():
            return candidate
    return Path("powershell.exe")


def create_desktop_shortcut(uid, label, launcher, exe, config_dir):
    """为实例创建/更新桌面入口。

    入口指向该实例自己的 launch-workbuddy.ps1，不复制客户端、不复用主实例路径。
    同一 uid 重复调用只更新原入口；标签变化时改名并清理旧文件；
    遇到同名的非本工具快捷方式不覆盖，自动追加序号。
    """
    launcher = Path(launcher)
    config_dir = Path(config_dir)
    if not launcher.is_file():
        raise RuntimeError("实例启动器不存在：%s" % launcher)

    desktop = desktop_dir()
    if not desktop.is_dir():
        raise RuntimeError("桌面目录不存在：%s" % desktop)

    data = _load_shortcuts()
    entry = data.get(uid) if isinstance(data.get(uid), dict) else {}
    old_path = Path(entry["path"]) if entry.get("path") else None

    base_name = "WorkBuddy - %s" % _safe_shortcut_name(label)
    target = desktop / (base_name + ".lnk")
    if target.exists() and (old_path is None or target.resolve() != old_path.resolve()):
        # 同名文件不是本工具为这个账号登记的入口：不覆盖，换一个可用名字
        n = 2
        while target.exists():
            target = desktop / ("%s (%d).lnk" % (base_name, n))
            n += 1

    args = (
        '-NoProfile -NonInteractive -ExecutionPolicy Bypass '
        '-WindowStyle Hidden -File "%s"' % launcher
    )
    env = os.environ.copy()
    env["WB_LNK"] = str(target)
    env["WB_TARGET"] = str(_powershell_exe())
    env["WB_ARGS"] = args
    env["WB_CWD"] = str(config_dir)
    env["WB_ICON"] = "%s,0" % exe
    env["WB_DESC"] = "WorkBuddy 账号：%s（%s）" % (label, config_dir.name)
    ps = (
        "$ErrorActionPreference='Stop';"
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:WB_LNK);"
        "$s.TargetPath=$env:WB_TARGET;"
        "$s.Arguments=$env:WB_ARGS;"
        "$s.WorkingDirectory=$env:WB_CWD;"
        "$s.IconLocation=$env:WB_ICON;"
        "$s.WindowStyle=7;"
        "$s.Description=$env:WB_DESC;"
        "$s.Save()"
    )
    try:
        subprocess.check_output(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            env=env, text=True, timeout=60, stderr=subprocess.STDOUT,
            creationflags=0x08000000,
        )
    except Exception as exc:
        raise RuntimeError("创建快捷方式失败：%s" % exc)

    if not target.is_file():
        raise RuntimeError("创建快捷方式失败：未生成 %s" % target)

    # 标签变化导致改名时，只删除本工具之前登记的那个入口
    if old_path and old_path != target and old_path.is_file():
        try:
            old_path.unlink()
        except OSError:
            pass

    data[uid] = {
        "path": str(target),
        "label": label,
        "updatedAt": now_iso(),
    }
    _save_shortcuts(data)
    return str(target)


# ================================================================ 身份与登录态

def default_profile_dir():
    home = os.environ.get("USERPROFILE") or str(Path.home())
    return (Path(home) / ".workbuddy").resolve()


def auth_dir_for(config_dir, is_default):
    isolated = (
        config_dir / "home" / "AppData" / "Local" / "CodeBuddyExtension"
        / "Data" / "Public" / "auth"
    )
    if isolated.is_dir():
        return isolated
    if is_default:
        local = os.environ.get("LOCALAPPDATA")
        if local:
            shared = Path(local) / "CodeBuddyExtension" / "Data" / "Public" / "auth"
            if shared.is_dir():
                return shared
    return None


def auth_file_uids(config_dir, is_default):
    found = set()
    newest = 0.0
    auth = auth_dir_for(config_dir, is_default)
    if not auth:
        return found, newest
    for info in auth.glob("*.info"):
        try:
            text = info.read_text(encoding="utf-8", errors="ignore")
            newest = max(newest, info.stat().st_mtime)
        except OSError:
            continue
        found.update(m.lower() for m in re.findall(UUID, text))
    return found, newest


def read_snapshot(config_dir):
    data = read_json(config_dir / "storage" / "skeleton" / "account-snapshot.json", {})
    primary = data.get("primary") if isinstance(data, dict) else None
    if isinstance(primary, dict) and primary.get("uid"):
        return primary
    return None


def identity_of(config_dir, is_default):
    uids, _ = auth_file_uids(config_dir, is_default)
    snap = read_snapshot(config_dir)
    if snap:
        uid = str(snap["uid"]).lower()
        if uids and uid not in uids:
            return None
        return {
            "uid": uid,
            "nickname": snap.get("nickname") or "",
            "confirmedByAuth": bool(uids),
        }
    if len(uids) == 1:
        return {"uid": next(iter(uids)), "nickname": "", "confirmedByAuth": True}
    return None


def auth_meta(config_dir, is_default, uid=None):
    """只读取登录态元数据：uid、到期时间、令牌是否为加密包。

    不返回、不记录明文令牌以外的东西；调用方也用不到。
    """
    auth = auth_dir_for(config_dir, is_default)
    if not auth:
        return None
    files = list(auth.glob("*.info"))
    if not files:
        return None

    def file_uid(f):
        try:
            d = json.loads(f.read_text(encoding="utf-8", errors="ignore"))
            return str(((d.get("account") or {}).get("uid")) or "").lower()
        except Exception:
            return ""

    chosen = None
    if uid:
        for f in files:
            if file_uid(f) == str(uid).lower():
                chosen = f
                break
    if chosen is None:
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        chosen = files[0]

    try:
        data = json.loads(chosen.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return None
    auth_obj = data.get("auth") or {}
    token = auth_obj.get("accessToken")
    encrypted = isinstance(token, dict)
    return {
        "uid": str(((data.get("account") or {}).get("uid")) or "").lower(),
        "expiresAt": int(auth_obj.get("expiresAt") or 0),
        "refreshExpiresAt": int(auth_obj.get("refreshExpiresAt") or 0),
        "domain": auth_obj.get("domain") or "www.workbuddy.cn",
        "token": token if isinstance(token, str) else None,
        "encrypted": encrypted,
        "updatedAt": datetime.fromtimestamp(chosen.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
    }


def expiry_text(meta):
    if not meta or not meta.get("expiresAt"):
        return "未知"
    ts = meta["expiresAt"] / 1000.0
    if ts <= time.time():
        return "已过期"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


# ================================================================ 进程与窗口

user32 = ctypes.WinDLL("user32", use_last_error=True) if os.name == "nt" else None
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True) if os.name == "nt" else None
STILL_ACTIVE = 259
WM_CLOSE = 0x0010


def pid_alive(pid):
    if os.name != "nt" or not pid:
        return False
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    code = wintypes.DWORD()
    ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
    kernel32.CloseHandle(handle)
    return bool(ok) and code.value == STILL_ACTIVE


def workbuddy_processes(exe):
    if not exe:
        return {}
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='WorkBuddy.exe'\" | "
        "ForEach-Object { \"$($_.ProcessId)|$($_.ExecutablePath)\" }"
    )
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            text=True, timeout=60, stderr=subprocess.DEVNULL,
            creationflags=0x08000000,
        )
    except Exception:
        return {}
    wanted = str(exe).lower()
    procs = {}
    for line in out.splitlines():
        if "|" not in line:
            continue
        pid_text, path = line.split("|", 1)
        if pid_text.strip().isdigit() and path.strip().lower() == wanted:
            procs[int(pid_text)] = path.strip()
    return procs


def live_pid(config_dir, procs):
    pids = []
    for marker in config_dir.glob("epoch-marker-*.json"):
        m = re.search(r"(\d+)", marker.stem)
        if m and int(m.group(1)) in procs:
            pids.append(int(m.group(1)))
    return max(pids) if pids else None


def heartbeat_age(config_dir):
    newest = None
    memory = config_dir / "app" / "memory"
    if memory.is_dir():
        for f in memory.glob("polling-lease-*.json"):
            data = read_json(f, {})
            ts = data.get("heartbeatAt") if isinstance(data, dict) else None
            if isinstance(ts, (int, float)):
                newest = ts if newest is None else max(newest, ts)
    if newest is None:
        return None
    return max(0.0, time.time() - newest / 1000.0)


def find_window(pid):
    if not user32 or not pid:
        return None
    found = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lparam):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            if "WorkBuddy" in buf.value:
                found.append(hwnd)
        return True

    user32.EnumWindows(callback_type(_cb), 0)
    return found[0] if found else None


def bring_to_front(hwnd):
    if not hwnd:
        return False
    user32.ShowWindow(hwnd, 9)
    user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002)
    user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, 0x0001 | 0x0002)
    user32.SetForegroundWindow(hwnd)
    user32.BringWindowToTop(hwnd)
    time.sleep(0.4)
    return user32.GetForegroundWindow() == hwnd


def close_instance(pid, timeout=25):
    hwnd = find_window(pid)
    if hwnd:
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not pid_alive(pid):
            return "graceful"
        time.sleep(0.5)
    subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 10
    while time.time() < deadline:
        if not pid_alive(pid):
            return "forced"
        time.sleep(0.5)
    return "failed"


# ================================================================ 账号登记

def register_account(uid, config_dir, label, exe, nickname="", is_default=False):
    reg = load_registry()
    existing = next((a for a in reg["accounts"] if a.get("accountUid") == uid), None)
    if existing:
        existing["configDir"] = str(config_dir)
        existing["label"] = label or existing.get("label") or config_dir.name
        if nickname:
            existing["nickname"] = nickname
        existing["lastLoginAt"] = now_iso()
        existing["status"] = "active"
        existing["clientExe"] = str(exe) if exe else existing.get("clientExe", "")
        record = existing
    else:
        record = {
            "accountUid": uid,
            "nickname": nickname,
            "label": label or config_dir.name,
            "configDir": str(config_dir),
            "createdAt": now_iso(),
            "lastLoginAt": now_iso(),
            "status": "active",
            "clientExe": str(exe) if exe else "",
        }
        reg["accounts"].append(record)
    if is_default:
        record["isDefaultProfile"] = True
    save_registry(reg)
    return record


def resolve_account(pattern):
    accounts = load_registry()["accounts"]
    pattern = str(pattern or "")
    if pattern.isdigit():
        idx = int(pattern)
        if 1 <= idx <= len(accounts):
            return accounts[idx - 1]
    for a in accounts:
        d = Path(str(a.get("configDir", "")))
        if pattern in (d.name, str(d), a.get("accountUid"), a.get("label"), a.get("nickname")):
            return a
    return None


def find_account(uid):
    for a in load_registry()["accounts"]:
        if str(a.get("accountUid", "")).lower() == str(uid or "").lower():
            return a
    return None


def known_uids():
    return {
        str(a.get("accountUid", "")).lower()
        for a in load_registry()["accounts"]
        if a.get("accountUid")
    }


def ensure_default_account():
    """识别默认 profile 的当前账号并登记（依据账号快照 + auth 文件）。"""
    root = default_profile_dir()
    ident = identity_of(root, True)
    if not ident:
        return None
    exe_value = ensure_settings()["workbuddy"].get("exePath")
    reg = load_registry()
    for a in reg["accounts"]:
        if a.get("accountUid") == ident["uid"]:
            changed = False
            if ident["nickname"] and a.get("nickname") != ident["nickname"]:
                a["nickname"] = ident["nickname"]
                changed = True
            if a.get("configDir") != str(root):
                a["configDir"] = str(root)
                changed = True
            if not a.get("isDefaultProfile"):
                a["isDefaultProfile"] = True
                changed = True
            if changed:
                save_registry(reg)
            return a
    record = {
        "accountUid": ident["uid"],
        "nickname": ident["nickname"],
        "label": ident["nickname"] or "主账号",
        "configDir": str(root),
        "createdAt": now_iso(),
        "lastLoginAt": now_iso(),
        "status": "active",
        "clientExe": exe_value or "",
        "isDefaultProfile": True,
    }
    reg["accounts"].append(record)
    save_registry(reg)
    return record


def wait_for_login(config_dir, is_default, started_at, timeout, interval):
    known = known_uids()
    deadline = time.time() + timeout
    while time.time() < deadline:
        ident = identity_of(config_dir, is_default)
        if ident and ident["uid"] not in known:
            _, newest = auth_file_uids(config_dir, is_default)
            if ident["confirmedByAuth"] and newest >= started_at - 5:
                return ident
        time.sleep(interval)
    return None


# ================================================================ 状态缓存

def load_state():
    data = read_json(STATE_PATH, {})
    return data if isinstance(data, dict) else {}


def update_state(uid, patch):
    with _STATE_LOCK:
        data = load_state()
        rec = data.get(uid)
        if not isinstance(rec, dict):
            rec = {}
        rec.update(patch)
        data[uid] = rec
        write_json(STATE_PATH, data)


# ================================================================ 列表

def scan_accounts():
    """返回界面所需的一行行账号数据；不联网，只做本地读取。"""
    cfg = ensure_settings()
    exe = Path(cfg["workbuddy"]["exePath"]) if cfg["workbuddy"].get("exePath") else None
    if exe and not exe.is_file():
        exe = None
    try:
        ensure_default_account()
    except Exception:
        pass

    reg = load_registry()
    procs = workbuddy_processes(exe) if exe else {}
    state = load_state()
    rows = []
    seen = {}

    for a in reg["accounts"]:
        uid = str(a.get("accountUid", "")).lower()
        config_dir = Path(str(a.get("configDir", "")))
        is_default = bool(a.get("isDefaultProfile"))
        pid = live_pid(config_dir, procs) if procs else None
        age = heartbeat_age(config_dir) if pid else None
        meta = None
        try:
            meta = auth_meta(config_dir, is_default, uid)
        except Exception:
            meta = None
        quota = (state.get(uid) or {}).get("quota") or {}
        expiry, days_left = _expiry_info(meta.get("expiresAt") if meta else None)
        rows.append({
            "product": "workbuddy",
            "key": uid,
            "uid": uid,
            "label": a.get("nickname") or a.get("label") or (uid[:8] if uid else "未知"),
            "nickname": a.get("nickname") or "",
            "configDir": str(config_dir),
            "isDefault": is_default,
            "running": bool(pid),
            "pid": pid,
            "heartbeatSec": int(age) if age is not None else None,
            "expiry": expiry,
            "daysLeft": days_left,
            "authUpdatedAt": meta.get("updatedAt") if meta else "",
            "quota": quota,
            "active": reg.get("activeUid") == a.get("accountUid"),
            "lastLoginAt": a.get("lastLoginAt") or "",
            "note": "",
            "canSwitch": True,
            "canStop": True,
        })
        seen[uid] = {
            "running": bool(pid),
            "pid": pid,
            "checkedAt": now_iso(),
        }

    # 轻量状态缓存：只记运行状态与最近一次观察时间
    if seen:
        with _STATE_LOCK:
            data = load_state()
            for uid, rec in seen.items():
                prev = data.get(uid) if isinstance(data.get(uid), dict) else {}
                prev.update(rec)
                data[uid] = prev
            write_json(STATE_PATH, data)

    rows.sort(key=lambda r: (0 if r["isDefault"] else 1, r["label"]))
    return rows


# ================================================================ 新建 / 切换 / 停止

def prepare_new_account(label=None):
    """建目录、生成启动器、拉起实例，立即返回；登录在 wait_new_login 里等。"""
    cfg = ensure_settings()
    value = cfg["workbuddy"].get("exePath")
    if not value or not Path(value).is_file():
        raise RuntimeError("未找到 WorkBuddy 可执行文件")
    exe = Path(value)
    root = Path(cfg["workbuddy"]["accountsRoot"]).expanduser()
    root.mkdir(parents=True, exist_ok=True)

    try:
        ensure_default_account()
    except Exception:
        pass

    config_dir = next_account_dir(root)
    config_dir.mkdir(parents=True, exist_ok=False)
    started = time.time()
    independent, pid = launch_workbuddy(exe, config_dir)
    return {
        "configDir": config_dir,
        "pid": pid,
        "startedAt": started,
        "label": label or "",
        "independent": independent,
        "launcher": config_dir / LAUNCHER_NAME,
        "exe": exe,
    }


def wait_new_login(info, timeout=None, stop_event=None):
    """等待新实例完成登录并登记。返回 {ok, message, record}。"""
    cfg = ensure_settings()
    timeout = timeout or int(cfg["launch"].get("loginTimeoutSec") or 1200)
    interval = int(cfg["launch"].get("pollIntervalSec") or 2)
    deadline = time.time() + timeout
    config_dir = info["configDir"]

    ident = None
    while time.time() < deadline:
        if stop_event is not None and stop_event.is_set():
            return {"ok": False, "message": "已取消", "record": None}
        ident = identity_of(config_dir, False)
        if ident and ident["uid"] not in known_uids():
            _, newest = auth_file_uids(config_dir, False)
            if ident["confirmedByAuth"] and newest >= info["startedAt"] - 5:
                break
            ident = None
        else:
            ident = None
        time.sleep(interval)

    if not ident:
        return {
            "ok": False,
            "message": "超时未识别到登录完成。目录已保留：%s" % config_dir,
            "record": None,
        }

    label = info.get("label") or ident["nickname"] or ("账号-%s" % config_dir.name[-2:])
    record = register_account(
        ident["uid"], config_dir, label, info["exe"],
        nickname=ident["nickname"], is_default=False,
    )

    shortcut = None
    warning = ""
    try:
        shortcut = create_desktop_shortcut(
            ident["uid"], label, info["launcher"], info["exe"], config_dir
        )
        message = "登录完成，账号已登记。\n桌面入口：%s" % shortcut
    except Exception as exc:
        warning = str(exc)
        message = "登录完成，账号已登记。\n桌面入口创建失败：%s" % exc
    return {
        "ok": True,
        "message": message,
        "record": record,
        "shortcut": shortcut,
        "warning": warning,
    }


def switch_account(uid, timeout=None):
    """已在运行则置前；未运行则拉起并等待就绪。返回结果字典。"""
    cfg = ensure_settings()
    value = cfg["workbuddy"].get("exePath")
    if not value or not Path(value).is_file():
        return {"ok": False, "message": "未找到 WorkBuddy 可执行文件"}
    exe = Path(value)
    try:
        ensure_default_account()
    except Exception:
        pass

    target = find_account(uid)
    if not target:
        return {"ok": False, "message": "账号未登记，请先刷新列表"}
    config_dir = Path(str(target["configDir"]))
    if not config_dir.is_dir():
        return {"ok": False, "message": "账号目录不存在：%s" % config_dir}
    is_default = bool(target.get("isDefaultProfile"))
    name = target.get("nickname") or target.get("label") or uid

    procs = workbuddy_processes(exe)
    pid = live_pid(config_dir, procs)
    if pid:
        hwnd = find_window(pid)
        raised = bring_to_front(hwnd) if hwnd else False
        set_active(uid)
        return {
            "ok": True,
            "alreadyRunning": True,
            "message": "%s 已在运行（pid=%s）%s" % (
                name, pid, "，窗口已置前" if raised else "，请在任务栏点选窗口"
            ),
        }

    independent, launched_pid = launch_workbuddy(exe, config_dir)
    timeout = timeout or int(cfg["launch"].get("switchTimeoutSec") or 180)
    deadline = time.time() + timeout
    new_pid = launched_pid
    ready = False
    ident = None
    while time.time() < deadline:
        procs_now = workbuddy_processes(exe)
        new_pid = live_pid(config_dir, procs_now) or new_pid
        if new_pid and pid_alive(new_pid):
            ident = identity_of(config_dir, is_default)
            if ident and ident["uid"] == uid and find_window(new_pid):
                ready = True
                break
        time.sleep(2)

    if not ready:
        if ident and ident["uid"] != uid:
            reason = "目标目录当前登录的是 %s，与登记账号不一致" % (
                ident["nickname"] or ident["uid"]
            )
        elif new_pid and pid_alive(new_pid):
            reason = "实例已启动，但窗口/身份未在超时内就绪"
        else:
            reason = "实例未成功启动"
        if new_pid and pid_alive(new_pid):
            close_instance(new_pid)
        return {"ok": False, "message": "切换失败：%s" % reason}

    hwnd = find_window(new_pid)
    raised = bring_to_front(hwnd)
    set_active(uid)
    reg = load_registry()
    for a in reg["accounts"]:
        if a.get("accountUid") == uid:
            a["lastLoginAt"] = now_iso()
    save_registry(reg)
    return {
        "ok": True,
        "alreadyRunning": False,
        "message": "%s 已启动并置前%s" % (
            name, "" if raised else "（系统未允许自动置前，请在任务栏点选）"
        ),
        "pid": new_pid,
        "independent": independent,
    }


def stop_account(uid, allow_default=False):
    cfg = ensure_settings()
    value = cfg["workbuddy"].get("exePath")
    if not value or not Path(value).is_file():
        return {"ok": False, "message": "未找到 WorkBuddy 可执行文件"}
    exe = Path(value)
    try:
        ensure_default_account()
    except Exception:
        pass

    target = find_account(uid)
    if not target:
        return {"ok": False, "message": "账号未登记，请先刷新列表"}
    is_default = bool(target.get("isDefaultProfile"))
    if is_default and not allow_default:
        return {"ok": False, "message": "这是默认 profile 主实例，需要确认后才能关闭"}
    config_dir = Path(str(target["configDir"]))
    name = target.get("nickname") or target.get("label") or uid

    procs = workbuddy_processes(exe)
    pid = live_pid(config_dir, procs)
    if not pid:
        return {"ok": True, "message": "%s 当前未运行" % name}

    mode = close_instance(pid)
    if pid_alive(pid):
        return {"ok": False, "message": "关闭失败：pid=%s 仍在运行" % pid}

    if load_registry().get("activeUid") == uid:
        set_active(None)
    return {
        "ok": True,
        "message": "已关闭 %s（%s）" % (name, "正常退出" if mode == "graceful" else "强制结束"),
    }


# ================================================================ 额度（按需）

def _manual(reason):
    return {
        "mode": "manual",
        "text": "需手动查看",
        "detail": reason,
        "checkedAt": now_iso(),
    }


def _query_quota(meta, uid):
    """单次、只读查询；任何异常都降级为「需手动查看」，不重试。"""
    if urlrequest is None:
        return _manual("当前环境不支持网络查询")
    url = "https://%s/v2/billing/meter/checkin-activity-status" % (meta["domain"] or "www.workbuddy.cn")
    body = b"{}"
    req = urlrequest.Request(url, data=body, method="POST")
    req.add_header("Authorization", "Bearer %s" % meta["token"])
    req.add_header("X-User-Id", uid)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    try:
        with urlrequest.urlopen(req, timeout=8) as resp:
            raw = resp.read()
    except Exception as exc:
        code = getattr(exc, "code", None)
        if code:
            return _manual("查询失败：HTTP %s（未重试）" % code)
        return _manual("查询失败：%s" % type(exc).__name__)

    try:
        data = json.loads(raw.decode("utf-8", errors="ignore"))
    except Exception:
        return _manual("返回内容无法解析")
    if not isinstance(data, dict) or data.get("code") not in (0, None):
        return _manual("接口返回 code=%s" % (data.get("code") if isinstance(data, dict) else "?"))

    payload = data.get("data") or {}
    checked = bool(payload.get("today_checked_in"))
    daily = payload.get("daily_credit")
    today = payload.get("today_credit")
    streak = payload.get("streak_days")
    parts = ["今日已领" if checked else "今日未领"]
    if daily not in (None, ""):
        parts.append("每日 %s" % daily)
    if today not in (None, ""):
        parts.append("今日 %s" % today)
    if streak not in (None, ""):
        parts.append("连续 %s 天" % streak)
    return {
        "mode": "api",
        "text": " · ".join(str(p) for p in parts),
        "detail": "来源：/v2/billing/meter/checkin-activity-status",
        "checkedAt": now_iso(),
    }


def refresh_quota(uid):
    """按需查询一次额度；不绕过、不伪造。"""
    target = find_account(uid)
    if not target:
        result = _manual("账号未登记")
    else:
        config_dir = Path(str(target["configDir"]))
        meta = None
        try:
            meta = auth_meta(config_dir, bool(target.get("isDefaultProfile")), uid)
        except Exception:
            meta = None
        if not meta:
            result = _manual("未找到登录态文件，请在客户端内查看")
        elif meta.get("encrypted"):
            result = _manual(
                "登录令牌在客户端内加密保存，本工具不读取、不绕过；"
                "请在客户端内查看额度"
            )
        elif not meta.get("token"):
            result = _manual("未找到可用的登录令牌")
        else:
            result = _query_quota(meta, uid)
    update_state(uid, {"quota": result})
    return result


# ================================================================ Trae / Qoder
#
# 只读：解密客户端自己的登录凭证用于查询额度/签到状态，不写回客户端文件，
# 不发送签到/领取请求，不启动调试端口。

TRAE_STATUS_URL = "https://api.trae.cn/trae/api/v2/ug/checkin_credits/status"
QODER_PLAN_URL = "https://openapi.qoder.sh/api/v2/user/plan"

TRAE_URE = bytes([
    82, 9, 106, 213, 48, 54, 165, 56, 191, 64, 163, 158, 129, 243, 215, 251,
    124, 227, 57, 130, 155, 47, 255, 135, 52, 142, 67, 68, 196, 222, 233, 203,
    84, 123, 148, 50, 166, 194, 35, 61, 238, 76, 149, 11, 66, 250, 195, 78,
    8, 46, 161, 102, 40, 217, 36, 178, 118, 91, 162, 73, 109, 139, 209, 37,
])
TRAE_DRE = bytes([
    31, 221, 168, 51, 136, 7, 199, 49, 177, 18, 16, 89, 39, 128, 236, 95,
    96, 81, 127, 169, 25, 181, 74, 13, 45, 229, 122, 159, 147, 201, 156, 239,
    160, 224, 59, 77, 174, 42, 245, 176, 200, 235, 187, 60, 131, 83, 153, 97,
    23, 43, 4, 126, 186, 119, 214, 38, 225, 105, 20, 99, 85, 33, 12, 125,
])

QODER_USER_TYPES = {
    "personal_standard": "个人标准版",
    "personal_pro": "个人专业版",
    "personal": "个人版",
    "enterprise": "企业版",
    "team": "团队版",
}


def _expiry_info(ts):
    """时间戳（秒/毫秒）或 ISO 字符串 → (显示日期, 剩余天数)。"""
    if not ts:
        return "未知", None
    if isinstance(ts, str):
        parsed = _iso_to_ts(ts)
        if not parsed:
            return (ts.strip()[:10] or "未知"), None
        ts = parsed
    try:
        value = float(ts)
    except Exception:
        return str(ts)[:10], None
    if value > 1e11:
        value /= 1000.0
    try:
        dt = datetime.fromtimestamp(value)
    except Exception:
        return "未知", None
    return dt.strftime("%Y-%m-%d"), (dt.date() - datetime.now().date()).days


def _aes_cbc(key, iv, data):
    return _aes.cbc_decrypt(key, iv, data)


def _aes_gcm(key, nonce, data):
    return _aes.gcm_decrypt(key, nonce, data)


if os.name == "nt":
    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    def _dpapi_unprotect(blob):
        buf = ctypes.create_string_buffer(blob)
        data_in = _DATA_BLOB(
            len(blob), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))
        )
        data_out = _DATA_BLOB()
        ok = _crypt32.CryptUnprotectData(
            ctypes.byref(data_in), None, None, None, None, 0,
            ctypes.byref(data_out),
        )
        if not ok:
            raise OSError(ctypes.get_last_error(), "CryptUnprotectData failed")
        try:
            return ctypes.string_at(data_out.pbData, data_out.cbData)
        finally:
            _k32.LocalFree(data_out.pbData)
else:
    def _dpapi_unprotect(_blob):
        raise RuntimeError("仅支持 Windows")


def trae_default_dir():
    appdata = os.environ.get("APPDATA")
    return Path(appdata) / "Trae CN" if appdata else None


def trae_instance_roots():
    """Trae 多开实例的根目录列表：配置 > 环境变量 > 程序同级 instances。"""
    roots = []
    cfg = ensure_settings()
    for item in (cfg.get("trae") or {}).get("instanceRoots") or []:
        text = str(item or "").strip()
        if text:
            roots.append(Path(text).expanduser())
    env = os.environ.get("WB_CB_TRAE_ROOTS")
    if env:
        for part in env.split(os.pathsep):
            part = part.strip()
            if part:
                roots.append(Path(part).expanduser())
    roots.append(app_base_dir() / "instances")
    return roots


def trae_instance_dirs():
    """默认实例 + 多开目录；只返回确实有 storage.json 的目录。"""
    candidates = []
    default = trae_default_dir()
    if default:
        candidates.append(default)
    env = os.environ.get("WB_CB_TRAE_INSTANCES")
    if env:
        for part in env.split(os.pathsep):
            part = part.strip()
            if part:
                candidates.append(Path(part))
    for base in trae_instance_roots():
        try:
            if base.is_dir():
                for child in sorted(base.iterdir()):
                    candidates.append(child / "appdata")
        except OSError:
            continue

    out, seen = [], set()
    for path in candidates:
        try:
            resolved = path.resolve()
        except Exception:
            resolved = path
        key = str(resolved).lower()
        if key in seen:
            continue
        if (resolved / "User" / "globalStorage" / "storage.json").is_file():
            seen.add(key)
            out.append(path)
    return out


def read_trae_auth(config_dir):
    """只读解密 Trae 的 iCubeAuthInfo，返回内存对象；失败返回 (None, 原因)。"""
    storage = read_json(
        Path(config_dir) / "User" / "globalStorage" / "storage.json", {}
    )
    if not isinstance(storage, dict):
        return None, "未找到 storage.json"
    blob = storage.get("iCubeAuthInfo://icube.cloudide")
    if not isinstance(blob, str) or not blob:
        return None, "未找到登录凭证"
    try:
        raw = base64.b64decode(blob)
        keymat = raw[6:38]
        digest = hashlib.sha512(
            hashlib.sha512(keymat).digest()
            + bytes(a ^ b for a, b in zip(TRAE_URE, TRAE_DRE))
        ).digest()
        plain = _aes_cbc(digest[:16], digest[16:32], raw[38:])
        auth = json.JSONDecoder().raw_decode(
            plain[64:].decode("utf-8", "ignore")
        )[0]
    except Exception as exc:
        return None, "凭证解密失败：%s" % exc
    if not isinstance(auth, dict):
        return None, "凭证内容无法解析"

    account = auth.get("account") if isinstance(auth.get("account"), dict) else {}
    region = auth.get("userRegion") if isinstance(auth.get("userRegion"), dict) else {}
    device = ""
    for key in storage:
        text = str(key)
        if text.startswith("iCubeAuthInfo://icube-dc:"):
            device = text.rsplit(":", 1)[-1]
            break
    return {
        "token": str(auth.get("token") or ""),
        "refreshToken": str(auth.get("refreshToken") or ""),
        "expiredAt": auth.get("expiredAt") or auth.get("expiresAt") or 0,
        "userId": str(auth.get("userId") or ""),
        "nickname": str(account.get("username") or ""),
        "email": str(account.get("email") or ""),
        "region": str(region.get("region") or "CN"),
        "deviceId": device,
        "host": str(auth.get("host") or "https://api.trae.cn"),
    }, ""


def _is_electron_main(cmd):
    """主进程命令行只有启动参数，没有脚本等位置参数。"""
    import shlex

    text = (cmd or "").strip()
    if text.startswith('"'):
        end = text.find('"', 1)
        rest = text[end + 1:] if end > 0 else ""
    else:
        parts = text.split(None, 1)
        rest = parts[1] if len(parts) > 1 else ""
    try:
        args = shlex.split(rest, posix=False)
    except ValueError:
        args = rest.split()
    if any(a.startswith("--type=") for a in args):
        return False
    return not any(not a.startswith("-") for a in args)


def _electron_mains(names):
    """返回指定进程名的主进程（排除 --type= 与带脚本参数的子进程）。"""
    if os.name != "nt" or not names:
        return []
    filter_expr = " or ".join("Name='%s'" % n for n in names)
    ps = (
        "Get-CimInstance Win32_Process -Filter \"%s\" | "
        "ForEach-Object { \"$($_.ProcessId)|$($_.Name)|$($_.CommandLine)\" }"
        % filter_expr
    )
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            text=True, timeout=30, stderr=subprocess.DEVNULL,
            creationflags=0x08000000,
        )
    except Exception:
        return []

    result = []
    for line in out.splitlines():
        parts = line.split("|", 2)
        if len(parts) < 3:
            continue
        pid_text, name, cmd = parts[0].strip(), parts[1].strip(), parts[2]
        if not pid_text.isdigit() or not _is_electron_main(cmd):
            continue
        match = re.search(r'--user-data-dir=(?:"([^"]+)"|(\S+))', cmd, re.I)
        result.append({
            "pid": int(pid_text),
            "name": name,
            "dataDir": Path(match.group(1) or match.group(2)) if match else None,
        })
    return result


def _same_dir(left, right):
    try:
        return str(Path(left).resolve()).lower() == str(Path(right).resolve()).lower()
    except Exception:
        return str(left).lower() == str(right).lower()


def scan_trae(mains=None):
    """Trae 实例列表；只读本地文件与进程信息，不联网。"""
    mains = mains if mains is not None else _electron_mains(["Trae CN.exe"])
    trae_mains = [p for p in mains if p["name"].lower() == "trae cn.exe"]
    default = trae_default_dir()
    rows = []
    for config_dir in trae_instance_dirs():
        is_default = bool(default) and _same_dir(config_dir, default)
        auth, reason = read_trae_auth(config_dir)
        if not auth and not is_default:
            continue
        pid = None
        for proc in trae_mains:
            if is_default:
                if proc["dataDir"] is None:
                    pid = proc["pid"]
                    break
            elif proc["dataDir"] is not None and _same_dir(proc["dataDir"], config_dir):
                pid = proc["pid"]
                break
        uid = (auth or {}).get("userId") or config_dir.parent.name
        key = "trae:%s" % uid
        expiry, days_left = _expiry_info((auth or {}).get("expiredAt"))
        quota = (load_state().get(key) or {}).get("quota") or {}
        if not auth:
            label = "Trae（未登录或无法读取）"
            note = reason
        else:
            label = auth["nickname"] or ("Trae %s" % uid[-4:])
            note = ""
            if not is_default:
                note = "第二实例：状态可查，领取受设备维度限制，以客户端为准"
        rows.append({
            "product": "trae",
            "key": key,
            "uid": str(uid),
            "label": label,
            "nickname": (auth or {}).get("nickname") or "",
            "configDir": str(config_dir),
            "isDefault": is_default,
            "running": bool(pid),
            "pid": pid,
            "heartbeatSec": None,
            "expiry": expiry,
            "daysLeft": days_left,
            "authUpdatedAt": "",
            "quota": quota,
            "active": False,
            "lastLoginAt": "",
            "note": note,
            "deviceId": (auth or {}).get("deviceId") or "",
            "canSwitch": False,
            "canStop": False,
            "readable": bool(auth),
        })
    rows.sort(key=lambda r: (0 if r["isDefault"] else 1, r["label"]))
    return rows


def qoder_dir():
    appdata = os.environ.get("APPDATA")
    return Path(appdata) / "com.qoder.app.stable" if appdata else None


def read_qoder_auth():
    root = qoder_dir()
    if not root:
        return None, "未找到 Qoder 数据目录"
    local_state = read_json(root / "Local State", {})
    if not isinstance(local_state, dict):
        return None, "未找到 Local State"
    encrypted_key = (local_state.get("os_crypt") or {}).get("encrypted_key")
    if not isinstance(encrypted_key, str) or not encrypted_key:
        return None, "未找到本机加密密钥"
    auth_file = root / "auth.v1.dat"
    if not auth_file.is_file():
        return None, "未找到 auth.v1.dat"
    try:
        key = _dpapi_unprotect(base64.b64decode(encrypted_key)[5:])
        raw = auth_file.read_bytes()
        if raw[:3] != b"v10":
            return None, "凭据格式不是 Chromium v10"
        plain = _aes_gcm(key, raw[3:15], raw[15:])
        auth = json.loads(plain.decode("utf-8", "ignore"))
    except Exception as exc:
        return None, "凭证解密失败：%s" % exc
    if not isinstance(auth, dict):
        return None, "凭证内容无法解析"
    user = auth.get("user") if isinstance(auth.get("user"), dict) else {}
    return {
        "token": str(auth.get("token") or ""),
        "refreshToken": str(auth.get("refreshToken") or ""),
        "expiresAt": auth.get("expiresAt") or "",
        "userId": str(user.get("id") or ""),
        "nickname": str(user.get("name") or ""),
        "email": str(user.get("email") or ""),
    }, ""


def scan_qoder(mains=None):
    """Qoder 单实例；只读本地文件与进程信息，不联网。"""
    root = qoder_dir()
    if not root or not (root / "auth.v1.dat").is_file():
        return []
    mains = mains if mains is not None else _electron_mains(["Qoder.exe"])
    qoder_mains = [p for p in mains if p["name"].lower() == "qoder.exe"]
    auth, reason = read_qoder_auth()
    pid = None
    for proc in qoder_mains:
        if proc["dataDir"] is None:
            pid = proc["pid"]
            break
    uid = (auth or {}).get("userId") or "unknown"
    key = "qoder:%s" % uid
    expiry, days_left = _expiry_info(_iso_to_ts((auth or {}).get("expiresAt")))
    quota = (load_state().get(key) or {}).get("quota") or {}
    if not auth:
        label = "Qoder（未登录或无法读取）"
        note = reason
    else:
        label = auth["nickname"] or "Qoder"
        note = auth.get("email") or ""
    return [{
        "product": "qoder",
        "key": key,
        "uid": uid,
        "label": label,
        "nickname": (auth or {}).get("nickname") or "",
        "configDir": str(root),
        "isDefault": True,
        "running": bool(pid),
        "pid": pid,
        "heartbeatSec": None,
        "expiry": expiry,
        "daysLeft": days_left,
        "authUpdatedAt": "",
        "quota": quota,
        "active": False,
        "lastLoginAt": "",
        "note": note,
        "canSwitch": False,
        "canStop": False,
        "readable": bool(auth),
    }]


def _iso_to_ts(value):
    if not value or not isinstance(value, str):
        return 0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0


def _post_json(url, headers, body=b"{}", timeout=8):
    req = urlrequest.Request(url, data=body, method="POST", headers=headers)
    with urlrequest.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _get_json(url, headers, timeout=8):
    req = urlrequest.Request(url, headers=headers, method="GET")
    with urlrequest.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _query_trae_quota(auth):
    headers = {
        "Authorization": "Cloud-IDE-JWT %s" % auth["token"],
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-User-Region": auth.get("region") or "CN",
    }
    try:
        raw = _post_json(TRAE_STATUS_URL, headers)
    except urlerror.HTTPError as exc:
        return _manual("查询失败：HTTP %s（未重试）" % exc.code)
    except Exception as exc:
        return _manual("查询失败：%s" % type(exc).__name__)
    try:
        data = json.loads(raw.decode("utf-8", "ignore"))
    except Exception:
        return _manual("返回内容无法解析")
    if not isinstance(data, dict):
        return _manual("返回内容无法解析")
    if data.get("code") not in (0, None):
        return _manual("接口返回 code=%s" % data.get("code"))

    parts = ["今日已领取" if data.get("checked_in") else "今日未领取"]
    credits = data.get("credits")
    extra = data.get("extra_credits")
    if credits not in (None, ""):
        parts.append("额度 %s" % credits)
    if extra not in (None, "", 0):
        parts.append("额外 %s" % extra)
    return {
        "mode": "api",
        "text": " · ".join(str(p) for p in parts),
        "detail": "来源：api.trae.cn checkin_credits/status",
        "checkedAt": now_iso(),
    }


def refresh_trae_quota(key):
    """按需查询一次 Trae 状态；不发送领取请求。"""
    row = next((r for r in scan_trae() if r["key"] == key), None)
    if not row:
        result = _manual("实例未找到")
    else:
        auth, reason = read_trae_auth(Path(row["configDir"]))
        if not auth:
            result = _manual(reason or "登录凭证不可用")
        elif not auth.get("token"):
            result = _manual("未找到可用的登录令牌")
        else:
            result = _query_trae_quota(auth)
    update_state(key, {"quota": result})
    return result


def _query_qoder_plan(auth):
    headers = {
        "Authorization": "Bearer %s" % auth["token"],
        "Accept": "application/json",
    }
    try:
        raw = _get_json(QODER_PLAN_URL, headers)
    except urlerror.HTTPError as exc:
        return _manual("查询失败：HTTP %s（未重试）" % exc.code)
    except Exception as exc:
        return _manual("查询失败：%s" % type(exc).__name__)
    try:
        data = json.loads(raw.decode("utf-8", "ignore"))
    except Exception:
        return _manual("返回内容无法解析")
    if not isinstance(data, dict):
        return _manual("返回内容无法解析")

    tier = data.get("plan_tier_name") or data.get("plan_name") or "未知套餐"
    paid = data.get("is_paid_plan")
    parts = [str(tier)]
    if paid is not None:
        parts.append("付费" if paid else "免费")
    user_type = data.get("user_type")
    if user_type:
        parts.append(QODER_USER_TYPES.get(str(user_type), str(user_type)))
    end_ts = data.get("end_date")
    if end_ts:
        end_text, _ = _expiry_info(end_ts)
        if end_text != "未知":
            parts.append("到期 %s" % end_text)
    return {
        "mode": "api",
        "text": " · ".join(parts),
        "detail": "来源：openapi.qoder.sh /api/v2/user/plan",
        "checkedAt": now_iso(),
    }


def refresh_qoder_quota():
    """按需查询一次 Qoder 套餐信息。"""
    auth, reason = read_qoder_auth()
    if not auth:
        result = _manual(reason or "登录凭证不可用")
    elif not auth.get("token"):
        result = _manual("未找到可用的登录令牌")
    else:
        result = _query_qoder_plan(auth)
    update_state("qoder:%s" % (auth.get("userId") if auth else "unknown"), {"quota": result})
    return result


def refresh_quota_any(product, key):
    if product == "workbuddy":
        return refresh_quota(key)
    if product == "trae":
        return refresh_trae_quota(key)
    if product == "qoder":
        return refresh_qoder_quota()
    return _manual("未知产品")


def scan_all_products():
    """界面用：三端分组，本地只读，不联网。"""
    mains = _electron_mains(["Trae CN.exe", "Qoder.exe"])
    return [
        {
            "product": "workbuddy",
            "title": "WorkBuddy",
            "hint": "多实例：切换、停止、桌面入口",
            "accounts": scan_accounts(),
        },
        {
            "product": "trae",
            "title": "Trae",
            "hint": "只读展示登录态与签到状态，不代为领取",
            "accounts": scan_trae(mains),
        },
        {
            "product": "qoder",
            "title": "Qoder",
            "hint": "只读展示登录态与套餐信息",
            "accounts": scan_qoder(mains),
        },
    ]
