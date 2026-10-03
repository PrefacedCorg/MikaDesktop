"""系统状态后端：网络 / 音量 / 电源，以及系统原生面板入口。

扩展窗口上的按钮需要这几类信息，本模块把它们全部收在一处（纯逻辑、不依赖 Qt
窗口），整体丢进后台线程轮询（见 :class:`SystemStatusWorker`）。

设计约定
--------
* **任何一项失败都不抛异常**：每个函数返回带 ``error`` 字段的 dict，界面照常显示
  （拿不到就显示"未知"），绝不因为一个 WMI/COM 小毛病把 dock 带崩。
* **COM 按线程初始化**：后台线程里 ``pythoncom`` / ``comtypes`` 都要先
  ``CoInitialize``；对象按线程缓存在 :data:`_local` 上（COM 单元模型要求同线程复用）。
* **原生面板走 shell URI（Win 热键这条路走不通）**：``ms-availablenetworks:`` 打开
  WLAN 网络列表页、``ms-actioncenter:`` 打开通知中心、``ms-settings:batterysaver``
  落在设置的电池页。**「快速设置主界面」没有对应 URI**，而 ``Win+A`` 在本机注不进去
  （SendInput 对 Win 键直接返回 0，keybd_event 也不触发 shell 热键），所以走
  "先开 WLAN 页 → UI Automation 点它的「后退」"回到主界面。
  URI 未注册时会弹"打开方式"对话框，所以调用前先查注册表。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import sys
import threading
import winreg
from typing import Any, Dict, List

from PySide6.QtCore import QThread, Signal

# 本模块可能被"没有日志"的场景导入（例如单测），日志取不到就退化
try:
    from . import log_maker
    log = log_maker.logger()
except Exception:  # pragma: no cover - 仅在日志模块缺失时走到
    import logging
    log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_STATUS_INTERVAL_MS",
    "SystemStatusWorker",
    "windows_build",
    "is_windows_11",
    "power_status",
    "network_status",
    "volume_status",
    "uri_registered",
    "shell_open",
    "open_battery_settings",
    "open_quick_settings",
    "open_quick_settings_main",
    "click_quick_settings_back",
    "open_notification_center",
    "collect_status",
]

#: Windows 11 起始内部版本号
WINDOWS_11_BUILD = 22000
#: 状态轮询默认间隔（毫秒）：音量/网络变化要跟得上，又不能太费
DEFAULT_STATUS_INTERVAL_MS = 2000

#: 每个线程一份的 COM / 原生句柄缓存与初始化标记
_local = threading.local()

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)


def _com_ready() -> None:
    """当前线程初始化 COM（可重复调用）。"""
    if getattr(_local, "com_ready", False):
        return
    try:
        import pythoncom

        pythoncom.CoInitialize()
    except Exception as e:  # pragma: no cover - 只在 pywin32 缺失时走到
        log.debug(f"pythoncom.CoInitialize 失败: {e}")
    try:
        import comtypes

        comtypes.CoInitialize()
    except Exception as e:
        log.debug(f"comtypes.CoInitialize 失败: {e}")
    _local.com_ready = True


# =========================================================================== #
# 版本
# =========================================================================== #
def windows_build() -> int:
    """当前 Windows 内部版本号（拿不到返回 0）。"""
    try:
        return int(sys.getwindowsversion().build)
    except Exception:
        return 0


def is_windows_11() -> bool:
    """是否是 Windows 11（按内部版本号判定；``platform.release()`` 在 Win11 上也常报 10）。"""
    return windows_build() >= WINDOWS_11_BUILD


# =========================================================================== #
# 电源 / 电池
# =========================================================================== #
class _SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_byte),
        ("BatteryFlag", ctypes.c_byte),
        ("BatteryLifePercent", ctypes.c_byte),
        ("SystemStatusFlag", ctypes.c_byte),
        ("BatteryLifeTime", ctypes.c_ulong),
        ("BatteryFullLifeTime", ctypes.c_ulong),
    ]


_kernel32.GetSystemPowerStatus.argtypes = [ctypes.POINTER(_SYSTEM_POWER_STATUS)]
_kernel32.GetSystemPowerStatus.restype = wt.BOOL

_BATTERY_FLAG_CHARGING = 0x08
_BATTERY_FLAG_NO_BATTERY = 0x80
_BATTERY_FLAG_UNKNOWN = 0xFF
_AC_UNKNOWN = 0xFF


def power_status() -> Dict[str, Any]:
    """电池 / 电源状态。

    Returns:
        ``{"present", "percent", "ac", "charging", "error"}``：
        ``present=False`` 表示这台机器没有电池（台式机 → 界面不显示电池按钮）。
    """
    result: Dict[str, Any] = {"present": False, "percent": None, "ac": None,
                              "charging": False, "error": ""}
    try:
        status = _SYSTEM_POWER_STATUS()
        if not _kernel32.GetSystemPowerStatus(ctypes.byref(status)):
            result["error"] = "GetSystemPowerStatus 失败"
            return result
        flag = int(status.BatteryFlag) & 0xFF
        present = flag not in (_BATTERY_FLAG_NO_BATTERY, _BATTERY_FLAG_UNKNOWN)
        percent = int(status.BatteryLifePercent)
        result["present"] = present
        result["percent"] = percent if 0 <= percent <= 100 else None
        result["ac"] = None if int(status.ACLineStatus) & 0xFF == _AC_UNKNOWN \
            else bool(int(status.ACLineStatus) & 0xFF)
        result["charging"] = bool(flag & _BATTERY_FLAG_CHARGING)
        return result
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        return result


# =========================================================================== #
# 网络
# =========================================================================== #
class _IP_ADAPTER_ADDRESSES(ctypes.Structure):
    """``IP_ADAPTER_ADDRESSES_LH`` 的前半段（够读到介质类型与运行状态）。

    开头是 ``union { ULONGLONG Alignment; struct { ULONG Length; IfIndex; } }``，
    在 64 位下等价于两个 ULONG；后面的指针字段按自然对齐排布，所以这段前缀
    与系统结构体一致。只读前半段可以让定义短很多，也不影响字段偏移。
    """

    _fields_ = [
        ("Length", ctypes.c_ulong),
        ("IfIndex", ctypes.c_ulong),
        ("Next", ctypes.c_void_p),
        ("AdapterName", ctypes.c_char_p),
        ("FirstUnicastAddress", ctypes.c_void_p),
        ("FirstAnycastAddress", ctypes.c_void_p),
        ("FirstMulticastAddress", ctypes.c_void_p),
        ("FirstDnsServerAddress", ctypes.c_void_p),
        ("DnsSuffix", ctypes.c_wchar_p),
        ("Description", ctypes.c_wchar_p),
        ("FriendlyName", ctypes.c_wchar_p),
        ("PhysicalAddress", ctypes.c_ubyte * 8),
        ("PhysicalAddressLength", ctypes.c_ulong),
        ("Flags", ctypes.c_ulong),
        ("Mtu", ctypes.c_ulong),
        ("IfType", ctypes.c_ulong),
        ("OperStatus", ctypes.c_int),
    ]


_iphlpapi.GetAdaptersAddresses.argtypes = [
    ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p,
    ctypes.POINTER(_IP_ADAPTER_ADDRESSES), ctypes.POINTER(ctypes.c_ulong),
]
_iphlpapi.GetAdaptersAddresses.restype = ctypes.c_ulong

_IF_TYPE_ETHERNET = 6
_IF_TYPE_IEEE80211 = 71
_IF_OPER_STATUS_UP = 1
_ERROR_BUFFER_OVERFLOW = 111


def _adapters() -> List[Dict[str, Any]]:
    """网卡列表（介质类型 / 运行状态 / 友好名）；失败返回空列表。"""
    flags = 0x0001 | 0x0002 | 0x0004 | 0x0008  # 跳过单播/任播/组播/DNS 地址
    try:
        size = ctypes.c_ulong(16 * 1024)
        buffer = ctypes.create_string_buffer(size.value)
        ret = _iphlpapi.GetAdaptersAddresses(
            0, flags, None, ctypes.cast(buffer, ctypes.POINTER(_IP_ADAPTER_ADDRESSES)),
            ctypes.byref(size),
        )
        if ret == _ERROR_BUFFER_OVERFLOW:
            buffer = ctypes.create_string_buffer(size.value)
            ret = _iphlpapi.GetAdaptersAddresses(
                0, flags, None, ctypes.cast(buffer, ctypes.POINTER(_IP_ADAPTER_ADDRESSES)),
                ctypes.byref(size),
            )
        if ret != 0:
            log.debug(f"GetAdaptersAddresses 返回 {ret}")
            return []

        out: List[Dict[str, Any]] = []
        node = ctypes.cast(buffer, ctypes.POINTER(_IP_ADAPTER_ADDRESSES))
        for _ in range(64):  # 防御：链表最多看 64 张网卡
            if not node:
                break
            adapter = node.contents
            out.append({
                "if_type": int(adapter.IfType),
                "oper_status": int(adapter.OperStatus),
                "friendly_name": adapter.FriendlyName or "",
                "description": adapter.Description or "",
            })
            if not adapter.Next:
                break
            node = ctypes.cast(adapter.Next, ctypes.POINTER(_IP_ADAPTER_ADDRESSES))
        return out
    except Exception as e:
        log.debug(f"读取网卡列表失败: {e}")
        return []


def _nlm():
    """本线程缓存的 INetworkListManager（失败返回 ``None``）。"""
    if getattr(_local, "nlm_failed", False):
        return None
    manager = getattr(_local, "nlm", None)
    if manager is not None:
        return manager
    try:
        _com_ready()
        import win32com.client

        manager = win32com.client.Dispatch("{DCB00C01-570F-4A9B-8D69-199FDBA5723B}")
        _local.nlm = manager
        return manager
    except Exception as e:
        log.debug(f"创建 NetworkListManager 失败: {e}")
        _local.nlm_failed = True
        return None


def network_status() -> Dict[str, Any]:
    """网络状态。

    Returns:
        ``{"connected", "internet", "name", "medium", "error"}``：
        ``medium`` 为 ``"wifi"`` / ``"ethernet"`` / ``""``。
    """
    result: Dict[str, Any] = {"connected": None, "internet": None, "name": "",
                              "medium": "", "error": ""}
    adapters = _adapters()
    up = [a for a in adapters if a["oper_status"] == _IF_OPER_STATUS_UP]
    if any(a["if_type"] == _IF_TYPE_IEEE80211 for a in up):
        result["medium"] = "wifi"
    elif any(a["if_type"] == _IF_TYPE_ETHERNET for a in up):
        result["medium"] = "ethernet"

    manager = _nlm()
    if manager is not None:
        try:
            result["connected"] = bool(manager.IsConnected)
            result["internet"] = bool(manager.IsConnectedToInternet)
            names = []
            for network in manager.GetNetworks(1):  # NLM_ENUM_NETWORK_CONNECTED
                try:
                    names.append(str(network.GetName() or ""))
                except Exception:
                    continue
            result["name"] = names[0] if names else ""
        except Exception as e:
            result["error"] = f"{type(e).__name__}: {e}"
            _local.nlm = None  # 下次重建（COM 对象可能已失效）

    if result["connected"] is None:      # NLM 不可用 → 用网卡运行状态兜底
        result["connected"] = bool(up)
        if not result["name"] and up:
            result["name"] = up[0]["friendly_name"]
    return result


# =========================================================================== #
# 音量
# =========================================================================== #
_volume_types = None


def _volume_api():
    """懒加载并返回 ``(comtypes, enum_clsid, IMMDeviceEnumerator, IAudioEndpointVolume)``。"""
    global _volume_types
    if _volume_types is not None:
        return _volume_types
    import comtypes
    from comtypes import COMMETHOD, GUID, HRESULT, IUnknown
    from ctypes import POINTER, c_void_p

    class IAudioEndpointVolume(IUnknown):
        _iid_ = GUID("{5CDF2C82-841E-4546-9722-0CF74078229A}")
        _methods_ = [
            COMMETHOD([], HRESULT, "RegisterControlChangeNotify", (["in"], c_void_p, "pNotify")),
            COMMETHOD([], HRESULT, "UnregisterControlChangeNotify", (["in"], c_void_p, "pNotify")),
            COMMETHOD([], HRESULT, "GetChannelCount", (["out"], POINTER(ctypes.c_uint), "pn")),
            COMMETHOD([], HRESULT, "SetMasterVolumeLevel",
                      (["in"], ctypes.c_float, "f"), (["in"], c_void_p, "ctx")),
            COMMETHOD([], HRESULT, "SetMasterVolumeLevelScalar",
                      (["in"], ctypes.c_float, "f"), (["in"], c_void_p, "ctx")),
            COMMETHOD([], HRESULT, "GetMasterVolumeLevel",
                      (["out"], POINTER(ctypes.c_float), "pf")),
            COMMETHOD([], HRESULT, "GetMasterVolumeLevelScalar",
                      (["out"], POINTER(ctypes.c_float), "pf")),
            COMMETHOD([], HRESULT, "SetChannelVolumeLevel",
                      (["in"], ctypes.c_uint, "n"), (["in"], ctypes.c_float, "f"),
                      (["in"], c_void_p, "ctx")),
            COMMETHOD([], HRESULT, "SetChannelVolumeLevelScalar",
                      (["in"], ctypes.c_uint, "n"), (["in"], ctypes.c_float, "f"),
                      (["in"], c_void_p, "ctx")),
            COMMETHOD([], HRESULT, "GetChannelVolumeLevel",
                      (["in"], ctypes.c_uint, "n"), (["out"], POINTER(ctypes.c_float), "pf")),
            COMMETHOD([], HRESULT, "GetChannelVolumeLevelScalar",
                      (["in"], ctypes.c_uint, "n"), (["out"], POINTER(ctypes.c_float), "pf")),
            COMMETHOD([], HRESULT, "SetMute",
                      (["in"], ctypes.c_int, "mute"), (["in"], c_void_p, "ctx")),
            COMMETHOD([], HRESULT, "GetMute", (["out"], POINTER(ctypes.c_int), "pmute")),
            COMMETHOD([], HRESULT, "GetVolumeStepInfo",
                      (["out"], POINTER(ctypes.c_uint), "step"),
                      (["out"], POINTER(ctypes.c_uint), "count")),
            COMMETHOD([], HRESULT, "VolumeStepUp", (["in"], c_void_p, "ctx")),
            COMMETHOD([], HRESULT, "VolumeStepDown", (["in"], c_void_p, "ctx")),
            COMMETHOD([], HRESULT, "QueryHardwareSupport",
                      (["out"], POINTER(ctypes.c_uint), "mask")),
            COMMETHOD([], HRESULT, "GetVolumeRange",
                      (["out"], POINTER(ctypes.c_float), "mn"),
                      (["out"], POINTER(ctypes.c_float), "mx"),
                      (["out"], POINTER(ctypes.c_float), "inc")),
        ]

    class IMMDevice(IUnknown):
        _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
        _methods_ = [
            COMMETHOD([], HRESULT, "Activate",
                      (["in"], ctypes.POINTER(GUID), "iid"),
                      (["in"], ctypes.c_uint, "ctx"),
                      (["in"], c_void_p, "params"),
                      (["out"], POINTER(POINTER(IAudioEndpointVolume)), "pp")),
        ]

    class IMMDeviceEnumerator(IUnknown):
        _iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
        _methods_ = [
            COMMETHOD([], HRESULT, "EnumAudioEndpoints",
                      (["in"], ctypes.c_uint, "flow"), (["in"], ctypes.c_uint, "mask"),
                      (["out"], POINTER(c_void_p), "devices")),
            COMMETHOD([], HRESULT, "GetDefaultAudioEndpoint",
                      (["in"], ctypes.c_uint, "flow"), (["in"], ctypes.c_uint, "role"),
                      (["out"], POINTER(POINTER(IMMDevice)), "endpoint")),
        ]

    _volume_types = (comtypes, IMMDeviceEnumerator, IAudioEndpointVolume)
    return _volume_types


def _volume_endpoint():
    """本线程缓存的默认扬声器音量接口（失败返回 ``None``）。"""
    if getattr(_local, "volume_failed", False):
        return None
    endpoint = getattr(_local, "volume", None)
    if endpoint is not None:
        return endpoint
    try:
        _com_ready()
        comtypes, enumerator_type, volume_type = _volume_api()
        enumerator = comtypes.CoCreateInstance(
            comtypes.GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}"),
            interface=enumerator_type, clsctx=comtypes.CLSCTX_ALL)
        device = enumerator.GetDefaultAudioEndpoint(0, 1)   # eRender, eMultimedia
        endpoint = device.Activate(
            ctypes.byref(volume_type._iid_), comtypes.CLSCTX_ALL, None)
        _local.volume = endpoint
        return endpoint
    except Exception as e:
        log.debug(f"创建 IAudioEndpointVolume 失败: {e}")
        _local.volume_failed = True
        return None


def volume_status() -> Dict[str, Any]:
    """主音量。

    Returns:
        ``{"percent", "muted", "error"}``；``percent`` 为 ``None`` 表示读不到。
    """
    result: Dict[str, Any] = {"percent": None, "muted": None, "error": ""}
    endpoint = _volume_endpoint()
    if endpoint is None:
        result["error"] = "音量接口不可用"
        return result
    try:
        scalar = float(endpoint.GetMasterVolumeLevelScalar())
        result["percent"] = max(0, min(100, int(round(scalar * 100))))
        result["muted"] = bool(endpoint.GetMute())
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        _local.volume = None
    return result


# =========================================================================== #
# 系统原生面板
# =========================================================================== #
def uri_registered(uri: str) -> bool:
    """shell URI 协议是否已注册（未注册时 ``ShellExecute`` 会弹"打开方式"对话框）。"""
    scheme = uri.split(":", 1)[0].strip()
    if not scheme:
        return False
    for root, base in ((winreg.HKEY_CURRENT_USER, r"Software\Classes"),
                       (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Classes")):
        try:
            with winreg.OpenKey(root, base + "\\" + scheme):
                return True
        except OSError:
            continue
    return False


def shell_open(uri: str) -> bool:
    """用 ShellExecute 打开一个 shell URI（成功返回 True）。"""
    if not uri_registered(uri):
        log.info(f"系统未注册 {uri}，跳过（避免弹出“打开方式”对话框）")
        return False
    try:
        os.startfile(uri)
        return True
    except Exception as e:
        log.warning(f"打开 {uri} 失败: {e}")
        return False


#: 左 Win 键（Win+A 打开快速设置主界面；实测本机注入无效，见下）
_VK_LWIN = 0x5B
_KEYEVENTF_KEYUP = 0x0002

_user32.keybd_event.argtypes = [ctypes.c_ubyte, ctypes.c_ubyte, ctypes.c_ulong,
                                ctypes.c_void_p]

# ---- UI Automation：用来点快速设置面板左上角的「后退」 ----
# 注：Win+A 本来是最直接的"打开快速设置主界面"方式，但实测本机（Win11 26300）
# 注入不进去 —— SendInput 对 Win 键直接返回 0，keybd_event 也不触发 shell 热键
# （连 Win 单击都弹不出开始菜单）。唯一可靠的路子是：用 shell URI 打开快速设置
# （它落在 WLAN 页），再 UIA 点面板自己的「后退」回到主界面。
_uia_cache = None


def _uia_objects():
    """懒加载 ``(module, automation, walker)``；不可用时三项都是 ``None``。"""
    global _uia_cache
    if _uia_cache is not None:
        return _uia_cache
    try:
        _com_ready()
        import comtypes
        import comtypes.client
        from comtypes import CLSCTX_INPROC_SERVER, CoCreateInstance

        comtypes.CoInitialize()
        module = comtypes.client.GetModule("UIAutomationCore.dll")
        automation = CoCreateInstance(module.CUIAutomation._reg_clsid_,
                                      interface=module.IUIAutomation,
                                      clsctx=CLSCTX_INPROC_SERVER)
        _uia_cache = (module, automation, automation.ControlViewWalker)
    except Exception as e:
        log.debug(f"UI Automation 不可用: {e}")
        _uia_cache = (None, None, None)
    return _uia_cache


def _control_center_element():
    """快速设置窗口的 UIA 元素（没打开时返回 ``None``）。"""
    _module, automation, walker = _uia_objects()
    if automation is None:
        return None
    try:
        child = walker.GetFirstChildElement(automation.GetRootElement())
    except Exception:
        return None
    while child:
        try:
            if (child.CurrentClassName or "") == "ControlCenterWindow":
                return child
        except Exception:
            pass
        child = walker.GetNextSiblingElement(child)
    return None


def _find_button(element, keyword: str, max_depth: int = 8, limit: int = 2000):
    """在 ``element`` 子树里按名字找按钮（只认 Button 控件类型）。"""
    _module, _automation, walker = _uia_objects()
    if element is None or walker is None:
        return None
    stack = [(element, 0)]
    seen = 0
    while stack and seen < limit:
        node, depth = stack.pop()
        if depth >= max_depth:
            continue
        try:
            child = walker.GetFirstChildElement(node)
        except Exception:
            continue
        while child:
            seen += 1
            try:
                if 50000 == int(child.CurrentControlType) and keyword in (child.CurrentName or ""):
                    return child
            except Exception:
                pass
            stack.append((child, depth + 1))
            child = walker.GetNextSiblingElement(child)
    return None


def click_quick_settings_back() -> bool:
    """点快速设置面板左上角的「后退」，从 WLAN 页回到主界面。

    返回 ``False`` 表示没找到那个按钮（面板没开、或本来就停在主界面）。
    """
    module, _automation, _walker = _uia_objects()
    if module is None:
        return False
    element = _find_button(_control_center_element(), "后退")
    if element is None:
        return False
    try:
        raw = element.GetCurrentPattern(10000)      # UIA_InvokePatternId
        typed = raw.QueryInterface(module.IUIAutomationInvokePattern)
        typed.Invoke()
        log.info("已点击快速设置的「后退」，回到主界面")
        return True
    except Exception as e:
        log.debug(f"点击快速设置的「后退」失败: {e}")
        return False


def _schedule_back_click(delay_ms: int = 700, retries: int = 3) -> None:
    """延迟点「后退」；没找到就稍后重试（走 QTimer，不阻塞界面）。"""

    def attempt(left: int) -> None:
        if click_quick_settings_back():
            return
        if left > 0:
            try:
                from PySide6.QtCore import QTimer

                QTimer.singleShot(300, lambda: attempt(left - 1))
            except Exception as e:
                log.debug(f"重试点击「后退」失败: {e}")

    try:
        from PySide6.QtCore import QTimer

        QTimer.singleShot(max(int(delay_ms), 0), lambda: attempt(max(int(retries), 0)))
    except Exception as e:      # 没有 Qt 事件循环（单测）时直接点一次
        log.debug(f"安排点击「后退」失败，改为立即执行: {e}")
        click_quick_settings_back()


def open_quick_settings_main(delay_ms: int = 700, retries: int = 3) -> bool:
    """打开「快速设置**主界面**」（磁贴 + 亮度/音量滑杆那块）。

    先用 ``ms-availablenetworks:`` 把面板打开（它落在 WLAN 页），随后用 UI Automation
    点面板自己的「后退」回到主界面 —— Win+A 在本机注入不进去，这是唯一可靠的路子。
    """
    if not shell_open("ms-availablenetworks:"):
        return False
    _schedule_back_click(delay_ms, retries)
    return True


def open_battery_settings() -> bool:
    """打开设置的「电池」页（Win11 落在「系统 › 电源和电池」，Win10 落在节电设置）。"""
    return shell_open("ms-settings:batterysaver") or shell_open("ms-settings:powersleep")


def open_quick_settings(kind: str = "network") -> bool:
    """按按钮语义打开对应的系统界面。

    | kind | Win11 | Win10 |
    | --- | --- | --- |
    | ``network`` | ``ms-availablenetworks:`` → WLAN 网络列表 | 同一个 URI → 网络浮出 |
    | ``volume`` | WLAN 页 + UIA 点「后退」→ 快速设置主界面 | ``ms-actioncenter:``（Win10 没有快速设置面板） |
    | ``power`` | 设置的「电池」页 | 设置的「电池」页 |

    """
    if kind == "power":
        return open_battery_settings()
    if kind == "volume":
        if is_windows_11():
            return open_quick_settings_main()
        return shell_open("ms-actioncenter:")
    # network（默认）：两个系统上都是网络浮出；Win11 里它落在「快速设置」窗口的 WLAN 页
    return shell_open("ms-availablenetworks:") or shell_open("ms-actioncenter:")


def open_notification_center() -> bool:
    """打开通知中心（Win11）／操作中心（Win10）：两个系统上都是 ``ms-actioncenter:``。"""
    return shell_open("ms-actioncenter:")


# =========================================================================== #
# 汇总与轮询线程
# =========================================================================== #
def collect_status() -> Dict[str, Any]:
    """采集一次完整状态（任何一项失败都只体现在该项的 ``error`` 里）。"""
    return {
        "power": power_status(),
        "network": network_status(),
        "volume": volume_status(),
        "windows_build": windows_build(),
    }


class SystemStatusWorker(QThread):
    """周期性采集系统状态，结果通过 ``status_changed`` 发回 GUI 线程。

    线程模型与 :class:`core.process_scan.ProcessScanWorker` 一致：自己的 while 循环
    + 事件唤醒，``quit()`` 被覆盖成置位停止事件，方便统一线程管理器收尾。
    """

    #: dict：见 :func:`collect_status`
    status_changed = Signal(dict)
    #: 采集异常（只上报，不中断循环）
    scan_failed = Signal(str)

    def __init__(self, interval_ms: int = DEFAULT_STATUS_INTERVAL_MS, parent=None):
        super().__init__(parent)
        self._interval = max(int(interval_ms), 200) / 1000.0
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()

    # -- 生命周期 ---------------------------------------------------------- #
    def request_refresh(self) -> None:
        """要求立刻再采集一次（例如刚切完音量/网络）。"""
        self._wake_event.set()

    def stop(self, wait_ms: int = 3000) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self.isRunning():
            self.wait(wait_ms)

    def quit(self) -> None:  # noqa: D102 - 覆盖：本线程跑自己的循环
        self._stop_event.set()
        self._wake_event.set()

    # -- 线程主体 ---------------------------------------------------------- #
    def run(self) -> None:  # noqa: D102 - QThread 入口
        _com_ready()  # COM 必须在本线程初始化，缓存的接口对象也只能本线程用
        while not self._stop_event.is_set():
            try:
                snapshot = collect_status()
                if not self._stop_event.is_set():
                    self.status_changed.emit(snapshot)
            except Exception as exc:  # noqa: BLE001 - 后台异常必须上报
                if not self._stop_event.is_set():
                    self.scan_failed.emit(f"{type(exc).__name__}: {exc}")
            self._wake_event.wait(self._interval)
            self._wake_event.clear()
