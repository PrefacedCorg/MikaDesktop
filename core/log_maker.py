import os
import sys
import datetime
from loguru import logger as log

# 格式化规则收敛在 core/text_utils.py（features 下的库也要用同一份实现）
from .text_utils import format_message as _format_message

__all__ = ["logger", "format_message"]



# 日志目录：优先基于程序所在目录（冻结时为 exe 目录），避免因工作目录不同而失败
if getattr(sys, 'frozen', False):
    _BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    _BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LOG_DIR = os.path.join(_BASE_DIR, "log")


def format_message(msg, args):
    """把 printf 风格的参数拼进消息里。

    实现已移到 :mod:`core.text_utils`（features 下的库要共用同一份逻辑，不能再各写
    一份）。文件名保留同名符号，避免既有 ``log_maker.format_message`` 调用点失效。
    """
    return _format_message(msg, args)


class logger:
    _initialized = False
    
    def __init__(self):
        self.is_debug = False
        if not logger._initialized:
            # 只添加文件处理器，loguru默认已经有stderr处理器了
            #
            # 日志只是旁路信息，绝不该成为「程序起不来」的原因：log 目录只读、
            # 被别的进程独占、磁盘写满时，这里失败会顺着 import 链把调用方整个
            # 带崩（core/sys32.py 顶部就专门为此兜了一层）。所以这里自己吞掉
            # 异常，退化到 loguru 默认的 stderr 输出即可。
            try:
                os.makedirs(_LOG_DIR, exist_ok=True)
                log.add(os.path.join(_LOG_DIR, datetime.datetime.now().strftime('%Y-%m-%d') + ".log"))
            except Exception as e:
                log.warning(f"日志文件不可用（{_LOG_DIR}），本次运行只在控制台输出: {e}")
            logger._initialized = True
    
    def enable_debug(self):
        self.is_debug = True

    def disable_debug(self):
        self.is_debug = False

    def debug(self, msg, *args, **kwargs):
        if self.is_debug:
            log.opt(depth=1).debug(format_message(msg, args), **kwargs)
    def info(self, msg, *args, **kwargs):
        log.opt(depth=1).info(format_message(msg, args), **kwargs)
    def warning(self, msg, *args, **kwargs):
        log.opt(depth=1).warning(format_message(msg, args), **kwargs)
    def error(self, msg, *args, **kwargs):
        log.opt(depth=1).error(format_message(msg, args), **kwargs)
    def critical(self, msg, *args, **kwargs):
        log.opt(depth=1).critical(format_message(msg, args), **kwargs)