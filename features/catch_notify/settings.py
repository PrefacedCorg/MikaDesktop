"""读取通知相关的系统开关 —— 用来回答「为什么一条都收不到」。

事实来源有两个，都是只读的：

1. 注册表 ``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\PushNotifications``
   的 ``ToastEnabled``：就是 Windows 设置里「获取来自应用和其他发送者的通知」
   那个**总开关**（值不存在 = 用户没改过 = 默认开启）。

2. 通知库自身的 ``HandlerSettings`` / ``Metadata`` 表（这就是设置界面里单应用
   开关的真正后端）：

   ========================  ====================================================
   ``s:toast``               该应用是否允许通知（单应用总开关）
   ``s:banner``              是否弹横幅 —— **关掉它不影响进通知中心**
   ``s:audio``               是否响声音
   ``s:lock:toast``          是否在锁屏显示
   ``s:listenerEnabled``     是否允许该应用作为「通知侦听器」
   ``c:storage:toast``       是否把 toast 存入通知中心
   ``Metadata["toast:maxCount"]``  通知中心最多保留多少条 toast
   ========================  ====================================================

这些开关决定的是「通知会不会进入通知中心」，而不是「能不能被本库读出来」：
本库读的就是通知中心的存储，所以**没进存储的通知读不到，进了存储的一律能读到
完整 XML**（哪怕它没弹横幅、哪怕正处于专注助手/勿扰）。
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from .errors import CatchNotifyError

logger = logging.getLogger(__name__)

__all__ = [
    "REGISTRY_PATH",
    "MASTER_SWITCH_VALUE",
    "SETTING_KEYS",
    "master_switch",
    "app_settings",
    "retention_limits",
    "describe",
    "warnings",
]

REGISTRY_PATH = r"Software\Microsoft\Windows\CurrentVersion\PushNotifications"
MASTER_SWITCH_VALUE = "ToastEnabled"

#: 平台开关（HandlerSettings.SettingKey）→ 友好名字
SETTING_KEYS = {
    "toast": "s:toast",
    "banner": "s:banner",
    "audio": "s:audio",
    "lock_toast": "s:lock:toast",
    "listener": "s:listenerEnabled",
    "storage": "c:storage:toast",
}

#: Metadata 里和「通知能留多久」有关的键
RETENTION_KEYS = (
    "toast:maxCount",
    "toastCondensed:maxCount",
    "tile:maxCount",
    "badge:maxCount",
    "raw:maxCount",
)


def master_switch() -> dict:
    """读取「获取来自应用和其他发送者的通知」总开关。

    返回 ``{'enabled': True/False/None, 'error': str|None}``；``None`` 表示读不到
    （非 Windows、或注册表被策略锁住）。
    """
    if not sys.platform.startswith("win"):  # pragma: no cover - 本库本来就只面向 Windows
        return {"enabled": None, "error": "当前不是 Windows 平台"}

    try:
        import winreg
    except ImportError as exc:  # pragma: no cover
        return {"enabled": None, "error": "winreg 不可用：%s" % exc}

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_PATH) as key:
            value, _ = winreg.QueryValueEx(key, MASTER_SWITCH_VALUE)
        return {"enabled": bool(value), "error": None}
    except FileNotFoundError:
        # 值和键都不存在 = 用户从未改过，默认是开启的
        return {"enabled": True, "error": None}
    except OSError as exc:
        return {"enabled": None, "error": str(exc)}


def app_settings(database: Any) -> dict:
    """每个应用的通知开关：``{AUMID: {'toast': True, 'banner': False, ...}}``。

    ``database`` 传 :class:`~.database.NotificationDatabase` 实例（复用它的连接）。
    """
    try:
        raw = database.handler_settings()
    except (CatchNotifyError, AttributeError):
        return {}

    result = {}
    for aumid, values in raw.items():
        entry = {}
        for name, key in SETTING_KEYS.items():
            if key in values:
                entry[name] = bool(values[key])
        if entry:
            result[aumid] = entry
    return result


def retention_limits(database: Any) -> dict:
    """通知中心的保留上限（``{'toast:maxCount': 20, ...}``）。"""
    try:
        metadata = database.metadata()
    except (CatchNotifyError, AttributeError):
        return {}
    return {key: metadata[key] for key in RETENTION_KEYS if key in metadata}


def describe(database: Any = None, *, db_path: Any = None) -> dict:
    """汇总所有开关，供 ``--check`` 打印或应用启动时自检。

    ``database`` 可以传入一个已打开的 :class:`~.database.NotificationDatabase`；
    不传就自己开一个短连接（拿不到也不抛异常，只是相关字段为空）。
    """
    report: dict = {
        "master_switch": master_switch(),
        "apps": {},
        "muted_apps": [],
        "retention": {},
        "error": None,
    }

    owned = False
    if database is None:
        from .database import NotificationDatabase

        try:
            database = NotificationDatabase(db_path)
            owned = True
        except CatchNotifyError as exc:
            report["error"] = "%s: %s" % (type(exc).__name__, exc)
            return report

    try:
        report["apps"] = app_settings(database)
        report["retention"] = retention_limits(database)
        report["muted_apps"] = sorted(
            aumid for aumid, entry in report["apps"].items() if entry.get("toast") is False
        )
    finally:
        if owned:
            database.close()

    return report


def warnings(report: dict) -> list:
    """把 :func:`describe` 的结果翻译成给人看的警告（没问题是空列表）。"""
    messages = []

    master = report.get("master_switch") or {}
    if master.get("enabled") is False:
        messages.append(
            "系统总开关「获取来自应用和其他发送者的通知」已关闭 —— "
            "通知平台不会把任何通知写进通知库，因此本程序收不到任何内容。"
            "请到 设置 → 系统 → 通知 打开它。"
        )
    elif master.get("enabled") is None:
        messages.append("无法读取通知总开关（%s），行为未知。" % master.get("error"))

    muted = report.get("muted_apps") or []
    if muted:
        messages.append("有 %d 个应用的通知开关是关闭的，这些应用的通知不会进入通知库：%s"
                        % (len(muted), ", ".join(muted[:3]) + ("…" if len(muted) > 3 else "")))

    retention = report.get("retention") or {}
    toast_max = retention.get("toast:maxCount")
    if toast_max:
        messages.append(
            "通知中心最多保留 %s 条 toast；被用户清除或被挤掉的通知会从库里删除，"
            "之后就拿不到它们的 XML 了（所以「完整 XML」只在通知仍在中心时可得）。" % toast_max
        )

    if report.get("error"):
        messages.append("读取开关时出错：%s" % report["error"])

    return messages
