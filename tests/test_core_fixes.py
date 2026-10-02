"""P0 修复验证：配置深合并/原子写、排除列表单一来源、GDI 句柄不泄漏。"""

import ctypes
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

failures = []


def check(name, ok, detail=""):
    print(("[PASS] " if ok else "[FAIL] ") + name + (("  " + str(detail)) if detail else ""))
    if not ok:
        failures.append(name)


# ---------- 1) 配置：深合并 + 原子写 ----------
import core.config_manager as Config

tmp = tempfile.mkdtemp(prefix="cfg-")
path = os.path.join(tmp, "settings.json")
try:
    Config.check(path)
    check("check() 会创建默认配置", os.path.isfile(path))

    Config.save_config(path, {"dock": {"apps": [{"name": "a", "path": "b"}]}})
    cfg = Config.load_config(path)
    check("深合并：只写 apps 时默认 except_processes 保留",
          len(cfg["dock"]["except_processes"]) == 11, cfg["dock"]["except_processes"])
    check("深合并：未提及的 xht 默认值保留",
          cfg["xht"]["notify_mode"] == Config.DEFAULT_CONFIG["xht"]["notify_mode"])
    check("DEFAULT_CONFIG 未被污染", Config.DEFAULT_CONFIG["dock"]["apps"] == [])

    Config.save_config(path, {"dock": {"except_processes": []}})
    check("空列表是有效值（可清空排除列表）",
          Config.load_config(path)["dock"]["except_processes"] == [])

    check("原子写不残留临时文件",
          not [f for f in os.listdir(tmp) if f.endswith(".tmp")])
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ---------- 2) 排除进程列表单一来源 ----------
import core.process_manager as pm

pmgr = pm.ProcessManager()
check("默认排除列表来自 config_manager",
      pmgr.except_processes == list(Config.DEFAULT_CONFIG["dock"]["except_processes"]),
      pmgr.except_processes)
check("默认列表包含 python.exe", "python.exe" in pmgr.except_processes)

pmgr.set_except_processes(None)
check("传 None 不改动列表", "python.exe" in pmgr.except_processes)
pmgr.set_except_processes([])
check("传空列表可清空", pmgr.except_processes == [])
pmgr.set_except_processes(["NotePad", "notepad.exe", "  ", "ab"])
check("规范化：补 .exe / 去重 / 去空",
      pmgr.except_processes == ["notepad.exe", "ab.exe"], pmgr.except_processes)

# ---------- 3) GDI 句柄不泄漏 ----------
from core.catch_ico import WindowsIconExtractor

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
user32.GetGuiResources.argtypes = [ctypes.c_void_p, ctypes.c_uint]
user32.GetGuiResources.restype = ctypes.c_uint


def gdi_count():
    return int(user32.GetGuiResources(kernel32.GetCurrentProcess(), 0))


target = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "shell32.dll")
if not os.path.isfile(target):
    check("找到用于测试的图标源", False, target)
else:
    extractor = WindowsIconExtractor(enable_cache=False)  # 关缓存，强制每次真正提取
    for _ in range(5):  # 预热，排除首次分配的一次性开销
        extractor.extract_file_icon(target, size=64, icon_index=0)
    before = gdi_count()
    rounds = 300
    result = None
    for _ in range(rounds):
        result = extractor.extract_file_icon(target, size=64, icon_index=0)
    after = gdi_count()
    growth = after - before
    check("图标提取成功（含图像数据）", bool(result and result.success and result.image))
    # 旧实现每轮泄漏 3+ 个句柄，300 轮会涨到 900 以上
    check("300 次提取后 GDI 句柄不增长", growth <= 20,
          "before=%d after=%d growth=%d" % (before, after, growth))

print()
print("FAILED: %d" % len(failures))
for name in failures:
    print("  - " + name)
sys.exit(1 if failures else 0)
