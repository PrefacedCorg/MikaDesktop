import ctypes
from ctypes import windll, Structure, wintypes, sizeof, byref, c_longlong
import win32con
import win32gui
import win32print

# 日志：sys32 位于 core 包最底层，不假设 log_maker 一定可用；取不到就退化为
# 标准库 logging，绝不让「日志不可用」把基础的窗口/屏幕 API 一起带崩。
try:
    from . import log_maker
    log = log_maker.logger()
except Exception:  # pragma: no cover - 仅在日志模块缺失时走到
    import logging
    log = logging.getLogger(__name__)

# LRESULT 在部分 Python 版本的 ctypes.wintypes 中缺失，手动定义（64位有符号整数）
if not hasattr(wintypes, 'LRESULT'):
    wintypes.LRESULT = c_longlong

MB_OK = win32con.MB_OKCANCEL
MB_OKCANCEL = win32con.MB_OKCANCEL
MB_YESNO = win32con.MB_YESNO
MB_YESNOCANCEL = win32con.MB_YESNOCANCEL
MB_HELP = win32con.MB_HELP
MB_RETRYCANCEL = win32con.MB_RETRYCANCEL
MB_ICONWARNING = win32con.MB_ICONWARNING
MB_ICONINFORMATION = win32con.MB_ICONINFORMATION
MB_ICONASTERISK = win32con.MB_ICONASTERISK
MB_ICONQUESTION = win32con.MB_ICONQUESTION
MB_ICONSTOP = win32con.MB_ICONSTOP

IDYES = win32con.IDYES
IDNO = win32con.IDNO
IDRETRY = win32con.IDRETRY
IDCANCEL = win32con.IDCANCEL

_user32 = windll.user32

# ========== DPI 感知（进程生命周期内只需设置一次） ==========
_user32.SetProcessDPIAware()

# ========== 屏幕尺寸缓存 ==========
# 这些值原先在模块加载时算一次就永远不变：换显示器、改分辨率或 DPI 缩放之后会一直
# 用旧值（dock 定位、全屏判定都跟着错）。现在统一由 refresh_metrics() 计算并写入
# 模块全局；导入时调用一次保持原有行为，运行期收到「屏幕变化」信号后可再调一次。
REAL_SCREEN_WIDTH = 0       # 主显示器物理分辨率（GetDeviceCaps，不受 DPI 缩放影响）
REAL_SCREEN_HEIGHT = 0
LOGICAL_SCREEN_WIDTH = 0    # 逻辑分辨率（受 DPI 缩放影响）
LOGICAL_SCREEN_HEIGHT = 0
PRIMARY_WORK_LEFT = 0       # 主显示器工作区（排除任务栏），屏幕绝对坐标
PRIMARY_WORK_TOP = 0
PRIMARY_WORK_RIGHT = 0
PRIMARY_WORK_BOTTOM = 0


class _MONITORINFO(Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


# 这两个函数的签名只在这里声明一次。ctypes 的 argtypes 是绑在**函数对象**上的，
# 而 ctypes.windll.user32 在进程内是同一个对象 —— 别的模块再用自己的结构体类型
# 声明一次 GetMonitorInfoW，就会覆盖这里的声明，导致本模块传进去的 byref 结构体
# 类型对不上而抛 ArgumentError（被下面的 except 吞掉 → 工作区永远读不到）。
# 需要显示器信息的地方一律调用 monitor_rect_for_window()，不要再自己声明。
_user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
_user32.MonitorFromWindow.restype = wintypes.HMONITOR
_user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
_user32.MonitorFromPoint.restype = wintypes.HMONITOR
_user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(_MONITORINFO)]
_user32.GetMonitorInfoW.restype = wintypes.BOOL


def monitor_rect_for_window(hwnd):
    """窗口所在显示器的 ``rcMonitor``（屏幕坐标）；读不到返回 ``None``。"""
    try:
        hmon = _user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
        if not hmon:
            return None
        mi = _MONITORINFO()
        mi.cbSize = sizeof(_MONITORINFO)
        if not _user32.GetMonitorInfoW(hmon, byref(mi)):
            return None
        rc = mi.rcMonitor
        return (rc.left, rc.top, rc.right, rc.bottom)
    except Exception as e:
        log.debug(f"读取窗口 {hwnd} 的显示器信息失败: {e}")
        return None


def refresh_metrics():
    """重新读取屏幕分辨率与主显示器工作区并更新本模块全局变量。

    返回一个 dict 便于调用方比较前后差异。任何一项读取失败都保留旧值，
    不会让整个刷新过程抛异常——屏幕状态异常时不该拖垮 dock。
    """
    global REAL_SCREEN_WIDTH, REAL_SCREEN_HEIGHT
    global LOGICAL_SCREEN_WIDTH, LOGICAL_SCREEN_HEIGHT
    global PRIMARY_WORK_LEFT, PRIMARY_WORK_TOP, PRIMARY_WORK_RIGHT, PRIMARY_WORK_BOTTOM

    # 物理分辨率（GetDeviceCaps 拿真实像素）
    hdc = None
    try:
        hdc = win32gui.GetDC(0)
        REAL_SCREEN_WIDTH = win32print.GetDeviceCaps(hdc, win32con.DESKTOPHORZRES)
        REAL_SCREEN_HEIGHT = win32print.GetDeviceCaps(hdc, win32con.DESKTOPVERTRES)
    except Exception:
        pass
    finally:
        if hdc:
            try:
                win32gui.ReleaseDC(0, hdc)
            except Exception:
                pass

    # 逻辑分辨率
    try:
        LOGICAL_SCREEN_WIDTH = _user32.GetSystemMetrics(0)   # SM_CXSCREEN
        LOGICAL_SCREEN_HEIGHT = _user32.GetSystemMetrics(1)  # SM_CYSCREEN
    except Exception:
        pass

    # 主显示器工作区
    try:
        hmon = _user32.MonitorFromPoint(wintypes.POINT(0, 0), 2)  # MONITOR_DEFAULTTONEAREST
        mi = _MONITORINFO()
        mi.cbSize = sizeof(_MONITORINFO)
        if hmon and _user32.GetMonitorInfoW(hmon, byref(mi)):
            PRIMARY_WORK_LEFT = mi.rcWork.left
            PRIMARY_WORK_TOP = mi.rcWork.top
            PRIMARY_WORK_RIGHT = mi.rcWork.right
            PRIMARY_WORK_BOTTOM = mi.rcWork.bottom
    except Exception:
        pass

    return {
        "real": (REAL_SCREEN_WIDTH, REAL_SCREEN_HEIGHT),
        "logical": (LOGICAL_SCREEN_WIDTH, LOGICAL_SCREEN_HEIGHT),
        "work": (PRIMARY_WORK_LEFT, PRIMARY_WORK_TOP,
                 PRIMARY_WORK_RIGHT, PRIMARY_WORK_BOTTOM),
    }


refresh_metrics()

HWND_TRAY = win32gui.FindWindow("Shell_TrayWnd", None)


def get_window_rect(hwnd: int):
    return win32gui.GetWindowRect(hwnd)

def hide_window(hwnd: int):
    _user32.ShowWindow(hwnd, win32con.SW_HIDE)

def show_window(hwnd: int):
    _user32.ShowWindow(hwnd, win32con.SW_SHOW)


def messagebox(title: str, text: str, buttons: int = MB_OK) -> int:
    """
    显示消息框
    Args:
        title (str): 标题
        text (str): 文本
        buttons (int, optional): 按钮. 默认为MB_OK.可选值:[MB_OK,MB_OKCANCEL,MB_YESNO,MB_YESNOCANCEL,MB_HELP,MB_RETRYCANCEL,MB_ICONWARNING,MB_ICONINFORMATION,MB_ICONASTERISK,MB_ICONQUESTION,MB_ICONSTOP]
    Returns:
        int: 按钮索引
       """
    return _user32.MessageBoxW(0, text, title, buttons)


# ========== AppBar（屏幕保留区域管理） ==========
# 使用 Windows AppBar API 管理工作区，比 SystemParametersInfo 更可靠
# 参考：https://learn.microsoft.com/en-us/windows/win32/shell/appbar

from ctypes import wintypes as _wt
import ctypes

_shell32 = windll.shell32

# kernel32：GetModuleHandleW 必须设置 64 位返回类型，否则句柄被截断
_kernel32 = windll.kernel32
_kernel32.GetModuleHandleW.restype = wintypes.HMODULE
_kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]

# AppBar 常量
_ABM_NEW = 0x00000000
_ABM_REMOVE = 0x00000001
_ABM_SETPOS = 0x00000003
_ABE_BOTTOM = 3
_WM_APP = 0x8000


class _APPBARDATA(Structure):
    _fields_ = [
        ("cbSize", _wt.UINT),
        ("hWnd", _wt.HWND),
        ("uCallbackMessage", _wt.UINT),
        ("uEdge", _wt.UINT),
        ("rc", wintypes.RECT),
        ("lParam", _wt.LPARAM),
    ]


# WNDCLASSEXW 在 Python 3.13 中被移除，手动定义
class _WNDCLASSEXW(Structure):
    _fields_ = [
        ("cbSize", _wt.UINT),
        ("style", _wt.UINT),
        ("lpfnWndProc", ctypes.WINFUNCTYPE(wintypes.LRESULT, _wt.HWND, _wt.UINT, _wt.WPARAM, _wt.LPARAM)),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", _wt.HMODULE),
        ("hIcon", _wt.HANDLE),
        ("hCursor", _wt.HANDLE),
        ("hbrBackground", _wt.HANDLE),
        ("lpszMenuName", _wt.LPCWSTR),
        ("lpszClassName", _wt.LPCWSTR),
        ("hIconSm", _wt.HANDLE),
    ]


# WndProc 回调类型（WINFUNCTYPE = Windows 调用约定，64位系统必需）
_WNDPROC = ctypes.WINFUNCTYPE(
    wintypes.LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)

# 给 DefWindowProcW 设置正确的参数类型，避免 64 位参数溢出导致崩溃
_user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.DefWindowProcW.restype = wintypes.LRESULT

# 窗口创建/注册函数设置 64 位安全签名
_user32.RegisterClassExW.argtypes = [ctypes.c_void_p]
_user32.RegisterClassExW.restype = wintypes.ATOM
_user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HANDLE, wintypes.HINSTANCE, ctypes.c_void_p,
]
_user32.CreateWindowExW.restype = wintypes.HWND

# AppBar 宿主窗口句柄和注册状态
_appbar_hwnd = None
_appbar_registered = False
_appbar_wndproc_ref = None  # 防止回调被 GC 回收
_appbar_class_registered = False  # 窗口类只需注册一次，重复注册会失败


def _appbar_wndproc(hwnd, msg, wparam, lparam):
    """AppBar 宿主窗口回调。"""
    try:
        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)
    except Exception:
        return 0


# AppBar 宿主窗口类名（模块级常量，确保字符串生命周期有效）
_APPBAR_CLASS_NAME = "DockAppBarHostClass"
_APPBAR_WINDOW_NAME = "DockAppBarHost"


def _create_appbar_host_window():
    """创建（或复用）隐藏的 AppBar 宿主窗口。"""
    global _appbar_hwnd, _appbar_wndproc_ref, _appbar_class_registered

    if _appbar_hwnd:
        return _appbar_hwnd

    hinstance = _kernel32.GetModuleHandleW(None)

    # 窗口类在整个进程里只需注册一次。分辨率变化、全屏程序退出后重新注册 AppBar
    # 时都会再次走到这里，若无条件调用 RegisterClassExW 会因「类名已存在」而失败。
    #
    # WNDPROC 回调必须在注册窗口类之前创建，而且**注册之后不能再替换**：已注册的
    # 窗口类记的是这个 ctypes 回调的代码指针，一旦把模块全局换成新的回调对象，
    # 旧对象会被回收，窗口类里就留下悬空指针 —— 之后再创建该类窗口（或收到消息）
    # 时进程会直接以 0xC000041D（用户回调中发生致命异常）崩掉。这正是"全屏程序
    # 结束后重新注册 AppBar"这条路必须走通的关键。
    if not _appbar_class_registered:
        _appbar_wndproc_ref = _WNDPROC(_appbar_wndproc)

        wc = _WNDCLASSEXW()
        wc.cbSize = sizeof(_WNDCLASSEXW)
        wc.lpfnWndProc = _appbar_wndproc_ref
        wc.hInstance = hinstance
        wc.lpszClassName = _APPBAR_CLASS_NAME

        atom = _user32.RegisterClassExW(byref(wc))
        if not atom:
            raise RuntimeError(f"AppBar 窗口类注册失败 error={ctypes.get_last_error()}")
        _appbar_class_registered = True

    _appbar_hwnd = _user32.CreateWindowExW(
        0, _APPBAR_CLASS_NAME, _APPBAR_WINDOW_NAME,
        0, 0, 0, 0, 0, 0, 0, hinstance, 0
    )
    if not _appbar_hwnd:
        raise RuntimeError(f"AppBar 宿主窗口创建失败 error={ctypes.get_last_error()}")
    return _appbar_hwnd


def is_appbar_registered() -> bool:
    """当前是否已把程序栏注册为 AppBar。

    调用方（例如全屏让位逻辑）需要知道「现在注销到底是不是空操作」，避免在没注册
    的时候反复走一遍 SHAppBarMessage(ABM_REMOVE)。
    """
    return bool(_appbar_registered and _appbar_hwnd)


def set_appbar_bottom(dock_top: int):
    """将程序栏注册为底部 AppBar，系统自动调整工作区。

    注册后，最大化窗口会停在 AppBar 上方，退出时自动恢复。
    请求的 rc.top 即新的工作区底部（dock 栏顶端）。

    可以反复调用（分辨率变化、全屏程序退出后重新注册）：宿主窗口会被复用，
    ABM_NEW 对同一个窗口重复注册是幂等的。

    Args:
        dock_top: dock 栏顶端的 Y 坐标（屏幕像素，即新工作区底部）
    """
    global _appbar_registered

    previous_hwnd = _appbar_hwnd
    hwnd = _create_appbar_host_window()
    # 复用同一个宿主窗口时，ABM_NEW 会返回 0 —— 那不是失败，而是"本来就注册着"。
    # 必须区分这两种情况：把已注册误判成未注册，全屏让位那边就会跳过注销，
    # 保留区再也还不回去。
    reused = bool(previous_hwnd) and previous_hwnd == hwnd and _appbar_registered

    abd = _APPBARDATA()
    abd.cbSize = sizeof(_APPBARDATA)
    abd.hWnd = hwnd
    abd.uCallbackMessage = _WM_APP
    abd.uEdge = _ABE_BOTTOM
    abd.rc.left = 0
    abd.rc.top = dock_top
    abd.rc.right = REAL_SCREEN_WIDTH
    abd.rc.bottom = REAL_SCREEN_HEIGHT

    # 注册 AppBar
    ret_new = _shell32.SHAppBarMessage(_ABM_NEW, byref(abd))
    if ret_new:
        _appbar_registered = True
    elif reused:
        log.debug(f"[AppBar] 宿主窗口已注册，忽略 ABM_NEW 的 0 返回值 hwnd=0x{hwnd:X}")
    else:
        _appbar_registered = False
        log.warning(f"[AppBar] ABM_NEW 失败 hwnd=0x{hwnd:X} error={ctypes.get_last_error()}")

    # 设置位置（系统会据此调整工作区）
    ret_pos = _shell32.SHAppBarMessage(_ABM_SETPOS, byref(abd))
    log.info(f"[AppBar] 注册完成 hwnd=0x{hwnd:X} NEW={ret_new} SETPOS={ret_pos} "
             f"rc=({abd.rc.left},{abd.rc.top},{abd.rc.right},{abd.rc.bottom})")


def remove_appbar():
    """注销 AppBar，恢复原始工作区。"""
    global _appbar_registered, _appbar_hwnd
    hwnd = _appbar_hwnd
    if hwnd:
        abd = _APPBARDATA()
        abd.cbSize = sizeof(_APPBARDATA)
        abd.hWnd = hwnd
        ret = _shell32.SHAppBarMessage(_ABM_REMOVE, byref(abd))
        log.info(f"[AppBar] 注销 hwnd=0x{hwnd:X} REMOVE={ret}")
        _user32.DestroyWindow(hwnd)
        _appbar_hwnd = None
    _appbar_registered = False
