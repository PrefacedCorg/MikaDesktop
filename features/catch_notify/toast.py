"""Toast 内容元素解析：把通知 XML 拆成「显示层能直接用的内容元素」。

Windows Toast 的 XML 不是「一堆文本」那么简单，它是一棵内容树：

.. code-block:: xml

    <toast launch="action=open&amp;id=42" scenario="reminder" duration="long"
           activationType="foreground" displayTimestamp="2026-10-02T20:00:00Z">
      <visual>
        <binding template="ToastGeneric">
          <text>标题</text>
          <text>正文第一行</text>
          <text>正文第二行</text>
          <text placement="attribution">来自 某某 App</text>
          <image placement="appLogoOverride" hint-crop="circle" src="file:///C:/logo.png"/>
          <image placement="hero" src="C:/hero.png"/>
          <progress title="下载" status="进行中" value="0.4"/>
        </binding>
      </visual>
      <actions>
        <input id="reply" type="text" placeHolderContent="回复…"/>
        <action content="回复" arguments="action=reply" hint-inputId="reply"/>
        <action content="打开" arguments="action=open" placement="contextMenu"/>
      </actions>
      <audio src="ms-winsoundevent:Notification.IM" silent="false"/>
      <header id="h" title="提醒" subtitle="今天" arguments="action=header"/>
    </toast>

旧模板（``ToastText01`` / ``ToastImageAndText02`` …）则用 ``<text id="1">`` 里的
``id`` 区分标题与正文。本模块把这些元素都解析成结构化模型：

* :class:`ToastText`     —— ``<text>``，带 ``title`` / ``body`` / ``attribution`` 角色
* :class:`ToastImage`    —— ``<image>``，``appLogoOverride`` / ``hero`` / 内联
* :class:`ToastAction`   —— ``<action>`` 按钮（含 ``arguments`` / ``activationType``）
* :class:`ToastInput`    —— ``<input>`` 输入框（text / selection / date / time）
* :class:`ToastHeader` / :class:`ToastAudio` / :class:`ToastProgress`
* :class:`ToastContent`  —— 上面这些的集合 + ``<toast>`` 自身属性

为什么自带一个 XML 扫描器
------------------------
本项目 ``build.py`` 的 cx_Freeze 配置把 ``xml`` 放进了 ``EXCLUDES``（见
``records.py`` 模块文档），所以这里不依赖 :mod:`xml` / :mod:`html` / :mod:`urllib`：
只用字符串扫描。扫描器刻意**宽容**：通知 XML 来自第三方应用，标签没闭合、属性没引号
都不该让显示层报错 —— 解析失败的元素会被跳过，问题记在
:attr:`ToastContent.parse_errors` 里。
"""

from __future__ import annotations

import codecs
import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

__all__ = [
    "ROLE_TITLE",
    "ROLE_BODY",
    "ROLE_ATTRIBUTION",
    "PLACEMENT_APP_LOGO",
    "PLACEMENT_HERO",
    "PLACEMENT_ATTRIBUTION",
    "PLACEMENT_INLINE",
    "ACTIVATION_FOREGROUND",
    "ACTIVATION_BACKGROUND",
    "ACTIVATION_PROTOCOL",
    "ACTIVATION_SYSTEM",
    "SCENARIO_ALARM",
    "SCENARIO_INCOMING_CALL",
    "SCENARIO_REMINDER",
    "ToastText",
    "ToastImage",
    "ToastAction",
    "ToastChoice",
    "ToastInput",
    "ToastHeader",
    "ToastAudio",
    "ToastProgress",
    "ToastBinding",
    "ToastContent",
    "unescape_xml",
    "decode_payload",
    "parse_toast",
    "local_image_path",
]

# -- 文本角色 -----------------------------------------------------------------
ROLE_TITLE = "title"
ROLE_BODY = "body"
ROLE_ATTRIBUTION = "attribution"

# -- <image placement> --------------------------------------------------------
PLACEMENT_APP_LOGO = "applogooverride"
PLACEMENT_HERO = "hero"
PLACEMENT_ATTRIBUTION = "attribution"
PLACEMENT_INLINE = ""

# -- <action activationType> / <toast activationType> ------------------------
ACTIVATION_FOREGROUND = "foreground"
ACTIVATION_BACKGROUND = "background"
ACTIVATION_PROTOCOL = "protocol"
ACTIVATION_SYSTEM = "system"

# -- <toast scenario> --------------------------------------------------------
SCENARIO_ALARM = "alarm"
SCENARIO_INCOMING_CALL = "incomingcall"
SCENARIO_REMINDER = "reminder"

_ENTITY = re.compile(r"&(?:#(?P<dec>\d+)|#x(?P<hex>[0-9a-fA-F]+)|(?P<named>[A-Za-z]+));")
_NAMED_ENTITIES = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'"}

#: 系统自带的 action 取值（由通知平台处理，不需要激活第三方 COM）
SYSTEM_ARGUMENTS = frozenset({"dismiss", "snooze", "dismissAndSnooze", "dismissAndGoToSettings"})


# --------------------------------------------------------------------------- #
# 基础工具（实体解码 / payload 解码）
# --------------------------------------------------------------------------- #
def unescape_xml(text: str) -> str:
    """解开 XML 预定义实体与数字字符引用；未知实体原样保留。"""
    if not text or "&" not in text:
        return text

    def replace(match: "re.Match[str]") -> str:
        dec, hexa, named = match.group("dec"), match.group("hex"), match.group("named")
        try:
            if dec is not None:
                return chr(int(dec, 10))
            if hexa is not None:
                return chr(int(hexa, 16))
        except (ValueError, OverflowError):
            return match.group(0)
        if named is not None:
            return _NAMED_ENTITIES.get(named, match.group(0))
        return match.group(0)

    return _ENTITY.sub(replace, text)


def decode_payload(payload: Any) -> str:
    """把 ``Notification.Payload`` 解码成 XML 文本。

    绝大多数是 UTF-8；少数是带 BOM 的 UTF-16；个别推送内容用 GBK。
    """
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload

    data = bytes(payload)
    if data.startswith(codecs.BOM_UTF8):
        return data.decode("utf-8-sig", "replace")
    if data[:2] in (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE):
        return data.decode("utf-16", "replace")

    for encoding in ("utf-8", "gbk", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")  # pragma: no cover - 兜底


def _percent_decode(text: str) -> str:
    """``%20`` 之类的百分号转义解码（不依赖 urllib：见模块文档）。"""
    if "%" not in text:
        return text
    out = bytearray()
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "%" and index + 2 < length + 1:
            try:
                out.append(int(text[index + 1:index + 3], 16))
                index += 3
                continue
            except ValueError:
                pass
        out.extend(char.encode("utf-8", "replace"))
        index += 1
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError:  # pragma: no cover - 非法字节序列
        return text


_MS_APP_DATA_ROOTS = {"local": "LocalState", "roaming": "RoamingState", "temp": "TempState"}


def local_image_path(src: Any, package_family_name: str = "") -> str | None:
    """把 ``<image src>`` 变成本机可读的图片路径；不是本地文件就返回 ``None``。

    支持：

    * ``file:///C:/x/y.png``（百分号转义、``file://server/share`` UNC 都认）
    * 直接的本地绝对路径 ``C:\\x\\y.png`` / ``\\\\server\\share\\y.png``
    * ``ms-appdata:///local/x.png`` —— 需要 ``package_family_name`` 才能定位到
      ``%LOCALAPPDATA%\\Packages\\<包族名>\\LocalState\\x.png``

    网络图片（``http(s)://``）与 ``ms-appx:///``（包内资源）一律返回 ``None``：
    前者 QLabel 不会去下载，后者要查包安装位置，都不值得在显示路径上冒险。
    """
    if not src:
        return None
    text = str(src).strip()
    if not text:
        return None
    lowered = text.lower()

    if lowered.startswith("http://") or lowered.startswith("https://"):
        return None

    if lowered.startswith("ms-appdata:///"):
        if not package_family_name:
            return None
        remainder = _percent_decode(text[len("ms-appdata:///"):])
        head, _, tail = remainder.partition("/")
        state = _MS_APP_DATA_ROOTS.get(head.lower())
        if state is None or not tail:
            return None
        local_appdata = os.environ.get("LOCALAPPDATA") or ""
        if not local_appdata:
            return None
        return os.path.join(local_appdata, "Packages", package_family_name, state,
                            tail.replace("/", os.sep))

    if lowered.startswith("ms-appx") or lowered.startswith("ms-resource"):
        return None

    if lowered.startswith("file:"):
        remainder = text[5:]
        if remainder.startswith("//"):
            remainder = remainder[2:]
        else:
            remainder = remainder.lstrip("/")
        remainder = _percent_decode(remainder)
        # file:///C:/x → "C:/x"；file://server/share/x → "server/share/x"（UNC）
        if re.match(r"^/[A-Za-z]:", remainder):
            remainder = remainder[1:]
        elif not re.match(r"^[A-Za-z]:", remainder) and "/" in remainder:
            host, _, tail = remainder.partition("/")
            if host:
                remainder = "//%s/%s" % (host, tail)
        text = remainder

    text = text.replace("/", os.sep) if os.sep == "\\" else text
    if not os.path.isabs(text):
        return None
    return text


# --------------------------------------------------------------------------- #
# 极小的宽容 XML 扫描器
# --------------------------------------------------------------------------- #
class _Node:
    """扫描器产出的最小节点：标签名 + 属性 + 子节点 + 直属文本。"""

    __slots__ = ("name", "attrs", "children", "texts")

    def __init__(self, name: str, attrs: dict) -> None:
        self.name = name
        self.attrs = attrs
        self.children: list = []
        self.texts: list = []

    def text(self) -> str:
        """本元素（含所有后代）的文本内容。"""
        parts = list(self.texts)
        for child in self.children:
            parts.append(child.text())
        return "".join(parts)

    def find_all(self, name: str) -> Iterator["_Node"]:
        """深度优先（= 文档顺序）产出所有同名后代。"""
        for child in self.children:
            if child.name == name:
                yield child
            for nested in child.find_all(name):
                yield nested

    def first(self, name: str) -> "_Node | None":
        for node in self.find_all(name):
            return node
        return None


def _parse_attributes(raw: str) -> dict:
    """解析标签里 ``k="v"`` / ``k='v'`` / ``k=v`` 形式的属性。"""
    attrs: dict = {}
    index = 0
    length = len(raw)
    while index < length:
        while index < length and raw[index].isspace():
            index += 1
        if index >= length:
            break
        start = index
        while index < length and raw[index] not in " \t\r\n=":
            index += 1
        key = raw[start:index]
        while index < length and raw[index].isspace():
            index += 1
        if index < length and raw[index] == "=":
            index += 1
            while index < length and raw[index].isspace():
                index += 1
            if index < length and raw[index] in "\"'":
                quote = raw[index]
                index += 1
                start = index
                while index < length and raw[index] != quote:
                    index += 1
                value = raw[start:index]
                if index < length:
                    index += 1
            else:
                start = index
                while index < length and not raw[index].isspace():
                    index += 1
                value = raw[start:index]
            if key:
                attrs[key.lower()] = unescape_xml(value)
        elif key:
            attrs[key.lower()] = ""
    return attrs


def _iter_tokens(text: str) -> Iterator[tuple]:
    """把 XML 文本切成 ``text`` / ``start`` / ``end`` 三种记号。"""
    index = 0
    length = len(text)
    while index < length:
        mark = text.find("<", index)
        if mark < 0:
            yield ("text", text[index:])
            return
        if mark > index:
            yield ("text", text[index:mark])

        if text.startswith("<!--", mark):
            end = text.find("-->", mark + 4)
            index = length if end < 0 else end + 3
            continue
        if text.startswith("<![CDATA[", mark):
            end = text.find("]]>", mark + 9)
            body = text[mark + 9:] if end < 0 else text[mark + 9:end]
            yield ("text", body)
            index = length if end < 0 else end + 3
            continue
        if text.startswith("<?", mark):
            end = text.find("?>", mark + 2)
            index = length if end < 0 else end + 2
            continue
        if text.startswith("<!", mark):
            end = text.find(">", mark + 2)
            index = length if end < 0 else end + 1
            continue

        # 普通标签：找到引号之外的 '>'
        cursor = mark + 1
        quote = ""
        while cursor < length:
            char = text[cursor]
            if quote:
                if char == quote:
                    quote = ""
            elif char in "\"'":
                quote = char
            elif char == ">":
                break
            cursor += 1
        if cursor >= length:
            # 没有闭合 '>'：剩下的当文本，别丢内容
            yield ("text", text[mark:])
            return

        inner = text[mark + 1:cursor].strip()
        index = cursor + 1
        if not inner:
            continue
        if inner.startswith("/"):
            yield ("end", inner[1:].strip().lower(), {}, False)
            continue

        self_closing = inner.endswith("/")
        if self_closing:
            inner = inner[:-1].strip()
        if not inner:
            continue
        split = 0
        while split < len(inner) and not inner[split].isspace():
            split += 1
        name = inner[:split].lower()
        yield ("start", name, _parse_attributes(inner[split:]), self_closing)


def _parse_document(text: str) -> list:
    """扫描出根节点列表；标签不闭合 / 交叉嵌套都不抛异常。"""
    roots: list = []
    stack: list = []
    for token in _iter_tokens(text):
        kind = token[0]
        if kind == "text":
            if stack:
                stack[-1].texts.append(token[1])
            continue

        name = token[1]
        if kind == "start":
            node = _Node(name, token[2])
            if stack:
                stack[-1].children.append(node)
            else:
                roots.append(node)
            if not token[3]:
                stack.append(node)
            continue

        # end：弹到同名节点为止（找不到就忽略这个收尾标签）
        for position in range(len(stack) - 1, -1, -1):
            if stack[position].name == name:
                del stack[position:]
                break
    return roots


def _is_legacy_template(template: str) -> bool:
    """``ToastText02`` / ``ToastImageAndText03`` 这类旧模板用 ``id`` 区分文本。"""
    lowered = (template or "").strip().lower()
    return bool(lowered) and not lowered.startswith("toastgeneric")


# --------------------------------------------------------------------------- #
# 内容元素模型
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ToastText:
    """``<text>`` 元素（含角色）。"""

    content: str = ""
    role: str = ROLE_BODY
    id: str = ""
    placement: str = ""
    lang: str = ""
    hint_style: str = ""
    hint_max_lines: str = ""
    hint_wrap: str = ""

    @property
    def is_title(self) -> bool:
        return self.role == ROLE_TITLE

    @property
    def is_attribution(self) -> bool:
        return self.role == ROLE_ATTRIBUTION

    def to_dict(self) -> dict:
        return {
            "content": self.content,
            "role": self.role,
            "id": self.id,
            "placement": self.placement,
            "lang": self.lang,
            "hint_style": self.hint_style,
            "hint_max_lines": self.hint_max_lines,
        }


@dataclass(frozen=True)
class ToastImage:
    """``<image>`` 元素。"""

    src: str = ""
    placement: str = PLACEMENT_INLINE
    alt: str = ""
    id: str = ""
    hint_crop: str = ""
    hint_align: str = ""

    @property
    def is_hero(self) -> bool:
        return self.placement == PLACEMENT_HERO

    @property
    def is_app_logo(self) -> bool:
        return self.placement == PLACEMENT_APP_LOGO

    @property
    def is_inline(self) -> bool:
        return not self.is_hero and not self.is_app_logo

    @property
    def is_remote(self) -> bool:
        lowered = (self.src or "").strip().lower()
        return lowered.startswith("http://") or lowered.startswith("https://")

    def path(self, package_family_name: str = "") -> str | None:
        """本机可读路径（拿不到就 ``None``）。"""
        return local_image_path(self.src, package_family_name)

    def to_dict(self) -> dict:
        return {
            "src": self.src,
            "placement": self.placement,
            "alt": self.alt,
            "id": self.id,
            "hint_crop": self.hint_crop,
            "hint_align": self.hint_align,
        }


@dataclass(frozen=True)
class ToastAction:
    """``<action>`` 按钮（或上下文菜单项）。"""

    content: str = ""
    arguments: str = ""
    activation_type: str = ACTIVATION_FOREGROUND
    placement: str = ""
    id: str = ""
    hint_button_style: str = ""
    hint_tooltip: str = ""
    hint_input_id: str = ""
    image_uri: str = ""

    @property
    def is_context_menu(self) -> bool:
        return self.placement == "contextmenu"

    @property
    def is_protocol(self) -> bool:
        return self.activation_type == ACTIVATION_PROTOCOL

    @property
    def is_background(self) -> bool:
        return self.activation_type == ACTIVATION_BACKGROUND

    @property
    def is_system(self) -> bool:
        """系统自带动作（关闭 / 稍后提醒…）：通知平台自己处理。"""
        if self.activation_type == ACTIVATION_SYSTEM:
            return True
        return self.arguments in SYSTEM_ARGUMENTS

    @property
    def needs_input(self) -> bool:
        return bool(self.hint_input_id)

    @property
    def label(self) -> str:
        return self.content or self.arguments or "操作"

    def to_dict(self) -> dict:
        return {
            "content": self.content,
            "arguments": self.arguments,
            "activation_type": self.activation_type,
            "placement": self.placement,
            "hint_button_style": self.hint_button_style,
            "hint_tooltip": self.hint_tooltip,
            "hint_input_id": self.hint_input_id,
            "image_uri": self.image_uri,
        }


@dataclass(frozen=True)
class ToastChoice:
    """``<selection>``：下拉输入框的一个选项。"""

    id: str = ""
    content: str = ""

    def to_dict(self) -> dict:
        return {"id": self.id, "content": self.content}


@dataclass(frozen=True)
class ToastInput:
    """``<input>`` 输入框。"""

    id: str = ""
    type: str = "text"
    title: str = ""
    placeholder: str = ""
    default_input: str = ""
    choices: tuple = ()

    @property
    def is_selection(self) -> bool:
        return self.type == "selection"

    @property
    def is_text(self) -> bool:
        return not self.is_selection

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "title": self.title,
            "placeholder": self.placeholder,
            "default_input": self.default_input,
            "choices": [choice.to_dict() for choice in self.choices],
        }


@dataclass(frozen=True)
class ToastHeader:
    """``<header>``：通知头部的标题 / 副标题（可点击）。"""

    id: str = ""
    title: str = ""
    subtitle: str = ""
    arguments: str = ""
    activation_type: str = ACTIVATION_FOREGROUND

    @property
    def label(self) -> str:
        return " · ".join(part for part in (self.title, self.subtitle) if part)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "subtitle": self.subtitle,
            "arguments": self.arguments,
            "activation_type": self.activation_type,
        }


@dataclass(frozen=True)
class ToastAudio:
    """``<audio>``：提示音（``silent="true"`` 表示静音通知）。"""

    src: str = ""
    loop: bool = False
    silent: bool = False

    def to_dict(self) -> dict:
        return {"src": self.src, "loop": self.loop, "silent": self.silent}


@dataclass(frozen=True)
class ToastProgress:
    """``<progress>``：进度条（下载进度之类）。"""

    title: str = ""
    status: str = ""
    value: str = ""
    value_string_override: str = ""

    @property
    def label(self) -> str:
        parts = [part for part in (self.title, self.status) if part]
        if self.value_string_override:
            parts.append(self.value_string_override)
        elif self.value:
            parts.append(self.value)
        return " · ".join(parts)

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "status": self.status,
            "value": self.value,
            "value_string_override": self.value_string_override,
        }


@dataclass(frozen=True)
class ToastBinding:
    """一个 ``<binding>``（多个 binding = 同一通知的多套备选布局）。"""

    template: str = ""
    lang: str = ""
    texts: tuple = ()
    images: tuple = ()
    progress: ToastProgress | None = None
    hints: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "template": self.template,
            "lang": self.lang,
            "texts": [text.to_dict() for text in self.texts],
            "images": [image.to_dict() for image in self.images],
            "progress": self.progress.to_dict() if self.progress else None,
            "hints": dict(self.hints),
        }


@dataclass(frozen=True)
class ToastContent:
    """一条 toast 通知的全部内容元素。"""

    template: str = ""
    scenario: str = ""
    duration: str = ""
    launch: str = ""
    activation_type: str = ""
    display_timestamp: str = ""
    use_button_style: bool = False
    toast_id: str = ""
    texts: tuple = ()
    all_texts: tuple = ()
    images: tuple = ()
    all_images: tuple = ()
    actions: tuple = ()
    inputs: tuple = ()
    header: ToastHeader | None = None
    audio: ToastAudio | None = None
    progress: ToastProgress | None = None
    bindings: tuple = ()
    parse_errors: tuple = ()

    # -- 文本 -------------------------------------------------------------- #
    @property
    def title(self) -> str:
        for text in self.texts:
            if text.role == ROLE_TITLE and text.content:
                return text.content
        return ""

    @property
    def body_lines(self) -> tuple:
        return tuple(text.content for text in self.texts
                     if text.role == ROLE_BODY and text.content)

    @property
    def body(self) -> str:
        return "\n".join(self.body_lines)

    @property
    def attribution(self) -> str:
        for text in self.texts:
            if text.role == ROLE_ATTRIBUTION and text.content:
                return text.content
        return ""

    @property
    def has_text(self) -> bool:
        return any(text.content for text in self.all_texts)

    # -- 图片 -------------------------------------------------------------- #
    def image(self, placement: str) -> ToastImage | None:
        for image in self.images:
            if image.placement == placement:
                return image
        return None

    @property
    def hero_image(self) -> ToastImage | None:
        return self.image(PLACEMENT_HERO)

    @property
    def app_logo(self) -> ToastImage | None:
        return self.image(PLACEMENT_APP_LOGO)

    @property
    def inline_images(self) -> tuple:
        return tuple(image for image in self.images if image.is_inline)

    # -- 按钮 / 输入 ------------------------------------------------------- #
    @property
    def button_actions(self) -> tuple:
        return tuple(action for action in self.actions if not action.is_context_menu)

    @property
    def context_menu_actions(self) -> tuple:
        return tuple(action for action in self.actions if action.is_context_menu)

    @property
    def has_actions(self) -> bool:
        return bool(self.button_actions)

    @property
    def has_inputs(self) -> bool:
        return bool(self.inputs)

    def action(self, index: int) -> ToastAction | None:
        """按 :attr:`actions` 的下标取按钮（越界返回 ``None``）。"""
        try:
            return self.actions[index]
        except (IndexError, TypeError):
            return None

    def input(self, input_id: str) -> ToastInput | None:
        for item in self.inputs:
            if item.id == input_id:
                return item
        return None

    def action_inputs(self, action: ToastAction | None) -> tuple:
        """这个按钮该带上哪些输入框。

        规则与 Windows 一致：``hint-inputId`` 指定哪个就带哪个；没有指定时，按钮会
        收集通知里的**全部**输入框。系统动作（关闭 / 稍后提醒）与协议动作不带输入。
        """
        if action is None or action.is_system or action.is_protocol or not self.inputs:
            return ()
        if action.hint_input_id:
            found = self.input(action.hint_input_id)
            return (found,) if found is not None else ()
        return tuple(self.inputs)

    # -- 场景 -------------------------------------------------------------- #
    @property
    def is_reminder(self) -> bool:
        return self.scenario == SCENARIO_REMINDER

    @property
    def is_alarm(self) -> bool:
        return self.scenario == SCENARIO_ALARM

    @property
    def is_incoming_call(self) -> bool:
        return self.scenario == SCENARIO_INCOMING_CALL

    @property
    def scenario_label(self) -> str:
        return {
            SCENARIO_ALARM: "闹钟",
            SCENARIO_INCOMING_CALL: "来电",
            SCENARIO_REMINDER: "提醒",
        }.get(self.scenario, self.scenario)

    @property
    def is_silent(self) -> bool:
        return bool(self.audio is not None and self.audio.silent)

    # -- 序列化 ------------------------------------------------------------ #
    def to_dict(self) -> dict:
        return {
            "template": self.template,
            "scenario": self.scenario,
            "duration": self.duration,
            "launch": self.launch,
            "activation_type": self.activation_type,
            "display_timestamp": self.display_timestamp,
            "title": self.title,
            "body": self.body,
            "attribution": self.attribution,
            "texts": [text.to_dict() for text in self.texts],
            "images": [image.to_dict() for image in self.images],
            "actions": [action.to_dict() for action in self.actions],
            "inputs": [item.to_dict() for item in self.inputs],
            "header": self.header.to_dict() if self.header else None,
            "audio": self.audio.to_dict() if self.audio else None,
            "progress": self.progress.to_dict() if self.progress else None,
            "bindings": [binding.to_dict() for binding in self.bindings],
            "parse_errors": list(self.parse_errors),
        }

    def __str__(self) -> str:
        return self.title or self.body or "(无内容)"


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
def _text_from_node(node: _Node, role: str) -> ToastText:
    attrs = node.attrs
    return ToastText(
        content=unescape_xml(node.text()).strip(),
        role=role,
        id=attrs.get("id", ""),
        placement=attrs.get("placement", ""),
        lang=attrs.get("lang", ""),
        hint_style=attrs.get("hint-style", ""),
        hint_max_lines=attrs.get("hint-maxlines", ""),
        hint_wrap=attrs.get("hint-wrap", ""),
    )


def _assign_roles(nodes: Iterable[_Node], legacy: bool) -> list:
    """给一个 binding 的 ``<text>`` 分配角色（顺序 + ``id`` + ``placement``）。"""
    texts = []
    title_taken = False
    for node in nodes:
        attrs = node.attrs
        placement = (attrs.get("placement") or "").strip().lower()
        text_id = (attrs.get("id") or "").strip()
        if placement == PLACEMENT_ATTRIBUTION:
            role = ROLE_ATTRIBUTION
        elif legacy and text_id:
            role = ROLE_TITLE if text_id == "1" else ROLE_BODY
            if role == ROLE_TITLE:
                title_taken = True
        elif not title_taken:
            role = ROLE_TITLE
            title_taken = True
        else:
            role = ROLE_BODY
        texts.append(_text_from_node(node, role))
    return texts


def _image_from_node(node: _Node) -> ToastImage:
    attrs = node.attrs
    return ToastImage(
        src=attrs.get("src", ""),
        placement=(attrs.get("placement") or "").strip().lower(),
        alt=attrs.get("alt", ""),
        id=attrs.get("id", ""),
        hint_crop=attrs.get("hint-crop", ""),
        hint_align=attrs.get("hint-align", ""),
    )


def _action_from_node(node: _Node) -> ToastAction:
    attrs = node.attrs
    return ToastAction(
        content=attrs.get("content", "") or node.text().strip(),
        arguments=attrs.get("arguments", ""),
        activation_type=(attrs.get("activationtype") or ACTIVATION_FOREGROUND).strip().lower(),
        placement=(attrs.get("placement") or "").strip().lower(),
        id=attrs.get("id", ""),
        hint_button_style=attrs.get("hint-buttonstyle", ""),
        hint_tooltip=attrs.get("hint-tooltip", ""),
        hint_input_id=attrs.get("hint-inputid", ""),
        image_uri=attrs.get("imageuri", ""),
    )


def _input_from_node(node: _Node) -> ToastInput:
    attrs = node.attrs
    choices = tuple(
        ToastChoice(id=choice.attrs.get("id", ""), content=choice.attrs.get("content", ""))
        for choice in node.find_all("selection")
    )
    return ToastInput(
        id=attrs.get("id", ""),
        type=(attrs.get("type") or "text").strip().lower(),
        title=attrs.get("title", ""),
        placeholder=attrs.get("placeholdercontent", ""),
        default_input=attrs.get("defaultinput", ""),
        choices=choices,
    )


def _header_from_node(node: _Node) -> ToastHeader:
    attrs = node.attrs
    return ToastHeader(
        id=attrs.get("id", ""),
        title=attrs.get("title", ""),
        subtitle=attrs.get("subtitle", ""),
        arguments=attrs.get("arguments", ""),
        activation_type=(attrs.get("activationtype") or ACTIVATION_FOREGROUND).strip().lower(),
    )


def _audio_from_node(node: _Node) -> ToastAudio:
    attrs = node.attrs
    return ToastAudio(
        src=attrs.get("src", ""),
        loop=_truthy(attrs.get("loop")),
        silent=_truthy(attrs.get("silent")),
    )


def _progress_from_node(node: _Node) -> ToastProgress:
    attrs = node.attrs
    return ToastProgress(
        title=attrs.get("title", ""),
        status=attrs.get("status", ""),
        value=attrs.get("value", ""),
        value_string_override=attrs.get("valuestringoverride", ""),
    )


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes")


def parse_toast(xml: Any) -> ToastContent:
    """解析 toast XML → :class:`ToastContent`。

    传 ``None`` / 空串 / 完全不是 XML 的内容都会得到空内容对象（不抛异常）。
    """
    if xml is None:
        return ToastContent()
    if isinstance(xml, (bytes, bytearray)):
        xml = decode_payload(xml)
    if not isinstance(xml, str) or not xml.strip():
        return ToastContent()

    errors: list = []
    try:
        roots = _parse_document(xml)
    except Exception as exc:  # noqa: BLE001 - 扫描器本身出问题也不能让显示层崩
        return ToastContent(parse_errors=("%s: %s" % (type(exc).__name__, exc),))

    toast_node = None
    for node in roots:
        if node.name == "toast":
            toast_node = node
            break
    if toast_node is None:
        # 片段（例如只有 <text>）也要能解析：把整个文档当成一个容器用
        toast_node = _Node("toast", {})
        toast_node.children = roots

    attrs = toast_node.attrs
    binding_nodes = list(toast_node.find_all("binding"))
    all_text_nodes = list(toast_node.find_all("text"))
    all_image_nodes = list(toast_node.find_all("image"))

    bindings = []
    for node in binding_nodes:
        template = node.attrs.get("template", "")
        text_nodes = list(node.find_all("text"))
        image_nodes = list(node.find_all("image"))
        progress_nodes = [child for child in node.children if child.name == "progress"]
        hints = {key: value for key, value in node.attrs.items() if key.startswith("hint-")}
        bindings.append(ToastBinding(
            template=template,
            lang=node.attrs.get("lang", ""),
            texts=tuple(_assign_roles(text_nodes, _is_legacy_template(template))),
            images=tuple(_image_from_node(image) for image in image_nodes),
            progress=_progress_from_node(progress_nodes[0]) if progress_nodes else None,
            hints=hints,
        ))

    primary = _pick_primary(binding_nodes, bindings)
    template = primary.template if primary is not None else ""
    if primary is None:
        # 没有 <binding>：把文档里的文本 / 图片都当作主内容
        primary_texts = tuple(_assign_roles(all_text_nodes, _is_legacy_template(template)))
        primary_images = tuple(_image_from_node(image) for image in all_image_nodes)
        primary_progress = None
        progress_node = toast_node.first("progress")
        if progress_node is not None:
            primary_progress = _progress_from_node(progress_node)
    else:
        primary_texts = primary.texts
        primary_images = primary.images
        primary_progress = primary.progress

    if not primary_texts and all_text_nodes:
        # 主 binding 没文本（例如只有图片）时，兜底用文档里所有文本
        primary_texts = tuple(_assign_roles(all_text_nodes, _is_legacy_template(template)))
    if not binding_nodes and not primary_texts and not all_text_nodes:
        errors.append("没有找到任何 <text> 内容元素")

    actions = tuple(_action_from_node(node) for node in toast_node.find_all("action"))
    inputs = tuple(_input_from_node(node) for node in toast_node.find_all("input"))
    header_node = toast_node.first("header")
    audio_node = toast_node.first("audio")

    return ToastContent(
        template=template,
        scenario=(attrs.get("scenario") or "").strip().lower(),
        duration=(attrs.get("duration") or "").strip().lower(),
        launch=attrs.get("launch", ""),
        activation_type=(attrs.get("activationtype") or "").strip().lower(),
        display_timestamp=attrs.get("displaytimestamp", ""),
        use_button_style=_truthy(attrs.get("usebuttonstyle")),
        toast_id=attrs.get("id", ""),
        texts=primary_texts,
        all_texts=tuple(_assign_roles(all_text_nodes, _is_legacy_template(template))),
        images=primary_images,
        all_images=tuple(_image_from_node(image) for image in all_image_nodes),
        actions=actions,
        inputs=inputs,
        header=_header_from_node(header_node) if header_node is not None else None,
        audio=_audio_from_node(audio_node) if audio_node is not None else None,
        progress=primary_progress,
        bindings=tuple(bindings),
        parse_errors=tuple(errors),
    )


def _pick_primary(binding_nodes: list, bindings: list) -> ToastBinding | None:
    """选「主 binding」：优先 ToastGeneric，其次第一个带文本的，再次第一个。"""
    if not bindings:
        return None
    for index, node in enumerate(binding_nodes):
        if (node.attrs.get("template") or "").strip().lower() == "toastgeneric":
            return bindings[index]
    for binding in bindings:
        if any(text.content for text in binding.texts):
            return binding
    return bindings[0]
