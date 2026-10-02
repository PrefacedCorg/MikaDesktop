"""通知按钮 / 通知本体的**激活**：把点击送回发通知的那个应用。

Windows 的 Toast 按钮不是「链接」，而是「激活请求」：通知里写着
``<action content="回复" arguments="action=reply" hint-inputId="reply"/>``，
系统在用户点击时调用该应用注册的 COM 回调
``INotificationActivationCallback::Activate(aumid, arguments, inputs, count)``。
XHT 只是通知的**旁观者**，所以要让按钮真的生效，就得替系统把这一步补上：

1. 从通知里取 AUMID（``item.app_id``）与按钮的 ``arguments`` / 输入框内容；
2. 在注册表里找这个 AUMID 注册的激活器
   （``HKCU\\Software\\Classes\\AppUserModelId\\<AUMID>`` → ``CustomActivator`` = CLSID）；
3. ``CoCreateInstance`` 该 CLSID（``LocalServer32`` 指向应用自己的 exe，它会随
   命令行 ``-Embedding`` 被拉起并注册类工厂），直接按 vtable 调用 ``Activate``；
4. 拿不到激活器时按内容元素降级：``activationType="protocol"`` 的按钮用
   ``ShellExecute`` 打开协议，通知本体则用 ``shell:AppsFolder\\<AUMID>`` 把应用
   带到前台；``arguments="dismiss"`` 这类系统动作则直接把它从通知中心移除。

关于 COM 调用为什么走 :mod:`ctypes` 而不是 pywin32
--------------------------------------------------
pywin32 的 ``pythoncom.CoCreateInstance`` 能拿到 ``PyIUnknown``，但**交不出**底层
接口指针（没有 ``__int__``，也没法直接交给 :mod:`ctypes`），而
``INotificationActivationCallback`` 没有 typelib / IDispatch，无法用
``win32com.client`` 生成包装。所以这里用 :mod:`ctypes` 直接按 vtable 下标调用：
``Activate`` 是 ``IUnknown`` 之后的第一项，也就是 slot 3。系统为该接口注册了
proxy/stub（``HKCR\\Interface\\{53E31837-…}\\ProxyStubClsid32``），因此**跨进程**
也能正确封送参数。

线程模型
--------
``CoCreateInstance`` 可能等待目标应用的后台进程启动，所以 :func:`perform` 应该在
工作线程里调用（XHT 显示层就是这么做的），本模块自己负责每个线程的
``CoInitializeEx``。
"""

from __future__ import annotations

import ctypes
import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Sequence

from .errors import ActivationError
from .toast import (
    ACTIVATION_BACKGROUND,
    ACTIVATION_FOREGROUND,
    ACTIVATION_PROTOCOL,
    ACTIVATION_SYSTEM,
    SYSTEM_ARGUMENTS,
    ToastAction,
)

logger = logging.getLogger(__name__)

__all__ = [
    "STRATEGY_COM",
    "STRATEGY_PROTOCOL",
    "STRATEGY_ACTIVATE_APP",
    "STRATEGY_SHELL_APPS",
    "STRATEGY_DISMISS",
    "STRATEGY_SYSTEM",
    "STRATEGY_UNSUPPORTED",
    "IID_INotificationActivationCallback",
    "CLSID_ApplicationActivationManager",
    "IID_IApplicationActivationManager",
    "ActivationRequest",
    "ActivationPlan",
    "ActivationResult",
    "RegistryActivatorLookup",
    "ComActivatorCaller",
    "AppActivationManager",
    "ShellLauncher",
    "aumid_candidates",
    "build_plan",
    "perform",
    "activate",
    "dismiss_from_history",
    "hresult_message",
    "invoke_activate_callback",
    "invoke_activate_application",
]

#: ``INotificationActivationCallback``（notificationactivationcallback.h）
IID_INotificationActivationCallback = "{53E31837-6600-4A81-9395-75CFFE746F94}"

STRATEGY_COM = "com"
STRATEGY_PROTOCOL = "protocol"
STRATEGY_ACTIVATE_APP = "activate-application"
STRATEGY_SHELL_APPS = "shell-apps-folder"
STRATEGY_DISMISS = "dismiss"
STRATEGY_SYSTEM = "system"
STRATEGY_UNSUPPORTED = "unsupported"

#: ``CLSID_ApplicationActivationManager`` / ``IApplicationActivationManager``
#: （shobjidl_core.h）—— 桌面程序用它按 AUMID 拉起打包应用，并且**能带 arguments**。
#: 打包应用（UWP / MSIX）不会在注册表里登记 ``CustomActivator``，所以这是它们的
#: 主要降级通道：很多应用在 ``OnLaunched`` 里解析的正是这些 arguments。
CLSID_ApplicationActivationManager = "{45BA127D-10A8-46EA-8AB7-56EA9078943C}"
IID_IApplicationActivationManager = "{2E941141-7F97-4756-BA1D-9DECDE894A3D}"

_CLSCTX_LOCAL_SERVER = 0x4
_CLSCTX_INPROC_SERVER = 0x1
_COINIT_APARTMENTTHREADED = 0x2
_RPC_E_CHANGED_MODE = -2147417850  # 0x80010106

_SLOT_RELEASE = 2
_SLOT_ACTIVATE = 3
_SLOT_ACTIVATE_APPLICATION = 3

_S_OK = 0
_S_FALSE = 1

#: ``AUMID\CustomActivator`` 所在位置（用户级优先，再退到机器级）
_AUMID_KEYS = (
    (0x80000001, r"Software\Classes\AppUserModelId"),   # HKEY_CURRENT_USER
    (0x80000002, r"Software\Classes\AppUserModelId"),   # HKEY_LOCAL_MACHINE
)
_CLSID_KEYS = (
    (0x80000001, r"Software\Classes\CLSID"),
    (0x80000002, r"Software\Classes\CLSID"),
)

_GUID_PATTERN = re.compile(r"^\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
                           r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}$")

_HRESULT_MESSAGES = {
    -2147221164: "系统里没有注册这个 CLSID（应用可能已卸载）",              # REGDB_E_CLASSNOTREG
    -2147221167: "注册表里的 CLSID 注册不完整",                            # REGDB_E_IIDNOTREG
    -2147024891: "系统拒绝了激活请求（权限不足）",                          # E_ACCESSDENIED
    -2146959355: "应用的后台进程启动失败",                                  # CO_E_SERVER_EXEC_FAILURE
    -2147221008: "COM 尚未在本线程初始化",                                 # CO_E_NOTINITIALIZED
    -2147417850: "本线程已用另一种套间模型初始化过 COM",                    # RPC_E_CHANGED_MODE
    -2147467262: "应用没有实现所需的通知激活接口",                          # E_NOINTERFACE
}


def hresult_message(code: int) -> str:
    """HRESULT → 人话（未知码就返回十六进制）。"""
    if code is None:
        return ""
    signed = ctypes.c_long(int(code)).value
    text = _HRESULT_MESSAGES.get(signed)
    if text:
        return text
    if signed >= 0:
        return "成功（0x%08X）" % (signed & 0xFFFFFFFF)
    return "HRESULT 0x%08X" % (signed & 0xFFFFFFFF)


def aumid_candidates(aumid: Any) -> tuple:
    """AUMID 的注册表写法可能和通知里的不一样（斜杠方向、大小写），全部试一遍。"""
    text = str(aumid or "").strip()
    if not text:
        return ()
    variants = [
        text,
        text.lower(),
        text.replace("\\", "/"),
        text.replace("/", "\\"),
        text.lower().replace("\\", "/"),
        text.lower().replace("/", "\\"),
    ]
    seen: list = []
    for variant in variants:
        if variant and variant not in seen:
            seen.append(variant)
    return tuple(seen)


def _looks_like_protocol(arguments: str) -> bool:
    """``ms-settings:`` / ``https://…`` 这类协议串（``C:\\x.exe`` 不算）。"""
    text = str(arguments or "").strip()
    if not text or re.match(r"^[A-Za-z]:[\\/]", text):
        return False
    return bool(re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*:", text))


def _normalize_inputs(inputs: Any) -> tuple:
    """把输入内容统一成 ``((key, value), …)``。"""
    if not inputs:
        return ()
    items = inputs.items() if isinstance(inputs, dict) else inputs
    normalized: list = []
    for entry in items:
        try:
            key, value = entry
        except (TypeError, ValueError):
            continue
        key = str(key or "")
        if not key:
            continue
        normalized.append((key, "" if value is None else str(value)))
    return tuple(normalized)


# --------------------------------------------------------------------------- #
# 计划
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ActivationRequest:
    """一次激活请求的内容元素。"""

    app_id: str = ""
    arguments: str = ""
    activation_type: str = ACTIVATION_FOREGROUND
    inputs: tuple = ()
    label: str = ""
    target: str = "action"          # action / body / header

    def to_dict(self) -> dict:
        return {
            "app_id": self.app_id,
            "arguments": self.arguments,
            "activation_type": self.activation_type,
            "inputs": [{"key": key, "value": value} for key, value in self.inputs],
            "label": self.label,
            "target": self.target,
        }


@dataclass(frozen=True)
class ActivationPlan:
    """「这一下点击该怎么送回去」的结论（纯数据，可直接测试）。"""

    strategy: str = STRATEGY_UNSUPPORTED
    request: ActivationRequest = field(default_factory=ActivationRequest)
    clsid: str = ""
    server_command: str = ""
    detail: str = ""

    @property
    def executable(self) -> bool:
        return self.strategy in (STRATEGY_COM, STRATEGY_PROTOCOL, STRATEGY_ACTIVATE_APP,
                                 STRATEGY_SHELL_APPS, STRATEGY_DISMISS)

    @property
    def exact(self) -> bool:
        """是不是「精确送达」：COM 激活器与协议打开是原样送达，拉起应用只是尽力。"""
        return self.strategy in (STRATEGY_COM, STRATEGY_PROTOCOL, STRATEGY_DISMISS)

    def describe(self) -> str:
        parts = ["%s(%s)" % (self.request.label or self.request.target, self.strategy)]
        if self.clsid:
            parts.append("CLSID %s" % self.clsid)
        if self.server_command:
            parts.append(self.server_command)
        if self.detail:
            parts.append(self.detail)
        return " · ".join(parts)

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "request": self.request.to_dict(),
            "clsid": self.clsid,
            "server_command": self.server_command,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ActivationResult:
    """激活结果。"""

    ok: bool = False
    method: str = ""
    detail: str = ""
    error: str = ""
    hresult: int = 0
    dismissed: bool = False

    @property
    def message(self) -> str:
        if self.ok:
            return self.detail or "已发送"
        return self.error or self.detail or "激活失败"

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "method": self.method,
            "detail": self.detail,
            "error": self.error,
            "hresult": self.hresult,
            "dismissed": self.dismissed,
        }


# --------------------------------------------------------------------------- #
# 注册表：AUMID → CLSID → LocalServer32
# --------------------------------------------------------------------------- #
class RegistryActivatorLookup:
    """读注册表找通知激活器。

    ``hive_keys`` 可注入（默认 HKCU 再 HKLM），测试时不必碰真实注册表。

    也可以只实现 ``clsid_for`` / ``server_command`` 两个方法当作替身使用，
    :func:`build_plan` 只依赖这两个方法。
    """

    def __init__(self, *, hive_keys: Sequence = _AUMID_KEYS, cache: bool = True):
        self._hive_keys = tuple(hive_keys)
        self._aumid_cache: dict = {}
        self._command_cache: dict = {}
        self._cache_enabled = bool(cache)
        self._lock = threading.Lock()

    # -- 对外 -------------------------------------------------------------- #
    def clsid_for(self, aumid: Any) -> str:
        """返回该 AUMID 注册的 ``CustomActivator`` CLSID；没有就返回空串。"""
        for candidate in aumid_candidates(aumid):
            if self._cache_enabled:
                with self._lock:
                    if candidate in self._aumid_cache:
                        cached = self._aumid_cache[candidate]
                        if cached:
                            return cached
                        continue
            found = self._read_custom_activator(candidate)
            if self._cache_enabled:
                with self._lock:
                    self._aumid_cache[candidate] = found
            if found:
                return found
        return ""

    def server_command(self, clsid: Any) -> str:
        """返回 CLSID 对应的 ``LocalServer32`` / ``InprocServer32`` 命令行。"""
        text = str(clsid or "").strip()
        if not _GUID_PATTERN.match(text):
            return ""
        if self._cache_enabled:
            with self._lock:
                if text in self._command_cache:
                    return self._command_cache[text]
        command = ""
        for hive, prefix in _CLSID_KEYS:
            for subkey in ("LocalServer32", "InprocServer32"):
                value = _read_registry_default(hive, r"%s\%s\%s" % (prefix, text, subkey))
                if value:
                    command = value
                    break
            if command:
                break
        if self._cache_enabled:
            with self._lock:
                self._command_cache[text] = command
        return command

    def describe(self, aumid: Any) -> dict:
        clsid = self.clsid_for(aumid)
        return {"aumid": str(aumid or ""), "clsid": clsid,
                "server_command": self.server_command(clsid) if clsid else ""}

    # -- 内部 -------------------------------------------------------------- #
    def _read_custom_activator(self, aumid: str) -> str:
        for hive, prefix in self._hive_keys:
            value = _read_registry_default(hive, r"%s\%s" % (prefix, aumid), "CustomActivator")
            if value and _GUID_PATTERN.match(value.strip()):
                return value.strip()
        return ""


_LOOKUP_LOCK = threading.Lock()
_DEFAULT_LOOKUP: RegistryActivatorLookup | None = None


def default_lookup() -> RegistryActivatorLookup:
    """进程级共享的注册表查询器（带缓存，避免每次点击都读注册表）。"""
    global _DEFAULT_LOOKUP
    with _LOOKUP_LOCK:
        if _DEFAULT_LOOKUP is None:
            _DEFAULT_LOOKUP = RegistryActivatorLookup()
        return _DEFAULT_LOOKUP


def _registry_value(hive: int, path: str, name: str | None):
    """读注册表值，任何失败都返回 ``None``（非 Windows / 无权限 / 不存在）。"""
    try:
        import winreg
    except ImportError:  # pragma: no cover - 非 Windows
        return None
    try:
        with winreg.OpenKey(hive, path, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return value
    except (OSError, ValueError):
        return None


def _read_registry_default(hive: int, path: str, name: str | None = None):
    """读默认值（``name=None``）时用 ``QueryValue``，否则读指定名字的值。"""
    if name is None:
        try:
            import winreg
        except ImportError:  # pragma: no cover - 非 Windows
            return None
        try:
            with winreg.OpenKey(hive, path, 0, winreg.KEY_READ) as key:
                return winreg.QueryValue(key, None)
        except (OSError, ValueError):
            return None
    return _registry_value(hive, path, name)


# --------------------------------------------------------------------------- #
# ctypes：INotificationActivationCallback
# --------------------------------------------------------------------------- #
class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _UserInputData(ctypes.Structure):
    """``NOTIFICATION_USER_INPUT_DATA { LPCWSTR Key; LPCWSTR Value; }``"""

    _fields_ = [("Key", ctypes.c_wchar_p), ("Value", ctypes.c_wchar_p)]


_ACTIVATE_PROTOTYPE = ctypes.WINFUNCTYPE(
    ctypes.c_long,                 # HRESULT
    ctypes.c_void_p,               # this
    ctypes.c_wchar_p,              # appUserModelId
    ctypes.c_wchar_p,              # invokedArgs
    ctypes.POINTER(_UserInputData),  # data
    ctypes.c_ulong,                # count
)
_RELEASE_PROTOTYPE = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
_ACTIVATE_APPLICATION_PROTOTYPE = ctypes.WINFUNCTYPE(
    ctypes.c_long,                 # HRESULT
    ctypes.c_void_p,               # this
    ctypes.c_wchar_p,              # appUserModelId
    ctypes.c_wchar_p,              # arguments
    ctypes.c_int,                  # ACTIVATEOPTIONS
    ctypes.POINTER(ctypes.c_ulong),  # processId
)


def _ole32():
    try:
        library = ctypes.WinDLL("ole32", use_last_error=True)
    except (AttributeError, OSError) as exc:  # pragma: no cover - 非 Windows
        raise ActivationError("当前平台没有 ole32.dll，无法激活通知按钮：%s" % exc) from exc
    library.CLSIDFromString.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(_GUID)]
    library.CLSIDFromString.restype = ctypes.c_long
    library.CoCreateInstance.argtypes = [
        ctypes.POINTER(_GUID), ctypes.c_void_p, ctypes.c_ulong,
        ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p),
    ]
    library.CoCreateInstance.restype = ctypes.c_long
    library.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    library.CoInitializeEx.restype = ctypes.c_long
    library.CoUninitialize.argtypes = []
    library.CoUninitialize.restype = None
    return library


def guid_from_string(text: str) -> _GUID:
    """``{...}`` 形式的 GUID 字符串 → :class:`_GUID`。"""
    library = _ole32()
    guid = _GUID()
    code = library.CLSIDFromString(ctypes.c_wchar_p(str(text)), ctypes.byref(guid))
    if code < 0:
        raise ActivationError("不是合法的 GUID：%r" % (text,))
    return guid


def _build_input_array(inputs: Sequence) -> tuple:
    """``((key, value), …)`` → (ctypes 数组或 None, 个数)。"""
    pairs = _normalize_inputs(inputs)
    if not pairs:
        return None, 0
    array = (_UserInputData * len(pairs))()
    for index, (key, value) in enumerate(pairs):
        array[index].Key = key
        array[index].Value = value
    return array, len(pairs)


def invoke_activate_callback(pointer: Any, aumid: str, arguments: str,
                             inputs: Sequence = ()) -> int:
    """按 vtable 调用接口指针上的 ``Activate``，返回 HRESULT。

    ``pointer`` 是 ``INotificationActivationCallback*``（``c_void_p`` 或整数地址）。
    单独拆出来是为了能在测试里用一段自造的 vtable 验证参数封送，而不必真的去
    拉起第三方应用。
    """
    address = pointer if isinstance(pointer, ctypes.c_void_p) else ctypes.c_void_p(pointer)
    if not address:
        raise ActivationError("激活接口指针为空")
    vtable = ctypes.cast(address, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    entry = vtable[_SLOT_ACTIVATE]
    if not entry:
        raise ActivationError("激活接口的 vtable 里没有 Activate")
    data, count = _build_input_array(inputs)
    activate = _ACTIVATE_PROTOTYPE(entry)
    return int(activate(address, str(aumid or ""), str(arguments or ""), data, count))


def _release(pointer: Any) -> None:
    address = pointer if isinstance(pointer, ctypes.c_void_p) else ctypes.c_void_p(pointer)
    if not address:
        return
    try:
        vtable = ctypes.cast(address, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        entry = vtable[_SLOT_RELEASE]
        if entry:
            _RELEASE_PROTOTYPE(entry)(address)
    except Exception:  # noqa: BLE001 - 释放失败不值得打断调用方
        logger.debug("释放接口失败", exc_info=True)


class _ComApartment:
    """按线程初始化 COM 套间（``with`` 用法；已经初始化过就什么都不做）。"""

    def __init__(self) -> None:
        self._library = None
        self._initialized = False
        self.apartment_result = _S_OK

    def __enter__(self) -> "_ComApartment":
        self._library = _ole32()
        self.apartment_result = self._library.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
        # S_FALSE = 本线程已经初始化过；RPC_E_CHANGED_MODE = 已经用别的套间初始化过
        self._initialized = self.apartment_result in (_S_OK, _S_FALSE)
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if self._initialized and self._library is not None:
            try:
                self._library.CoUninitialize()
            except Exception:  # noqa: BLE001
                logger.debug("CoUninitialize 失败", exc_info=True)
        return False

    @property
    def failed(self) -> bool:
        return self.apartment_result < 0 and self.apartment_result != _RPC_E_CHANGED_MODE


def _create_instance(clsid: str, iid: str) -> tuple:
    """``CoCreateInstance`` → ``(hresult, 接口指针)``。"""
    library = _ole32()
    interface = ctypes.c_void_p()
    context = _CLSCTX_LOCAL_SERVER | _CLSCTX_INPROC_SERVER
    code = library.CoCreateInstance(
        ctypes.byref(guid_from_string(clsid)), None, context,
        ctypes.byref(guid_from_string(iid)), ctypes.byref(interface),
    )
    return int(code), interface


def invoke_activate_application(pointer: Any, aumid: str, arguments: str = "",
                                options: int = 0) -> tuple:
    """按 vtable 调用 ``IApplicationActivationManager::ActivateApplication``。

    返回 ``(HRESULT, 进程号)``；签名与 ``INotificationActivationCallback`` 无关，
    单独拆出来同样是为了能在测试里用自造 vtable 验证。
    """
    address = pointer if isinstance(pointer, ctypes.c_void_p) else ctypes.c_void_p(pointer)
    if not address:
        raise ActivationError("应用激活接口指针为空")
    vtable = ctypes.cast(address, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    entry = vtable[_SLOT_ACTIVATE_APPLICATION]
    if not entry:
        raise ActivationError("应用激活接口的 vtable 里没有 ActivateApplication")
    process_id = ctypes.c_ulong(0)
    activate = _ACTIVATE_APPLICATION_PROTOTYPE(entry)
    code = int(activate(address, str(aumid or ""), str(arguments or ""),
                        int(options), ctypes.byref(process_id)))
    return code, int(process_id.value)


class ComActivatorCaller:
    """``CoCreateInstance`` + ``Activate`` 的实际执行者（可在测试里替换）。"""

    def __init__(self, *, iid: str = IID_INotificationActivationCallback):
        self.iid = iid

    def call(self, clsid: str, aumid: str, arguments: str,
             inputs: Sequence = ()) -> tuple:
        """返回 ``(hresult, detail)``；``hresult < 0`` 表示失败。"""
        with _ComApartment() as apartment:
            if apartment.failed:
                code = apartment.apartment_result
                return code, "CoInitializeEx 失败：%s" % hresult_message(code)

            code, interface = _create_instance(clsid, self.iid)
            if code < 0:
                return code, "CoCreateInstance 失败：%s" % hresult_message(code)
            try:
                code = invoke_activate_callback(interface, aumid, arguments, inputs)
            finally:
                _release(interface)
            return code, hresult_message(code)


class AppActivationManager:
    """``IApplicationActivationManager``：按 AUMID 拉起打包应用（可带 arguments）。

    打包应用不在注册表里登记 ``CustomActivator``，但它们的 ``OnLaunched`` /
    ``OnActivated`` 通常就是解析这些 arguments 的入口，所以这是最接近「点一下系统
    通知按钮」的降级方案。桌面应用（没有包标识、只注册了 AUMID）可能失败，失败时
    由 :func:`perform` 退回 ``shell:AppsFolder``。
    """

    def __init__(self, *, clsid: str = CLSID_ApplicationActivationManager,
                 iid: str = IID_IApplicationActivationManager, options: int = 0):
        self.clsid = clsid
        self.iid = iid
        self.options = int(options)

    def activate(self, aumid: str, arguments: str = "") -> tuple:
        """返回 ``(是否成功, 说明)``。"""
        if not aumid:
            return False, "缺少 AUMID，无法拉起应用"
        with _ComApartment() as apartment:
            if apartment.failed:
                code = apartment.apartment_result
                return False, "CoInitializeEx 失败：%s" % hresult_message(code)

            code, interface = _create_instance(self.clsid, self.iid)
            if code < 0:
                return False, "CoCreateInstance(ApplicationActivationManager) 失败：%s" \
                              % hresult_message(code)
            try:
                code, process_id = invoke_activate_application(
                    interface, aumid, arguments, self.options)
            finally:
                _release(interface)

        if code < 0:
            return False, "ActivateApplication 失败：%s" % hresult_message(code)
        return True, "已按 AUMID 拉起应用%s（进程 %d）" % (
            "并传入 arguments=%s" % arguments if arguments else "", process_id)


# --------------------------------------------------------------------------- #
# ShellExecute 回退
# --------------------------------------------------------------------------- #
def _shell32():
    try:
        library = ctypes.WinDLL("shell32", use_last_error=True)
    except (AttributeError, OSError) as exc:  # pragma: no cover - 非 Windows
        raise ActivationError("当前平台没有 shell32.dll：%s" % exc) from exc
    library.ShellExecuteW.argtypes = [
        ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
        ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int,
    ]
    library.ShellExecuteW.restype = ctypes.c_void_p  # HINSTANCE
    return library


class ShellLauncher:
    """用 ``ShellExecuteW`` 打开协议 / 把应用带到前台。"""

    def __init__(self, *, show: int = 1):
        self.show = int(show)

    def execute(self, target: str, parameters: str = "") -> tuple:
        library = _shell32()
        ctypes.set_last_error(0)
        result = library.ShellExecuteW(None, None, str(target), str(parameters or ""),
                                       None, self.show)
        value = int(result or 0)
        if value <= 32:
            error = ctypes.get_last_error()
            return False, "ShellExecute 失败（代码 %s，Win32 错误 %s）" % (value, error)
        return True, "已通过 ShellExecute 打开 %s" % target

    def open_protocol(self, arguments: str) -> tuple:
        return self.execute(arguments)

    def open_apps_folder(self, aumid: str) -> tuple:
        if not aumid:
            return False, "缺少 AUMID，无法定位应用"
        return self.execute("explorer.exe", "shell:AppsFolder\\%s" % aumid)


# --------------------------------------------------------------------------- #
# 通知中心移除（系统动作 / 激活后收尾）
# --------------------------------------------------------------------------- #
def _remove_via_listener(item: Any) -> tuple:
    """``UserNotificationListener.RemoveNotification(id)``：需要「访问通知」授权。"""
    try:
        from winsdk.windows.ui.notifications.management import UserNotificationListener
    except ImportError:
        return False, "未安装 winsdk"
    notification_id = int(getattr(item, "id", 0) or 0)
    if notification_id <= 0:
        return False, "通知没有可用的编号"
    try:
        listener = UserNotificationListener.current
        listener.remove_notification(notification_id)
        return True, "已通过 UserNotificationListener 移除"
    except Exception as exc:  # noqa: BLE001 - 移除失败不算激活失败
        return False, "UserNotificationListener.RemoveNotification: %s" % _short_error(exc)


def _remove_via_winsdk(app_id: str, tag: str, group: str) -> tuple:
    """``ToastNotificationHistory.Remove(tag, group, aumid)``。

    注意：这个 API 要求调用方有**包标识**，普通桌面进程常常直接返回
    ``0x80073D54``（该进程没有程序包标识符），所以它只是「再试一次」。
    """
    try:
        from winsdk.windows.ui.notifications import ToastNotificationManager
    except ImportError:
        return False, "未安装 winsdk"
    try:
        ToastNotificationManager.history.remove(tag or "", group or "", app_id or "")
        return True, "已从通知中心移除"
    except Exception as exc:  # noqa: BLE001 - 移除失败不算激活失败
        return False, "ToastNotificationHistory.Remove: %s" % _short_error(exc)


def _remove_via_bridge(app_id: str, tag: str, group: str) -> tuple:
    """winsdk 不可用时，退回用桥接 exe（它依赖 .NET Framework 自带的 WinRT）。

    桥接 exe 需要带 ``remove`` 子命令（见 ``native/NotificationBridge.cs``）；
    老版本 exe 不认这个模式，会返回「没有结果」，这里如实报告。
    """
    try:
        from .bridge import WinRTBridge
    except ImportError:  # pragma: no cover
        return False, "桥接程序不可用"
    bridge = WinRTBridge()
    if not bridge.available:
        return False, "没有可用的 NotificationBridge.exe"
    try:
        for payload in bridge._stream("remove", str(tag or ""), str(group or ""),
                                      str(app_id or ""), stop_event=None):  # noqa: SLF001
            if payload.get("type") == "removed":
                return bool(payload.get("ok")), str(payload.get("detail") or "")
    except Exception as exc:  # noqa: BLE001
        return False, "%s: %s" % (type(exc).__name__, exc)
    return False, "桥接程序的 remove 子命令不可用（需要重新编译 native/NotificationBridge.exe）"


def _short_error(exc: BaseException) -> str:
    text = str(exc).strip().replace("\n", " ")
    return text if len(text) <= 120 else text[:120] + "…"


def dismiss_from_history(item: Any) -> tuple:
    """把通知从通知中心移除（best-effort）。返回 ``(是否成功, 说明)``。

    依次尝试：``UserNotificationListener.RemoveNotification``（需要通知访问授权）→
    ``ToastNotificationHistory.Remove``（需要包标识）→ 桥接 exe 的 ``remove``。
    三条路都失败很正常：**这不是本项目的强项**，只是「点了忽略就把那条也收掉」的
    锦上添花；失败只影响提示文案，不影响按钮本身的激活。
    """
    app_id = str(getattr(item, "app_id", "") or "")
    tag = str(getattr(item, "tag", "") or "")
    group = str(getattr(item, "group", "") or "")
    if not app_id and not getattr(item, "id", 0):
        return False, "缺少 AUMID 与通知编号，无法定位要移除的通知"

    reasons = []
    for attempt in (lambda: _remove_via_listener(item),
                    lambda: _remove_via_winsdk(app_id, tag, group),
                    lambda: _remove_via_bridge(app_id, tag, group)):
        ok, detail = attempt()
        if ok:
            return True, detail
        reasons.append(detail)
        logger.debug("移除通知失败：%s", detail)
    return False, "；".join(reasons)


# --------------------------------------------------------------------------- #
# 组装与执行
# --------------------------------------------------------------------------- #
def build_plan(item: Any, *, action: ToastAction | None = None, arguments: str | None = None,
               inputs: Any = None, target: str = "", lookup: Any = None) -> ActivationPlan:
    """决定「这次点击」用哪种策略送回去。

    * ``action`` 给了就是按钮点击；否则是通知本体 / header 点击（用 ``launch``）。
    * ``arguments`` 可覆盖按钮自带的 ``arguments``。
    * ``inputs`` 是用户填写的 ``{输入框 id: 文本}``（或 ``((id, 文本), …)``）。
    """
    content = getattr(item, "content", None)
    app_id = str(getattr(item, "app_id", "") or "")
    normalized_inputs = _normalize_inputs(inputs)

    if action is not None:
        target = target or "action"
        label = action.label
        if arguments is None:
            arguments = action.arguments
        activation_type = action.activation_type or ACTIVATION_FOREGROUND
    else:
        target = target or ("header" if arguments and content is not None
                            and content.header is not None
                            and arguments == content.header.arguments else "body")
        launch = getattr(content, "launch", "") if content is not None else ""
        label = str(getattr(item, "title", "") or "") or "通知"
        if arguments is None:
            arguments = launch
        activation_type = (getattr(content, "activation_type", "") or ACTIVATION_FOREGROUND)

    request = ActivationRequest(
        app_id=app_id,
        arguments=str(arguments or ""),
        activation_type=str(activation_type or ACTIVATION_FOREGROUND).lower(),
        inputs=normalized_inputs,
        label=str(label or ""),
        target=target,
    )

    # 1) 系统动作：通知平台自己就能做，我们只负责把通知拿掉
    if request.activation_type == ACTIVATION_SYSTEM or request.arguments in SYSTEM_ARGUMENTS:
        detail = "系统动作「%s」按关闭处理" % (request.arguments or "dismiss")
        return ActivationPlan(strategy=STRATEGY_DISMISS, request=request, detail=detail)

    if not app_id and request.activation_type != ACTIVATION_PROTOCOL:
        return ActivationPlan(strategy=STRATEGY_UNSUPPORTED, request=request,
                              detail="通知里没有 AUMID，无法定位应用")

    # 2) 协议动作：arguments 本身就是要打开的 URI（ms-settings:、https://…）
    if request.activation_type == ACTIVATION_PROTOCOL:
        if not request.arguments:
            return ActivationPlan(strategy=STRATEGY_UNSUPPORTED, request=request,
                                  detail="协议动作缺少 arguments")
        return ActivationPlan(strategy=STRATEGY_PROTOCOL, request=request,
                              detail="用协议打开 %s" % request.arguments)

    # 3) 应用注册的 COM 激活器（Win32 通知的标准做法）
    lookup = lookup if lookup is not None else default_lookup()
    clsid = ""
    try:
        clsid = str(lookup.clsid_for(app_id) or "")
    except Exception as exc:  # noqa: BLE001 - 注册表读取失败不能让点击直接崩
        logger.debug("查询通知激活器失败：%s", exc)
    if clsid:
        command = ""
        try:
            command = str(lookup.server_command(clsid) or "")
        except Exception:  # noqa: BLE001
            command = ""
        return ActivationPlan(strategy=STRATEGY_COM, request=request, clsid=clsid,
                              server_command=command,
                              detail="通过应用注册的 COM 激活器发送 arguments=%s"
                                     % (request.arguments or "(空)"))

    # 4) 打包应用（UWP / MSIX）不登记 CustomActivator：用 ApplicationActivationManager
    #    按 AUMID 拉起并把 arguments 交进去，这是最接近「点一下系统通知」的降级方案。
    if app_id:
        if target != "action":
            return ActivationPlan(strategy=STRATEGY_ACTIVATE_APP, request=request,
                                  detail="应用没有注册通知激活器，改为按 AUMID 拉起应用"
                                         + ("（带 launch 参数）" if request.arguments else ""))
        if request.activation_type in (ACTIVATION_FOREGROUND, ""):
            return ActivationPlan(
                strategy=STRATEGY_ACTIVATE_APP, request=request,
                detail="应用没有注册通知激活器，尽力把动作交给应用（可能只被带到前台）")
        if request.activation_type == ACTIVATION_BACKGROUND:
            return ActivationPlan(strategy=STRATEGY_UNSUPPORTED, request=request,
                                  detail="后台动作需要应用注册 COM 激活器，当前系统里没有登记 %s"
                                         % app_id)

    return ActivationPlan(strategy=STRATEGY_UNSUPPORTED, request=request,
                          detail="应用 %s 没有在注册表里登记可用的通知激活器" % app_id)


def perform(plan: ActivationPlan, *, item: Any = None, com_caller: Any = None,
            launcher: Any = None, dismisser: Any = None, app_activator: Any = None,
            dismiss_after: bool = True) -> ActivationResult:
    """执行 :func:`build_plan` 给出的计划。

    ``com_caller`` / ``app_activator`` / ``launcher`` / ``dismisser`` 可注入，
    测试里不必碰真实系统。
    """
    strategy = plan.strategy
    if strategy == STRATEGY_UNSUPPORTED:
        return ActivationResult(ok=False, method=strategy, error=plan.detail or "无法激活")
    if strategy == STRATEGY_SYSTEM:
        return ActivationResult(ok=True, method=strategy, detail="系统动作，无需处理")

    if strategy == STRATEGY_DISMISS:
        remove = dismisser or dismiss_from_history
        if item is None:
            return ActivationResult(ok=False, method=strategy, error="缺少通知对象，无法移除")
        ok, detail = _safe_call(remove, item)
        return ActivationResult(ok=bool(ok), method=strategy, detail=detail if ok else "",
                                error="" if ok else detail, dismissed=bool(ok))

    if strategy == STRATEGY_PROTOCOL:
        launcher = launcher or ShellLauncher()
        ok, detail = _safe_call(launcher.open_protocol, plan.request.arguments)
        if not ok:
            return ActivationResult(ok=False, method=strategy, error=detail)
        result = ActivationResult(ok=True, method=strategy, detail=detail)
        return _maybe_dismiss(result, plan, item, dismisser, dismiss_after)

    if strategy == STRATEGY_ACTIVATE_APP:
        activator = app_activator or AppActivationManager()
        ok, detail = _safe_call(activator.activate, plan.request.app_id,
                                plan.request.arguments)
        if ok:
            result = ActivationResult(ok=True, method=strategy, detail=detail)
            return _maybe_dismiss(result, plan, item, dismisser, dismiss_after)
        # 桌面应用常常不在 AppsFolder 里，但「打开应用」总比什么都不做强
        logger.debug("ActivateApplication 失败（%s），退回 shell:AppsFolder", detail)
        launcher = launcher or ShellLauncher()
        fallback_ok, fallback_detail = _safe_call(launcher.open_apps_folder, plan.request.app_id)
        if fallback_ok:
            result = ActivationResult(ok=True, method=STRATEGY_SHELL_APPS,
                                      detail="%s（ActivateApplication 失败：%s）"
                                             % (fallback_detail, detail))
            return _maybe_dismiss(result, plan, item, dismisser, dismiss_after)
        return ActivationResult(ok=False, method=strategy,
                                error="%s；退回打开应用也失败：%s" % (detail, fallback_detail))

    if strategy == STRATEGY_SHELL_APPS:
        launcher = launcher or ShellLauncher()
        ok, detail = _safe_call(launcher.open_apps_folder, plan.request.app_id)
        if not ok:
            return ActivationResult(ok=False, method=strategy, error=detail)
        result = ActivationResult(ok=True, method=strategy, detail=detail)
        return _maybe_dismiss(result, plan, item, dismisser, dismiss_after)

    # STRATEGY_COM
    caller = com_caller or ComActivatorCaller()
    try:
        code, detail = caller.call(plan.clsid, plan.request.app_id, plan.request.arguments,
                                   plan.request.inputs)
    except ActivationError as exc:
        return ActivationResult(ok=False, method=strategy, error=str(exc))
    except Exception as exc:  # noqa: BLE001 - COM 调用的任何意外都要变成「失败」而不是崩界面
        return ActivationResult(ok=False, method=strategy,
                                error="激活时出错：%s: %s" % (type(exc).__name__, exc))
    ok = int(code) >= 0
    if not ok:
        # 调用方给的 detail 可能只是符号名（E_XXX），补上人话的 HRESULT 说明
        message = hresult_message(code) or ""
        if detail and detail not in message:
            message = "%s（%s）" % (message, detail) if message else detail
        return ActivationResult(ok=False, method=strategy,
                                error="COM 激活失败：%s" % (message or "未知错误"),
                                hresult=int(code))
    result = ActivationResult(ok=True, method=strategy,
                              detail="已通过 COM 激活器发送（%s）" % (detail or "S_OK"),
                              hresult=int(code))
    return _maybe_dismiss(result, plan, item, dismisser, dismiss_after)


def _maybe_dismiss(result: ActivationResult, plan: ActivationPlan, item: Any,
                   dismisser: Any, dismiss_after: bool) -> ActivationResult:
    """激活成功后顺手把通知从通知中心拿掉（和系统点击行为一致）。"""
    if not dismiss_after or item is None:
        return result
    remove = dismisser or dismiss_from_history
    ok, detail = _safe_call(remove, item)
    if ok:
        return ActivationResult(ok=result.ok, method=result.method, detail=result.detail,
                                error=result.error, hresult=result.hresult, dismissed=True)
    logger.debug("激活成功但移除通知失败：%s", detail)
    return result


def _safe_call(function, *args) -> tuple:
    try:
        outcome = function(*args)
    except Exception as exc:  # noqa: BLE001
        return False, "%s: %s" % (type(exc).__name__, exc)
    if isinstance(outcome, tuple) and len(outcome) == 2:
        return bool(outcome[0]), str(outcome[1])
    return bool(outcome), ""


def activate(item: Any, *, action: ToastAction | None = None, arguments: str | None = None,
             inputs: Any = None, target: str = "", lookup: Any = None,
             com_caller: Any = None, launcher: Any = None, dismisser: Any = None,
             app_activator: Any = None, dismiss_after: bool = True) -> ActivationResult:
    """:func:`build_plan` + :func:`perform` 的便捷入口（同步执行）。"""
    plan = build_plan(item, action=action, arguments=arguments, inputs=inputs,
                      target=target, lookup=lookup)
    logger.debug("激活计划：%s", plan.describe())
    return perform(plan, item=item, com_caller=com_caller, launcher=launcher,
                   dismisser=dismisser, app_activator=app_activator,
                   dismiss_after=dismiss_after)
