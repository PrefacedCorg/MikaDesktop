# 右侧扩展窗口

主 dock 栏右边挂一个"扩展窗口"：**高度与 dock 完全相同**，宽度限制在 **150 ~ 600**
之间，**启动时以最小宽度（150）注册**，当前不显示任何内容。它和 dock 一起参与布
局、一起让位。

实现：`core/dock_extension.py`（窗口本体 + 纯函数布局算式），接线：`dock.py`。

```
                     ┌─ 可用几何（AppBar 注册前的工作区） ─────────────┐
                     │                                                  │
                     │        ┌────────────┬───gap──┬──────────┐        │
                     │        │   dock     │         │  扩展窗口 │        │
                     │        └────────────┴─────────┴──────────┘        │
                     │                    ↑ work_bottom（底边对齐）      │
```

## 1. 尺寸

| 项 | 值 | 来源 |
| --- | --- | --- |
| 高度 | 与 dock 相同（**取运行时实际高度**） | `follow_dock()` 用 `dock.height()`；名义值 `DockConstants.WINDOW_HEIGHT`（48）只在显示前兜底 |
| 宽度下限 / 启动宽度 | 150 | `DockConstants.EXTENSION_MIN_WIDTH` |
| 宽度上限 | 600 | `DockConstants.EXTENSION_MAX_WIDTH` |
| 与 dock 的间隙 | 12 | `DockConstants.EXTENSION_GAP` |

* `DockExtensionWindow.set_extension_width(w)` 把 `w` 夹到 `[150, 600]`；
  `DockApp.set_extension_width(w)` 在此之上再重排 dock + 扩展窗口，供后续往里放内容
  时使用；这两个方法是当前唯一的宽度入口。
* 窗口标志与 XHT 一致（`FramelessWindowHint | WindowStaysOnTopHint | Tool |
  WindowDoesNotAcceptFocus`）：无边框、置顶、不进任务栏、点击不抢焦点；另加
  `WA_ShowWithoutActivating`，让"恢复显示"不把焦点从用户窗口上抢走。

## 2. 一并计算位置

布局算式收敛在纯函数 `core.dock_extension.layout_rects()` 里，dock 与扩展窗口的目标
矩形一次算完（`dock.py: update_window_position`）：

```
max_group = 可用宽度 * 0.9
max_dock  = max(max_group - gap - 扩展窗口宽, 两个按钮宽)
dock 宽   = min(内容需要宽度 与 Qt 最小宽度 的较大者, max_dock)
x         = 可用几何.x + (可用宽度 - 整体宽度) // 2      # 整体居中
y         = 保存的原始工作区底部 - 窗口高度              # 底边对齐
```

要点：

* **整体居中**：不只看 dock 自己的宽度，扩展窗口宽度（+ 间隙）也计入；实测 1536 宽
  可用区、814 宽 dock、150 宽扩展窗口时左右留白各 280。
* **dock 宽度取"内容估算"与"Qt 最小宽度"的较大者**：`DockApp` 的按钮 + 布局边距/
  间距会被 Qt 强制成一个最小宽度（实测内容估算 724、实际 814），只用估算值会出现"窗
  口显示后被撑宽"，整体偏离中心、间隙也算错。
* **dock 的 90% 上限会为扩展窗口让位**（`max_group - gap - 扩展窗口宽`），避免
  "dock 撑满 90% 屏宽 + 右侧还有扩展窗口"把扩展窗口挤出屏幕。
* **窄屏降级**：dock 至少保留两个按钮的宽度（菜单 + 设置）；整体仍然贴左。

### 落位：贴住 dock 的**实际**矩形

目标矩形只用来定位 dock；扩展窗口最终由 `DockExtensionWindow.follow_dock(dock_rect)`
摆放：

```
扩展窗口.x = dock.right() + 1 + EXTENSION_GAP
扩展窗口.y = dock.y,  高度 = dock.height()          # 高度始终等于 dock 实际高度
```

`DockApp.moveEvent` / `resizeEvent` 都会调用 `_follow_extension()`，所以：

* dock 被 Qt 撑高（常量写的是 48，实际 90）、改宽、或做 220ms 宽度动画的每一帧，扩
  展窗口都跟着走 —— 间隙恒为 `EXTENSION_GAP`（12px），**永远不重叠**；
* 高度不写死常量，取 dock 的真实高度（"高度与 dock 栏相等"是运行时保证的，不只是名
  义值）；
* 扩展窗口不需要自己的几何动画，跟着 dock 的每一帧走即可。

## 3. 一并让位（全屏程序）

扩展窗口**没有**自己的 AppBar 保留区：它落在 dock 已注册的底部 AppBar 条带内部
（`rc.left=0 ~ REAL_SCREEN_WIDTH`），不需第二次 `SHAppBarMessage`。但显隐必须跟着
dock：

| 时机 | dock | 扩展窗口 |
| --- | --- | --- |
| 启动 / 恢复显示 | `show()` | `_ensure_dock_visible()` 里一并 `show()` |
| 非系统程序全屏（让位） | `hide()`，AppBar 注销 | `_set_extension_visible(False)` 一并隐藏 |
| 全屏结束（恢复） | AppBar 重新注册、重排、`show()` | `exit_fullscreen_suppression()` 的 `finally` 里一并恢复 |
| 系统把 dock 重新显示 | `_ensure_dock_visible()` | 同上，一起恢复 |

另外，扩展窗口的窗口句柄会随 `shown_signal` 登记进全屏监听的忽略列表
（`FullscreenWatcherWorker.set_ignored_hwnds`），否则一个置顶的扩展窗口可能被判成
"全屏程序"而触发让位循环。

## 4. 验证

```bash
python tests/test_dock_extension.py     # 尺寸夹取 / follow_dock 间隙 / 一并布局（纯函数断言）
python tests/test_fullscreen_watch.py   # 让位时扩展窗口一起隐藏、恢复、忽略列表
python tests/manual_dock_fullscreen_cycle.py   # 真机端到端（短暂显示真实 dock + 扩展窗口，
                                               # 自己收尾注销 AppBar）
```

实测（1536×816 可用区，dock 实测 814×90）：`dock=(280,767,814,90)`、
`扩展=(1106,767,150,90)` —— 间隙 12px、不重叠、等高、整体居中（左右留白各 280），
且让位/恢复时两者一起隐藏/显示。
