"""手动验证：看真实通知被解析成了什么内容元素，并把按钮真的发回应用。

    python tests/manual_toast_activate.py                # 只观察：列出当前通知与激活计划
    python tests/manual_toast_activate.py --limit 5      # 只看最新 5 条
    python tests/manual_toast_activate.py --source winrt # 走 WinRT 桥（需授权 + exe）
    python tests/manual_toast_activate.py --activate 0   # 激活最新一条的第 0 个按钮（会确认）
    python tests/manual_toast_activate.py --activate-body
    python tests/manual_toast_activate.py --activate 0 --input reply=好的 --yes

默认**只读**：读通知库 / WinRT，打印内容元素与「如果点下去会怎么做」的计划。
只有显式给出 ``--activate`` / ``--activate-body`` 才会真的通知目标应用（可能把应用
拉起来），而且默认要按一次回车确认；``--yes`` 跳过确认，``--plan-only`` 只打印计划。

这个脚本会与真实系统交互，请自己判断要不要跑。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.catch_notify import (  # noqa: E402 - 路径要先就位
    SOURCE_AUTO,
    SOURCE_DATABASE,
    SOURCE_WINRT,
    build_plan,
    perform,
    read_notifications,
)

LINE = "─" * 72


def parse_inputs(pairs) -> dict:
    values: dict = {}
    for pair in pairs or ():
        key, separator, value = str(pair).partition("=")
        if not separator:
            print(f"⚠️ 忽略无法解析的 --input：{pair}（应为 输入框id=文本）")
            continue
        values[key.strip()] = value
    return values


def show(item, index: int) -> None:
    content = item.content
    print(LINE)
    print(f"[{index}] {item.display_app} | kind={item.kind} | template={content.template or '-'}"
          f" | scenario={content.scenario or '-'}")
    if item.app_id:
        print(f"     AUMID  : {item.app_id}")
    print(f"     文本   : {[text.to_dict() for text in content.texts]}")
    print(f"     归属   : {content.attribution or '-'}")
    for image in content.images:
        print(f"     图片   : [{image.placement or 'inline'}] {image.path() or image.src}")
    if content.header is not None:
        print(f"     header : {content.header.label}（{content.header.arguments or '-'}）")
    if content.progress is not None:
        print(f"     progress: {content.progress.label}")
    if content.audio is not None:
        print(f"     audio  : {'静音' if content.audio.silent else (content.audio.src or '默认')}")
    for entry in content.inputs:
        print(f"     输入框 : {entry.id} ({entry.type}) 默认={entry.default_input!r}")
    for position, action in enumerate(content.actions):
        marker = "菜单" if action.is_context_menu else "按钮"
        print(f"     {marker}   : #{position} {action.label} → {action.arguments or '-'}"
              f" [{action.activation_type}]")
    if content.parse_errors:
        print(f"     解析提示: {content.parse_errors}")
    for position, action in enumerate(content.button_actions):
        print(f"     计划#{position}: {build_plan(item, action=action).describe()}")
    print(f"     计划(本体): {build_plan(item).describe()}")


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="真实通知的内容元素体检 + 按钮激活")
    parser.add_argument("--source", choices=(SOURCE_AUTO, SOURCE_DATABASE, SOURCE_WINRT),
                        default=SOURCE_AUTO, help="数据源（默认 auto）")
    parser.add_argument("--limit", type=int, default=8, help="最多看最新几条，默认 8")
    parser.add_argument("--activate", type=int, default=None, metavar="N",
                        help="激活最新一条通知的第 N 个按钮")
    parser.add_argument("--activate-body", action="store_true", help="激活最新一条通知本体")
    parser.add_argument("--input", dest="inputs", action="append", metavar="ID=文本",
                        help="按钮需要的输入框内容，可重复")
    parser.add_argument("--plan-only", action="store_true", help="只打印计划，不执行")
    parser.add_argument("--yes", action="store_true", help="不询问确认")
    args = parser.parse_args(argv)

    items = read_notifications(args.source)
    if not items:
        print("当前没有通知可看（通知中心是空的，或者数据源不可用）。")
        return 0
    items = items[-max(args.limit, 1):]
    for index, item in enumerate(items):
        show(item, index)

    if args.activate is None and not args.activate_body:
        print(LINE)
        print("以上只是观察结果。要真的把动作发回应用，加 --activate N 或 --activate-body。")
        return 0

    newest = items[-1]
    content = newest.content
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

    inputs = parse_inputs(args.inputs)
    plan = build_plan(newest, action=action, inputs=inputs,
                      target="action" if action is not None else "body")
    print(LINE)
    print(f"准备执行：{plan.describe()}")
    if plan.request.inputs:
        print(f"          输入内容：{dict(plan.request.inputs)}")
    if args.plan_only:
        print("（--plan-only：只显示计划，未执行）")
        return 0
    if not args.yes:
        answer = input("这会把动作发给目标应用（可能把它拉起来），继续？[y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("已取消。")
            return 0

    result = perform(plan, item=newest)
    print(("✅ " if result.ok else "❌ ") + result.message
          + (f"  [method={result.method}]" if result.method else ""))
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
