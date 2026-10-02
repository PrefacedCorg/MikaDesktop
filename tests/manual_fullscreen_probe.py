"""手动验证（不修改系统状态）：在真实桌面上观察全屏判定。

用法::

    python tests/manual_fullscreen_probe.py [观察秒数，默认 30]

脚本只做「看」这件事：每轮读一次前台窗口（以及它的 root owner），打印窗口类、
进程、窗口矩形、所在显示器，以及判定结论与**理由**。它不会注销 AppBar、不会
隐藏任何窗口，所以可以放心在正常使用电脑时跑。

想看「检测到全屏」的样子：在观察期间把浏览器按 F11 全屏，或全屏播放一段视频，
结论应当立刻从「不会让位」变成「会让位」；按 F11 退出后再变回来。
"""

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.fullscreen_watch import (  # noqa: E402
    SYSTEM_PROCESS_NAMES,
    DEFAULT_TOLERANCE,
    collect_candidates,
    covers_monitor,
    is_system_process,
    is_system_window_class,
    normalize_process_name,
    select_fullscreen_window,
)
from core.process_manager import ProcessManager  # noqa: E402


def reject_reason(snapshot):
    """给出「这个候选为什么没有触发让位」的人话解释（与判定顺序保持一致）。"""
    if not (snapshot.foreground or snapshot.root_owner):
        return "不是前台窗口，也不是前台窗口的 root owner"
    if not snapshot.visible:
        return "窗口不可见"
    if snapshot.iconic:
        return "窗口已最小化"
    if snapshot.tool_window:
        return "工具窗口（WS_EX_TOOLWINDOW）"
    if is_system_window_class(snapshot.class_name):
        return f"系统外壳窗口类（{snapshot.class_name}）"
    if is_system_process(snapshot.process_name, snapshot.exe_path):
        return f"Windows 系统组件（{normalize_process_name(snapshot.process_name) or snapshot.exe_path}）"
    if not covers_monitor(snapshot.rect, snapshot.monitor_rect, DEFAULT_TOLERANCE):
        return (f"没有铺满它所在的显示器（窗口 {snapshot.rect} "
                f"vs 显示器 {snapshot.monitor_rect}）")
    return "通过全部判定"


def describe(snapshot):
    return (f"hwnd=0x{snapshot.hwnd:X} class={snapshot.class_name!r} "
            f"proc={snapshot.process_name or '?'} title={snapshot.title!r}\n"
            f"      窗口={snapshot.rect} 显示器={snapshot.monitor_rect} "
            f"前台={snapshot.foreground} root_owner={snapshot.root_owner} "
            f"可见={snapshot.visible} 最小化={snapshot.iconic} 工具窗={snapshot.tool_window}")


def main():
    seconds = 30.0
    if len(sys.argv) > 1:
        try:
            seconds = max(float(sys.argv[1]), 1.0)
        except ValueError:
            pass

    print(f"共 {len(SYSTEM_PROCESS_NAMES)} 个系统组件被列入永不触发让位的名单，"
          f"覆盖判定容差 {DEFAULT_TOLERANCE}px")
    print(f"观察 {seconds:g} 秒（Ctrl+C 可提前结束）\n")

    manager = ProcessManager()
    last_signature = None
    polls = 0
    fullscreen_polls = 0
    deadline = time.time() + seconds

    try:
        while time.time() < deadline:
            polls += 1
            try:
                snapshots = collect_candidates(manager, ignored_hwnds=[])
            except Exception as exc:  # noqa: BLE001 - 探针脚本，出错也要继续观察
                print(f"  !! 采集候选窗口失败: {type(exc).__name__}: {exc}")
                snapshots = []

            chosen = select_fullscreen_window(snapshots)
            if chosen is not None:
                fullscreen_polls += 1

            signature = (chosen.hwnd if chosen else None,
                         tuple(s.hwnd for s in snapshots))
            if signature != last_signature:
                last_signature = signature
                stamp = time.strftime("%H:%M:%S")
                if chosen is not None:
                    print(f"[{stamp}] 会触发让位（注销 AppBar + 隐藏 dock）")
                    print(f"      {chosen.describe()}")
                    print(f"      {describe(chosen)}")
                else:
                    print(f"[{stamp}] 不会让位")
                    if not snapshots:
                        print("      没有候选窗口（前台窗口为空：可能是桌面或锁屏）")
                    for snapshot in snapshots:
                        print(f"      {snapshot.describe()}")
                        print(f"      {describe(snapshot)}")
                        print(f"      → {reject_reason(snapshot)}")
                print()

            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\n（提前结束）")

    print(f"完成：共检测 {polls} 轮，其中 {fullscreen_polls} 轮判定为全屏。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
