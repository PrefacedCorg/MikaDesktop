"""P2 线程模型统一验证：XHT 通知监听线程登记进 ThreadManager 并可反复启停。"""

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

failures = []


def check(name, ok, detail=""):
    print(("[PASS] " if ok else "[FAIL] ") + name + (("  " + str(detail)) if detail else ""))
    if not ok:
        failures.append(name)


from PySide6.QtWidgets import QApplication, QWidget

app = QApplication.instance() or QApplication(sys.argv)

import core.thread_mgr.manager as tm
from core.text_utils import format_message
from features.XHT.Lib.Notify import NotificationBadge, NotificationPresenter


class FakeWindow(QWidget):
    """NotificationPresenter 只用到这几个窗口能力。"""

    def __init__(self):
        super().__init__()
        self.is_hidden = False

    def set_time_visible(self, visible):
        pass

    def ShowWindow(self):
        pass

    def HideWindow(self):
        pass

    def AutoSetSize(self):
        pass


def pump(seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)


# ---------- 共享格式化实现 ----------
check("format_message 处理 printf 风格", format_message("a %s", ("b",)) == "a b")
check("format_message 处理 {} 风格", format_message("a {}", ("b",)) == "a b")
check("format_message 无参原样返回", format_message("plain", ()) == "plain")

# ---------- 线程登记 ----------
manager = tm.ThreadManager()
presenter = NotificationPresenter(
    FakeWindow(), NotificationBadge(None), {"notify_enabled": True}, None,
    thread_manager=manager,
)

presenter.start()
pump(0.5)
check("通知监听线程已登记到 ThreadManager", presenter._watcher_thread_id is not None)

info = manager.get_thread_info(presenter._watcher_thread_id) if presenter._watcher_thread_id else None
check("ThreadManager 中状态为 RUNNING",
      info is not None and info.state == tm.ThreadState.RUNNING,
      info.state if info else "无记录")
check("监视线程确实在运行", presenter.watcher.isRunning())
check("活跃线程计数为 1", manager.get_active_count() == 1,
      "active=%s" % manager.get_active_count())

# ---------- 停止 ----------
presenter.stop()
check("stop() 后监视线程已退出", not presenter.watcher.isRunning())

info = manager.get_thread_info(presenter._watcher_thread_id)
check("ThreadManager 中状态翻为 STOPPED",
      info is not None and info.state == tm.ThreadState.STOPPED,
      info.state if info else "无记录")
check("活跃线程计数回到 0", manager.get_active_count() == 0,
      "active=%s" % manager.get_active_count())

presenter.stop()  # 幂等，不该抛异常
check("重复 stop() 不抛异常", True)

# ---------- 重启 ----------
presenter.start()
pump(0.5)
info = manager.get_thread_info(presenter._watcher_thread_id)
check("重启后监视线程再次运行", presenter.watcher.isRunning())
check("重启后 ThreadManager 状态为 RUNNING",
      info is not None and info.state == tm.ThreadState.RUNNING,
      info.state if info else "无记录")

# ---------- stop_all 统一收尾 ----------
manager.stop_all()
pump(0.3)
check("stop_all() 之后监视线程已停止", not presenter.watcher.isRunning())

print()
print("FAILED: %d" % len(failures))
for name in failures:
    print("  - " + name)
sys.exit(1 if failures else 0)
