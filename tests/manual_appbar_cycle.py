"""手动验证（会短暂改动系统工作区）：AppBar 注册 → 注销 → 重新注册。

用法::

    python tests/manual_appbar_cycle.py

验证的是"全屏让位"最关键的一环：把程序栏注册为 AppBar 会把系统工作区底部抬高，
注销后立刻恢复，并且**可以再次注册**（全屏程序结束后要把 AppBar 装回去，这条
路径必须可靠，否则工作区就再也回不来了）。

脚本会在自己的进程里临时注册一个底部 AppBar（几秒），期间最大化窗口的可用区域
会略微变小；无论如何结束都会注销。AppBar 的保留区是跟着宿主窗口/进程走的，进程
一退出系统就会回收，所以即使被强制中断也不会把工作区永久改坏。
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import core.sys32 as sys32  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print(("[PASS] " if ok else "[FAIL] ") + name + (("  " + str(detail)) if detail else ""))
    if not ok:
        failures.append(name)


def work_area():
    """当前主显示器工作区 (left, top, right, bottom)。"""
    return sys32.refresh_metrics()["work"]


def main():
    sys32.refresh_metrics()
    original = work_area()
    print(f"物理分辨率 {(sys32.REAL_SCREEN_WIDTH, sys32.REAL_SCREEN_HEIGHT)}，"
          f"当前工作区 {original}")

    dock_height = 100
    dock_top = original[3] - dock_height
    if dock_top <= original[1]:
        print("工作区太小，无法进行保留区测试")
        return 1

    check("初始状态未注册 AppBar", sys32.is_appbar_registered() is False)

    try:
        # ---- 1) 注册：工作区底部应当被抬高 ----
        sys32.set_appbar_bottom(dock_top)
        registered = work_area()
        check("注册后 is_appbar_registered() 为真", sys32.is_appbar_registered() is True)
        check("注册后工作区底部被抬高", registered[3] < original[3],
              f"{original} -> {registered}")
        check("请求的保留高度基本生效", abs(registered[3] - dock_top) <= 8,
              f"请求 dock_top={dock_top}，实际工作区底部={registered[3]}")

        # ---- 2) 注销：工作区应当还原 ----
        sys32.remove_appbar()
        removed = work_area()
        check("注销后 is_appbar_registered() 为假", sys32.is_appbar_registered() is False)
        check("注销后工作区底部还原", removed[3] == original[3], f"{removed} vs {original}")

        # ---- 3) 重新注册：全屏结束后恢复 AppBar 走的正是这条路径 ----
        sys32.set_appbar_bottom(dock_top)
        again = work_area()
        check("注销后可以再次注册", sys32.is_appbar_registered() is True)
        check("再次注册得到同样的保留高度", again[3] == registered[3],
              f"{again} vs {registered}")

        # ---- 4) 已注册状态下重复注册：ABM_NEW 会返回 0，不能被当成"未注册" ----
        sys32.set_appbar_bottom(dock_top + 40)
        check("已注册时重复注册仍报告已注册", sys32.is_appbar_registered() is True)
        re_polled = work_area()
        check("重复注册会按新位置调整保留区", re_polled[3] == dock_top + 40,
              f"{re_polled} 期望底部={dock_top + 40}")

        # ---- 5) 重复注销应当是安全的空操作 ----
        sys32.remove_appbar()
        final = work_area()
        check("注销后工作区恢复", final == original, f"{final} vs {original}")
        sys32.remove_appbar()
        check("第二次注销依然安全", work_area() == original)
    finally:
        # 无论如何都要把工作区还回去
        try:
            sys32.remove_appbar()
        except Exception as exc:  # noqa: BLE001
            print(f"清理 AppBar 时出错: {exc}")
        restored = work_area()
        check("收尾后工作区已恢复", restored == original, f"{restored} vs {original}")

    print()
    print("FAILED: %d" % len(failures))
    for name in failures:
        print("  - " + name)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
