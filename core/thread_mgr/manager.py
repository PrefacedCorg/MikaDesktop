from PySide6.QtCore import QThread, QObject, Signal
import uuid
import threading
from typing import Dict, List, Optional
from datetime import datetime

class ThreadState:
    """线程状态枚举"""
    CREATED = "created"     # 已注册
    RUNNING = "running"     # 运行中
    PAUSED = "paused"       # 已暂停
    STOPPING = "stopping"   # 停止中
    STOPPED = "stopped"     # 已停止
    ERROR = "error"         # 错误状态
    COMPLETED = "completed" # 已完成

class ThreadPriority:
    """线程优先级"""
    LOW = -1    # 低优先级
    NORMAL = 0  # 普通优先级
    HIGH = 1    # 高优先级

class ThreadInfo:
    """线程信息类"""
    def __init__(self, id: str, name: str, level: int, state: str, 
                 created_time: datetime, worker: QThread):
        self.id = id
        self.name = name
        self.level = level
        self.state = state
        self.created_time = created_time
        self.worker = worker
        self.start_time: Optional[datetime] = None
        self.end_time: Optional[datetime] = None
        self.error: Optional[str] = None
        #: 该 worker 被启动过几次。QThread 可以重复 start()，而上一轮排队的
        #: finished 信号可能在下一轮才被投递，用它把过期信号认出来丢掉。
        self.run_count = 0
        #: 当前绑定的 finished 回调，重新 run() 时要先断开旧的
        self.finished_handler = None
        #: 上一次报错的回调，同理需要在重新 run() 时替换
        self.error_handler = None

class ThreadManager(QObject):
    """改进的线程管理器"""
    
    # 信号定义
    thread_started = Signal(str, str)  # (thread_id, thread_name)
    thread_stopped = Signal(str, str)  # (thread_id, thread_name)
    thread_error = Signal(str, str, str)  # (thread_id, thread_name, error_message)
    thread_state_changed = Signal(str, str, str)  # (thread_id, old_state, new_state)
    
    def __init__(self, max_threads: int = 16):
        super().__init__()
        self.max_threads = max_threads
        self.threads: Dict[str, ThreadInfo] = {}
        self._lock = threading.RLock()
        self._active_count = 0
    
    def create(self, name: str, level: int = ThreadPriority.NORMAL, 
               start_when_create: bool = False, worker: QThread = None) -> str:
        """创建并注册线程"""
        if level not in [ThreadPriority.LOW, ThreadPriority.NORMAL, ThreadPriority.HIGH]:
            raise ValueError("level must be in [-1, 0, 1]")
        if worker is None:
            raise ValueError("worker must be provided")
        
        with self._lock:
            # 检查线程数量限制
            if len(self.threads) >= self.max_threads:
                raise RuntimeError(f"已达到最大线程数限制: {self.max_threads}")
            
            thread_id = str(uuid.uuid4())
            created_time = datetime.now()
            
            thread_info = ThreadInfo(
                id=thread_id,
                name=name,
                level=level,
                state=ThreadState.CREATED,
                created_time=created_time,
                worker=worker
            )
            
            self.threads[thread_id] = thread_info

            # 注意：这里只连 errorOccurred。finished 的连接放在 run() 里，
            # 因为 QThread 可以重复 start()，必须按「第几轮」重新绑定，
            # 否则上一轮排队的 finished 会把新一轮的状态误判成 COMPLETED。
            if hasattr(worker, 'errorOccurred'):
                thread_info.error_handler = (
                    lambda error, tid=thread_id: self._on_thread_error(tid, error)
                )
                worker.errorOccurred.connect(thread_info.error_handler)

            if start_when_create:
                self.run(thread_id)

            return thread_id
    
    def run(self, thread_id: str) -> bool:
        """启动线程"""
        with self._lock:
            if thread_id not in self.threads:
                return False
            
            thread_info = self.threads[thread_id]
            
            if thread_info.state not in [ThreadState.CREATED, ThreadState.STOPPED, ThreadState.PAUSED]:
                return False
            
            try:
                old_state = thread_info.state
                thread_info.state = ThreadState.RUNNING
                thread_info.start_time = datetime.now()
                thread_info.end_time = None
                thread_info.error = None

                # 按「第几轮」重新绑定 finished：先断开上一轮的回调，再把本轮
                # 的 run_count 捕获进去。上一轮遗留的、尚未投递的 finished 信号
                # 会带着旧代数回来，在 _on_thread_finished 里被直接忽略。
                thread_info.run_count += 1
                generation = thread_info.run_count
                previous = thread_info.finished_handler
                if previous is not None:
                    try:
                        thread_info.worker.finished.disconnect(previous)
                    except (RuntimeError, TypeError):
                        pass
                handler = (lambda gen: (lambda: self._on_thread_finished(thread_id, gen)))(generation)
                thread_info.finished_handler = handler
                thread_info.worker.finished.connect(handler)

                thread_info.worker.start()
                self._active_count += 1
                
                self.thread_state_changed.emit(thread_id, old_state, ThreadState.RUNNING)
                self.thread_started.emit(thread_id, thread_info.name)
                
                return True
            except Exception as e:
                thread_info.state = ThreadState.ERROR
                thread_info.error = str(e)
                self.thread_error.emit(thread_id, thread_info.name, str(e))
                return False
    
    def stop(self, thread_id: str, wait: bool = True, timeout: int = 5000) -> bool:
        """停止线程。

        判定以「worker 是否真的在跑」为准，而不只看状态机：状态可能因为上一轮
        遗留的信号停在 COMPLETED，但线程实际仍在运行，那种情况下也必须能停掉。
        """
        with self._lock:
            if thread_id not in self.threads:
                return False

            thread_info = self.threads[thread_id]
            was_active = thread_info.state in (ThreadState.RUNNING, ThreadState.PAUSED)

            if not was_active and not thread_info.worker.isRunning():
                return False

            try:
                old_state = thread_info.state
                thread_info.state = ThreadState.STOPPING

                thread_info.worker.quit()

                if wait:
                    if not thread_info.worker.wait(timeout):
                        thread_info.worker.terminate()
                        thread_info.worker.wait(1000)

                thread_info.state = ThreadState.STOPPED
                thread_info.end_time = datetime.now()
                # 只有先前被计入活跃数时才减，避免反复 stop 把计数减成负数
                if was_active:
                    self._active_count -= 1

                self.thread_state_changed.emit(thread_id, old_state, ThreadState.STOPPED)
                self.thread_stopped.emit(thread_id, thread_info.name)

                return True
            except Exception as e:
                thread_info.state = ThreadState.ERROR
                thread_info.error = str(e)
                self.thread_error.emit(thread_id, thread_info.name, str(e))
                return False
    
    def pause(self, thread_id: str) -> bool:
        """暂停线程（如果线程支持暂停功能）"""
        with self._lock:
            if thread_id not in self.threads:
                return False
            
            thread_info = self.threads[thread_id]
            
            if thread_info.state != ThreadState.RUNNING:
                return False
            
            # 这里需要线程对象实现暂停功能
            if hasattr(thread_info.worker, 'pause'):
                try:
                    old_state = thread_info.state
                    thread_info.worker.pause()
                    thread_info.state = ThreadState.PAUSED
                    
                    self.thread_state_changed.emit(thread_id, old_state, ThreadState.PAUSED)
                    return True
                except Exception as e:
                    thread_info.state = ThreadState.ERROR
                    thread_info.error = str(e)
                    self.thread_error.emit(thread_id, thread_info.name, str(e))
                    return False
            return False
    
    def resume(self, thread_id: str) -> bool:
        """恢复暂停的线程"""
        with self._lock:
            if thread_id not in self.threads:
                return False
            
            thread_info = self.threads[thread_id]
            
            if thread_info.state != ThreadState.PAUSED:
                return False
            
            if hasattr(thread_info.worker, 'resume'):
                try:
                    old_state = thread_info.state
                    thread_info.worker.resume()
                    thread_info.state = ThreadState.RUNNING
                    
                    self.thread_state_changed.emit(thread_id, old_state, ThreadState.RUNNING)
                    return True
                except Exception as e:
                    thread_info.state = ThreadState.ERROR
                    thread_info.error = str(e)
                    self.thread_error.emit(thread_id, thread_info.name, str(e))
                    return False
            return False
    
    def destroy(self, thread_id: str) -> bool:
        """销毁线程"""
        with self._lock:
            if thread_id not in self.threads:
                return False
            
            thread_info = self.threads[thread_id]
            
            # 先停止线程
            if thread_info.state in [ThreadState.RUNNING, ThreadState.PAUSED]:
                self.stop(thread_id, wait=True)
            
            try:
                thread_info.worker.deleteLater()
                del self.threads[thread_id]
                return True
            except Exception as e:
                return False
    
    def stop_all(self) -> None:
        """停止所有线程。

        只要 worker 实际在运行就停，不依赖状态机是否恰好是 RUNNING —— 否则一个
        状态被误标成 COMPLETED 的线程会在这里被漏掉，退出时留在后台。
        """
        with self._lock:
            for thread_id, thread_info in list(self.threads.items()):
                if (thread_info.state in (ThreadState.RUNNING, ThreadState.PAUSED)
                        or thread_info.worker.isRunning()):
                    self.stop(thread_id, wait=False)
    
    def get_thread_info(self, thread_id: str) -> Optional[ThreadInfo]:
        """获取线程信息"""
        with self._lock:
            return self.threads.get(thread_id)
    
    def get_all_threads(self) -> List[ThreadInfo]:
        """获取所有线程信息"""
        with self._lock:
            return list(self.threads.values())
    
    def get_threads_by_state(self, state: str) -> List[ThreadInfo]:
        """按状态筛选线程"""
        with self._lock:
            return [info for info in self.threads.values() if info.state == state]
    
    def get_active_count(self) -> int:
        """获取活跃线程数量"""
        with self._lock:
            return self._active_count
    
    def get_total_count(self) -> int:
        """获取总线程数量"""
        with self._lock:
            return len(self.threads)
    
    def _on_thread_finished(self, thread_id: str, generation: Optional[int] = None) -> None:
        """线程完成回调。

        ``generation`` 是启动那一轮记录下来的 ``run_count``：QThread 可以重复
        start()，上一轮排队的 finished 信号可能在下一轮才被投递，届时必须丢弃，
        否则会把正在运行的新一轮错误地标成 COMPLETED。
        """
        with self._lock:
            if thread_id in self.threads:
                thread_info = self.threads[thread_id]
                if generation is not None and generation != thread_info.run_count:
                    return
                old_state = thread_info.state
                
                if thread_info.state == ThreadState.RUNNING:
                    thread_info.state = ThreadState.COMPLETED
                    thread_info.end_time = datetime.now()
                    self._active_count -= 1
                    
                    self.thread_state_changed.emit(thread_id, old_state, ThreadState.COMPLETED)
                    self.thread_stopped.emit(thread_id, thread_info.name)
    
    def _on_thread_error(self, thread_id: str, error: str) -> None:
        """线程错误回调"""
        with self._lock:
            if thread_id in self.threads:
                thread_info = self.threads[thread_id]
                old_state = thread_info.state
                
                thread_info.state = ThreadState.ERROR
                thread_info.error = error
                thread_info.end_time = datetime.now()
                # 只有先前确实被计入活跃数时才减，避免计数被减成负数
                if old_state in (ThreadState.RUNNING, ThreadState.PAUSED):
                    self._active_count -= 1
                
                self.thread_state_changed.emit(thread_id, old_state, ThreadState.ERROR)
                self.thread_error.emit(thread_id, thread_info.name, error)