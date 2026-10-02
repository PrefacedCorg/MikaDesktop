"""命令行入口 —— 库之上的薄壳，不含任何核心逻辑。

::

    python -m features.catch_notify --once
    python -m features.catch_notify --watch --json
    python -m features.catch_notify --check
    python -m features.catch_notify --source winrt --once
    python -m features.catch_notify --once --activate 0 --plan-only
    python -m features.catch_notify --once --activate 0 --input reply=好的
    python -m features.catch_notify --once --activate-body

``--activate`` / ``--activate-body`` 会把最新一条通知的按钮（或通知本体）通过
:mod:`features.catch_notify.activation` 真正发回应用，建议先加 ``--plan-only``
确认它打算怎么做。

库用法见 :mod:`features.catch_notify` 的包文档。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from pathlib import Path

from . import settings
from .bridge import WinRTBridge
from .database import default_database_path
from .errors import AccessDenied, BridgeUnavailable, CatchNotifyError
from .records import PROVENANCE_DATABASE, Notification
from .sources import (
    SOURCE_AUTO,
    SOURCE_DATABASE,
    SOURCE_WINRT,
    describe_environment,
    read_notifications,
    watch_notifications,
)

#: 美化 XML 是可选功能：本项目的 cx_Freeze 配置把 xml 包放进了 EXCLUDES，
#: 所以这里必须容忍导入失败，退化成直接输出原始 XML。
try:
    from xml.dom import minidom
except ImportError:  # pragma: no cover - 取决于打包配置
    minidom = None

LINE = "─" * 64


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #
def pretty_xml(xml: str, *, raw: bool = False) -> str:
    if raw or not xml or minidom is None:
        return xml
    try:
        document = minidom.parseString(xml)
        lines = [line for line in document.toprettyxml(indent="  ").splitlines() if line.strip()]
        if lines and lines[0].startswith("<?xml"):
            lines = lines[1:]
        return "\n".join(lines)
    except Exception:  # noqa: BLE001 - 美化失败就退回原文
        return xml


def render(item: Notification, *, raw_xml: bool = False, indent: str = "  ") -> str:
    lines = [LINE]
    lines.append(f"📩 通知 #{item.id}  [{item.kind or '?'}]")
    lines.append(f"{indent}应用    : {item.display_app}")
    if item.app_id:
        lines.append(f"{indent}AUMID   : {item.app_id}")
    family = item.extra.get("package_family_name")
    if family:
        lines.append(f"{indent}包族名  : {family}")
    if item.arrived_at_iso:
        lines.append(f"{indent}时间    : {item.arrived_at_iso}")
    if item.tag or item.group:
        lines.append(f"{indent}Tag     : {item.tag or '-'}    Group: {item.group or '-'}")

    lines.extend(render_content(item, indent=indent))

    if item.source == PROVENANCE_DATABASE:
        source_label = "通知库 wpndatabase.db（逐字节原始 XML）"
    else:
        xml_source = item.extra.get("xml_source") or ""
        if xml_source == "history":
            source_label = "WinRT History.GetHistory(aumid) → GetXml()（原始 XML）"
        elif xml_source == "reconstructed":
            source_label = "由 UserNotificationListener 的 binding 重建（非原始 XML）"
        else:
            source_label = f"未取到 XML（{xml_source or 'unknown'}）"
    lines.append(f"{indent}XML 来源: {source_label}")

    history_error = item.extra.get("history_error")
    if history_error:
        lines.append(f"{indent}⚠️ 历史查询失败: {history_error}")
    for warning in item.extra.get("warnings") or ():
        lines.append(f"{indent}⚠️ {warning}")

    if item.xml:
        lines.append(f"{indent}XML     :")
        for line in pretty_xml(item.xml, raw=raw_xml).splitlines():
            lines.append(f"{indent}  {line}")
    else:
        lines.append(f"{indent}XML     : (无)")
    return "\n".join(lines)


def render_content(item: Notification, *, indent: str = "  ") -> list:
    """把 :attr:`Notification.content` 里的内容元素逐项打出来。

    没有 XML（例如 WinRT 源重建失败）时退回 ``texts`` 顺序显示，输出不会空。
    """
    content = item.content
    lines: list = []
    if content.has_text:
        for text in content.texts:
            label = {"title": "标题", "body": "正文", "attribution": "归属"}.get(
                text.role, "文本")
            detail = []
            if text.id:
                detail.append(f"id={text.id}")
            if text.placement:
                detail.append(f"placement={text.placement}")
            suffix = ("（%s）" % " ".join(detail)) if detail else ""
            lines.append(f"{indent}{label}    : {text.content}{suffix}")
        # 主 binding 之外的文本（多语言 / 多布局）也列出来，避免「明明有内容却没显示」
        remaining = [(text.content, text.role, text.id) for text in content.texts]
        for text in content.all_texts:
            key = (text.content, text.role, text.id)
            if key in remaining:
                remaining.remove(key)
                continue
            if text.content:
                lines.append(f"{indent}其他文本: {text.content}")
    else:
        if item.title:
            lines.append(f"{indent}标题    : {item.title}")
        if item.body:
            lines.append(f"{indent}正文    : {item.body}")
        for extra in item.other_texts:
            lines.append(f"{indent}文本    : {extra}")

    if content.template:
        lines.append(f"{indent}模板    : {content.template}")
    if content.scenario:
        lines.append(f"{indent}场景    : {content.scenario}（{content.scenario_label}）")
    if content.duration:
        lines.append(f"{indent}时长    : {content.duration}")
    if content.launch:
        lines.append(f"{indent}launch  : {content.launch}")
    if content.display_timestamp:
        lines.append(f"{indent}时间戳  : {content.display_timestamp}")
    if content.header is not None and content.header.label:
        lines.append(f"{indent}头部    : {content.header.label}"
                     f"（arguments={content.header.arguments or '-'}）")
    if content.progress is not None and content.progress.label:
        lines.append(f"{indent}进度    : {content.progress.label}")
    for image in content.images:
        detail = image.placement or "inline"
        location = image.path() or image.src
        lines.append(f"{indent}图片    : [{detail}] {location}"
                     + (f"  alt={image.alt}" if image.alt else ""))
    for action in content.actions:
        kind = "菜单" if action.is_context_menu else "按钮"
        extra = []
        if action.hint_input_id:
            extra.append(f"input={action.hint_input_id}")
        if action.is_system:
            extra.append("系统动作")
        suffix = ("（%s）" % " ".join(extra)) if extra else ""
        lines.append(f"{indent}{kind}    : {action.label} → "
                     f"{action.arguments or '-'} [{action.activation_type}]{suffix}")
    for entry in content.inputs:
        choices = "、".join(choice.content or choice.id for choice in entry.choices)
        detail = f"（{entry.type}）"
        if entry.placeholder:
            detail += f" 提示={entry.placeholder}"
        if entry.default_input:
            detail += f" 默认={entry.default_input}"
        if choices:
            detail += f" 选项={choices}"
        lines.append(f"{indent}输入框  : {entry.id or '-'}{detail}")
    if content.audio is not None:
        sound = "静音" if content.audio.silent else (content.audio.src or "默认提示音")
        lines.append(f"{indent}声音    : {sound}")
    for message in content.parse_errors:
        lines.append(f"{indent}⚠️ 解析 : {message}")
    return lines


def dump_xml(item: Notification, dump_dir: Path) -> None:
    """把逐字节原始 XML 落盘（没有原始字节时退回文本编码）。"""
    if not item.xml and item.xml_bytes is None:
        return
    dump_dir.mkdir(parents=True, exist_ok=True)
    safe_app = "".join(ch if ch.isalnum() or ch in "-._" else "_" for ch in (item.app_id or "unknown"))
    name = f"{item.id}_{item.kind or 'toast'}_{safe_app[:60]}.xml"
    payload = item.xml_bytes if item.xml_bytes is not None else item.xml.encode("utf-8")
    (dump_dir / name).write_bytes(payload)


def emit(item: Notification, args, dump_dir: Path | None) -> None:
    if dump_dir is not None:
        dump_xml(item, dump_dir)
    if args.json:
        print(json.dumps(item.to_dict(), ensure_ascii=False), flush=True)
    else:
        print(render(item, raw_xml=args.raw_xml), flush=True)


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #
def cmd_check(args) -> int:
    report = describe_environment(
        db_path=args.db, bridge_path=args.bridge, probe_access=args.probe
    )
    database = report["database"]
    bridge = report["bridge"]

    print("=" * 62)
    print("catch_notify 环境自检（把整段输出发给维护者即可）")
    print("=" * 62)
    print(f"系统        : {report['platform']}")
    print(f"Python      : {report['python']}  ({report['executable']})")
    print(f"sqlite3     : {report['sqlite_version']}")

    print(f"\n数据库(默认源): {database['path']}")
    print(f"  存在      : {database['exists']}")
    if database["exists"]:
        modified = (
            dt.datetime.fromtimestamp(database["modified"]).isoformat(timespec="seconds")
            if database["modified"] else "-"
        )
        print(f"  大小/时间 : {database['size']} 字节 / {modified}")
        for name, present in (database["sidecars"] or {}).items():
            print(f"  附属文件  : {name} -> {present}")
    if database["readable"]:
        print(f"  可读      : 是（通知条数 {database['count']}，模式 {database['mode']}）")
        print(f"  通知类型  : {database['kinds']}")
    else:
        print(f"  可读      : 否 -> {database['error']}")

    print(f"\nWinRT 源    : {bridge['executable'] or '未安装（默认源不需要它）'}")
    print(f"  native 目录: {bridge['native_dir'] or '-'}")
    print(f"  编译脚本  : {bridge['build_script'] or '-'}")
    if bridge["access"] is not None:
        access = bridge["access"]
        print(f"  访问授权  : 允许={access.get('allowed')} 状态={access.get('status')}")
    elif bridge["available"] and not args.probe:
        print("  访问授权  : 未查询（加 --probe 可查一次，可能弹出系统授权对话框）")

    switches = settings.describe(db_path=args.db)
    master = switches.get("master_switch") or {}
    suffix = "" if master.get("error") is None else f"（读取失败：{master['error']}）"
    print(f"\n系统通知总开关: {master.get('enabled')}{suffix}")
    if switches.get("retention"):
        print(f"  通知中心上限: {switches['retention']}")
    app_count = len(switches.get("apps") or {})
    muted_count = len(switches.get("muted_apps") or [])
    print(f"  单应用开关  : 已记录 {app_count} 个应用，其中关闭通知的 {muted_count} 个")
    for message in settings.warnings(switches):
        print(f"  ⚠️ {message}")

    print(f"\n可用数据源  : {report['available']}")
    verdict = "数据库源可用（不需要 SDK / 不需要授权 / 不需要编译）" \
        if report["available"]["db"] else "数据库源不可用，见上面的失败原因"
    print(f"结论        : {verdict}")
    return 0


def cmd_request(args) -> int:
    bridge = WinRTBridge(args.bridge)
    if not bridge.available:
        print("❌ 找不到 NotificationBridge.exe。")
        print("   数据库源（默认）不需要它；想用 WinRT 源请先 --build，或用 --bridge PATH 指定。")
        return 1

    print("正在申请通知访问授权…（如弹出系统对话框请点「允许」）")
    allowed, status = bridge.request_access()
    print(f"授权状态：{status}（允许={allowed}）")
    if not allowed:
        print("  请到 设置 → 隐私和安全性 → 通知 里允许本程序访问通知后重试。")
        return 3
    return 0


def cmd_build(args) -> int:
    bridge = WinRTBridge(args.bridge, native_dir=args.native_dir)
    if bridge.build_script is None:
        print("❌ 找不到 native/build.ps1。")
        print("   把 native/（NotificationBridge.cs + build.ps1）放到 "
              f"{Path(__file__).resolve().parent} 下，或用 --native-dir 指定目录。")
        return 1

    print(f"正在编译（{bridge.build_script}）…")
    try:
        code = bridge.build()
    except (BridgeUnavailable, OSError) as exc:
        print(f"❌ 编译失败：{exc}")
        return 1
    if code != 0:
        print(f"❌ 编译失败，退出码 {code}")
        return code
    print("✅ 编译成功。" if bridge.available else "✅ 编译完成，但没找到产物，请检查 build.ps1 的输出路径。")
    return 0


def cmd_once(args) -> int:
    items = read_notifications(
        args.source,
        kinds=_kinds(args),
        db_path=args.db,
        bridge_path=args.bridge,
        limit=args.limit,
    )
    dump_dir = Path(args.dump) if args.dump else None
    for item in items:
        emit(item, args, dump_dir)

    if args.activate is not None or args.activate_body:
        return _activate_from_args(items, args)
    return 0


def _parse_input_values(pairs) -> dict:
    """``--input id=文本`` → ``{id: 文本}``。"""
    values: dict = {}
    for pair in pairs or ():
        key, separator, value = str(pair).partition("=")
        if not separator:
            print(f"⚠️ 忽略无法解析的 --input：{pair}（应为 输入框id=文本）")
            continue
        values[key.strip()] = value
    return values


def _activate_from_args(items, args) -> int:
    """``--activate N`` / ``--activate-body``：把最新一条通知的动作发回应用。

    激活是**真的会通知到目标应用**的操作（可能把它拉起来），所以这里先打印
    「打算怎么做」，``--plan-only`` 可以只看计划不执行。
    """
    from .activation import build_plan, perform

    if not items:
        print("❌ 没有可用的通知，无法激活。")
        return 1

    item = items[-1]
    content = item.content
    action = None
    if args.activate is not None:
        buttons = content.button_actions
        if not buttons:
            print("❌ 最新一条通知没有可点击按钮。")
            return 2
        if not 0 <= args.activate < len(buttons):
            print(f"❌ 按钮下标越界：有效范围 0..{len(buttons) - 1}")
            return 2
        action = buttons[args.activate]

    inputs = _parse_input_values(args.inputs)
    plan = build_plan(item, action=action, inputs=inputs,
                      target="action" if action is not None else "body")
    print(f"激活计划: {plan.describe()}")
    if args.plan_only:
        print("（--plan-only：只显示计划，未真正执行）")
        return 0

    result = perform(plan, item=item)
    print(("✅ " if result.ok else "❌ ") + result.message
          + (f"  [method={result.method}]" if result.method else ""))
    return 0 if result.ok else 1


def _print_switch_warnings(db_path) -> None:
    """启动监听前提示「其实一条都收不到」的常见原因（提示失败不影响主流程）。"""
    try:
        report = settings.describe(db_path=db_path)
    except Exception:  # noqa: BLE001 - 诊断不该拦截主流程
        return
    for message in settings.warnings(report):
        if message.startswith("读取开关时出错"):
            continue
        print(f"⚠️ {message}")


def cmd_watch(args) -> int:
    print(f"✅ 监听已启动 | 数据源: {args.source} | 间隔 {args.interval}ms")
    if args.source in (SOURCE_AUTO, SOURCE_DATABASE):
        print(f"   {default_database_path()}")
        _print_switch_warnings(args.db)
    print("   说明：XML 直接来自通知平台的原始 payload。Ctrl+C 退出。\n")
    dump_dir = Path(args.dump) if args.dump else None
    try:
        for item in watch_notifications(
            args.source,
            kinds=_kinds(args),
            interval=max(args.interval, 50) / 1000.0,
            skip_existing=args.skip_existing,
            db_path=args.db,
            bridge_path=args.bridge,
        ):
            emit(item, args, dump_dir)
    except KeyboardInterrupt:
        print("\n👋 已停止监听。")
    return 0


def _kinds(args):
    return None if args.all_kinds else ("toast",)


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m features.catch_notify",
        description="抓取 Windows 通知并获取完整通知 XML",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="默认走通知库（完整原始 XML，无需授权）；--source winrt 走 Windows 原生 WinRT/COM。",
    )
    parser.add_argument("--source", choices=(SOURCE_AUTO, SOURCE_DATABASE, SOURCE_WINRT),
                        default=SOURCE_AUTO,
                        help="数据源：auto=数据库优先（默认），db=仅通知库，winrt=仅 WinRT/COM")
    parser.add_argument("--watch", action="store_true", help="持续监听（默认行为）")
    parser.add_argument("--once", action="store_true", help="只打印当前通知后退出")
    parser.add_argument("--check", action="store_true", help="打印环境自检报告（排障用）")
    parser.add_argument("--probe", action="store_true", help="自检时顺便查询 WinRT 授权状态")
    parser.add_argument("--request", action="store_true", help="申请 WinRT 通知访问授权")
    parser.add_argument("--build", action="store_true", help="编译 native/NotificationBridge.exe")
    parser.add_argument("--json", action="store_true", help="输出 NDJSON")
    parser.add_argument("--raw-xml", action="store_true", help="不美化，直接输出原始 XML")
    parser.add_argument("--dump", metavar="DIR", help="把逐字节原始 XML 写入该目录")
    parser.add_argument("--skip-existing", action="store_true", help="只报启动后新出现的通知（数据库源）")
    parser.add_argument("--all-kinds", action="store_true", help="连 tile / badge 一起输出")
    parser.add_argument("--limit", type=int, default=None, metavar="N", help="--once 时最多取最新的 N 条")
    parser.add_argument("--activate", type=int, default=None, metavar="N",
                        help="--once 时把最新一条通知的第 N 个按钮发回应用（0 起）")
    parser.add_argument("--activate-body", action="store_true",
                        help="--once 时激活最新一条通知本体（相当于点击通知）")
    parser.add_argument("--input", dest="inputs", action="append", metavar="ID=文本",
                        help="配合 --activate 使用：按钮需要的输入框内容，可重复")
    parser.add_argument("--plan-only", action="store_true",
                        help="只打印激活计划，不真的调用应用")
    parser.add_argument("--interval", type=int, default=800, metavar="MS", help="轮询间隔毫秒，默认 800")
    parser.add_argument("--db", metavar="PATH", help="指定 wpndatabase.db 路径")
    parser.add_argument("--bridge", metavar="PATH", help="指定 NotificationBridge.exe 路径")
    parser.add_argument("--native-dir", metavar="DIR", help="指定含 build.ps1 / exe 的 native 目录")
    parser.add_argument("--log-level", default="INFO",
                        choices=("DEBUG", "INFO", "WARNING", "ERROR"), help="日志级别，默认 INFO")
    return parser


def main(argv=None) -> int:
    # Windows 控制台默认可能是 GBK，XML 里有非 ASCII 时容易炸，统一成 UTF-8。
    # line_buffering：输出被重定向/管道时 Python 默认块缓冲，监听模式的启动横幅
    # 会迟迟不出现，所以这里强制按行刷新。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass

    args = build_parser().parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level, logging.INFO),
                        format="%(message)s", stream=sys.stderr)

    try:
        if args.build:
            return cmd_build(args)
        if args.check:
            return cmd_check(args)
        if args.request:
            return cmd_request(args)
        if args.once:
            return cmd_once(args)
        return cmd_watch(args)
    except AccessDenied as exc:
        print(f"❌ {exc}")
        return 3
    except CatchNotifyError as exc:
        print(f"❌ {exc}")
        print("   提示：--check 可以做一次完整环境自检。")
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
