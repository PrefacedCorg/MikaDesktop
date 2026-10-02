"""纯文本工具函数。

单独一个模块、零依赖（不导入 loguru / PySide6 / pywin32），这样无论调用方是
日志模块还是位于 :mod:`features` 下的库，都能共用同一份实现而不产生循环导入。
"""

from __future__ import annotations

__all__ = ["format_message"]


def format_message(msg, args):
    """把 printf 风格的参数拼进消息里。

    本项目的 logger 原来签名是 ``info(self, msg, **kwargs)``，多传位置参数会直接抛
    ``TypeError: logger.info() takes 2 positional arguments but 4 were given``。
    这里统一兜住两种写法：``log.info("a %s", b)``（printf）和 ``log.info("a {}", b)``（loguru）。

    这段逻辑原先在 :mod:`core.log_maker` 和 :mod:`features.XHT.Lib.Notify` 各有一份，
    任何一边改了另一边不会跟着变，所以收敛到这里作为唯一实现。
    """
    if not args:
        return msg
    if not isinstance(msg, str):
        return " ".join(str(item) for item in (msg,) + tuple(args))
    try:
        return msg % (args if len(args) > 1 else args[0])
    except (TypeError, ValueError):
        pass
    try:
        return msg.format(*args)
    except (IndexError, KeyError, ValueError):
        return msg + " " + " ".join(str(item) for item in args)
