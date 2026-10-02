"""统一门面：调用方不必关心底层用的是哪个数据源。

``source="auto"``（默认）优先数据库源，只有它不可用（库不存在 / 结构不认识）时
才退回 WinRT 桥接源。这个「数据库优先」的取舍是有意的：

* 数据库源零依赖、零授权，并且给的是**逐字节原始 XML**；
* WinRT 源要用户手动授权，且 exe 需要单独分发。
"""

from __future__ import annotations

import logging
import platform as _platform
import sqlite3
import sys
from pathlib import Path
from typing import Any, Callable, Iterator

from .bridge import WinRTBridge
from .database import NotificationDatabase, default_database_path
from .errors import BridgeUnavailable, CatchNotifyError
from .records import Notification

logger = logging.getLogger(__name__)

__all__ = [
    "SOURCE_AUTO",
    "SOURCE_DATABASE",
    "SOURCE_WINRT",
    "open_source",
    "read_notifications",
    "watch_notifications",
    "available_sources",
    "describe_environment",
]

#: 自动选择（默认）：数据库优先，不可用时退回 WinRT
SOURCE_AUTO = "auto"
#: 通知库 wpndatabase.db
SOURCE_DATABASE = "db"
#: Windows 原生 WinRT/COM 桥接程序
SOURCE_WINRT = "winrt"


def open_source(source: str = SOURCE_AUTO, *, db_path: Any = None, bridge_path: Any = None):
    """打开数据源，返回 ``(实际使用的源名, 数据源对象)``。

    ``auto`` 模式下如果两个源都用不了，抛的是**数据库源的那个异常**
    （它通常更有信息量：库不存在 / 结构不符）。
    """
    source = (source or SOURCE_AUTO).lower()

    if source == SOURCE_DATABASE:
        return SOURCE_DATABASE, NotificationDatabase(db_path)

    if source == SOURCE_WINRT:
        bridge = WinRTBridge(bridge_path)
        if not bridge.available:
            bridge.require()  # 抛出带安装说明的 BridgeUnavailable
        return SOURCE_WINRT, bridge

    if source != SOURCE_AUTO:
        raise ValueError("未知的数据源：%r（可选：auto / db / winrt）" % source)

    database_error = None
    try:
        return SOURCE_DATABASE, NotificationDatabase(db_path)
    except (CatchNotifyError, sqlite3.Error) as exc:
        database_error = exc

    bridge = WinRTBridge(bridge_path)
    if bridge.available:
        logger.info("数据库源不可用（%s），改用 WinRT 桥接源。", database_error)
        return SOURCE_WINRT, bridge

    assert database_error is not None
    raise database_error


def read_notifications(
    source: str = SOURCE_AUTO,
    *,
    kinds: Any = None,
    db_path: Any = None,
    bridge_path: Any = None,
    since_order: int = 0,
    limit: int | None = None,
    include_xml: bool = True,
    bridge_timeout: float | None = 60.0,
) -> list:
    """一次性读取当前通知，返回 :class:`~.records.Notification` 列表。

    ``since_order`` 走的是通知库的 ``[Order]``；它是 SQLite 的 rowid，**会回退**
    （较新的通知被删掉后，新通知拿到的是「当前最大 Order + 1」）。想持续监听新通知
    请用 :func:`watch_notifications`，它会检测回退并重扫去重。
    """
    name, backend = open_source(source, db_path=db_path, bridge_path=bridge_path)
    try:
        if name == SOURCE_DATABASE:
            return backend.read(
                kinds=kinds, since_order=since_order, limit=limit, include_xml=include_xml
            )
        return backend.snapshot(timeout=bridge_timeout)
    finally:
        _close(backend)


def watch_notifications(
    source: str = SOURCE_AUTO,
    *,
    kinds: Any = None,
    interval: float = 0.8,
    skip_existing: bool = False,
    stop_event: Any = None,
    db_path: Any = None,
    bridge_path: Any = None,
    on_error: Callable[[Exception], None] | None = None,
) -> Iterator[Notification]:
    """持续产出新通知的生成器；``stop_event`` 一置位就尽快结束。

    注意：``skip_existing`` 只对数据库源生效。WinRT 源启动时会把当前通知先报
    一遍（桥接程序的行为如此），需要在应用层自行去重的话，可以先调
    :func:`read_notifications` 拿到现有 id 再过滤。
    """
    name, backend = open_source(source, db_path=db_path, bridge_path=bridge_path)
    try:
        if name == SOURCE_DATABASE:
            yield from backend.watch(
                kinds=kinds,
                interval=interval,
                skip_existing=skip_existing,
                stop_event=stop_event,
                on_error=on_error,
            )
        else:
            yield from backend.watch(int(max(interval, 0.1) * 1000), stop_event=stop_event)
    finally:
        _close(backend)


def _close(backend: Any) -> None:
    close = getattr(backend, "close", None)
    if callable(close):
        close()


def available_sources(*, db_path: Any = None, bridge_path: Any = None) -> dict:
    """``{'db': bool, 'winrt': bool}`` —— 哪些源当前可用（不做任何会弹窗的操作）。"""
    result = {"db": False, "winrt": False}

    try:
        database = NotificationDatabase(db_path)
    except (CatchNotifyError, sqlite3.Error):
        pass
    else:
        result["db"] = True
        database.close()

    result["winrt"] = WinRTBridge(bridge_path).available
    return result


def describe_environment(
    *,
    db_path: Any = None,
    bridge_path: Any = None,
    probe_access: bool = False,
    access_timeout: float | None = 5.0,
) -> dict:
    """收集排障需要的全部事实，供 CLI 的 ``--check`` 打印。

    ``probe_access`` 默认 False：查授权状态可能弹出系统授权对话框，排障时不该
    有副作用，需要时显式打开。
    """
    database_path = Path(db_path) if db_path else default_database_path()
    report = {
        "platform": _platform.platform(),
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "sqlite_version": sqlite3.sqlite_version,
        "database": {
            "path": str(database_path),
            "exists": database_path.is_file(),
            "size": None,
            "modified": None,
            "readable": False,
            "count": None,
            "kinds": None,
            "mode": None,
            "error": None,
            "sidecars": {},
        },
        "bridge": {
            "executable": None,
            "available": False,
            "native_dir": None,
            "build_script": None,
            "access": None,
        },
        "available": {"db": False, "winrt": False},
    }

    database_info = report["database"]
    if database_path.is_file():
        try:
            stat = database_path.stat()
            database_info["size"] = stat.st_size
            database_info["modified"] = stat.st_mtime
        except OSError as exc:  # pragma: no cover
            database_info["error"] = str(exc)
        for suffix in ("-wal", "-shm"):
            side = Path(str(database_path) + suffix)
            database_info["sidecars"][side.name] = side.is_file()

    try:
        database = NotificationDatabase(db_path)
    except (CatchNotifyError, sqlite3.Error) as exc:
        database_info["error"] = "%s: %s" % (type(exc).__name__, exc)
    else:
        try:
            database_info["readable"] = True
            database_info["count"] = database.count()
            database_info["kinds"] = database.kinds()
            database_info["mode"] = "snapshot" if database.using_snapshot else "direct"
            report["available"]["db"] = True
        finally:
            database.close()

    bridge = WinRTBridge(bridge_path)
    bridge_info = report["bridge"]
    bridge_info["available"] = bridge.available
    bridge_info["executable"] = str(bridge.executable) if bridge.executable else None
    bridge_info["native_dir"] = str(bridge.native_dir) if bridge.native_dir else None
    script = bridge.build_script
    bridge_info["build_script"] = str(script) if script else None
    report["available"]["winrt"] = bridge.available

    if bridge.available and probe_access:
        try:
            allowed, status = bridge.request_access(timeout=access_timeout)
            bridge_info["access"] = {"allowed": allowed, "status": status}
        except BridgeUnavailable as exc:
            bridge_info["access"] = {"allowed": False, "status": "error", "error": str(exc)}

    return report
