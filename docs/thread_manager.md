# 线程管理器（`core/thread_mgr`）

统一线程的创建 / 启动 / 暂停 / 恢复 / 停止 / 销毁，带状态跟踪、优先级与信号。
实现：`core/thread_mgr/manager.py`（`ThreadManager(QObject)`，内部 `threading.RLock`
保证线程安全，默认最多 16 个线程）。

## 核心概念

**ThreadState**（7 种状态）：

| 状态 | 含义 | 状态 | 含义 |
| ---- | ---- | ---- | ---- |
| `CREATED` | 已注册未启动 | `STOPPED` | 已停止 |
| `RUNNING` | 运行中 | `ERROR` | 出错 |
| `PAUSED` | 已暂停 | `COMPLETED` | 自然运行结束 |
| `STOPPING` | 停止中 | | |

**ThreadPriority**：`LOW (-1)` / `NORMAL (0)` / `HIGH (1)`。

**ThreadInfo**：`id`（UUID）、`name`、`level`、`state`、`created_time`、`worker`、
`start_time`、`end_time`、`error`，另有 `run_count`（启动轮次，用于丢弃过期的
`finished` 信号，见文末）、`finished_handler`、`error_handler`（重新 `run()` 时重绑）。

## 快速开始

```python
from core.thread_mgr.manager import ThreadManager, ThreadPriority
from PySide6.QtCore import QThread

manager = ThreadManager(max_threads=16)

class MyWorker(QThread):
    def run(self):
        for i in range(10):
            print(f"Working... {i}")
            self.msleep(1000)

thread_id = manager.create(name="MyWorker", level=ThreadPriority.NORMAL,
                           start_when_create=True, worker=MyWorker())
print(manager.get_thread_info(thread_id).state)
manager.stop(thread_id, wait=True, timeout=5000)   # quit → wait(timeout) → 超时 terminate
manager.destroy(thread_id)                         # 先停，再 deleteLater 并移除记录
```

## API

### 生命周期方法

| 方法 | 签名 | 说明 |
| ---- | ---- | ---- |
| `create` | `(name, level=NORMAL, start_when_create=False, worker) -> str` | 注册线程并返回 `thread_id`。`level` 非法或缺 `worker` 抛 `ValueError`；超过 `max_threads` 抛 `RuntimeError`。此时只连 `errorOccurred`，`finished` 的连接在 `run()` 里按轮次绑定。 |
| `run` | `(thread_id) -> bool` | 启动线程。仅当状态为 `CREATED` / `STOPPED` / `PAUSED` 才成功；按 `run_count` 轮次重绑 `finished` 回调后 `worker.start()`。 |
| `stop` | `(thread_id, wait=True, timeout=5000) -> bool` | `quit()` → `wait(timeout)`，超时则 `terminate()`。**以「worker 是否真的在跑」判定**（状态可能被上一轮遗留信号停在 `COMPLETED`，但线程仍在跑），状态机与实际不一致时也能停掉。 |
| `pause` | `(thread_id) -> bool` | 仅 `RUNNING` 时有效，且要求 worker 实现了 `pause()` 方法。 |
| `resume` | `(thread_id) -> bool` | 仅 `PAUSED` 时有效，且要求 worker 实现了 `resume()` 方法。 |
| `destroy` | `(thread_id) -> bool` | 运行/暂停中先 `stop()`，再 `worker.deleteLater()` 并从记录中删除。 |
| `stop_all` | `() -> None` | 不等待地停掉所有线程；判定同样以 `worker.isRunning()` 为准，不只看状态机，避免误标 `COMPLETED` 的线程退出时留在后台。 |

### 查询方法

| 方法 | 返回 |
| ---- | ---- |
| `get_thread_info(thread_id)` | `ThreadInfo`，不存在返回 `None` |
| `get_all_threads()` | 全部 `ThreadInfo` 列表 |
| `get_threads_by_state(state)` | 按状态筛选的列表 |
| `get_active_count()` | 活跃（RUNNING）线程数 |
| `get_total_count()` | 总记录数 |

### 信号

```python
thread_started       = Signal(str, str)      # (thread_id, thread_name)
thread_stopped       = Signal(str, str)      # (thread_id, thread_name)
thread_error         = Signal(str, str, str) # (thread_id, thread_name, error_message)
thread_state_changed = Signal(str, str, str) # (thread_id, old_state, new_state)

manager.thread_started.connect(lambda tid, name: print(f"{name} started"))
manager.thread_state_changed.connect(lambda tid, old, new: print(f"{old} -> {new}"))
```

## 支持暂停 / 恢复的 worker

`pause()` / `resume()` 只是转调 worker 对象上的同名方法，worker 需要自己实现：

```python
class PausableWorker(QThread):
    def __init__(self):
        super().__init__()
        self._paused = False
        self._pause_lock = threading.Lock()

    def pause(self):                 # manager.pause() 会调用它
        with self._pause_lock:
            self._paused = True

    def resume(self):                # manager.resume() 会调用它
        with self._pause_lock:
            self._paused = False

    def run(self):
        while not self.isInterruptionRequested():
            if self._paused:
                self.msleep(100)     # 暂停时短暂休眠，不退出
                continue
            ...                      # 实际工作
            self.msleep(1000)

thread_id = manager.create(name="PausableWorker", worker=PausableWorker())
manager.run(thread_id); manager.pause(thread_id); manager.resume(thread_id)
```

## 重复启停同一个 worker

同一个 worker 可以 `stop()` 之后再 `run()` 重启（例如「关闭通知 → 再打开通知」），
有两点需要 worker 自己配合：

1. **`quit()` 要真的能停**：`stop()` 的流程是 `worker.quit()` → `worker.wait(timeout)`，
   而 `QThread.quit()` 只对跑事件循环（`exec()`）的线程有效。如果 worker 重写了
   `run()` 跑自己的循环，请一并重写 `quit()` 去置位停止标志，否则 `wait()` 必然超时，
   进而走到 `terminate()` 强杀线程。
2. **重启前要清掉停止标志**：在 `run()` 开头把停止标志复位，否则重启后会立刻退出。

管理器侧已经处理了「上一轮排队的 `finished` 信号在下一轮才被投递」的问题：每次
`run()` 都会按轮次（`run_count`）重新绑定回调，过期信号会被忽略，不会把正在运行的
新一轮误标成 `COMPLETED`；`stop_all()` 也以「worker 是否真的在跑」为准。

## 最佳实践

* **命名**：用有意义的名称（`功能_任务`），便于调试与监控。
* **错误处理**：检查方法返回值；连接 `thread_error` 信号处理异常。
* **资源**：及时 `destroy()`；退出时 `stop_all()` 清理；合理设置 `max_threads`。

## 故障排除

| 问题 | 排查方向 |
| ---- | -------- |
| 线程无法启动 | 状态是否为 `CREATED`/`STOPPED`/`PAUSED`；`worker` 是否有效 QThread；是否达到 `max_threads` |
| 暂停/恢复无效 | worker 是否实现了 `pause()`/`resume()`；状态是否为 `RUNNING`/`PAUSED` |
| 信号不触发 | 连接是否正确；Qt 事件循环是否在运行 |
| 内存泄漏 | 及时 `destroy()`；避免大量短期线程 |

调试时可直接打印状态：

```python
for t in manager.get_all_threads():
    print(f"  {t.name} ({t.id}): {t.state}")   # 总数/活跃数: get_total_count() / get_active_count()
```

## 性能考虑

管理器本身开销很小（一把锁 + 状态记录），主要开销来自工作线程本身：默认上限 16 个
线程，按需调整 `max_threads`，避免创建过多线程。
