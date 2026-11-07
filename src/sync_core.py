# -*- coding: utf-8 -*-
"""
workbuddy-codebuddy-chat-sync 核心同步逻辑

与界面完全解耦，既可以被托盘程序 import，也可以直接用命令行运行。

设计原则：
1. 不硬编码任何路径 —— 全部基于 %USERPROFILE% / %APPDATA% / %LOCALAPPDATA% 推导
2. 不硬编码任何账号 ID —— 每次运行从数据库里现场扫描
3. 所有写操作前先做 WAL 安全检查，失败即放弃，绝不写坏原库
"""
import datetime
import json
import os
import re
import shutil
import sqlite3
import sys

# ---------------------------------------------------------------- 路径探测

HOME = os.path.expanduser("~")
APPDATA = os.environ.get("APPDATA") or os.path.join(HOME, "AppData", "Roaming")
LOCALAPPDATA = os.environ.get("LOCALAPPDATA") or os.path.join(HOME, "AppData", "Local")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 运行时数据（配置、日志、锁）故意放在用户目录而不是项目文件夹里，
# 这样整个项目目录永远是干净的，可以放心打包/上传/发给别人。
DATA_DIR = os.environ.get(
    "WB_CB_SYNC_DATA", os.path.join(APPDATA, "workbuddy-codebuddy-chat-sync")
)
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
LOG_PATH = os.path.join(DATA_DIR, "sync.log")

# 两个软件的数据位置。允许用环境变量覆盖，方便别人排查或装到非标准目录。
WB_DB = os.environ.get(
    "WB_CB_SYNC_WB_DB", os.path.join(HOME, ".workbuddy", "workbuddy.db")
)
CB_DB = os.environ.get(
    "WB_CB_SYNC_CB_DB",
    os.path.join(APPDATA, "CodeBuddy CN", "codebuddy-sessions.vscdb"),
)
CB_DATA = os.environ.get(
    "WB_CB_SYNC_CB_DATA", os.path.join(LOCALAPPDATA, "CodeBuddyExtension", "Data")
)

LOG_MAX = 200 * 1024
LOG_KEEP_LINES = 500


# ---------------------------------------------------------------- 基础设施

def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)


def log(msg):
    ensure_dirs()
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX:
            with open(LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
                tail = f.readlines()[-LOG_KEEP_LINES:]
            with open(LOG_PATH, "w", encoding="utf-8") as f:
                f.writelines(tail)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (ts, msg))
    except Exception:
        pass


def load_config():
    """读取 config.json，文件不存在或损坏时返回默认值"""
    default = {
        "accounts": {},
        "auto_sync": True,
        "interval_seconds": 120,
        "merge_history_files": True,
    }
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        for k, v in default.items():
            cfg.setdefault(k, v)
        return cfg
    except Exception:
        return default


# ---------------------------------------------------------------- 账号昵称

NAME_CACHE = os.path.join(DATA_DIR, "account-names.json")

# CodeBuddy 日志里 getUserInfo 的返回长这样：
# [{"uid":"9fc5...","nickname":"某某","uin":"...","lastLogin":true,...}]
_USERINFO_RE = re.compile(
    r'\{"uid"\s*:\s*"([0-9a-fA-F-]{36})"[^{}]{0,200}?"nickname"\s*:\s*"([^"]{0,60})"'
)

_names_scanned = False


def _load_name_cache():
    try:
        with open(NAME_CACHE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_name_cache(d):
    try:
        ensure_dirs()
        with open(NAME_CACHE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _scan_logs_for_names(max_sessions=0):
    """
    从 CodeBuddy 的日志里翻账号昵称。

    登录时扩展会调用 getUserInfo，返回的 JSON 里同时带 uid 和 nickname，
    这是本地唯一能拿到的明文昵称来源。

    max_sessions=0 表示扫全部历史会话（第一次跑会慢几秒），
    之后结果落盘缓存，不会重复扫。
    """
    found = {}
    logs_root = os.path.join(APPDATA, "CodeBuddy CN", "logs")
    if not os.path.isdir(logs_root):
        return found

    try:
        sessions = [
            os.path.join(logs_root, d)
            for d in os.listdir(logs_root)
            if os.path.isdir(os.path.join(logs_root, d))
        ]
        if max_sessions:
            sessions = sorted(sessions, key=os.path.getmtime, reverse=True)[:max_sessions]
    except Exception:
        return found

    for sess in sessions:
        for dirpath, _dirnames, filenames in os.walk(sess):
            for fn in filenames:
                if not fn.endswith(".log"):
                    continue
                full = os.path.join(dirpath, fn).lower()
                # 只看扩展自己的日志，普通窗口日志里没有 getUserInfo
                if "coding-copilot" not in full and "tencent-cloud" not in full:
                    continue
                try:
                    with open(
                        os.path.join(dirpath, fn), "r", encoding="utf-8", errors="ignore"
                    ) as f:
                        for line in f:
                            if "nickname" not in line:
                                continue
                            m = _USERINFO_RE.search(line)
                            if m:
                                found[m.group(1).lower()] = m.group(2)
                except Exception:
                    continue
    return found


def detected_names():
    """返回 {账号ID: 昵称}，首次调用才去扫日志，结果落盘复用"""
    global _names_scanned
    cache = _load_name_cache()
    if _names_scanned:
        return cache
    _names_scanned = True
    try:
        fresh = _scan_logs_for_names()
        if fresh:
            cache.update({k.lower(): v for k, v in fresh.items()})
            _save_name_cache(cache)
    except Exception:
        pass
    return cache


def account_label(uid, cfg=None):
    """
    账号显示名，优先级：
      1. config.json 里手填的名字（用户可以覆盖）
      2. 从日志里自动读到的昵称
      3. ID 前 8 位兜底
    """
    cfg = cfg or load_config()
    names = cfg.get("accounts") or {}
    if uid in names:
        return names[uid]
    auto = detected_names()
    if uid and uid.lower() in auto:
        return auto[uid.lower()]
    return uid[:8] if uid else "未知"


def _ro(path):
    """以只读方式打开 sqlite，避免在软件运行时抢占写锁"""
    return sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)


# ---------------------------------------------------------------- WorkBuddy

def wb_available():
    return os.path.isfile(WB_DB)


def wb_counts():
    """{user_id: 会话数}"""
    if not wb_available():
        return {}
    try:
        con = _ro(WB_DB)
        try:
            return dict(
                con.execute(
                    "SELECT user_id, COUNT(*) FROM sessions "
                    "WHERE deleted_at IS NULL GROUP BY user_id"
                )
            )
        finally:
            con.close()
    except Exception:
        return {}


def wb_current():
    """当前登录账号 = 最近有活动的那条会话所属账号"""
    if not wb_available():
        return None
    try:
        con = _ro(WB_DB)
        try:
            row = con.execute(
                "SELECT user_id FROM sessions "
                "WHERE deleted_at IS NULL ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
            return row[0] if row else None
        finally:
            con.close()
    except Exception:
        return None


def wb_merge(target):
    """把所有会话归到 target 名下，返回改动行数"""
    if not wb_available():
        return 0
    con = sqlite3.connect(WB_DB, timeout=8)
    try:
        n = con.execute(
            "UPDATE sessions SET user_id=? WHERE user_id<>?", (target, target)
        ).rowcount
        con.commit()
        return n
    finally:
        con.close()


# ---------------------------------------------------------------- CodeBuddy（索引层）

def cb_available():
    return os.path.isfile(CB_DB)


def _cb_rows(con):
    """读出 ItemTable 里所有会话，返回 [(key, raw_text, dict, was_bytes)]"""
    out = []
    for k, v in con.execute("SELECT key, value FROM ItemTable"):
        is_bytes = isinstance(v, (bytes, bytearray))
        raw = v.decode("utf-8") if is_bytes else v
        try:
            d = json.loads(raw)
        except Exception:
            continue
        if not isinstance(d, dict) or "userId" not in d:
            continue
        out.append((k, raw, d, is_bytes))
    return out


def cb_counts():
    if not cb_available():
        return {}
    try:
        con = _ro(CB_DB)
        try:
            cnt = {}
            for _, _, d, _ in _cb_rows(con):
                u = d.get("userId", "?")
                cnt[u] = cnt.get(u, 0) + 1
            return cnt
        finally:
            con.close()
    except Exception:
        return {}


def cb_current():
    if not cb_available():
        return None
    try:
        con = _ro(CB_DB)
        try:
            rows = _cb_rows(con)
            if not rows:
                return None
            latest = max(rows, key=lambda x: x[2].get("updatedAt") or 0)
            return latest[2].get("userId")
        finally:
            con.close()
    except Exception:
        return None


def cb_merge(target):
    """把索引层所有会话的 userId 改成 target，返回改动条数"""
    if not cb_available():
        return 0
    con = sqlite3.connect(CB_DB, timeout=8)
    try:
        n = 0
        for k, raw, d, is_bytes in _cb_rows(con):
            old = d.get("userId")
            if not old or old == target:
                continue
            new = raw.replace(old, target)
            con.execute(
                "UPDATE ItemTable SET value=? WHERE key=?",
                (new.encode("utf-8") if is_bytes else new, k),
            )
            n += 1
        con.commit()
        return n
    finally:
        con.close()


# ---------------------------------------------------------------- CodeBuddy（文件层）

def _history_root(uid):
    """<Data>/<uid>/CodeBuddyIDE/<uid>/history"""
    return os.path.join(CB_DATA, uid, "CodeBuddyIDE", uid, "history")


def _long(path):
    r"""
    Windows 上超过 260 字符的路径需要 \\?\ 前缀才能访问。
    CodeBuddy 会把工具调用的产物存到很深的目录里，路径轻松超过限制，
    不加这个前缀复制会直接报 WinError 3。
    """
    if os.name != "nt":
        return path
    p = os.path.abspath(path)
    if p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p


def _copy_tree_skip(src, dst):
    """
    递归复制目录，跳过目标已存在的文件，返回实际复制的文件数。
    自己写递归而不用 shutil.copytree，是为了全程走 _long()，
    否则深层路径在 copytree 内部拼接时照样会炸。
    """
    src = _long(src)
    dst = _long(dst)
    copied = 0
    for dirpath, _dirnames, filenames in os.walk(src):
        rel = os.path.relpath(dirpath, src)
        target_dir = dst if rel == "." else os.path.join(dst, rel)
        try:
            os.makedirs(target_dir, exist_ok=True)
        except OSError:
            pass
        for fn in filenames:
            s = os.path.join(dirpath, fn)
            d = os.path.join(target_dir, fn)
            try:
                if not os.path.exists(d):
                    shutil.copy2(s, d)
                    copied += 1
            except OSError:
                continue
    return copied


def _count_files(path):
    if not os.path.isdir(_long(path)):
        return 0
    n = 0
    for _, _, files in os.walk(_long(path)):
        n += len(files)
    return n


def _merge_json_index(dst_file, src_files):
    """合并多个工作区的 index.json：conversations 按 id 去重"""
    merged = {}
    order = []
    for f in [dst_file] + list(src_files):
        if not f or not os.path.isfile(f):
            continue
        try:
            with open(f, "r", encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception:
            continue
        for c in d.get("conversations") or []:
            cid = c.get("id")
            if not cid:
                continue
            if cid not in merged:
                order.append(cid)
                merged[cid] = c
            else:
                merged[cid] = c  # 后出现的覆盖前面的
    if not merged:
        return False
    out = {"conversations": [merged[i] for i in order]}
    try:
        with open(dst_file, "r", encoding="utf-8") as fh:
            cur = json.load(fh)
        if cur.get("current"):
            out["current"] = cur["current"]
    except Exception:
        pass
    with open(dst_file, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False)
    return True


def _merge_recent(dst_file, src_files):
    """合并 recent.json：{会话id: 时间戳}，取较新的那个"""
    merged = {}
    for f in [dst_file] + list(src_files):
        if not f or not os.path.isfile(f):
            continue
        try:
            with open(f, "r", encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception:
            continue
        for k, v in (d or {}).items():
            if k not in merged or str(v) > str(merged[k]):
                merged[k] = v
    if not merged:
        return False
    with open(dst_file, "w", encoding="utf-8") as fh:
        json.dump(merged, fh, ensure_ascii=False, indent=2)
    return True


def cb_file_merge(target, other_uids=None):
    """
    把其他账号的聊天正文目录合并到当前账号目录下。
    跳过已存在的文件，只补缺失的部分，可以反复执行。

    返回 (复制文件数, 处理的工作区数)
    """
    if not os.path.isdir(CB_DATA):
        return (0, 0)

    dst_root = _history_root(target)
    # 目标账号可能还没产生过任何会话目录（刚登录的新号），先建出来，
    # 否则第一次合并会直接放弃
    if not os.path.isdir(dst_root):
        try:
            os.makedirs(dst_root, exist_ok=True)
        except Exception as e:
            log("创建目标目录失败 %s: %s" % (dst_root, e))
            return (0, 0)

    if other_uids is None:
        other_uids = [
            d
            for d in os.listdir(CB_DATA)
            if d != target
            and not d.endswith(".bak")
            and d not in ("Public", "default")
            and os.path.isdir(_history_root(d))
        ]

    copied = 0
    workspaces = 0
    failed = 0

    for uid in other_uids:
        src_root = _history_root(uid)
        if not os.path.isdir(src_root) or os.path.abspath(src_root) == os.path.abspath(dst_root):
            continue
        for hash_name in os.listdir(src_root):
            src_ws = os.path.join(src_root, hash_name)
            if not os.path.isdir(src_ws):
                continue
            dst_ws = os.path.join(dst_root, hash_name)
            os.makedirs(dst_ws, exist_ok=True)
            workspaces += 1

            # 先合并索引文件，让列表能看到
            _merge_json_index(
                os.path.join(dst_ws, "index.json"),
                [os.path.join(src_ws, "index.json")],
            )
            _merge_recent(
                os.path.join(dst_ws, "recent.json"),
                [os.path.join(src_ws, "recent.json")],
            )

            # 再复制会话正文，跳过同名文件
            for conv in os.listdir(src_ws):
                src_conv = os.path.join(src_ws, conv)
                if not os.path.isdir(src_conv):
                    continue
                dst_conv = os.path.join(dst_ws, conv)
                try:
                    copied += _copy_tree_skip(src_conv, dst_conv)
                except Exception as e:
                    failed += 1
                    if failed <= 3:
                        log("复制会话失败 %s: %s" % (conv, e))

    if failed > 3:
        log("另有 %d 个会话复制失败（同类错误，已省略）" % (failed - 3))

    return (copied, workspaces)



# ---------------------------------------------------------------- 统一入口

def discover_accounts():
    """
    发现出现过的全部账号 ID。

    除了查数据库，还会扫描两个软件的用户数据目录（目录名就是账号 ID）。
    这一点很重要：记录被归并之后，数据库里往往只剩当前账号，
    但目录还在，靠目录才能记住"这台机器上曾经有哪几个号"。
    """
    import re

    uuid_re = re.compile(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
    )
    seen = []

    def add(uid):
        if uid and uuid_re.match(uid) and uid not in seen:
            seen.append(uid)

    for uid in list(wb_counts().keys()) + list(cb_counts().keys()):
        add(uid)

    for base in (os.path.join(HOME, ".workbuddy"), CB_DATA):
        try:
            for name in os.listdir(base):
                if os.path.isdir(os.path.join(base, name)):
                    add(name)
        except Exception:
            pass

    return seen


def stats():
    return {"wb": wb_counts(), "cb": cb_counts()}


def sync(product=None, target=None, quiet=False):
    """
    执行一次同步。

    product: "wb" / "cb" / None（None 表示两个都做，各自独立判断）
    target : 指定归到哪个账号。None 表示按"最近活动"自动判断。

    返回可读的结果字符串。
    """
    cfg = load_config()
    parts = []
    changed = False

    jobs = []
    if product in (None, "wb"):
        jobs.append(("WorkBuddy", "wb"))
    if product in (None, "cb"):
        jobs.append(("CodeBuddy", "cb"))

    for name, key in jobs:
        try:
            if key == "wb":
                dst = target or wb_current()
                if not dst:
                    parts.append("%s: 未发现会话，跳过" % name)
                    continue
                n = wb_merge(dst)
                parts.append(
                    "%s: 当前 %s，%s"
                    % (name, account_label(dst, cfg), ("归并 %d 行" % n) if n else "无需变更")
                )
                changed = changed or bool(n)
            else:
                dst = target or cb_current()
                if not dst:
                    parts.append("%s: 未发现会话，跳过" % name)
                    continue
                n = cb_merge(dst)
                note = "归并 %d 条" % n if n else "无需变更"
                if cfg.get("merge_history_files", True):
                    c, w = cb_file_merge(dst)
                    if c:
                        note += "，补正文 %d 个文件/共 %d 个工作区" % (c, w)
                parts.append("%s: 当前 %s，%s" % (name, account_label(dst, cfg), note))
                changed = changed or bool(n) or "补正文" in note
        except sqlite3.OperationalError as e:
            parts.append("%s: 数据库被占用(%s)" % (name, e))
        except Exception as e:
            parts.append("%s: 失败(%s: %s)" % (name, type(e).__name__, e))

    summary = " | ".join(parts)
    if changed and not quiet:
        log(summary)
    return summary


def main():
    """命令行入口：python -m src.sync_core [--product wb|cb] [--account <uid>]"""
    product = None
    target = None
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--product" and i + 1 < len(args):
            product = args[i + 1]
        elif a.startswith("--product="):
            product = a.split("=", 1)[1]
        elif a == "--account" and i + 1 < len(args):
            target = args[i + 1]
        elif a.startswith("--account="):
            target = a.split("=", 1)[1]

    print(sync(product=product, target=target))
    if "--stats" in args:
        cfg = load_config()
        for product_key, title in (("wb", "WorkBuddy"), ("cb", "CodeBuddy")):
            data = stats().get(product_key, {})
            if data:
                line = "，".join(
                    "%s %d 条" % (account_label(u, cfg), n) for u, n in data.items()
                )
                print("  %s: %s" % (title, line))


if __name__ == "__main__":
    main()
