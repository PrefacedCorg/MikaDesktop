# 全屏程序让位（隐藏 dock + 短暂注销 AppBar）

## 需求

> 有程序（**非系统程序**）全屏显示时，隐藏 dock 栏并短暂注销 AppBar；
> 全屏程序解除时，恢复 AppBar 并重新显示 dock 栏。

dock 通过 AppBar 在屏幕底部保留了一块工作区（见 `core/sys32.py`），这样最大化
窗口会自动避开 dock。但全屏窗口应当独占整块屏幕：只要 AppBar 的保留区还在，
系统就不会把整块屏幕交给它。所以全屏时必须**注销 AppBar**，退出全屏后再**装
回去**。

## 状态机

```
                    ┌──────────────────────────────────────────────┐
                    │ 正常态：AppBar 已注册，dock 可见并置顶          │
                    └──────────────────────────────────────────────┘
                          │                            ▲
   连续 enter_confirm 轮  │                            │ 连续 exit_confirm 轮
   命中「非系统程序全屏」   │                            │ 未命中
                          ▼                            │
                    ┌──────────────────────────────────────────────┐
                    │ 让位态：AppBar 已注销（工作区还给系统）          │
                    │        dock 窗口隐藏、提示条收起                │
                    └──────────────────────────────────────────────┘
```

* `enter_confirm` 默认 **1**：全屏程序一出现就让位，dock 不会在全屏画面上多停一拍。
* `exit_confirm` 默认 **2**：连续两轮（≈800ms）没检测到全屏才恢复。这是为了扛住
  Alt+Tab 切窗口、全屏程序弹系统对话框这类瞬时抖动 —— 否则 AppBar 会反复注册/
  注销，把整个桌面的工作区来回拉扯。
* 只有**状态真正翻转**时才会发信号，所以稳态下不会反复动 AppBar。

## 判定规则：什么算「非系统程序全屏显示」

实现见 `core/fullscreen_watch.py`。一个窗口要**同时**满足下面五条才算数：

| # | 条件 | 为什么 |
| - | ---- | ------ |
| 1 | 是**前台窗口**，或前台窗口的 root owner | 「用户正在看的东西」才该让位；多显示器下用户在副屏工作时，主屏挂着的全屏游戏不该把 dock 一起藏掉。root owner 用来覆盖全屏程序弹出的对话框/菜单。 |
| 2 | 可见、未最小化、不是工具窗口（`WS_EX_TOOLWINDOW`） | 输入法提示、悬浮工具栏之类的辅助窗口铺满屏幕也不代表"有程序全屏"。 |
| 3 | 窗口矩形完整覆盖**它所在显示器**的 `rcMonitor`（容差默认 2px） | 真正的全屏是铺满显示器；**普通最大化窗口过不了这一条**——dock 的 AppBar 已经抬高了工作区底边，最大化窗口的底边停在工具区底边附近，够不到显示器底边。 |
| 4 | 窗口类不是系统外壳（`Progman`/`WorkerW`/`Shell_TrayWnd`/任务视图…） | 桌面、任务栏铺满屏幕也不是"某个程序全屏"。 |
| 5 | 所属进程不是 Windows 自身组件（见下） | 这才是需求里「非系统程序」的落点。 |

### 「系统程序」的界定

* 内置名单 `SYSTEM_PROCESS_NAMES`：explorer / dwm / 登录锁屏 / 外壳（开始菜单、
  搜索、输入法）/ UAC `consent.exe` / 设置 / 任务管理器等，共 24 项。
* 用户可以再用 `fullscreen.except_processes` 追加（每行一个进程名）。
* **刻意不复用** `dock.except_processes`：那份列表的语义是「不要在 dock 上显示」，
  里面既有 `applicationframehost.exe`（UWP 应用的全屏窗口在系统里属于它），也有
  `python.exe`（本项目自己就是 python 跑起来的）。拿它判断全屏会误伤 UWP 全屏和
  pygame 一类用 python 跑的全屏程序。

### 为什么是轮询而不是 `SetWinEventHook`

事件钩子需要一条带消息循环的专用线程，回调还跑在系统上下文里，出错时排查成本很
高。项目里 `core/process_scan.ProcessScanWorker` 已经确立了「后台线程定时轮询 +
信号投递回 GUI 线程」的模式，这里沿用同一套：每轮只是一两次
`GetForegroundWindow` / `GetWindowRect` 和一次**带 TTL 缓存**的进程查询
（复用 `ProcessManager.proc_info_for_pid`），成本远低于既有的进程扫描，而且能被
`core/thread_mgr` 统一启停。

## 接线（`dock.py`）

| 环节 | 位置 | 说明 |
| ---- | ---- | ---- |
| 启动监听 | `_start_fullscreen_watch()` | 在 `thread_manager` 就绪后登记线程；`fullscreen.enabled=false` 时不启动。 |
| 进入让位 | `enter_fullscreen_suppression()` | **先注销 AppBar，再隐藏窗口**。AppBar 的宿主窗口是另一个隐藏窗口，两步互不依赖；先注销是为了尽早把工作区还给全屏窗口，不给它留一帧被保留区挤压的机会。 |
| 退出让位 | `exit_fullscreen_suppression()` | 刷新屏幕指标 → 按最新位置重新注册 AppBar → 重排并显示 dock → 补一次置顶保险。 |
| 幂等 | 两个方法都以 `_fs_suppressed` 早退 | 重复信号不会把 AppBar 注册/注销玩乱。 |
| 屏幕变化 | `_on_screen_changed()` | 让位期间**跳过** AppBar 重新注册（否则会把保留区塞回全屏窗口，让位当场失效），只刷新指标，等全屏结束再按新分辨率注册。 |
| 安全恢复 | `_ensure_dock_visible()` | 让位期间不强行显示 dock。 |
| 退出程序 | `exit_app()` / `atexit` | 先停监听线程再注销 AppBar，避免退出过程中又被信号动一次 AppBar。 |
| 设置界面 | `_apply_fullscreen_settings()` | 开关即时启停监听线程；排除列表即时下发给线程。 |

位置计算统一走 `_dock_target_y()`：读**启动时保存的**原始工作区底部
（`_original_work_area_bottom`）减去窗口高度。因为用的是保存值，反复注销/注册
AppBar 不会累积漂移；`update_window_position()` 用的是同一个算式，两者不会打架。

## 配置

`settings.json`（老配置文件缺失时由 `config_manager.load_config` 自动补齐）：

```json
"fullscreen": {
  "enabled": true,
  "poll_interval_ms": 400,
  "enter_confirm": 1,
  "exit_confirm": 2,
  "tolerance": 2,
  "except_processes": []
}
```

设置界面 → **Dock** 页 → 「全屏程序」分组里可以开关，并维护不让位的程序名单；
`poll_interval_ms` / `enter_confirm` / `exit_confirm` / `tolerance` 属于调参项，
只在配置文件里手改（界面不会覆盖它们）。

## 已知边界与取舍

* **窗口化全屏（borderless windowed）**：只要窗口矩形铺满显示器且是前台，一样会
  触发让位 —— 这正是想要的。
* **副屏全屏**：前台窗口在副屏铺满 → 让位（dock 在主屏底部，也会让位，因为工作区
  是全局概念）。用户在副屏全屏时，主屏的 dock 也会消失，这是当前取舍。
* **打包运行时**按 `sys.executable` 排除自己；源码运行（`python dock.py`）时**不**
  按进程名排除自己，否则 python 写的全屏程序会被一起放过。dock 自己的窗口另有
  句柄兜底（`set_ignored_hwnds`）。
* **用户拿回 dock 的办法**：按 Win 键/Alt+Tab 让全屏程序失去前台。前台一旦不再是
  全屏窗口（开始菜单属于系统组件、不在候选里），`exit_confirm` 轮之后 dock 就回来。
* **`exit_confirm=2` 的代价**：全屏程序关闭后 dock 会晚约 0.8 秒出现。

## 测试与验证

自动回归（`tests/`，均为可直接执行的独立脚本）：

```bash
python tests/test_fullscreen_watch.py   # 判定规则 / 去抖状态机 / 监听线程 / 设置界面往返
```

手动验证脚本（会与真实系统交互，已加注释说明影响范围）：

```bash
python tests/manual_fullscreen_probe.py [秒数]   # 只看不动：打印前台窗口与判定理由
python tests/manual_appbar_cycle.py              # 真机 AppBar 注册→注销→重新注册
python tests/manual_dock_fullscreen_cycle.py     # 启动真实 dock 实例，走一遍让位/恢复
```

`manual_fullscreen_probe.py` 不会改动任何系统状态，可以边用电脑边跑；想看到
「会让位」的判定，就在观察期间把浏览器按 F11 全屏。

`manual_dock_fullscreen_cycle.py` 覆盖两层：先直接调让位/恢复入口验证 AppBar 与
窗口状态，再**造一个铺满显示器的前台窗口**跑真正的自动链路（监听线程 → 信号 →
dock 让位 → 窗口关闭后自动恢复），全程不手动调用让位方法。另外两个脚本会让屏幕
底部短暂出现 dock、工作区短暂变化（约 3 秒会被一块深灰色窗口铺满），收尾都会把
工作区还原。

最近一次真机结果（1920×1080，系统任务栏可见时工作区底部 1020）：

```
让位前工作区底部=959（dock 的 AppBar 保留区生效）
让位后 AppBar 已注销 → 工作区底部回到 1020，dock 隐藏
恢复后 AppBar 重新注册 → 工作区底部精确回到 959，dock 重新显示
自动链路：模拟全屏窗口 rect=(0,0,1920,1080) → 自动让位 → 关闭后自动恢复
```

日志关键字：`[全屏]`、`[AppBar]`。

## 附：本次实现顺带修掉的两个坑

这两个都是**既有代码里潜伏的问题**，被「全屏结束后必须重新注册 AppBar」这条新
路径踩了出来：

1. **AppBar 宿主窗口类的 WNDPROC 悬空指针**（`core/sys32.py`）。
   老实现每次创建宿主窗口都重新构造 `_WNDPROC` 回调并覆盖模块全局，而窗口类只
   注册一次、里面存的是**第一个**回调的代码指针。全局一换，旧回调被回收，窗口类
   就指向了已释放内存 —— 之后再创建该类窗口会以 `0xC000041D`（用户回调中发生致命
   异常）直接崩掉进程。也就是「重新注册 AppBar 必崩」。现在回调只在注册窗口类那
   一次创建，之后不再替换。

2. **ctypes 签名冲突导致工作区读数为空**（`core/sys32.py` + `core/fullscreen_watch.py`）。
   `ctypes.windll.user32` 在进程内是同一个对象，`argtypes` 挂在函数对象上。新模块
   曾用自己的 `MONITORINFO` 结构体再声明一次 `GetMonitorInfoW`，覆盖掉 sys32 的
   声明，导致 sys32 传进去的 `byref` 结构体类型对不上而抛 `ArgumentError` ——
   异常被 `except` 吞掉，`refresh_metrics()` 从此永远返回旧值（进程刚起来时是
   全 0），屏幕变化后工作区再也不更新。现在这类「多个模块都要用」的函数只在
   `core/sys32.py` 声明一次，并提供 `monitor_rect_for_window()` 供其它模块调用。

另外把 `ABM_NEW` 的返回值判断改准确了：**同一个宿主窗口重复 `ABM_NEW` 会返回 0，
那是"本来就注册着"而不是失败**。老实现（以及本次修改的第一版）会因此把状态标成
「未注册」，让位逻辑就会跳过注销，保留区再也还不回去。
