# -*- coding: utf-8 -*-
"""多产品用量采集。

原则：
1. 能拿到真实 token 的，一律按 token × 公开 API 单价折算金额；
   厂商积分 / credits 只做展示，不参与计价（积分对应额度会被随时暗改）。
2. Trae 本地拿不到 token 明细，只展示厂商积分口径，并明确不计入金额汇总。
3. WorkBuddy / CodeBuddy 的原始记录按文件大小 + mtime 做增量缓存，
   首次全量扫描后，后续刷新只解析变化的文件。
"""
import datetime
import glob
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict

try:
    import accounts as acct
except Exception:  # 独立运行采集脚本时允许缺失
    acct = None

HOME = os.path.expanduser("~")
APPDATA = os.environ.get("APPDATA") or os.path.join(HOME, "AppData", "Roaming")
LOCALAPPDATA = os.environ.get("LOCALAPPDATA") or os.path.join(HOME, "AppData", "Local")

HERE = os.path.dirname(os.path.abspath(__file__))


def _data_dir():
    if acct is not None:
        try:
            return str(acct.DATA_DIR)
        except Exception:
            pass
    return os.path.join(APPDATA, "workbuddy-codebuddy-chat-sync")


DATA_DIR = _data_dir()
CACHE_PATH = os.path.join(DATA_DIR, "usage-cache.json")
SNAPSHOT_PATH = os.path.join(DATA_DIR, "usage-snapshot.json")
TRAE_CACHE_PATH = os.path.join(DATA_DIR, "trae-usage.json")

CACHE_VERSION = 4
QODER_STALE_SEC = 600
TRAE_USAGE_URL = "https://api.trae.cn/trae/api/v2/pay/ide_user_ent_usage"

PRODUCT_TITLES = {
    "qoder": "Qoder",
    "workbuddy": "WorkBuddy",
    "codebuddy": "CodeBuddy",
    "trae": "Trae",
}

_CACHE_LOCK = threading.Lock()
_SR_RE = re.compile(r"<system-reminder.*?</system-reminder>", re.S)
_TAG_RE = re.compile(r"<[^>]{1,80}>")

_WB_ROOT = os.path.join(HOME, ".workbuddy", "projects")
_CB_ROOT = os.path.join(LOCALAPPDATA, "CodeBuddyExtension", "Data")
_CB_META = os.path.join(APPDATA, "CodeBuddy CN", "codebuddy-sessions.vscdb")


# ---------------------------------------------------------------- 通用工具

def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _atomic_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, path)


def _read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def _asset_path(name):
    if getattr(sys, "frozen", False):
        # 优先读 exe 同级：用户可以直接改单价，不用重新打包
        beside = os.path.join(os.path.dirname(sys.executable), name)
        if os.path.isfile(beside):
            return beside
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    else:
        base = HERE
    return os.path.join(base, name)


def load_pricing():
    data = _read_json(_asset_path("usage_pricing.json"), {}) or {}
    models = data.get("models") or {}
    out = {}
    for key, val in models.items():
        if not isinstance(val, dict):
            continue
        try:
            out[_norm_model(key)] = {
                "name": val.get("name") or key,
                "input": float(val.get("input") or 0),
                "output": float(val.get("output") or 0),
                "cacheRead": float(val.get("cacheRead") or 0),
                "source": val.get("source") or "",
            }
        except Exception:
            continue
    fallback = _norm_model(data.get("fallback") or "deepseek-v4.1-flash")
    if fallback not in out:
        out[fallback] = {
            "name": "DeepSeek-V4.1-Flash", "input": 0.5, "output": 4.0,
            "cacheRead": 0.1, "source": "fallback",
        }
    return {"currency": data.get("currency") or "CNY",
            "unit": data.get("unit") or "元 / 百万 token",
            "note": data.get("note") or "", "models": out, "fallback": fallback}


def _norm_model(name):
    text = str(name or "").strip().lower().replace("_", "-")
    text = re.sub(r"\s+", "-", text)
    return text or "unknown"


def _price(pricing, model):
    key = _norm_model(model)
    models = pricing["models"]
    if key in models:
        return key, models[key], True
    # 常见display名兜底：DeepSeek-V4.1-Flash / GLM-5.3-Flash
    compact = key.replace("-", "")
    for mid, val in models.items():
        if compact == mid.replace("-", ""):
            return mid, val, True
    return pricing["fallback"], models[pricing["fallback"]], False


def _cost(pricing, model, new_input, cache_read, out):
    key, price, known = _price(pricing, model)
    value = (new_input * price["input"]
             + cache_read * price["cacheRead"]
             + out * price["output"]) / 1e6
    return value, key, price, known


def _ms(value):
    """毫秒时间戳归一化；支持数字、数字字符串、ISO 字符串。"""
    if value in (None, ""):
        return 0
    if isinstance(value, (int, float)):
        v = float(value)
        return int(v if v > 1e11 else v * 1000) if v > 1e8 else 0
    text = str(value).strip()
    if text.isdigit():
        v = float(text)
        return int(v if v > 1e11 else v * 1000)
    try:
        dt = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return 0


def _date_of(ms):
    if not ms:
        return "unknown"
    try:
        return datetime.datetime.fromtimestamp(ms / 1000.0).strftime("%Y-%m-%d")
    except Exception:
        return "unknown"


def _stamp(ms):
    if not ms:
        return ""
    try:
        return datetime.datetime.fromtimestamp(ms / 1000.0).strftime("%m-%d %H:%M")
    except Exception:
        return ""


def _clean_title(text):
    if not text:
        return ""
    text = _SR_RE.sub("", str(text))
    text = _TAG_RE.sub(" ", text)
    text = " ".join(text.split())
    return text[:80]


def _content_text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind in ("text", "input_text", "output_text"):
            parts.append(str(block.get("text") or ""))
        elif kind == "thinking":
            continue
    return " ".join(parts)


def _record_cost(pricing, turns):
    """turns: [{ts, model, new, cache, out}]，返回 (cost, model_rows, day_rows)。"""
    cost = 0.0
    models = defaultdict(lambda: defaultdict(float))
    days = defaultdict(lambda: defaultdict(float))
    for turn in turns:
        value, key, price, known = _cost(
            pricing, turn.get("model"), turn.get("new") or 0,
            turn.get("cache") or 0, turn.get("out") or 0)
        cost += value
        models[key]["requests"] += 1
        models[key]["new"] += turn.get("new") or 0
        models[key]["cache"] += turn.get("cache") or 0
        models[key]["out"] += turn.get("out") or 0
        models[key]["cost"] += value
        models[key]["estimated"] = 1 if not known else 0
        models[key]["name"] = price["name"]
        day = _date_of(turn.get("ts"))
        days[day]["requests"] += 1
        days[day]["new"] += turn.get("new") or 0
        days[day]["cache"] += turn.get("cache") or 0
        days[day]["out"] += turn.get("out") or 0
        days[day]["cost"] += value
    return cost, models, days


def _model_rows(models, sessions_by_model=None):
    rows = []
    for mid, val in models.items():
        processed = val["new"] + val["cache"]
        rows.append({
            "id": mid,
            "name": val.get("name") or mid,
            "requests": int(val["requests"]),
            "sessions": int((sessions_by_model or {}).get(mid, 0)),
            "newInput": int(round(val["new"])),
            "cacheRead": int(round(val["cache"])),
            "out": int(round(val["out"])),
            "processed": int(round(processed)),
            "cost": round(val["cost"], 4),
            "cacheHit": round((val["cache"] / processed) if processed else 0, 4),
            "estimated": bool(val.get("estimated")),
        })
    rows.sort(key=lambda r: -r["processed"])
    return rows


def _day_rows(days):
    rows = []
    for date, val in days.items():
        processed = val["new"] + val["cache"]
        rows.append({
            "date": date,
            "requests": int(val["requests"]),
            "newInput": int(round(val["new"])),
            "cacheRead": int(round(val["cache"])),
            "out": int(round(val["out"])),
            "processed": int(round(processed)),
            "cost": round(val["cost"], 4),
            "cacheHit": round((val["cache"] / processed) if processed else 0, 4),
        })
    rows.sort(key=lambda r: r["date"])
    return rows


def _totals_from(rows, sessions, model_rows):
    processed = sum(r["processed"] for r in rows)
    cache = sum(r["cacheRead"] for r in rows)
    return {
        "requests": sum(r["requests"] for r in rows),
        "sessions": len(sessions),
        "newInput": sum(r["newInput"] for r in rows),
        "cacheRead": cache,
        "out": sum(r["out"] for r in rows),
        "processed": processed,
        "cost": round(sum(r["cost"] for r in rows), 4),
        "cacheHit": round((cache / processed) if processed else 0, 4),
    }


# ---------------------------------------------------------------- 缓存

def _load_cache():
    with _CACHE_LOCK:
        data = _read_json(CACHE_PATH, {}) or {}
    if data.get("version") != CACHE_VERSION:
        return {"version": CACHE_VERSION, "wb": {}, "cb": {}}
    data.setdefault("wb", {})
    data.setdefault("cb", {})
    return data


def _save_cache(cache):
    with _CACHE_LOCK:
        try:
            _atomic_json(CACHE_PATH, cache)
        except Exception:
            pass


def _file_stamp(path):
    try:
        st = os.stat(path)
        return int(st.st_size), int(st.st_mtime)
    except OSError:
        return None


# ---------------------------------------------------------------- WorkBuddy

def _wb_files():
    return sorted(glob.glob(os.path.join(_WB_ROOT, "*", "*.jsonl")))


def _parse_wb(path):
    """单个 WorkBuddy 转录文件 → 会话（含逐轮 token）。"""
    sid = os.path.basename(path)[:-len(".jsonl")]
    project = os.path.basename(os.path.dirname(path))
    title = ""
    cwd = ""
    first_user = ""
    turns = []
    try:
        fh = open(path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return None
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not cwd and isinstance(rec.get("cwd"), str):
                cwd = rec["cwd"]
            if rec.get("type") == "ai-title" and rec.get("aiTitle"):
                title = str(rec["aiTitle"])
            msg = rec.get("message")
            if not isinstance(msg, dict):
                continue
            if msg.get("role") == "user" and not first_user:
                first_user = _clean_title(_content_text(msg.get("content")))
            usage = msg.get("usage")
            if not isinstance(usage, dict):
                continue
            try:
                inp = int(usage.get("input_tokens") or 0)
                cache = int(usage.get("cache_read_input_tokens") or 0)
                out = int(usage.get("output_tokens") or 0)
            except (TypeError, ValueError):
                continue
            if inp <= 0 and out <= 0:
                continue
            model = ""
            pd = rec.get("providerData")
            if isinstance(pd, dict):
                model = pd.get("model") or pd.get("requestModelName") or ""
            elif isinstance(pd, str):
                m = re.search(r"['\"]model['\"]\s*:\s*['\"]([^'\"]+)", pd)
                model = m.group(1) if m else ""
            turns.append({
                "ts": _ms(rec.get("timestamp")),
                "model": model or "unknown",
                "new": max(0, inp - cache),
                "cache": max(0, cache),
                "out": max(0, out),
            })
    if not turns:
        return None
    turns.sort(key=lambda t: t["ts"])
    last = turns[-1]["ts"]
    return {
        "id": sid,
        "title": title or first_user or ("会话 " + sid[:8]),
        "workspace": os.path.basename(cwd.rstrip("\\/")) or project,
        "cwd": cwd,
        "turns": turns,
        "lastTs": last,
    }


def collect_workbuddy(pricing):
    cache = _load_cache()
    sessions = []
    dirty = False
    keep = set()
    for path in _wb_files():
        keep.add(path)
        stamp = _file_stamp(path)
        if not stamp:
            continue
        size, mtime = stamp
        ent = cache["wb"].get(path)
        if (not ent or ent.get("size") != size or ent.get("mtime") != mtime
                or ent.get("version") != CACHE_VERSION):
            data = _parse_wb(path)
            ent = {"version": CACHE_VERSION, "size": size, "mtime": mtime, "data": data}
            cache["wb"][path] = ent
            dirty = True
        if ent.get("data"):
            sessions.append(ent["data"])
    for stale in [p for p in cache["wb"] if p not in keep]:
        del cache["wb"][stale]
        dirty = True
    if dirty:
        _save_cache(cache)

    if not sessions:
        return {
            "key": "workbuddy", "title": PRODUCT_TITLES["workbuddy"],
            "available": bool(os.path.isdir(_WB_ROOT)),
            "error": "" if os.path.isdir(_WB_ROOT) else "未发现 ~/.workbuddy/projects",
            "source": _WB_ROOT, "totals": {}, "models": [], "days": [],
            "sessions": [], "note": "读取本地转录中的真实 token。",
        }

    all_turns = []
    model_sessions = defaultdict(set)
    out_sessions = []
    for sess in sessions:
        for turn in sess["turns"]:
            model_sessions[_norm_model(turn["model"])].add(sess["id"])
        all_turns.extend(sess["turns"])
        cost, models, _days = _record_cost(pricing, sess["turns"])
        processed = sum((t["new"] + t["cache"]) for t in sess["turns"])
        cache_read = sum(t["cache"] for t in sess["turns"])
        out_sessions.append({
            "id": sess["id"], "title": sess["title"], "workspace": sess["workspace"],
            "model": Counter(t["model"] for t in sess["turns"]).most_common(1)[0][0],
            "lastTs": sess["lastTs"], "lastSeen": _stamp(sess["lastTs"]),
            "requests": len(sess["turns"]), "processed": processed,
            "out": sum(t["out"] for t in sess["turns"]),
            "cost": round(cost, 4),
            "cacheHit": round((cache_read / processed) if processed else 0, 4),
            "status": None,
        })
    cost, models, days = _record_cost(pricing, all_turns)
    model_rows = _model_rows(models, {k: len(v) for k, v in model_sessions.items()})
    day_rows = _day_rows(days)
    out_sessions.sort(key=lambda s: s["lastTs"], reverse=True)
    totals = _totals_from(day_rows, out_sessions, model_rows)
    totals["cost"] = round(cost, 4)
    return {
        "key": "workbuddy", "title": PRODUCT_TITLES["workbuddy"],
        "available": True, "error": "", "source": _WB_ROOT,
        "totals": totals, "models": model_rows, "days": day_rows,
        "sessions": out_sessions[:80],
        "note": "按转录里的真实 input/cache/output token，乘公开 API 单价折算。",
    }


# ---------------------------------------------------------------- CodeBuddy

def _cb_meta():
    """会话索引库：conversationId → 标题 / 工作区 / 账号。"""
    out = {}
    if not os.path.isfile(_CB_META):
        return out
    try:
        con = sqlite3.connect("file:%s?mode=ro" % _CB_META.replace("\\", "/"), uri=True)
        try:
            for key, value in con.execute("SELECT key, value FROM ItemTable"):
                if not str(key).startswith("session:"):
                    continue
                raw = value.decode("utf-8", "ignore") if isinstance(value, (bytes, bytearray)) else value
                try:
                    data = json.loads(raw)
                except Exception:
                    continue
                cid = str(data.get("conversationId") or str(key)[8:])
                out[cid] = data
        finally:
            con.close()
    except Exception:
        pass
    return out


def _cb_files():
    return glob.glob(os.path.join(_CB_ROOT, "*", "CodeBuddyIDE", "*", "history", "*", "*", "index.json"))


def _cb_best_files():
    """同一会话会被同步工具复制到多个账号目录；按文件大小取最完整的一份。"""
    groups = defaultdict(list)
    for path in _cb_files():
        conv = os.path.basename(os.path.dirname(path))
        groups[conv].append(path)
    best = {}
    for conv, paths in groups.items():
        def rank(p):
            stamp = _file_stamp(p) or (0, 0)
            bak = -1 if ".bak" in p else 0
            return (stamp[0], bak, stamp[1])
        best[conv] = max(paths, key=rank)
    return best


def _parse_cb(path, meta):
    conv = os.path.basename(os.path.dirname(path))
    ws_hash = os.path.basename(os.path.dirname(os.path.dirname(path)))
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
    except Exception:
        return None
    requests = data.get("requests") or []
    if not requests:
        return None

    msg_models = {}
    msg_dir = os.path.join(os.path.dirname(path), "messages")
    roles = {}
    for item in data.get("messages") or []:
        if isinstance(item, dict) and item.get("id"):
            roles[item["id"]] = item.get("role")

    def model_of(message_id):
        """只读该轮真正需要的那条消息，避免首次扫描几万个消息文件。"""
        if message_id in msg_models:
            return msg_models[message_id]
        model = ""
        try:
            with open(os.path.join(msg_dir, message_id + ".json"),
                      "r", encoding="utf-8", errors="replace") as fh:
                msg = json.load(fh)
        except Exception:
            msg = None
        if isinstance(msg, dict):
            extra = msg.get("extra")
            if isinstance(extra, str):
                try:
                    extra = json.loads(extra)
                except Exception:
                    extra = None
            if isinstance(extra, dict):
                model = str(extra.get("modelName") or extra.get("modelId") or "")
        msg_models[message_id] = model
        return model

    turns = []
    for req in requests:
        usage = req.get("usage")
        if not isinstance(usage, dict):
            continue
        try:
            inp = int(usage.get("inputTokens") or 0)
            cache = int(usage.get("cacheTokens") or 0)
            out = int(usage.get("outputTokens") or 0)
        except (TypeError, ValueError):
            continue
        if inp <= 0 and out <= 0:
            continue
        ids = [m for m in (req.get("messages") or []) if m]
        model = ""
        for mid in ids:
            if roles.get(mid) == "assistant":
                model = model_of(mid)
                if model:
                    break
        if not model:
            for mid in ids:
                model = model_of(mid)
                if model:
                    break
        turns.append({
            "ts": _ms(req.get("startedAt")),
            "model": model or "unknown",
            "new": max(0, inp - cache),
            "cache": max(0, cache),
            "out": max(0, out),
        })
    if not turns:
        return None
    turns.sort(key=lambda t: t["ts"])
    info = meta.get(conv) or {}
    cwd = str(info.get("cwd") or "")
    title = info.get("customTitle") or info.get("title") or ("会话 " + conv[:8])
    return {
        "id": conv,
        "title": str(title),
        "workspace": os.path.basename(cwd.rstrip("/\\")) or ws_hash[:8],
        "cwd": cwd,
        "turns": turns,
        "lastTs": turns[-1]["ts"],
        "_updated": info.get("updatedAt") or 0,
    }


def collect_codebuddy(pricing):
    cache = _load_cache()
    meta = _cb_meta()
    sessions = []
    dirty = False
    best = _cb_best_files()
    keep = set(best.values())
    for conv, path in best.items():
        stamp = _file_stamp(path)
        if not stamp:
            continue
        size, mtime = stamp
        ent = cache["cb"].get(path)
        if (not ent or ent.get("size") != size or ent.get("mtime") != mtime
                or ent.get("version") != CACHE_VERSION):
            data = _parse_cb(path, meta)
            ent = {"version": CACHE_VERSION, "size": size, "mtime": mtime, "data": data}
            cache["cb"][path] = ent
            dirty = True
        if ent.get("data"):
            sessions.append(ent["data"])
    for stale in [p for p in cache["cb"] if p not in keep]:
        del cache["cb"][stale]
        dirty = True
    if dirty:
        _save_cache(cache)

    if not sessions:
        return {
            "key": "codebuddy", "title": PRODUCT_TITLES["codebuddy"],
            "available": bool(os.path.isdir(_CB_ROOT)),
            "error": "" if os.path.isdir(_CB_ROOT) else "未发现 CodeBuddy 数据目录",
            "source": _CB_ROOT, "totals": {}, "models": [], "days": [],
            "sessions": [], "note": "按会话索引中的真实 token 折算，已按会话去重。",
        }

    all_turns = []
    model_sessions = defaultdict(set)
    out_sessions = []
    for sess in sessions:
        for turn in sess["turns"]:
            model_sessions[_norm_model(turn["model"])].add(sess["id"])
        all_turns.extend(sess["turns"])
        cost, _models, _days = _record_cost(pricing, sess["turns"])
        processed = sum((t["new"] + t["cache"]) for t in sess["turns"])
        cache_read = sum(t["cache"] for t in sess["turns"])
        out_sessions.append({
            "id": sess["id"], "title": sess["title"], "workspace": sess["workspace"],
            "model": Counter(t["model"] for t in sess["turns"]).most_common(1)[0][0],
            "lastTs": sess["lastTs"], "lastSeen": _stamp(sess["lastTs"]),
            "requests": len(sess["turns"]), "processed": processed,
            "out": sum(t["out"] for t in sess["turns"]),
            "cost": round(cost, 4),
            "cacheHit": round((cache_read / processed) if processed else 0, 4),
            "status": None,
        })
    cost, models, days = _record_cost(pricing, all_turns)
    model_rows = _model_rows(models, {k: len(v) for k, v in model_sessions.items()})
    day_rows = _day_rows(days)
    out_sessions.sort(key=lambda s: s["lastTs"], reverse=True)
    totals = _totals_from(day_rows, out_sessions, model_rows)
    totals["cost"] = round(cost, 4)
    return {
        "key": "codebuddy", "title": PRODUCT_TITLES["codebuddy"],
        "available": True, "error": "", "source": _CB_ROOT,
        "totals": totals, "models": model_rows, "days": day_rows,
        "sessions": out_sessions[:80],
        "note": "按会话索引里的真实 token 折算；多账号重复目录已按会话 ID 去重。",
    }


# ---------------------------------------------------------------- Qoder

def _qoder_usage_dir():
    env = os.environ.get("USAGE_QODER_DIR") or os.environ.get("QODER_USAGE_DIR")
    if env and os.path.isdir(env):
        return env
    roots = [
        os.path.join(HOME, "Documents", "Qoder"),
        os.path.join(HOME, "Qoder"),
        r"D:\Qoder",
        r"E:\Qoder",
    ]
    cands = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        cands.extend(glob.glob(os.path.join(root, "*", "usage")))
        cands.extend(glob.glob(os.path.join(root, "*", "*", "usage")))
    cands = [p for p in cands if os.path.isfile(os.path.join(p, "collect.py"))
             or os.path.isfile(os.path.join(p, "snapshot.json"))]
    if not cands:
        return None
    return max(cands, key=lambda p: os.path.getmtime(p))


def _python_for_script():
    if not getattr(sys, "frozen", False):
        return sys.executable
    for name in ("pythonw.exe", "python.exe"):
        found = shutil.which(name)
        if found:
            return found
    local = os.environ.get("LOCALAPPDATA") or ""
    for pattern in (os.path.join(local, "Programs", "Python", "Python3*", "pythonw.exe"),
                    os.path.join(local, "Programs", "Python", "Python3*", "python.exe")):
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[-1]
    return None


def _run_qoder_collect(usage_dir):
    script = os.path.join(usage_dir, "collect.py")
    if not os.path.isfile(script):
        return False, "未找到 collect.py"
    python = _python_for_script()
    if not python:
        return False, "未找到 Python，无法自动刷新 Qoder 快照"
    try:
        flags = 0x08000000 if os.name == "nt" else 0
        proc = subprocess.run(
            [python, script], cwd=usage_dir, timeout=300,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            creationflags=flags,
        )
        if proc.returncode != 0:
            tail = (proc.stderr or b"")[-300:].decode("utf-8", "replace")
            return False, tail or ("collect.py 退出码 %s" % proc.returncode)
        return True, ""
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)


def collect_qoder(pricing, force=False):
    usage_dir = _qoder_usage_dir()
    if not usage_dir:
        return {
            "key": "qoder", "title": PRODUCT_TITLES["qoder"],
            "available": False, "error": "未找到 Qoder 用量看板目录",
            "source": "", "totals": {}, "models": [], "days": [], "sessions": [],
            "credits": 0.0, "note": "",
        }
    snap_path = os.path.join(usage_dir, "snapshot.json")
    err = ""
    stale = (not os.path.isfile(snap_path)
             or time.time() - os.path.getmtime(snap_path) > QODER_STALE_SEC)
    if force or stale:
        ok, msg = _run_qoder_collect(usage_dir)
        if not ok:
            err = msg
    data = _read_json(snap_path, {}) or {}
    if not data:
        return {
            "key": "qoder", "title": PRODUCT_TITLES["qoder"],
            "available": False, "error": err or "读不到 snapshot.json",
            "source": usage_dir, "totals": {}, "models": [], "days": [], "sessions": [],
            "credits": 0.0, "note": "",
        }

    totals = data.get("totals") or {}
    models = []
    for row in data.get("models") or []:
        processed = row.get("processed") or 0
        models.append({
            "id": row.get("id") or row.get("name") or "",
            "name": row.get("name") or row.get("id") or "",
            "requests": int(row.get("requests") or 0),
            "sessions": int(row.get("sessions") or 0),
            "newInput": int(row.get("newInput") or 0),
            "cacheRead": int(row.get("cacheRead") or 0),
            "out": int(row.get("out") or 0),
            "processed": int(processed),
            "cost": round(float(row.get("cost") or 0), 4),
            "cacheHit": round(float(row.get("cacheHit") or 0), 4),
            "estimated": False,
        })
    days = []
    for row in data.get("days") or []:
        days.append({
            "date": row.get("date") or "unknown",
            "requests": int(row.get("requests") or 0),
            "newInput": int(row.get("newInput") or 0),
            "cacheRead": int(row.get("cacheRead") or 0),
            "out": int(row.get("out") or 0),
            "processed": int(row.get("processed") or 0),
            "cost": round(float(row.get("cost") or 0), 4),
            "cacheHit": round(float(row.get("cacheHit") or 0), 4),
        })
    sessions = []
    for row in data.get("sessions") or []:
        sessions.append({
            "id": row.get("shortId") or row.get("id") or "",
            "title": row.get("title") or "(无标题)",
            "workspace": row.get("workspace") or row.get("project") or "",
            "model": row.get("model") or "",
            "lastTs": row.get("lastSeen") or row.get("date") or "",
            "lastSeen": row.get("lastSeen") or "",
            "requests": int(row.get("requests") or 0),
            "processed": int(row.get("processed") or 0),
            "out": int(row.get("out") or 0),
            "cost": round(float(row.get("cost") or 0), 4),
            "cacheHit": round(float(row.get("cacheHit") or 0), 4),
            "status": row.get("status"),
        })
    clean_totals = {
        "requests": int(totals.get("requests") or 0),
        "sessions": int(totals.get("sessions") or 0),
        "newInput": int(totals.get("newInput") or 0),
        "cacheRead": int(totals.get("cacheRead") or 0),
        "out": int(totals.get("out") or 0),
        "processed": int(totals.get("processed") or 0),
        "cost": round(float(totals.get("cost") or 0), 4),
        "cacheHit": round(float(totals.get("cacheHit") or 0), 4),
    }
    return {
        "key": "qoder", "title": PRODUCT_TITLES["qoder"],
        "available": True, "error": err, "source": usage_dir,
        "totals": clean_totals, "models": models, "days": days,
        "sessions": sessions[:80],
        "credits": round(float(totals.get("credits") or 0), 3),
        "note": "Qoder 免费模型不返回 token，快照由 collect.py 按上下文比例还原；金额按 token × API 单价折算。",
    }


# ---------------------------------------------------------------- Trae

def _trae_from_cache():
    data = _read_json(TRAE_CACHE_PATH, {}) or {}
    if not data.get("checkedAt"):
        return {
            "key": "trae", "title": PRODUCT_TITLES["trae"],
            "available": False, "error": "尚未查询过 Trae 额度",
            "checkedAt": "", "accounts": [], "consumed": 0.0, "total": 0.0,
            "remaining": 0.0, "ratio": 0.0, "expire": "",
            "note": "Trae 未提供 token 明细，仅展示厂商积分口径，未计入金额汇总。",
        }
    return data


def _expire_text(ts):
    try:
        value = float(ts)
        if value > 1e11:
            value /= 1000.0
        if value <= 0:
            return ""
        return datetime.datetime.fromtimestamp(value).strftime("%Y-%m-%d")
    except Exception:
        return ""


def _query_trae():
    if acct is None:
        return None, "账号模块不可用"
    try:
        rows = acct.scan_trae()
    except Exception as exc:
        return None, "扫描 Trae 实例失败：%s" % type(exc).__name__
    if not rows:
        return None, "未发现 Trae 实例"
    accounts = []
    errors = []
    for row in rows:
        try:
            auth, reason = acct.read_trae_auth(__import__("pathlib").Path(row["configDir"]))
        except Exception as exc:
            errors.append("%s：%s" % (row.get("label"), type(exc).__name__))
            continue
        if not auth or not auth.get("token"):
            errors.append("%s：%s" % (row.get("label"), reason or "登录凭证不可用"))
            continue
        headers = {
            "Authorization": "Cloud-IDE-JWT %s" % auth["token"],
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-User-Region": auth.get("region") or "CN",
        }
        try:
            raw = acct._get_json(TRAE_USAGE_URL, headers)
            payload = json.loads(raw.decode("utf-8", "ignore"))
        except Exception as exc:
            errors.append("%s：%s" % (row.get("label"), type(exc).__name__))
            continue
        if not isinstance(payload, dict):
            errors.append("%s：返回内容无法解析" % row.get("label"))
            continue
        usage = payload.get("usage_summary") or {}
        try:
            consumed = float(usage.get("consumed_amount") or 0)
            total = float(usage.get("total_amount") or 0)
            remaining = float(payload.get("total_balance")
                              if payload.get("total_balance") is not None else 0)
        except (TypeError, ValueError):
            consumed = total = remaining = 0.0
        expires = []
        for pack in payload.get("user_entitlement_pack_list") or []:
            base = pack.get("entitlement_base_info") or {}
            for key in ("end_time", "expire_time"):
                text = _expire_text(base.get(key) if key in base else pack.get(key))
                if text:
                    expires.append(text)
        accounts.append({
            "label": row.get("label") or "Trae",
            "uid": row.get("uid") or "",
            "consumed": round(consumed, 2),
            "total": round(total, 2),
            "remaining": round(remaining, 2),
            "expire": min(expires) if expires else "",
        })
    if not accounts:
        return None, "；".join(errors[:3]) or "查询失败"
    consumed = sum(a["consumed"] for a in accounts)
    total = sum(a["total"] for a in accounts)
    remaining = sum(a["remaining"] for a in accounts)
    expires = [a["expire"] for a in accounts if a.get("expire")]
    return {
        "key": "trae", "title": PRODUCT_TITLES["trae"],
        "available": True,
        "error": "；".join(errors[:3]),
        "checkedAt": now_iso(),
        "accounts": accounts,
        "consumed": round(consumed, 2),
        "total": round(total, 2),
        "remaining": round(remaining, 2),
        "ratio": round((consumed / total) if total else 0, 4),
        "expire": min(expires) if expires else "",
        "note": "Trae 未提供 token 明细，仅展示厂商积分口径，未计入金额汇总。",
    }, ""


def collect_trae(online=False):
    """online=False 时只读缓存，绝不自动联网。"""
    if online:
        result, err = _query_trae()
        if result:
            _atomic_json(TRAE_CACHE_PATH, result)
            return result
        cached = _trae_from_cache()
        if cached.get("available"):
            cached = dict(cached)
            cached["error"] = err or cached.get("error") or ""
            return cached
        return {
            "key": "trae", "title": PRODUCT_TITLES["trae"],
            "available": False, "error": err or "查询失败",
            "checkedAt": "", "accounts": [], "consumed": 0.0, "total": 0.0,
            "remaining": 0.0, "ratio": 0.0, "expire": "",
            "note": "Trae 未提供 token 明细，仅展示厂商积分口径，未计入金额汇总。",
        }
    return _trae_from_cache()


# ---------------------------------------------------------------- 汇总

def _combined(products):
    keys = ("qoder", "workbuddy", "codebuddy")
    cost = sum(float((products.get(k) or {}).get("totals", {}).get("cost") or 0)
               for k in keys)
    processed = sum(int((products.get(k) or {}).get("totals", {}).get("processed") or 0)
                    for k in keys)
    requests = sum(int((products.get(k) or {}).get("totals", {}).get("requests") or 0)
                   for k in keys)
    sessions = sum(int((products.get(k) or {}).get("totals", {}).get("sessions") or 0)
                   for k in keys)
    cache_read = sum(int((products.get(k) or {}).get("totals", {}).get("cacheRead") or 0)
                     for k in keys)
    by_product = []
    for key in ("qoder", "workbuddy", "codebuddy", "trae"):
        product = products.get(key) or {}
        totals = product.get("totals") or {}
        by_product.append({
            "key": key,
            "title": PRODUCT_TITLES.get(key, key),
            "cost": round(float(totals.get("cost") or 0), 4),
            "processed": int(totals.get("processed") or 0),
            "requests": int(totals.get("requests") or 0),
            "sessions": int(totals.get("sessions") or 0),
            "available": bool(product.get("available")),
        })
    return {
        "cost": round(cost, 4),
        "processed": processed,
        "requests": requests,
        "sessions": sessions,
        "cacheHit": round((cache_read / processed) if processed else 0, 4),
        "byProduct": by_product,
    }


def _public(snapshot):
    """落盘前去掉逐轮明细，避免快照过大。"""
    result = json.loads(json.dumps(snapshot, ensure_ascii=False))
    for product in result.get("products", {}).values():
        for sess in product.get("sessions") or []:
            sess.pop("turns", None)
    return result


def build_snapshot(force_qoder=False, online_trae=False):
    pricing = load_pricing()
    products = {
        "qoder": collect_qoder(pricing, force=force_qoder),
        "workbuddy": collect_workbuddy(pricing),
        "codebuddy": collect_codebuddy(pricing),
        "trae": collect_trae(online=online_trae),
    }
    snapshot = {
        "generatedAt": now_iso(),
        "pricing": {
            "currency": pricing["currency"],
            "unit": pricing["unit"],
            "note": pricing["note"],
        },
        "products": products,
        "combined": _combined(products),
    }
    try:
        _atomic_json(SNAPSHOT_PATH, _public(snapshot))
    except Exception:
        pass
    return snapshot


def load_snapshot():
    return _read_json(SNAPSHOT_PATH, {}) or {}
