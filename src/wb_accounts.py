#!/usr/bin/env python3
r"""WorkBuddy 多账号管理 · 命令行外壳

真正的逻辑在同目录的 accounts.py，界面与 CLI 共用同一套数据层。
本文件只负责解析参数、打印结果，行为与旧版保持一致。

用法（在项目根目录执行）：
  python .\src\wb_accounts.py detect
  python .\src\wb_accounts.py add [--label 账号-01] [--root D:\\path] [--timeout 1200]
  python .\src\wb_accounts.py switch wb-01
  python .\src\wb_accounts.py stop wb-01 [--force]
  python .\src\wb_accounts.py list

只做本地操作：不发签到/领取请求，不启动调试端口，不做自动化点击。
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import accounts as acct  # noqa: E402


def log(msg):
    print(msg, flush=True)


def cmd_detect(_args):
    cfg = acct.ensure_settings()
    root = Path(cfg["workbuddy"]["accountsRoot"])
    exe = cfg["workbuddy"].get("exePath")
    log("本工具数据目录: %s" % acct.data_dir_text())
    log("账号清单      : %s" % acct.ACCOUNTS_PATH)
    log("账号设置      : %s" % acct.SETTINGS_PATH)
    log("客户端路径    : %s" % (exe or "未找到"))
    log("账号根目录    : %s (%s)" % (root, "存在" if root.exists() else "尚未创建"))
    log("默认 profile  : %s" % acct.default_profile_dir())
    log("账号数        : %d" % len(acct.load_registry()["accounts"]))
    return 0 if exe else 2


def cmd_add(args):
    cfg = acct.ensure_settings()
    if args.root:
        cfg["workbuddy"]["accountsRoot"] = str(Path(args.root).expanduser())
        acct.save_settings(cfg)
    if args.exe:
        cfg["workbuddy"]["exePath"] = str(Path(args.exe).expanduser())
        acct.save_settings(cfg)

    value = acct.ensure_settings()["workbuddy"].get("exePath")
    if not value or not Path(value).is_file():
        log("未找到 WorkBuddy 可执行文件，请用 --exe 指定。")
        return 2

    try:
        info = acct.prepare_new_account(args.label)
    except Exception as exc:
        log("新建失败：%s" % exc)
        return 2

    log("新建配置目录  : %s" % info["configDir"])
    log("客户端        : %s" % info["exe"])
    log("已启动实例    : pid=%s（%s）" % (
        info["pid"], "独立启动器，不受本进程退出影响" if info["independent"] else "直接启动"
    ))
    log("启动器        : %s" % info["launcher"])
    log("现有 WorkBuddy 实例未受影响。")
    log("等待登录：请在打开的 WorkBuddy 窗口完成登录（只读本地账号快照与认证文件）。")

    result = acct.wait_new_login(info, timeout=args.timeout)
    if not result["ok"]:
        log(result["message"])
        return 3

    record = result["record"]
    log("登录完成，账号已登记。")
    log("账号标识(uid) : %s" % record["accountUid"])
    log("昵称          : %s" % (record.get("nickname") or "（快照未提供）"))
    log("显示名        : %s" % record.get("label"))
    log("配置目录      : %s" % record.get("configDir"))
    log("创建时间      : %s" % record.get("createdAt"))
    log("桌面入口      : %s" % (result.get("shortcut") or "未创建（%s）" % result.get("warning")))
    log("账号总数      : %d" % len(acct.load_registry()["accounts"]))
    return 0


def cmd_switch(args):
    target = acct.resolve_account(args.account)
    if not target:
        log("未找到账号：%s（可先运行 list）" % args.account)
        return 2
    log("切换目标：%s  uid=%s" % (
        target.get("nickname") or target.get("label") or args.account,
        target.get("accountUid"),
    ))
    log("配置目录：%s" % target.get("configDir"))
    result = acct.switch_account(target["accountUid"], timeout=args.timeout)
    log(result["message"])
    return 0 if result["ok"] else 4


def cmd_stop(args):
    target = acct.resolve_account(args.account)
    if not target:
        log("未找到账号：%s（可先运行 list）" % args.account)
        return 2
    if target.get("isDefaultProfile") and not args.force:
        log("拒绝：这是默认 profile（日常在用的主实例）。确认要关闭请加 --force。")
        return 2
    result = acct.stop_account(target["accountUid"], allow_default=args.force)
    log(result["message"])
    return 0 if result["ok"] else 5


def cmd_list(_args):
    rows = acct.scan_accounts()
    reg = acct.load_registry()
    if not rows:
        log("暂无已登记账号。")
        return 0

    log("共 %d 个账号（识别依据：账号快照 + auth 文件）" % len(rows))
    for i, row in enumerate(rows, 1):
        if row["running"]:
            state = "运行中(pid=%s" % row["pid"]
            if row["heartbeatSec"] is not None:
                state += ", 心跳%ss前" % row["heartbeatSec"]
            state += ")"
        else:
            state = "未运行"
        mark = "  ← 生效" if row["active"] else ""
        tag = "  [默认 profile]" if row["isDefault"] else ""
        log("[%d] %s  uid=%s" % (i, row["label"], row["uid"]))
        log("    状态: %s  登录态到期: %s%s%s" % (state, row["expiry"], tag, mark))
        log("    目录: %s" % row["configDir"])
        log("    最近登录: %s" % row["lastLoginAt"])
        quota = row.get("quota") or {}
        if quota:
            log("    额度: %s（%s 查询）" % (quota.get("text"), quota.get("checkedAt")))
        else:
            log("    额度: —（未查询）")

    active = reg.get("activeUid")
    if active:
        label = next((r["label"] for r in rows if r["uid"] == active), active)
        log("生效账号: %s (%s)" % (label, active))
    else:
        log("生效账号: 未设置（可用 switch 指定）")
    return 0


def main():
    parser = argparse.ArgumentParser(description="WorkBuddy 多账号管理（本地只读，不发送签到请求）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_detect = sub.add_parser("detect", help="显示数据目录与自动探测结果")
    p_detect.set_defaults(func=cmd_detect)

    p_add = sub.add_parser("add", help="新增一个账号：建目录、启动、等待登录、登记")
    p_add.add_argument("--label", help="显示名，默认按目录序号生成")
    p_add.add_argument("--root", help="账号根目录（写入账号设置）")
    p_add.add_argument("--exe", help="WorkBuddy.exe 路径（写入账号设置）")
    p_add.add_argument("--timeout", type=int, help="等待登录秒数，默认 1200")
    p_add.set_defaults(func=cmd_add)

    p_switch = sub.add_parser("switch", help="切换到某账号：拉起实例（或置前）并记为生效")
    p_switch.add_argument("account", help="账号序号、目录名（如 wb-01）、uid、昵称或显示名")
    p_switch.add_argument("--timeout", type=int, help="等待实例就绪的秒数，默认 180")
    p_switch.set_defaults(func=cmd_switch)

    p_stop = sub.add_parser("stop", help="关闭某个账号的实例（只关该实例进程树）")
    p_stop.add_argument("account", help="账号序号、目录名、uid、昵称或显示名")
    p_stop.add_argument("--force", action="store_true", help="允许关闭默认 profile 主实例")
    p_stop.set_defaults(func=cmd_stop)

    p_list = sub.add_parser("list", help="列出已登记账号")
    p_list.set_defaults(func=cmd_list)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
