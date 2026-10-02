"""本库的异常层次。

单独一个模块，方便 database / bridge / sources 互相引用而不产生循环导入。
所有异常都继承 :class:`CatchNotifyError`，调用方可以只 catch 一个基类。
"""

from __future__ import annotations

__all__ = [
    "CatchNotifyError",
    "DatabaseUnavailable",
    "SchemaError",
    "BridgeUnavailable",
    "AccessDenied",
    "ActivationError",
]


class CatchNotifyError(RuntimeError):
    """本库所有错误的基类。"""


class DatabaseUnavailable(CatchNotifyError):
    """通知数据库不存在或打不开（从未收到过通知、换了用户账户、文件被清理等）。"""


class SchemaError(CatchNotifyError):
    """通知数据库结构与预期不符（通常是 Windows 版本差异）。"""


class BridgeUnavailable(CatchNotifyError):
    """WinRT 桥接程序缺失、无法启动或异常退出。"""


class AccessDenied(CatchNotifyError):
    """系统未授予「访问通知」权限（WinRT 数据源特有）。"""


class ActivationError(CatchNotifyError):
    """通知按钮 / 通知本体的激活失败（注册表里没有激活器、COM 调用失败等）。"""
