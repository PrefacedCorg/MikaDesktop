"""后台进程扫描线程。

为什么要单独一个线程
--------------------
Dock 需要每 500ms 知道「哪些应用正在运行」。这件事的成本远超想象：

* ``EnumWindows`` 枚举全系统窗口；
* ``psutil.process_iter`` 遍历全系统进程（每个都要解析 exe 路径）；
* 对每个新出现的进程做一次 GDI 图标提取（ExtractIconEx + 绘制 + PIL 合成）。

放在 GUI 线程上按 500ms 的周期跑，就会变成周期性的界面卡顿。这里把整段扫描挪到
工作线程，结果通过信号投递回 GUI 线程；界面更新一律发生在 GUI 线程里。

线程模型
--------
``scan_finished`` / ``scan_failed`` 通过 Qt 信号跨线程投递，Qt 会自动排队到接收者
所在线程的事件循环，所以 Dock 那边的槽函数不需要额外加锁。

``stop()`` / ``quit()`` 都只置位停止事件，循环会在 ``interval`` 内退出 —— 不依赖
``QThread.quit()`` 默认的「退出事件循环」语义（本线程跑的是自己的循环，没有
``exec()``），因此 :class:`core.thread_mgr.manager.ThreadManager` 的 ``stop()`` /
``stop_all()`` 也能正常停掉它。
"""

from __future__ import annotations

import threading

from PySide6.QtCore import QThread, Signal

__all__ = ["ProcessScanWorker", "DEFAULT_INTERVAL_MS"]

#: 默认扫描间隔（毫秒），与原来 QTimer 的 500ms 保持一致
DEFAULT_INTERVAL_MS = 500


class ProcessScanWorker(QThread):
    """周期性扫描运行中的进程，扫描结果通过 ``scan_finished`` 发回 GUI 线程。"""

    #: dict: 规范化前的 exe 路径 -> {'name', 'path', 'icon'}
    scan_finished = Signal(dict)
    #: 扫描过程中的异常描述（只上报，不中断循环）
    scan_failed = Signal(str)

    def __init__(self, process_manager, interval_ms: int = DEFAULT_INTERVAL_MS, parent=None):
        super().__init__(parent)
        self._process_manager = process_manager
        self._interval = max(int(interval_ms), 50) / 1000.0
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()

    # -- 生命周期 ---------------------------------------------------------- #
    def request_scan(self) -> None:
        """要求立刻再扫一次，不必等下一个周期。"""
        self._wake_event.set()

    def stop(self, wait_ms: int = 3000) -> None:
        """请求停止并（默认）等待线程退出；可重复调用。"""
        self._stop_event.set()
        self._wake_event.set()
        if self.isRunning():
            self.wait(wait_ms)

    def quit(self) -> None:  # noqa: D102 - 覆盖 QThread.quit，配合 ThreadManager.stop()
        # 本线程跑的是自己的 while 循环而不是事件循环，QThread.quit() 对它无效。
        # ThreadManager.stop() 会调用 quit() 再 wait()，这里改成置位停止事件，
        # 否则 wait() 必然超时并进而 terminate() 强杀线程。
        self._stop_event.set()
        self._wake_event.set()

    # -- 线程主体 ---------------------------------------------------------- #
    def run(self) -> None:  # noqa: D102 - QThread 入口
        while not self._stop_event.is_set():
            try:
                # 这里刻意传 skip_known=False 且不传已知应用列表：worker 因此不依赖
                # 任何 GUI 线程状态，拿到的就是「全系统有可见窗口的进程」，交由 GUI
                # 线程再去和 pinned/apps 列表比对。
                result = self._process_manager.get_running_processes([], skip_known=False)
                if not self._stop_event.is_set():
                    self.scan_finished.emit(result)
            except Exception as exc:  # noqa: BLE001 - 后台线程异常必须上报，否则静默失效
                if not self._stop_event.is_set():
                    self.scan_failed.emit(f"{type(exc).__name__}: {exc}")

            # 睡到下一轮，或被 request_scan() 提前唤醒
            self._wake_event.wait(self._interval)
            self._wake_event.clear()
