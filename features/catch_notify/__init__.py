"""catch_notify —— 抓取 Windows 通知，并拿到**完整的通知 XML**。

快速开始
--------
::

    from features.catch_notify import NotificationDatabase, watch_notifications

    # 1) 一次性读取（默认数据库源，零依赖、零授权）
    with NotificationDatabase() as db:
        for item in db.read(kinds="toast"):
            print(item.display_app, item.title, item.body)
            print(item.xml)               # 完整原始 XML
            print(item.xml_bytes)         # 逐字节原件

    # 2) 持续监听（放工作线程里；stop_event 一置位就退出）
    for item in watch_notifications(stop_event=my_event):
        handle(item)

    # 3) 走 Windows 原生 WinRT/COM（需要用户授权 + 预编译的 exe）
    from features.catch_notify import read_notifications, SOURCE_WINRT
    for item in read_notifications(SOURCE_WINRT):
        handle(item)

内容元素与按钮激活
------------------
::

    from features.catch_notify import activate

    content = item.content               # 解析好的 Toast 内容元素
    print(content.title, content.body_lines, content.attribution)
    print(content.template, content.scenario, content.hero_image, content.app_logo)
    for index, action in enumerate(content.button_actions):
        print(index, action.label, action.arguments, action.activation_type)

    # 点击第 0 个按钮：把 arguments（和用户填写的输入框内容）发回应用
    result = activate(item, action=content.button_actions[0],
                      inputs={item.id: "好的" for item in content.inputs})
    print(result.ok, result.message)

两个数据源
----------
====================  ==================================================
``db``（默认）         通知库 ``wpndatabase.db``。零依赖、零授权，XML 是逐字节原件。
``winrt``             原生 WinRT/COM 桥接程序。官方实时 API，但需要用户授权，
                      且要单独分发 ``NotificationBridge.exe``。
====================  ==================================================

两者都归一化成 :class:`~.records.Notification`，``item.xml`` 就是完整通知 XML
（含 ``launch`` / ``arguments`` / ``actions`` / ``hint-*`` 等全部内容）。

关于低阶 COM 接口
-----------------
``INotificationActivationCallback`` 是**回调**接口（你实现、系统在用户点击你自己
应用的通知时调你），只给 ``invokedArgs`` 和用户输入，不含 XML，也读不到别人的
通知；``INotificationListener`` 等私有 shell 接口在公开 SDK 里 0 命中。原始 XML
真正的存放处就是 ``wpndatabase.db`` 的 ``Notification.Payload``。

日志
----
本库用标准库 :mod:`logging`（库不应该替应用决定输出方式）。要接到 loguru::

    from loguru import logger
    import logging
    logging.getLogger("features.catch_notify").addHandler(
        logging.StreamHandler()
    )
"""

from __future__ import annotations

from . import settings
from .activation import (
    ActivationPlan,
    ActivationRequest,
    ActivationResult,
    ComActivatorCaller,
    RegistryActivatorLookup,
    ShellLauncher,
    activate,
    build_plan,
    dismiss_from_history,
    perform,
)
from .bridge import WinRTBridge, locate_executable, locate_native_dir
from .database import NotificationDatabase, default_database_path
from .errors import (
    AccessDenied,
    ActivationError,
    BridgeUnavailable,
    CatchNotifyError,
    DatabaseUnavailable,
    SchemaError,
)
from .records import (
    KIND_BADGE,
    KIND_TILE,
    KIND_TOAST,
    PROVENANCE_DATABASE,
    PROVENANCE_WINRT,
    Notification,
    extract_texts,
)
from .sources import (
    SOURCE_AUTO,
    SOURCE_DATABASE,
    SOURCE_WINRT,
    available_sources,
    describe_environment,
    open_source,
    read_notifications,
    watch_notifications,
)
from .settings import app_settings, master_switch, retention_limits
from .toast import (
    ROLE_ATTRIBUTION,
    ROLE_BODY,
    ROLE_TITLE,
    ToastAction,
    ToastContent,
    ToastImage,
    ToastInput,
    ToastProgress,
    ToastText,
    local_image_path,
    parse_toast,
)

__version__ = "1.1.1"

__all__ = [
    # 数据模型
    "Notification",
    "extract_texts",
    "KIND_TOAST",
    "KIND_TILE",
    "KIND_BADGE",
    "PROVENANCE_DATABASE",
    "PROVENANCE_WINRT",
    # 内容元素（Toast XML）
    "parse_toast",
    "ToastContent",
    "ToastText",
    "ToastImage",
    "ToastAction",
    "ToastInput",
    "ToastProgress",
    "ROLE_TITLE",
    "ROLE_BODY",
    "ROLE_ATTRIBUTION",
    "local_image_path",
    # 激活（按钮 / 通知本体）
    "activate",
    "build_plan",
    "perform",
    "dismiss_from_history",
    "ActivationPlan",
    "ActivationRequest",
    "ActivationResult",
    "RegistryActivatorLookup",
    "ComActivatorCaller",
    "ShellLauncher",
    # 数据源
    "NotificationDatabase",
    "WinRTBridge",
    "default_database_path",
    "locate_executable",
    "locate_native_dir",
    "SOURCE_AUTO",
    "SOURCE_DATABASE",
    "SOURCE_WINRT",
    # 门面
    "open_source",
    "read_notifications",
    "watch_notifications",
    "available_sources",
    "describe_environment",
    # 系统开关（诊断「为什么收不到」）
    "settings",
    "master_switch",
    "app_settings",
    "retention_limits",
    # 异常
    "CatchNotifyError",
    "DatabaseUnavailable",
    "SchemaError",
    "BridgeUnavailable",
    "AccessDenied",
    "ActivationError",
    # 元信息
    "__version__",
]
