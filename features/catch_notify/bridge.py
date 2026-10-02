"""数据源二：Windows 原生 WinRT/COM 桥接程序（可选）。

为什么保留它
    * ``UserNotificationListener`` 是官方的**实时**通知 API；
    * 官方取原始 XML 的路径是
      ``ToastNotificationManager.History.GetHistory(aumid)`` → ``Content.GetXml()``。

代价（所以它是可选的、非默认）
    * 「访问通知」授权是按**可执行文件**记录的：新编译出的 exe 默认是 Denied，
      用户必须去 设置 → 隐私和安全性 → 通知 里放行；
    * 需要 ``NotificationBridge.exe``。运行它只依赖 .NET Framework 4.x（Win10
      1903+/Win11 系统自带）；但**编译**它需要 Windows SDK 的 ``Windows.winmd``。

资源查找顺序（``NOTIFICATION_BRIDGE`` / ``NOTIFICATION_BRIDGE_DIR`` 可覆盖）：
    1. 调用方显式传入的路径
    2. 环境变量 ``NOTIFICATION_BRIDGE``
    3. ``<包目录>/native/NotificationBridge.exe``
    4. ``<冻结后可执行文件目录>/native/NotificationBridge.exe``
"""

from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Iterator

from .errors import BridgeUnavailable
from .records import KIND_TOAST, PROVENANCE_WINRT, Notification, parse_datetime

logger = logging.getLogger(__name__)

__all__ = [
    "WinRTBridge",
    "locate_executable",
    "locate_native_dir",
    "EXECUTABLE_NAME",
    "BUILD_SCRIPT_NAME",
]

EXECUTABLE_NAME = "NotificationBridge.exe"
BUILD_SCRIPT_NAME = "build.ps1"
ENV_EXECUTABLE = "NOTIFICATION_BRIDGE"
ENV_NATIVE_DIR = "NOTIFICATION_BRIDGE_DIR"

#: 消息队列里的结束哨兵
_EOF = object()

_SETUP_HINT = (
    "找不到 %s。\n"
    "  本库默认用的是数据库源，不需要这个 exe。\n"
    "  想启用 WinRT/COM 源，把编译好的 exe 放到 <包目录>/native/ 下，"
    "或用 %s 环境变量 / bridge_path 参数指定路径。\n"
    "  exe 由 native/build.ps1 编译（编译机需要 Windows SDK，运行机不需要）。"
    % (EXECUTABLE_NAME, ENV_EXECUTABLE)
)


def _resource_roots() -> list:
    """资源搜索根：包目录 + 冻结后的可执行文件目录。"""
    roots = []
    if getattr(sys, "frozen", False):  # cx_Freeze / PyInstaller
        try:
            roots.append(Path(sys.executable).resolve().parent)
        except (OSError, ValueError):  # pragma: no cover
            pass
    unpacked = getattr(sys, "_MEIPASS", None)  # PyInstaller onefile 解包目录
    if unpacked:
        roots.append(Path(unpacked))
    roots.append(Path(__file__).resolve().parent)

    unique = []
    for root in roots:
        if root not in unique:
            unique.append(root)
    return unique


def locate_native_dir(explicit: Any = None) -> Path | None:
    """找到存放桥接程序的 ``native/`` 目录。"""
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    env = os.environ.get(ENV_NATIVE_DIR)
    if env:
        candidates.append(Path(env))
    for root in _resource_roots():
        candidates.append(root / "native")
        candidates.append(root)

    for candidate in candidates:
        if not candidate.is_dir():
            continue
        if (candidate / EXECUTABLE_NAME).is_file() or (candidate / BUILD_SCRIPT_NAME).is_file():
            return candidate
    return None


def locate_executable(explicit: Any = None) -> Path | None:
    """找到 ``NotificationBridge.exe``，找不到返回 None。"""
    if explicit:
        path = Path(explicit)
        if path.is_file():
            return path
        # 传了目录就往下找一层
        nested = path / EXECUTABLE_NAME
        if nested.is_file():
            return nested
        return None

    env = os.environ.get(ENV_EXECUTABLE)
    if env and Path(env).is_file():
        return Path(env)

    for root in _resource_roots():
        for candidate in (root / "native" / EXECUTABLE_NAME, root / EXECUTABLE_NAME):
            if candidate.is_file():
                return candidate
    return None


class WinRTBridge:
    """调用 ``NotificationBridge.exe`` 的薄封装。

    ``watch()`` / ``snapshot()`` / ``request_access()`` 都是阻塞式生成器或调用，
    放进工作线程里用（GUI 场景不要在主线程直接 watch）。
    """

    def __init__(self, executable: Any = None, *, native_dir: Any = None):
        self.native_dir = locate_native_dir(native_dir)
        self.executable = locate_executable(executable)

    # -- 状态 -------------------------------------------------------------- #
    @property
    def available(self) -> bool:
        return self.executable is not None

    @property
    def build_script(self) -> Path | None:
        if self.native_dir is None:
            return None
        script = self.native_dir / BUILD_SCRIPT_NAME
        return script if script.is_file() else None

    def require(self) -> Path:
        if self.executable is None:
            raise BridgeUnavailable(_SETUP_HINT)
        return self.executable

    # -- 授权 -------------------------------------------------------------- #
    def request_access(self, *, timeout: float | None = 60.0) -> tuple:
        """申请/查询通知访问授权，返回 ``(是否已授权, 状态字符串)``。

        状态可能是 ``Allowed`` / ``Denied`` / ``Unspecified`` / ``timeout``。
        未授权时系统可能弹出授权对话框，所以这里带超时，避免界面卡死。
        """
        stop_event = threading.Event()
        timer = self._start_timer(timeout, stop_event)
        try:
            for payload in self._stream("request", stop_event=stop_event):
                if payload.get("type") == "access":
                    return bool(payload.get("allowed")), str(payload.get("status") or "")
        finally:
            self._cancel_timer(timer)
        return False, "timeout"

    # -- 读取 -------------------------------------------------------------- #
    def snapshot(self, *, timeout: float | None = 60.0) -> list:
        """取当前全部通知（含 History 返回的原始 XML）。"""
        stop_event = threading.Event()
        timer = self._start_timer(timeout, stop_event)
        result = []
        try:
            for payload in self._stream("snapshot", stop_event=stop_event):
                if payload.get("type") == "notification":
                    result.append(self._to_notification(payload))
        finally:
            self._cancel_timer(timer)
        return result

    def watch(self, interval_ms: int = 800, *, stop_event: Any = None) -> Iterator[Notification]:
        """持续产出新通知。``stop_event`` 可随时结束循环。"""
        for payload in self._stream("watch", str(max(int(interval_ms), 100)), stop_event=stop_event):
            if payload.get("type") == "notification":
                yield self._to_notification(payload)

    # -- 编译 -------------------------------------------------------------- #
    def build(self, *, timeout: float = 900.0) -> int:
        """调用 ``native/build.ps1`` 现场编译 exe，返回退出码。

        只有编译机需要 Windows SDK；已经拿到 exe 的用户不需要调它。
        """
        script = self.build_script
        if script is None:
            raise BridgeUnavailable(_SETUP_HINT)
        shell = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
        completed = subprocess.run(
            [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            cwd=str(script.parent),
            timeout=timeout,
        )
        return int(completed.returncode)

    # -- 内部 -------------------------------------------------------------- #
    @staticmethod
    def _start_timer(timeout: float | None, stop_event: threading.Event):
        if not timeout or timeout <= 0:
            return None
        timer = threading.Timer(timeout, stop_event.set)
        timer.daemon = True
        timer.start()
        return timer

    @staticmethod
    def _cancel_timer(timer) -> None:
        if timer is not None:
            timer.cancel()

    @staticmethod
    def _pump(stream, messages: "queue.Queue") -> None:
        """把子进程的 NDJSON 逐行丢进队列（读操作不能阻塞消费端）。"""
        try:
            for raw in iter(stream.readline, b""):
                text = raw.decode("utf-8", "replace").strip()
                if not text:
                    continue
                try:
                    messages.put(json.loads(text))
                except json.JSONDecodeError:
                    logger.debug("桥接程序输出了非 JSON 行：%s", text[:200])
        except (OSError, ValueError) as exc:  # pragma: no cover
            logger.debug("读取桥接程序输出失败：%s", exc)
        finally:
            try:
                stream.close()
            except OSError:
                pass
            messages.put(_EOF)

    @staticmethod
    def _drain_stderr(stream) -> None:
        try:
            for raw in iter(stream.readline, b""):
                text = raw.decode("utf-8", "replace").rstrip()
                if text:
                    logger.debug("bridge: %s", text)
        except (OSError, ValueError):  # pragma: no cover
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def _stream(self, mode: str, *extra: str, stop_event: Any = None) -> Iterator[dict]:
        """启动子进程并逐条产出它输出的 JSON 对象。

        用「后台线程读 + 队列取」而不是直接迭代 ``stdout``：否则没有新通知时
        读操作会一直阻塞，``stop_event`` 要等到下一条通知才能生效。
        """
        executable = self.require()
        command = [str(executable), mode]
        command.extend(extra)

        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(executable.parent),
            )
        except OSError as exc:
            raise BridgeUnavailable("无法启动 %s：%s" % (executable, exc)) from exc

        messages: "queue.Queue" = queue.Queue()
        threading.Thread(target=self._pump, args=(process.stdout, messages), daemon=True).start()
        threading.Thread(target=self._drain_stderr, args=(process.stderr,), daemon=True).start()

        try:
            while True:
                if stop_event is not None and stop_event.is_set():
                    break
                try:
                    item = messages.get(timeout=0.2)
                except queue.Empty:
                    if process.poll() is not None and messages.empty():
                        break
                    continue

                if item is _EOF:
                    break
                if item.get("type") == "fatal":
                    raise BridgeUnavailable(str(item.get("error") or "桥接程序报致命错误"))
                yield item
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:  # pragma: no cover
                    process.kill()

    @staticmethod
    def _to_notification(payload: dict) -> Notification:
        xml_source = str(payload.get("xmlSource") or "")
        history_error = payload.get("historyError") or ""
        warnings = list(payload.get("warnings") or ())
        return Notification(
            id=int(payload.get("id") or 0),
            kind=KIND_TOAST,
            app_name=payload.get("appName") or "",
            app_id=payload.get("aumid") or "",
            xml=payload.get("xml") or "",
            texts=tuple(payload.get("texts") or ()),
            arrived_at=parse_datetime(payload.get("creationTime")),
            source=PROVENANCE_WINRT,
            xml_is_original=(xml_source == "history"),
            extra={
                "xml_source": xml_source,
                "history_count": payload.get("historyCount"),
                "history_error": history_error,
                "package_family_name": payload.get("packageFamilyName") or "",
                "bindings": payload.get("bindings") or [],
                "warnings": warnings,
            },
        )

    def __repr__(self) -> str:
        return "<WinRTBridge %s>" % (self.executable or "未安装")
