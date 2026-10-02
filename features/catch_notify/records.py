"""通知数据模型与纯工具函数。

这一层完全不碰 I/O：只负责把「数据库里的原始字节」和「WinRT 桥接程序输出的
JSON」归一化成同一个 :class:`Notification`，让上层两个数据源共用一套类型。

通知 XML 的**内容元素**解析（文本角色、图片、按钮、输入框…）在 :mod:`.toast`
里；:class:`Notification` 通过 :attr:`Notification.content` 懒解析并缓存结果，
所以显示层既可以用老的三件套（``title`` / ``body`` / ``texts``），也可以直接拿
结构化的 :class:`~.toast.ToastContent`。

刻意不依赖 ``xml`` / ``html`` 包（本项目 cx_Freeze 配置把 ``xml`` 放进了
EXCLUDES），所以 XML 解析、实体解码都是自己实现的（见 :mod:`.toast`）。
"""

from __future__ import annotations

import base64
import datetime as dt
from dataclasses import dataclass, field
from typing import Any

# 实体解码 / payload 解码 / 内容元素解析的唯一实现在 .toast；
# 这里重新导出前两个，历史上它们属于本模块（外部 from .records import ...）。
from .toast import (
    ToastAction,
    ToastContent,
    ToastImage,
    ToastInput,
    decode_payload,
    parse_toast,
    unescape_xml,
)

__all__ = [
    "Notification",
    "KIND_TOAST",
    "KIND_TILE",
    "KIND_BADGE",
    "PROVENANCE_DATABASE",
    "PROVENANCE_WINRT",
    "FILETIME_EPOCH_DELTA",
    "decode_payload",
    "filetime_to_datetime",
    "parse_datetime",
    "extract_texts",
    "unescape_xml",
]

KIND_TOAST = "toast"
KIND_TILE = "tile"
KIND_BADGE = "badge"

#: :attr:`Notification.source` 的取值 —— 表示这条通知是从哪条路拿到的。
#: 注意它和 ``sources.SOURCE_*``（用户传的 ``--source db/winrt/auto`` 选择器）
#: 是两回事：一个是「记录来源」，一个是「用哪个数据源」。
PROVENANCE_DATABASE = "wpndatabase"
PROVENANCE_WINRT = "winrt"

#: 1601-01-01 到 1970-01-01 之间的 100 纳秒数（Windows FILETIME 起点）
FILETIME_EPOCH_DELTA = 116444736000000000


def extract_texts(xml: Any) -> tuple:
    """按出现顺序取出通知 XML 里所有 ``<text>`` 的文本（已解实体、已 strip）。

    解析走 :func:`~.toast.parse_toast` 的宽容扫描器：CDATA、属性、乱序或未闭合的
    标签都能得到与文档顺序一致的文本列表。
    """
    return tuple(text.content for text in parse_toast(xml).all_texts if text.content)


def filetime_to_datetime(value: Any) -> dt.datetime | None:
    """Windows FILETIME（100 纳秒，1601 起点）→ 带本地时区的 datetime。"""
    if not value:
        return None
    try:
        seconds = (int(value) - FILETIME_EPOCH_DELTA) / 10_000_000
        return dt.datetime.fromtimestamp(seconds, dt.timezone.utc).astimezone()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def parse_datetime(value: Any) -> dt.datetime | None:
    """解析 ISO-8601 字符串（桥接程序输出的 creationTime）；失败返回 None。"""
    if not value:
        return None
    if isinstance(value, dt.datetime):
        return value
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _iso(value: dt.datetime | None) -> str:
    return value.isoformat(timespec="seconds") if value else ""


@dataclass
class Notification:
    """一条通知。两个数据源都会归一化成它。

    属性
    ----
    id           通知在系统里的编号
    kind         ``toast`` / ``tile`` / ``badge``
    app_id       AppUserModelId（数据库源的 ``PrimaryId``）
    app_name     应用显示名（系统没登记时为空，可用 :attr:`display_app` 兜底）
    xml          通知 XML 文本
    texts        所有 ``<text>`` 的文本，``texts[0]`` 是标题
    xml_is_original   True = 原始 XML；False = 由 binding 重建，不是原件
    xml_bytes    逐字节原始 payload（只有数据库源能提供），可直接落盘
    """

    id: int = 0
    kind: str = KIND_TOAST
    app_name: str = ""
    app_id: str = ""
    xml: str = ""
    texts: tuple = ()
    tag: str = ""
    group: str = ""
    arrived_at: dt.datetime | None = None
    expires_at: dt.datetime | None = None
    source: str = PROVENANCE_DATABASE
    order: int = 0
    payload_type: str = ""
    xml_is_original: bool = True
    xml_bytes: bytes | None = None
    extra: dict = field(default_factory=dict)
    #: :attr:`content` 的解析结果缓存（不参与比较与序列化）
    _content_cache: Any = field(default=None, init=False, repr=False, compare=False)

    # -- 内容元素 ---------------------------------------------------------- #
    @property
    def content(self) -> ToastContent:
        """解析好的内容元素（:class:`~.toast.ToastContent`），惰性求值 + 缓存。

        没有 XML（例如 WinRT 源重建失败）时返回空内容对象，不会抛异常。
        """
        cached = self._content_cache
        if cached is None:
            cached = parse_toast(self.xml)
            self._content_cache = cached
        return cached

    @property
    def settings(self) -> ToastContent:
        """:attr:`content` 的别名（拼写更贴近「内容元素」这个说法）。"""
        return self.content

    @property
    def text_roles(self) -> tuple:
        """主 binding 文本的角色序列，例如 ``('title', 'body', 'attribution')``。"""
        return tuple(text.role for text in self.content.texts)

    @property
    def template(self) -> str:
        return self.content.template

    @property
    def scenario(self) -> str:
        return self.content.scenario

    @property
    def actions(self) -> tuple:
        return self.content.actions

    @property
    def inputs(self) -> tuple:
        return self.content.inputs

    @property
    def images(self) -> tuple:
        return self.content.images

    @property
    def attribution(self) -> str:
        return self.content.attribution

    @property
    def has_actions(self) -> bool:
        return self.content.has_actions

    @property
    def has_inputs(self) -> bool:
        return self.content.has_inputs

    # -- 便捷读取 ---------------------------------------------------------- #
    @property
    def title(self) -> str:
        """标题：优先按内容元素的角色取；解析不到就退回 ``texts[0]``。"""
        content = self.content
        if content.title:
            return content.title
        if content.has_text:
            # XML 能解析但确实没有 title 角色（例如只有一条 attribution 文本）
            return ""
        return self.texts[0] if self.texts else ""

    @property
    def body(self) -> str:
        """正文：内容元素里所有 ``body`` 角色的行，按文档顺序。"""
        content = self.content
        if content.body_lines:
            return content.body
        if content.has_text:
            return ""
        return self.texts[1] if len(self.texts) > 1 else ""

    @property
    def other_texts(self) -> tuple:
        """标题与第一段正文之外的其他文本（额外的正文行 + 归属文本）。

        与旧实现（``texts[2:]``）在常见通知上结果一致，但归属文本
        （``placement="attribution"``）不会再被当成正文行。
        """
        content = self.content
        if not content.has_text:
            return self.texts[2:]
        extras = list(content.body_lines[1:])
        if content.attribution:
            extras.append(content.attribution)
        return tuple(extras)

    @property
    def display_app(self) -> str:
        return self.app_name or self.app_id

    @property
    def arrived_at_iso(self) -> str:
        return _iso(self.arrived_at)

    @property
    def expires_at_iso(self) -> str:
        return _iso(self.expires_at)

    @property
    def summary(self) -> str:
        return f"{self.display_app}: {self.title or '(无标题)'}"

    # -- 序列化 ------------------------------------------------------------ #
    def to_dict(self, *, with_xml_bytes: bool = False) -> dict:
        """转成 JSON 友好的 dict（``xml`` 是文本，原始字节需要显式打开）。"""
        data = {
            "id": self.id,
            "kind": self.kind,
            "app_name": self.app_name,
            "app_id": self.app_id,
            "title": self.title,
            "body": self.body,
            "attribution": self.attribution,
            "texts": list(self.texts),
            "text_roles": list(self.text_roles),
            "content": self.content.to_dict(),
            "tag": self.tag,
            "group": self.group,
            "arrived_at": self.arrived_at_iso,
            "expires_at": self.expires_at_iso,
            "source": self.source,
            "order": self.order,
            "payload_type": self.payload_type,
            "xml_is_original": self.xml_is_original,
            "xml": self.xml,
            "extra": self.extra,
        }
        if with_xml_bytes:
            raw = self.xml_bytes
            data["xml_bytes_b64"] = base64.b64encode(raw).decode("ascii") if raw else None
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Notification":
        """``to_dict`` 的逆操作（``extra`` 原样带回）。"""
        return cls(
            id=int(data.get("id") or 0),
            kind=data.get("kind") or KIND_TOAST,
            app_name=data.get("app_name") or "",
            app_id=data.get("app_id") or "",
            xml=data.get("xml") or "",
            texts=tuple(data.get("texts") or ()),
            tag=data.get("tag") or "",
            group=data.get("group") or "",
            arrived_at=parse_datetime(data.get("arrived_at")),
            expires_at=parse_datetime(data.get("expires_at")),
            source=data.get("source") or "wpndatabase",
            order=int(data.get("order") or 0),
            payload_type=data.get("payload_type") or "",
            xml_is_original=bool(data.get("xml_is_original", True)),
            xml_bytes=base64.b64decode(data["xml_bytes_b64"]) if data.get("xml_bytes_b64") else None,
            extra=dict(data.get("extra") or {}),
        )

    def __str__(self) -> str:
        return self.summary
