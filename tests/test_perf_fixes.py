"""P1 性能修复验证：后台扫描线程、进程信息缓存、图标内存缓存。"""

import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

failures = []


def check(name, ok, detail=""):
    print(("[PASS] " if ok else "[FAIL] ") + name + (("  " + str(detail)) if detail else ""))
    if not ok:
        failures.append(name)


import core.process_manager as pm

# ---------- 1) 路径规范化公开接口 ----------
pmgr = pm.ProcessManager()
check("norm_path 公开接口与内部实现一致",
      pmgr.norm_path(r"C:\A\B.exe") == pmgr._norm_path(r"C:\A\B.exe"))

# ---------- 2) pid -> (exe, name) 短 TTL 缓存 ----------
exe1, name1 = pmgr._proc_info_for_pid(os.getpid())
entry = pmgr._proc_info_cache.get(os.getpid())
check("能解析出当前进程的 exe", bool(exe1) and exe1.lower().endswith(".exe"), exe1)
check("能解析出当前进程名", name1 == os.path.basename(sys.executable).lower(), name1)

ts1 = entry[2] if entry else None
pmgr._proc_info_for_pid(os.getpid())
ts2 = pmgr._proc_info_cache[os.getpid()][2]
check("同一 pid 在 TTL 内命中缓存（未重复走 psutil）", ts1 is not None and ts1 == ts2)

# 不存在的 pid 不该抛异常，且应缓存 (None, '')
missing_exe, missing_name = pmgr._proc_info_for_pid(999999)
check("无效 pid 返回 (None, '') 而不抛异常",
      missing_exe is None and missing_name == "")

# ---------- 3) 图标内存缓存（含负缓存） ----------
bogus = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                     "System32", "definitely_not_here_xyz.exe")
check("不存在的路径直接返回 None", pmgr.extract_icon(bogus) is None)
key = pmgr.norm_path(bogus)
check("失败结果被负缓存", key in pmgr._icon_cache and pmgr._icon_cache[key] is None)
check("再次调用命中负缓存", pmgr.extract_icon(bogus) is None)
pmgr.invalidate_icon_cache(bogus)
check("可以按路径失效图标缓存", key not in pmgr._icon_cache)

# ---------- 4) 图标命中内存缓存后不再依赖磁盘 ----------
target_dll = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                          "System32", "shell32.dll")
if not os.path.isfile(target_dll):
    check("找到用于测试的图标源", False, target_dll)
else:
    tmp_local = tempfile.mkdtemp(prefix="appicon-")
    old_local = os.environ.get("LOCALAPPDATA")
    os.environ["LOCALAPPDATA"] = tmp_local
    try:
        pmgr2 = pm.ProcessManager()
        first = pmgr2.extract_icon(target_dll)
        check("图标提取成功并写入缓存目录",
              bool(first) and os.path.isfile(first), first)
        second = pmgr2.extract_icon(target_dll)
        check("第二次调用返回同一缓存路径", first == second)
        if first and os.path.isfile(first):
            os.remove(first)
            third = pmgr2.extract_icon(target_dll)
            check("命中内存缓存时不再回读磁盘 / 不重新提取", third == first)
    finally:
        if old_local is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = old_local
        shutil.rmtree(tmp_local, ignore_errors=True)

# ---------- 5) 后台扫描线程 ----------
from PySide6.QtCore import QCoreApplication
from core.process_scan import ProcessScanWorker

app = QCoreApplication.instance() or QCoreApplication(sys.argv)

results = []
errors = []
worker = ProcessScanWorker(pm.ProcessManager(), interval_ms=200)
worker.scan_finished.connect(lambda payload: results.append(payload))
worker.scan_failed.connect(lambda message: errors.append(message))
worker.start()

# 第一轮扫描要做全量图标提取，可能偏慢；给足 60 秒
deadline = time.time() + 60
while time.time() < deadline and not results and not errors:
    app.processEvents()
    time.sleep(0.02)

check("后台扫描线程产出了结果", len(results) > 0, "errors=%s" % errors[:2])
if results:
    payload = results[0]
    check("扫描结果是非空 dict", isinstance(payload, dict) and len(payload) > 0,
          "进程数=%d" % len(payload))
    sample = next(iter(payload.values()))
    check("结果条目包含 name/path/icon 字段",
          {"name", "path", "icon"} <= set(sample))

# 再等一轮，验证周期性重跑
before = len(results)
deadline = time.time() + 20
while time.time() < deadline and len(results) <= before:
    app.processEvents()
    time.sleep(0.02)
check("扫描会周期性重复执行", len(results) > before, "轮次=%d" % len(results))

worker.stop()
check("stop() 之后线程已退出", not worker.isRunning())

print()
print("FAILED: %d" % len(failures))
for name in failures:
    print("  - " + name)
sys.exit(1 if failures else 0)
