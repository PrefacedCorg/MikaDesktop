import psutil
import win32gui
import win32process
import win32con
import os
import sys
import hashlib
import time

from . import make_app_icon
from . import log_maker
from . import config_manager

log = log_maker.logger()


class ProcessManager:
    #: 输入法候选框之类的窗口不算「应用的可见窗口」
    _IGNORED_WINDOW_CLASSES = frozenset([
        "MSCTFIME UI", "IAIMETIPWndClass", "TIPBand", "Candidate",
    ])

    #: pid -> (exe, name) 短 TTL 缓存：轮询间隔 500ms，取 1 秒既覆盖一轮内的重复
    #: 查询，又不会因为 PID 复用拿到过期结果。
    _PROC_INFO_TTL = 1.0
    _PROC_INFO_MAX = 512

    #: 图标提取结果缓存的最大条目数（含失败结果的负缓存）
    _ICON_CACHE_MAX = 512

    def __init__(self):
        # 排除进程的默认值只在 config_manager.DEFAULT_CONFIG 里定义一次，
        # 避免这里再硬编码一份导致两处漂移（历史上就漏了 python.exe / wetype_*）。
        self.except_processes = list(
            config_manager.DEFAULT_CONFIG["dock"]["except_processes"]
        )
        # lazy extractor instance (复用 CatchIco 提取器，避免频繁创建)
        self._extractor = None
        # pid -> (exe_path, name, 记录时刻)：见 _proc_info_for_pid
        self._proc_info_cache = {}
        # 图标提取结果的进程内缓存：命中可直接跳过磁盘判断与 GDI 提取，
        # 值可能是路径字符串，也可能是 None（表示提取失败，做负缓存避免反复重试）
        self._icon_cache = {}
        try:
            from .catch_ico import WindowsIconExtractor
            # 不立即实例化过重资源，延迟在需要时创建
            self._extractor_class = WindowsIconExtractor
        except Exception:
            self._extractor_class = None

    def _norm_path(self, p):
        try:
            return os.path.abspath(p).lower()
        except Exception:
            return str(p).lower()

    def norm_path(self, p):
        """规范化路径用于比较（公开接口）。

        调用方（例如 dock.py）需要同一套比较规则，不该去碰带下划线的私有方法。
        """
        return self._norm_path(p)

    def set_except_processes(self, proc_list):
        """
        更新排除进程列表（用户可通过设置界面调用）。
        规范化为小写、去重、每项尽量带 .exe（若用户只写了进程名则自动补 .exe）。

        传 ``None`` 表示「不改动」；传空列表表示「清空排除列表」—— 在设置界面
        把输入框全部删空是一个明确的意图，不该和「没配置」混为一谈。
        """
        if proc_list is None:
            return
        try:
            normalized = []
            for s in proc_list:
                if not s:
                    continue
                if not isinstance(s, str):
                    s = str(s)
                s = s.strip().lower()
                if not s:
                    continue
                # 若用户只写了名称（例如 "python"），自动补 .exe；若已有扩展名则保留
                if '.' not in s:
                    s = s + '.exe'
                if s not in normalized:
                    normalized.append(s)
            self.except_processes = normalized
        except Exception as e:
            log.error(f"设置排除进程列表时出错: {e}")

    def _get_extractor(self):
        if self._extractor is None and self._extractor_class:
            try:
                self._extractor = self._extractor_class()
            except Exception as e:
                self._extractor = None
        return self._extractor

    # ------------------------------------------------------------------ #
    # 进程 / 窗口信息缓存
    # ------------------------------------------------------------------ #
    def _proc_info_for_pid(self, pid):
        """返回 ``(exe_path, name_lower)``，失败时为 ``(None, '')``。

        走一个短 TTL 缓存：同一轮轮询里 ``is_process_running`` /
        ``get_app_visible_windows`` / ``get_running_processes`` 会对同一个 pid
        反复调用 psutil，而每次 ``exe()`` / ``name()`` 都要走一次系统调用，是这些
        查询的主要开销。TTL 取 1 秒（轮询间隔 500ms），既覆盖了一轮内的重复查询，
        又不会因为 PID 复用而拿到过期结果。
        """
        now = time.monotonic()
        cached = self._proc_info_cache.get(pid)
        if cached is not None and (now - cached[2]) < self._PROC_INFO_TTL:
            return cached[0], cached[1]

        exe_path = None
        name = ''
        try:
            proc = psutil.Process(pid)
            exe_path = proc.exe()
            name = (proc.name() or '').lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
        except Exception as e:
            log.debug(f"解析进程 {pid} 信息失败: {e}")

        self._proc_info_cache[pid] = (exe_path, name, now)
        if len(self._proc_info_cache) > self._PROC_INFO_MAX:
            # 先清过期项；仍然超限就直接重建，避免长期运行时无限增长
            for key, value in list(self._proc_info_cache.items()):
                if (now - value[2]) >= self._PROC_INFO_TTL:
                    del self._proc_info_cache[key]
            if len(self._proc_info_cache) > self._PROC_INFO_MAX:
                self._proc_info_cache.clear()
        return exe_path, name

    def proc_info_for_pid(self, pid):
        """公开接口：``pid -> (exe_path, name_lower)``，走同一份 TTL 缓存。

        调用方（例如 :mod:`core.fullscreen_watch`）需要"这个窗口属于哪个进程"，
        必须复用同一份缓存，否则每次前台窗口变化都要额外走一次 psutil。
        """
        return self._proc_info_for_pid(pid)

    def _enum_visible_windows(self):
        """一次性枚举所有「可见且有标题」的窗口，返回 ``pid -> [(hwnd, 标题, 类名)]``。

        ``EnumWindows`` 是 is_process_running / get_app_visible_windows /
        get_running_processes 三个方法的共同成本大头，集中到一处，避免每个方法
        各自完整枚举一遍。
        """
        pid_windows = {}

        def _collect(hwnd, _param):
            try:
                if win32gui.IsWindow(hwnd) and win32gui.IsWindowVisible(hwnd):
                    title = win32gui.GetWindowText(hwnd)
                    if title and title.strip():
                        _, pid = win32process.GetWindowThreadProcessId(hwnd)
                        pid_windows.setdefault(pid, []).append(
                            (hwnd, title, win32gui.GetClassName(hwnd))
                        )
            except Exception:
                pass
            return True

        try:
            win32gui.EnumWindows(_collect, None)
        except Exception as e:
            log.debug(f"枚举窗口失败: {e}")
        return pid_windows

    def is_process_running(self, app_path):
        """检查指定路径的应用是否正在运行 - 仅当有可见窗口时"""
        try:
            normalized_app = self._norm_path(app_path)
            current_process_name = os.path.basename(sys.executable).lower()

            for pid in self._enum_visible_windows():
                exe_path, name = self._proc_info_for_pid(pid)
                if not exe_path:
                    continue
                # 跳过排除列表中的进程和程序本身
                if name in self.except_processes or name == current_process_name:
                    continue
                if self._norm_path(exe_path) == normalized_app:
                    return True
            return False

        except Exception as e:
            log.error(f"检查窗口时出错: {e}")
            return False

    def get_running_processes(self, known_apps_paths, skip_known: bool = True):
        """获取系统中所有正在运行的进程，找出未添加但运行的应用
        
        Args:
            known_apps_paths: 已知应用路径列表
            skip_known: True=跳过已知应用(默认), False=包含已知应用用于状态检查
        """
        running_processes = {}
        try:
            # 一次性枚举所有可见窗口，建立 pid -> visible-window-info 映射
            pid_windows = self._enum_visible_windows()

            # 规范化已知应用路径，避免重复检查
            normalized_known_paths = {self._norm_path(p) for p in known_apps_paths}
            current_process_name = os.path.basename(sys.executable).lower()

            # 现在遍历进程并快速判断
            # 注意：这里刻意不请求 'cmdline' —— 解析每个进程的命令行代价很高，
            # 而下面全程都没有用到它。
            for proc in psutil.process_iter(['pid', 'name', 'exe']):
                try:
                    process_info = proc.info
                    exe_path = process_info.get('exe')

                    # 基本过滤
                    if not exe_path or not os.path.exists(exe_path):
                        continue

                    # 检查进程名称是否在排除列表中
                    process_name = (process_info.get('name') or '').lower()
                    if process_name in self.except_processes or process_name == current_process_name:
                        continue  # 跳过排除列表和程序自身

                    pid = process_info.get('pid')
                    windows = pid_windows.get(pid)
                    if not windows:
                        continue  # 没有可见窗口，跳过

                    # 过滤特殊类名的窗口
                    if not any(cls not in self._IGNORED_WINDOW_CLASSES
                               for _hwnd, _title, cls in windows):
                        continue

                    # 检查是否已知（固定或用户添加）
                    if skip_known and self._norm_path(exe_path) in normalized_known_paths:
                        continue

                    if exe_path not in running_processes:
                        app_name = (process_info.get('name') or '').replace('.exe', '')
                        # 使用图标提取函数获取图标（带进程内缓存，失败结果也会缓存）
                        try:
                            icon_path = self.extract_icon(exe_path) or ''
                        except Exception:
                            icon_path = ''
                        running_processes[exe_path] = {
                            'name': app_name,
                            'path': exe_path,
                            'icon': icon_path
                        }

                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
                except Exception as e:
                    log.debug(f"处理进程 {process_info.get('name', 'Unknown')} 时出错: {e}")
                    continue
        except Exception as e:
            log.error(f"获取运行进程时出错: {e}")

        return running_processes

    def get_app_visible_windows(self, app_path):
        """获取应用的所有可见窗口，返回 ``[(hwnd, 标题), ...]``"""
        try:
            normalized_app = self._norm_path(app_path)
            current_process_name = os.path.basename(sys.executable).lower()

            visible_windows = []
            for pid, windows in self._enum_visible_windows().items():
                exe_path, name = self._proc_info_for_pid(pid)
                if not exe_path or self._norm_path(exe_path) != normalized_app:
                    continue
                # 跳过排除列表中的系统服务和程序本身
                if name in self.except_processes or name == current_process_name:
                    continue
                for hwnd, title, _cls in windows:
                    visible_windows.append((hwnd, title))
            return visible_windows
        except Exception as e:
            log.error(f"检查窗口时出错: {e}")
            return []

    def close_app_window(self, app_path):
        """关闭应用窗口。

        按**完整路径**匹配进程，避免同名进程（例如两个不同目录下的 chrome.exe）
        被误关。按文件名匹配的老实现在 ``dock.py`` 里还有一份，这里保留单一实现
        供调用方复用。
        """
        normalized_app = self._norm_path(app_path)
        current_process_name = os.path.basename(sys.executable).lower()

        def enum_windows_proc(hwnd, _param):
            if win32gui.IsWindowVisible(hwnd):
                try:
                    _, pid = win32process.GetWindowThreadProcessId(hwnd)
                    exe_path, name = self._proc_info_for_pid(pid)
                    if not exe_path or self._norm_path(exe_path) != normalized_app:
                        return True
                    if name in self.except_processes or name == current_process_name:
                        return True
                    # 检查窗口标题是否为空（避免关闭系统窗口）
                    window_title = win32gui.GetWindowText(hwnd)
                    if window_title.strip():
                        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
                        log.info(f"已发送关闭命令到窗口: {window_title}")
                        return False  # 找到并处理了窗口，停止枚举
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    pass
                except Exception as e:
                    log.debug(f"关闭窗口 {hwnd} 时出错: {e}")
            return True  # 继续枚举其他窗口

        try:
            win32gui.EnumWindows(enum_windows_proc, 0)
        except Exception as e:
            log.error(f"关闭窗口时出错: {e}")

    def terminate_app_process(self, app_path):
        """终止应用进程"""
        app_filename = os.path.basename(app_path)
        
        try:
            # 遍历所有进程，找到匹配的应用进程并终止
            for proc in psutil.process_iter(['pid', 'name', 'exe']):
                try:
                    process_info = proc.info
                    if process_info['exe'] and os.path.abspath(process_info['exe']) == os.path.abspath(app_path):
                        # 检查是否为系统服务
                        process_name = process_info['name'].lower()
                        if process_name in self.except_processes:
                            continue  # 跳过系统服务
                        
                        # 检查是否为程序本身
                        current_process_name = os.path.basename(sys.executable).lower()
                        if process_name == current_process_name:
                            continue  # 跳过程序自身
                        
                        # 终止进程
                        proc.terminate()
                        log.info(f"已终止进程: {process_info['name']} (PID: {proc.pid})")
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
                except Exception as e:
                    log.debug(f"终止进程 {proc.name()} 时出错: {e}")
                    continue
        except Exception as e:
            log.error(f"终止应用进程时出错: {e}")
            
    def extract_icon(self, exe_path):
        """提取图标并返回缓存 PNG 路径；失败返回 None。

        结果按规范化路径缓存在**内存**里，成功和失败都缓存：
        * 成功：省掉每次 ``os.path.exists`` + 可能的 GDI 提取；
        * 失败：负缓存。图标提取对某些 exe 必然失败（无图标资源、UWP 存根等），
          不缓存的话每次轮询都会重新走一遍昂贵的提取流程。

        需要重新提取时调用 :meth:`invalidate_icon_cache`。
        """
        if not exe_path:
            return None
        cache_key = self._norm_path(exe_path)
        if cache_key in self._icon_cache:
            return self._icon_cache[cache_key]

        icon_path = self._extract_icon_uncached(exe_path)

        if len(self._icon_cache) >= self._ICON_CACHE_MAX:
            self._icon_cache.clear()
        self._icon_cache[cache_key] = icon_path
        return icon_path

    def invalidate_icon_cache(self, exe_path=None):
        """清空图标内存缓存（传路径只失效那一个）。"""
        if exe_path is None:
            self._icon_cache.clear()
        else:
            self._icon_cache.pop(self._norm_path(exe_path), None)

    def _extract_icon_uncached(self, exe_path):
        """真正执行图标提取；使用CatchIco.py并通过 MakeAppIcon.compose_on_template 生成统一风格图标"""
        try:
            # 使用包含路径哈希的缓存名，避免不同路径同名冲突
            cache_dir = os.path.join(os.getenv('LOCALAPPDATA') or os.path.expanduser("~"), 'AppIcon')
            os.makedirs(cache_dir, exist_ok=True)
            name = os.path.splitext(os.path.basename(exe_path))[0]
            md5 = hashlib.md5((os.path.abspath(exe_path)).encode('utf-8')).hexdigest()[:8]
            icon_path = os.path.join(cache_dir, f"{name}_{md5}.png")
            if os.path.exists(icon_path):
                return icon_path
            extractor = self._get_extractor()
            if not extractor:
                return None

            extracted_icon = extractor.extract_file_icon(exe_path, size=64)
            if extracted_icon.success and extracted_icon.image:
                # 先写临时文件再原子替换。进程扫描线程和界面线程可能同时为同一个
                # exe 提取图标，直接写同一个目标文件会互相截断，界面上就会读到
                # 一个写了一半的 PNG（QPixmap 加载失败 → 图标空白）。
                tmp_path = icon_path + ".tmp"
                try:
                    try:
                        # 优先使用合成库生成统一风格图标
                        composed_bytes = make_app_icon.overlay.compose_on_template(
                            extracted_icon.image
                        )
                        with open(tmp_path, "wb") as f:
                            f.write(composed_bytes)
                    except Exception:
                        # 合成失败则回退为直接保存提取到的图像
                        # （显式指定 PNG：临时文件后缀不会被 PIL 识别）
                        extracted_icon.image.save(tmp_path, format="PNG")
                    os.replace(tmp_path, icon_path)
                    return icon_path
                except Exception as e:
                    log.error(f"保存/合成图标时出错: {e}")
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                    return None
            else:
                return None
        except Exception as e:
            log.error(f"使用图标提取器出错: {e}")
            return None
