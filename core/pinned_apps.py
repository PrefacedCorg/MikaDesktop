"""从 Windows 任务栏固定项（``User Pinned\\TaskBar`` 里的 .lnk）解析出应用信息。

从 ``dock.py`` 抽出来：这段逻辑只依赖 pywin32 与一个「能提取图标的对象」，
跟主窗口的界面状态无关，放在主入口里既难测试也把那个文件撑得很长。
"""

from __future__ import annotations

import os

from win32com.shell import shell  # type: ignore

__all__ = ["pinned_taskbar_dir", "app_info_from_shortcut", "discover_pinned_apps"]


def pinned_taskbar_dir():
    """返回任务栏固定项所在目录（不存在时仍然返回路径，由调用方判断）。"""
    appdata = os.getenv('APPDATA')
    if not appdata:
        return None
    return os.path.join(
        appdata, 'Microsoft', 'Internet Explorer', 'Quick Launch',
        'User Pinned', 'TaskBar',
    )


def app_info_from_shortcut(shortcut_path, process_manager, logger=None):
    """解析单个 .lnk，返回 ``{'name', 'path', 'icon', 'is_pinned'}``；失败返回 None。"""
    def _warn(message):
        if logger is not None:
            logger.warning(message)

    try:
        import pythoncom

        shortcut = pythoncom.CoCreateInstance(
            shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER,
            shell.IID_IShellLink,
        )
        persist_file = shortcut.QueryInterface(pythoncom.IID_IPersistFile)
        persist_file.Load(shortcut_path)

        # 获取目标路径
        target_path = shortcut.GetPath(shell.SLGP_RAWPATH)[0]
        if not target_path or not os.path.exists(target_path):
            return None

        # 获取应用名称（从快捷方式名称或可执行文件名）
        app_name = os.path.splitext(os.path.basename(shortcut_path))[0]
        if not app_name:
            app_name = os.path.splitext(os.path.basename(target_path))[0]

        # 快捷方式自带图标且文件确实存在时直接用它，否则统一交给进程管理器提取
        # （原实现在这里先置 None 再判一次 not exists，是永远走不到的死分支）
        icon_path, _icon_index = shortcut.GetIconLocation()
        if not icon_path or not os.path.exists(icon_path):
            icon_path = process_manager.extract_icon(target_path)

        return {
            'name': app_name,
            'path': target_path,
            'icon': icon_path,
            'is_pinned': True,  # 标记为固定应用
        }
    except Exception as e:
        _warn(f"解析快捷方式 {shortcut_path} 失败: {e}")
        return None


def discover_pinned_apps(process_manager, logger=None):
    """扫描任务栏固定项，返回去重后的应用信息列表（解析失败当作没有）。"""
    apps = []
    try:
        pinned_dir = pinned_taskbar_dir()
        if not pinned_dir or not os.path.exists(pinned_dir):
            return apps

        for item in os.listdir(pinned_dir):
            if not item.endswith('.lnk'):
                continue
            app_info = app_info_from_shortcut(
                os.path.join(pinned_dir, item), process_manager, logger
            )
            if not app_info:
                continue
            # 检查是否已存在，避免重复
            if not any(app['name'] == app_info['name'] for app in apps):
                apps.append(app_info)
    except Exception as e:
        if logger is not None:
            logger.error(f"获取任务栏固定应用失败: {e}")
        return []
    return apps
