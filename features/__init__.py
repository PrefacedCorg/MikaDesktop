"""features 包。

下面这些子模块依赖 GUI 依赖（PySide6 等）。这里逐个容错导入，原因是
:mod:`features.catch_notify` 是纯标准库实现的，不应该因为 GUI 依赖缺失而连
一起无法使用；依赖齐全时行为与原来完全一致。
"""

import logging

_logger = logging.getLogger(__name__)

try:
    from . import process_mgr
except ImportError as exc:  # pragma: no cover - 只在缺少 GUI 依赖时走到
    _logger.debug("features.process_mgr 未导入：%s", exc)

try:
    from . import XHT
except ImportError as exc:  # pragma: no cover - 只在缺少 GUI 依赖时走到
    _logger.debug("features.XHT 未导入：%s", exc)
