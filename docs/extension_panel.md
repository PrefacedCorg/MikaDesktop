# 扩展窗口状态面板

扩展窗口（dock 右侧那块卡片，`core/dock_extension.py`）里放的是
**网络 / 音量 / 电源 + 通知中心入口**，见 `core/extension_panel.py`：

```
┌──────────────────────────────────────────┐
│ [网络] [音量] [电源?] │ [通知中心]         │  ← 高度 90，与 dock 相同
└──────────────────────────────────────────┘
```

| 按钮 | 图标 | tooltip | 点击 |
| --- | --- | --- | --- |
| 网络 | `res/icon_network*.png`（断网为红色） | 网络名 + 介质 + 是否联网 | WLAN 网络列表（`ms-availablenetworks:`） |
| 音量 | `res/icon_volume_*.png` | 音量百分比（静音会标注） | **快速设置主界面**（磁贴 + 亮度/音量滑杆，见第 2 节） |
| 电源 | `res/icon_battery_*.png`（红/橙/浅绿/绿） | 电量 + 充放电状态 | 设置的「电池」页（`ms-settings:batterysaver`）；**没有电池的机器上不显示** |
| 通知中心 | `res/icon_notify.png` | 固定文案 | 系统通知中心（`ms-actioncenter:`） |

按钮尺寸与 dock 一致（60×60，图标 48，`DockConstants.EXTENSION_BUTTON_STYLE`），
状态由图标本身的颜色/字形表达（见第 3 节）。

## 1. 状态从哪来

后端都在 `core/system_status.py`，纯逻辑、不依赖 Qt 窗口：

| 项 | 取法 |
| --- | --- |
| 电源 | `GetSystemPowerStatus`（ctypes）：`BatteryFlag` 的 bit7 表示"没有电池" |
| 网络 | `INetworkListManager`（COM，`win32com`）拿连接/联网与网络名；`GetAdaptersAddresses`（iphlpapi）拿介质（无线 71 / 有线 6）与运行状态兜底 |
| 音量 | `IAudioEndpointVolume`（WASAPI，`comtypes` 手写接口声明）读主音量与静音 |

* 轮询在后台线程 `SystemStatusWorker`（默认 2s，登记进 dock 的统一线程管理器），
  结果通过 `status_changed` 信号回 GUI 线程；COM 在本线程 `CoInitialize`，
  接口对象按线程缓存在 `_local` 上（COM 单元模型要求同线程复用）。
* **任何一项失败都不抛异常**：对应字段带 `error`，界面显示"未知/读取失败"，
  绝不把 dock 带崩。

## 2. 原生面板：每个键打开哪里

| 动作 | Windows 11（实测） | Windows 10 |
| --- | --- | --- |
| 网络 | `ms-availablenetworks:` → **WLAN 网络列表** | 同一个 URI → 网络浮出 |
| 音量 | **快速设置主界面**（截图里那块：网络/蓝牙/投影磁贴 + 亮度/音量滑杆） | `ms-actioncenter:`（Win10 没有快速设置面板，退到操作中心里的音量快速操作） |
| 电源 | 设置的「电池」页：`ms-settings:batterysaver`（Win11 落在「系统 › 电源和电池」，实测） | 同一个 URI → 节电设置 |
| 通知 | `ms-actioncenter:` → 「通知中心」 | 同一个 URI → 「操作中心」 |

**「快速设置主界面」没有对应的 shell URI**，标准做法是 `Win+A` 热键 —— 但本机
（Win11 26300）注入不进去：`SendInput` 对 Win 键直接返回 0，`keybd_event` 也不触发
shell 热键（连单独按 Win 都弹不出开始菜单）。所以走的是：

```
ms-availablenetworks:  →  面板打开（落在 WLAN 页）
                       →  等 ~0.7s
                       →  UI Automation 找到面板里的「后退」按钮并 Invoke
                       →  回到快速设置主界面
```

这段在 `core/system_status.open_quick_settings_main()` / `click_quick_settings_back()`
里，用 `QTimer` 延迟 + 最多 3 次重试，不阻塞界面；面板本来就停在主界面时找不到
「后退」按钮，安全跳过。

`ms-availablenetworks:` / `ms-actioncenter:` 都是**切换**语义：面板开着时再调一次会
把它关掉（和任务栏上点网络/音量图标一样）。所以手动测试脚本在触发前会先确认面板处于
关闭状态，否则那一步会变成"关闭"。

排查时的一个坑：这些面板是 XAML 岛窗口，**`EnumWindows` / `IsWindowVisible` 看不到
它们**（类名 `ControlCenterWindow` 根本不出现在顶层窗口列表里），要判断开关得看
**前台窗口**或用 **UI Automation** 枚举；`tests/manual_extension_panel.py` 里两种
都用了。UI Automation 的「后退」按钮名字是中文「后退」。

## 3. 图标

面板用的图标都放在 `res/`，风格与 `res/icon_settings.png` 一致：**用
`core/make_app_icon/overlay.py` 的模板合成出来的白底圆角卡片 + 字形**，
画布 256×256、按钮里按 48px 显示（和 dock 的应用图标同一套做法）。

| 文件 | 含义 |
| --- | --- |
| `icon_network.png` / `icon_network_eth.png` / `icon_network_off.png` | 无线 / 有线 / 断开（断开是红色斜杠） |
| `icon_volume_high.png` / `icon_volume_low.png` / `icon_volume_mute.png` | 音量高 / 低 / 静音 |
| `icon_battery_full/mid/low/empty/charging.png` | 绿(满) / 浅绿(中) / 橙(低) / 红(空) / 充电 |
| `icon_notify.png` | 通知中心 |

**状态由图标颜色表达**，所以面板按钮只保留 dock 那套统一描边，没有"激活态描边"
变体 —— 再叠一层主色边框会和图标的颜色语义打架（比如红色断网图标套一圈高亮边框）。
图标本身是手工维护的资产，`tests/test_extension_panel.py` 会检查面板引用到的每个
文件都存在。

> `res/icon_keyboard.png` 原本是给"输入法切换"按钮用的，该按钮已按需求删除，
> 图片留在了 `res/` 里（当前没有代码引用）。

## 4. 宽度与让位

* 扩展窗口宽度 = 内容自然宽度（含 15px 边距），夹在 **150~600**；
  电池按钮显隐会改变内容宽度，面板发出 `preferred_width_changed`，dock 收到后重排
  整个"dock + 扩展窗口"的组合（见 [dock_extension.md](dock_extension.md)）。
* 全屏让位时扩展窗口连同面板一起隐藏、恢复时一起显示（面板是被窗口一起藏起来的，
  不需要额外代码）。

## 5. 验证

```bash
python tests/test_extension_panel.py        # 自动化：图标/文案映射、电池显隐与宽度、
                                            # 点击行为（假 API，不碰系统）
python tests/manual_extension_panel.py      # 真机：显示面板 + 打开快速设置/通知中心 +
                                            # 命中测试，并输出预览图
python tests/manual_dock_fullscreen_cycle.py  # 真机：真实 dock + 扩展窗口 + 让位
```

真机实测（Win11 26300）：面板 282×60、扩展窗口 312×90；网络键 → WLAN 列表、
音量键 → 快速设置主界面（UIA 名字里能读到「蓝牙 / 投影 / 亮度」）、电源键 → 设置
（`ApplicationFrameWindow` / 设置）；通知键 → 「通知中心」并能 Esc 关闭；光标落在
面板按钮上时 `QApplication.widgetAt()` 拿到的就是那个按钮（说明置顶的工具窗口能
正常接住鼠标）。

> **手动点击仍建议真人验一遍**：这台开发机上系统拦下了所有合成输入
> （`SendInput` 连鼠标左键都返回 0），脚本没法模拟真人点击，只能做命中测试 +
> Qt 层的 `clicked` 验证。换一台不拦合成输入的机器，`manual_extension_panel.py`
> 会自动多做一步"真实鼠标点击通知按钮"的检查。
