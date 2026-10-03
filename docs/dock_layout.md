# dock 布局：间距忽大忽小的排查与修复

## 现象

真机截图里第二组（`app_container`，用户应用组）按钮间距时而正常 10px、时而变成
24px，其余分组正常；重新截图有时又好了。像素实测（1437×135，DPR 1.25）：

```
卡片宽度 75px（= 60 逻辑像素）
第一组（固定应用）间距 12/13px（= 10 逻辑像素，正常）
第二组（用户应用）间距 30px   （= 24 逻辑像素，偏大）
第三组（运行中）+ 设置      间距 12/13px（正常）
```

## 根因：窗口缩不回去，多出来的宽度全被 `app_container` 吞掉

`content_layout` 里只有 `app_container` 带 stretch（`addWidget(self.app_container, 1)`）。
只要窗口比"布局最小宽度"宽，多出来的宽度就全部给这个容器；容器内部都是固定尺寸按
钮，于是这些宽度被当成间隙平分到按钮之间 —— 正好落在第二组上。截图里多出的 70px
（= 一个按钮 60 + 一个间距 10）被 4 个按钮的 5 个边界平分，每个边界 +14px →
10 + 14 = 24px，与实测完全吻合。

而"窗口为什么会偏宽"，是 Qt 的**异步布局冒泡** + 一个防抖保护共同造成的：

| # | 行为 | 后果 |
| - | - | - |
| 1 | `create_app_button` 只把按钮 `addWidget` 进布局；新按钮仍是隐藏状态，要等 Qt 处理 ChildPolished / LayoutRequest 后才显示 | `QWidgetItem::isEmpty()` 为真，布局把这一组的最小尺寸算成 0 |
| 2 | 按钮增删 / 分组显隐把布局标脏，重算走**稍后处理的 LayoutRequest** | 同一次调用里 `minimumSizeHint()` 还是**上一次**的最小尺寸（探针实测：内容已减少，仍报 884 而不是 592） |
| 3 | 布局把最小尺寸写到窗口自身的 `minimumSize` 也是异步的 | 即使目标算对了，`setGeometry`/收缩动画会被**旧的最小尺寸夹回去**（实测窗口停在 884，新最小宽度 662） |
| 4 | `_geom_anim_target` 的"目标没变就不重启动画"保护 | 动画被夹住没到位后，后续调用直接 return，**偏宽的几何被永久锁死**，直到下一次内容变化才可能解锁 |

所以：**内容变少时窗口经常不收缩**（偶发、与扫描/增删时序有关），多出来的宽度被第
二组吞掉。这是本次改动之前就存在的老问题，与右侧扩展窗口无关（扩展窗口只是让 dock
的目标宽度又变了一次，更容易触发）。

## 修复（`dock.py`）

1. **新按钮立刻显示**（`create_app_button`，[dock.py:234](../dock.py#L234)）：
   `layout.addWidget(button)` 之后 `button.show()`，布局马上把它算进最小尺寸，而不
   是等到下一次事件循环。
2. **同步取布局最小尺寸**（`_refresh_layout_minimum`，[dock.py:962](../dock.py#L962)）：
   先 `activate()` 窗口布局 + 中央部件布局，再读 `minimumSizeHint()`；目标宽度用这
   个真实值，而不是内容估算值。
3. **同步放低窗口最小尺寸**（`_relax_minimum_size`，[dock.py:985](../dock.py#L985)）：
   布局最小尺寸变小时，同步 `setMinimumWidth/Height`（只放低、不抬高），否则收缩目
   标会被旧的 `minimumSize` 夹回去；放大方向本来不受夹，保流动画。
4. **目标高度用布局最小高度**（`update_window_position`，[dock.py:900](../dock.py#L900)）：
   dock 名义高度是 48，Qt 实际最小高度是 90；不补齐的话 `current == target` 永远不
   成立，每轮轮询都会重启动画。y 不变（AppBar 保留区上边界仍按名义高度算）。
5. **收敛保护**（[dock.py:916](../dock.py#L916)）：只有"确实正往这个目标做动画"时才跳
   过；动画已结束却没到位则直接落位；另外 `geometry_anim.finished` 里再补一次
   （`_on_geometry_anim_finished`，[dock.py:1002](../dock.py#L1002)）。

## 不变量

跑完任何一次内容变化后必须成立：

```
dock.width() == dock.layout().totalMinimumSize().width()      # 窗口没有富余宽度
每组按钮间距   == DockConstants.BUTTON_SPACING (10)            # 富余宽度没有落到按钮之间
```

## 验证

```bash
python tests/test_dock_layout.py             # offscreen + 假屏幕跑真实布局方法，
                                             # 覆盖增删/快速连续变化/反复收缩放大 5 轮
python tests/manual_dock_fullscreen_cycle.py # 真机：宽度 == 布局最小宽度、三组间距都 = 10
```

真机实测：`dock=(210,767,954,90)`、`min=954`、三组间距 `[10,10] / [10,10,10] / [10,10,10]`、
扩展窗口间隙 12px、整体居中（左右留白各 210）。
