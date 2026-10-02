import sys
from PySide6.QtWidgets import QApplication, QWidget, QHBoxLayout, QMessageBox
from PySide6.QtGui import Qt, QColor, QPainter, QBrush
from PySide6.QtCore import (
    QAbstractAnimation, QEasingCurve, QPoint, QPropertyAnimation, QSize, QTime, QTimer,
    QVariantAnimation, Property,
)
import platform

from . import Element, Notify

DEFAULT_CONFIG = {
    "edge_height": 4,
    "horizontal_edge_margin": 4,
    "drag_threshold": 8,
    "windowpos": "R",
    # 通知提示相关默认值（含义见 features/XHT/Lib/Notify.py）
    "notify_enabled": True,
    "notify_mode": Notify.DEFAULT_MODE,
    "notify_duration": Notify.DEFAULT_DURATION,
    # Toast 内容元素：按钮 / 图片 / 点击通知本体时是否激活应用
    "notify_actions": True,
    "notify_images": True,
    "notify_click_activates": False,
}

#: 未捕获异常是否直接结束整个程序。
#:
#: 默认 False：dock 是桌面外壳的一部分，某个信号回调里的一次异常不应该把整条
#: 程序栏一起带走（而且这条小黑条只是它的一个附属窗口）。异常仍然会写日志并弹框，
#: 不会静默。需要恢复「出错就退出」的老行为时，把这个常量改成 True 即可。
QUIT_ON_UNHANDLED_EXCEPTION = False

#: 记录是否已经装过本模块的异常钩子。全局异常钩子只能装一次 —— 原实现放在
#: ``Window.__init__`` 里，每创建一个窗口就覆盖一次 sys.excepthook，会把别处
#: 安装的处理器（以及本模块上一次装的）静默顶掉。
_EXCEPTHOOK_INSTALLED = False
_EXCEPTHOOK_LOGGER = None


def _install_excepthook(logger) -> None:
    """安装全局未捕获异常处理器（只装一次），并链式调用原有处理器。"""
    global _EXCEPTHOOK_INSTALLED, _EXCEPTHOOK_LOGGER
    _EXCEPTHOOK_LOGGER = logger
    if _EXCEPTHOOK_INSTALLED:
        return

    previous_hook = sys.excepthook

    def _hook(exc_type, exc_value, traceback):
        try:
            message = f"{exc_type.__name__}: {exc_value}"
            target = _EXCEPTHOOK_LOGGER
            if target is not None:
                target.critical(f"未捕获异常: {message}", exc_info=True)
        except Exception:
            pass

        # 链到原有处理器，避免把别的组件装的钩子静默吞掉
        try:
            if previous_hook not in (sys.__excepthook__, _hook):
                previous_hook(exc_type, exc_value, traceback)
        except Exception:
            pass

        try:
            QTimer.singleShot(0, lambda: _show_error_window(exc_value))
        except Exception:
            pass

    sys.excepthook = _hook
    _EXCEPTHOOK_INSTALLED = True


def _show_error_window(message) -> None:
    """弹出错误框；是否退出程序由 QUIT_ON_UNHANDLED_EXCEPTION 决定。"""
    try:
        QMessageBox.critical(None, "严重错误", str(message))
    except Exception:
        pass
    if QUIT_ON_UNHANDLED_EXCEPTION:
        app = QApplication.instance()
        if app is not None:
            app.quit()


class Window(QWidget):
    def __init__(self, config: dict = DEFAULT_CONFIG, elements : Element.ElementList =[], logger = None,
                 thread_manager = None):
        super().__init__()
        #先决条件
        self.config = config
        self.logger = logger
        # 统一线程管理器（可选）：传入时通知监听线程会登记进去，随它一起收尾
        self.thread_manager = thread_manager
        _install_excepthook(logger)
        self.background_color = QColor(0, 0, 0)
        self.setStyleSheet("""
                          QMenu {
                          background-color: black;
                          color: white;
                          border: 1px solid #cccccc;
                          border-radius: 8px;
                          }
                          QMenu::item {
                          padding: 6px 26px;
                          font-size: 12px;
                          }
                          QMenu::item:selected {
                          color: black;
                          font-size: 12px;
                          background-color: #e0e0e0;
                          }""")
        self.config = config

        #布局
        self.global_layout = QHBoxLayout()
        self.global_layout.setGeometry

        #动画
        # 尺寸动画。用 QVariantAnimation 而不是 QPropertyAnimation(self, b"size")：
        # 动画只驱动高度，宽度由 AutoSetSize 立即定下来（内容是换行富文本，
        # 宽度参与动画会让每帧「需要的高度」都不同 —— 见 AutoSetSize 的说明）。
        self.size_animation = QVariantAnimation(self)  # 初始化尺寸动画
        self.size_animation.setDuration(180)
        self.size_animation.valueChanged.connect(self._apply_animated_size)
        #: 本次尺寸动画的目标尺寸（宽度固定用它，见 _apply_animated_size）
        self._size_target = None
        # 尺寸动画结束回调只连接一次，避免 AutoSetSize 反复调用导致回调累积
        self.size_animation.finished.connect(self._on_size_animation_finished)
        self.show_animation = QPropertyAnimation(self, b"pos")   # 初始化显示动画
        self.hide_animation = QPropertyAnimation(self, b"pos")   # 初始化隐藏动画
        # 显示/隐藏动画回调只连接一次，避免反复调用导致回调累积
        self.show_animation.finished.connect(self._on_show_animation_finished)
        self.hide_animation.finished.connect(self._on_hide_animation_finished)
        self.is_hiding = False  # 动画状态

        # 新增初始化位置动画
        self.position_animation = QPropertyAnimation(self, b"pos")  # 初始化位置动画
        
        # 身位相关
        self.edge_height = self.config.get("edge_height")  # 边缘
        self.horizontal_edge_margin = self.config.get("horizontal_edge_margin")  # 水平方向边距
        self.is_hidden = False  # 是否隐藏
        self.windowpos = self.config.get("windowpos")  # 窗口位置
        self.drag_threshold = self.config.get("drag_threshold")  # 拖动触发阈值
        self.window_start_pos = None

        # 加载样式表
        self.setStyleSheet("""
                           QLabel {
                           color: white; 
                           font-size: 18px; 
                           font-weight: bold;
                           }""")
        # 延迟设置元素列表，确保窗口和布局已正确初始化
        self.elements_to_set = elements

        # ---- 窗口初始化（原 initUI 逻辑，确保 __init__ 阶段即完成） ----
        # 设置最小/最大尺寸
        self.setMinimumSize(120, 16)
        self.setMaximumSize(800, 600)

        # 设置窗口标题和标志
        # Tool + WindowStaysOnTopHint：置顶工具窗口，不出现在任务栏，且可正常接收鼠标事件
        # （原 Qt.ToolTip 在部分平台不接收鼠标事件，会导致拖拽/双击失效）
        # WindowDoesNotAcceptFocus：点击时不抢占键盘焦点
        self.setWindowTitle("MikaDock(XHT)")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                            | Qt.Tool | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.installEventFilter(self)

        # 设置布局
        self.setLayout(self.global_layout)

        # 设置元素列表（触发 AutoSetSize 计算正确尺寸）
        self.setElementList(self.elements_to_set)
        self.elements_to_set = None

        # 确保窗口尺寸与内容匹配
        self.AutoSetSize()

        # 通知提示（图标/内容部件 + 后台监听线程）
        self._init_notifications()

    def Quit(self):
        sys.exit(0)

    def ToggleWindow(self):
        if self.is_hidden:
            self.ShowWindow()
        else:
            self.HideWindow()

    def showEvent(self, event):
        super().showEvent(event)
        # 从屏幕外滑入到目标位置。
        # 注意：这里必须以 _calc_target_pos() 的结果为终点，不能使用 self.pos()
        # （Qt 给顶层窗口的默认位置），否则 windowpos 计算出的目标位置永远不会生效。
        target_pos = self._calc_target_pos()
        screen = QApplication.primaryScreen().availableGeometry()

        if self.windowpos == "L":
            initial_pos = QPoint(screen.x() - self.width(), target_pos.y())
        elif self.windowpos == "R":
            initial_pos = QPoint(screen.x() + screen.width(), target_pos.y())
        else:
            initial_pos = QPoint(target_pos.x(), screen.y() - self.height())

        self.position_animation.setDuration(250)
        self.position_animation.setStartValue(initial_pos)
        self.position_animation.setEndValue(target_pos)
        self.position_animation.setEasingCurve(QEasingCurve.OutQuad)
        self.position_animation.start()
        self.raise_()

    def getBackgroundColor(self):
        return self.background_color

    def setBackgroundColor(self, color):
        self.background_color = color
        self.update()

    backgroundColor = Property(QColor, getBackgroundColor, setBackgroundColor)

    def addTimer(self, interval:int, func:callable):
        """添加/替换本窗口的定时器。

        注意：窗口只持有 **一个** 定时器引用，重复调用会用新定时器覆盖旧引用。
        需要多个定时器时请自行保存引用，或改用独立的 QTimer。
        """
        # 先停掉旧的，避免覆盖引用后旧定时器仍在触发（既费 CPU 又难排查）
        old_timer = getattr(self, "timer", None)
        if old_timer is not None:
            try:
                old_timer.stop()
                old_timer.deleteLater()
            except Exception:
                pass
        self.timer = QTimer(self)
        self.timer.timeout.connect(func)
        self.timer.start(interval)

        

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QBrush(self.background_color))
        painter.setPen(Qt.PenStyle.NoPen)
        if platform.system() == "Windows":
            painter.drawRoundedRect(self.rect(), 24, 24)
        if platform.system() == "Linux":
            painter.drawRoundedRect(self.rect(), 24, 24)
        if platform.system() == "Darwin":
            painter.drawRoundedRect(self.rect(), 32, 32)

    def update_time(self):
        self.current_time = QTime.currentTime().toString("hh:mm")    
        self.time_label.setText(self.current_time)
        #self.a=self.a*10+1
        #self.time_label.setText(str(self.a))
        self.AutoSetSize()

    def _calc_target_pos(self) -> QPoint:
        """计算当前 windowpos 对应的目标位置（含工作区偏移，垂直居中）"""
        screen = QApplication.primaryScreen().availableGeometry()
        if self.windowpos == "L":
            target_x = screen.x() + self.horizontal_edge_margin
        elif self.windowpos == "R":
            target_x = screen.x() + screen.width() - self.width() - self.horizontal_edge_margin
        else:
            target_x = screen.x() + (screen.width() - self.width()) // 2

        # 顶部对齐工作区（保留 edge_height 边距）
        target_y = screen.y() + self.edge_height
        return QPoint(target_x, target_y)

    def update_position(self):
        """将窗口移动到当前 windowpos 对应的位置"""
        # 隐藏中/已隐藏时不重新定位，避免把窗口拉回可见位置或打断隐藏动画
        if self.is_hidden or self.is_hiding:
            return

        target_pos = self._calc_target_pos()

        if self.position_animation.state() == QPropertyAnimation.Running:
            self.position_animation.stop()

        self.position_animation.setDuration(250)
        self.position_animation.setStartValue(self.pos())
        self.position_animation.setEndValue(target_pos)
        self.position_animation.setEasingCurve(QEasingCurve.OutQuad)
        self.position_animation.start()

    def _content_size(self) -> QSize:
        """按内容算目标尺寸（保证够显示时间文本，并且不超过最大尺寸）。"""
        size = self.global_layout.sizeHint().expandedTo(self.minimumSize())
        size = size.boundedTo(self.maximumSize())
        # 确保宽度至少能显示"hh:mm"格式的时间
        size.setWidth(max(size.width(), 80))
        # 高度也要满足「这个宽度下换行后真正需要的高度」：达不到的请求会被布局顶回去
        size.setHeight(self._height_for(size.width(), size.height()))
        return size

    def _height_for(self, width: int, height: int) -> int:
        """「这个宽度下真正需要多高」与给定高度的较大者。

        通知内容是会自动换行的富文本：宽度越窄，需要的高度越高。布局会按这个高度把
        窗口顶回来，所以**请求小于它的高度是做不到的** —— Windows 那边看到的实际几何
        和请求不一致，就会每帧打一条
        ``QWindowsWindow::setGeometry: Unable to set geometry …``。这里先把高度补齐，
        请求就一定是能落地的。
        """
        needed = 0
        if self.global_layout.hasHeightForWidth():
            needed = self.global_layout.heightForWidth(max(int(width), self.minimumWidth()))
        return max(int(height), int(needed), self.minimumHeight())

    def _apply_animated_size(self, value) -> None:
        """尺寸动画的每帧落地。动画动的是**高度**，宽度固定在这个动画的目标宽度上。"""
        target = getattr(self, "_size_target", None)
        if target is None:
            return
        try:
            height = int(value)
        except (TypeError, ValueError):
            return
        self.resize(target.width(), self._height_for(target.width(), height))

    def AutoSetSize(self):
        """按内容调整窗口尺寸。

        宽度**立即到位、不参与动画**：宽度一变，「该宽度下需要的高度」就变，动画中间帧
        要么被布局顶回去（每帧一条 setGeometry 警告），要么把窗口先撑到一两百像素再缩
        回来（实测 98 → 213 → 98 的抖动）。宽度先定下来，高度动画才在固定的换行条件下
        进行；而高度方向：变小会平滑收起，变大的话布局本来就已经要求那么高，直接到位
        （不会闪、也不会报警告）。
        """
        self.global_layout.activate()
        self.updateGeometry()

        content_size = self._content_size()
        if self.size() == content_size:
            return

        if self.size_animation.state() == QAbstractAnimation.State.Running:
            self.size_animation.stop()

        start_height = self._height_for(content_size.width(), self.size().height())
        self.resize(content_size.width(), start_height)
        self._size_target = content_size

        if start_height == content_size.height() or self.size_animation.duration() <= 0:
            self.resize(content_size)
            self.update_position()
            return

        self.size_animation.setStartValue(start_height)
        self.size_animation.setEndValue(content_size.height())
        self.size_animation.start()

    def _on_size_animation_finished(self):
        """尺寸动画结束：兜底校正尺寸并重新定位（在 __init__ 中仅连接一次）"""
        target = getattr(self, "_size_target", None)
        if target is not None:
            self.resize(target)
        self.update_position()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.drag_start_pos = event.globalPos()
            self.window_start_pos = self.pos()
            self.is_dragging = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton:
            if self.drag_start_pos is not None:
                delta = event.globalPos() - self.drag_start_pos
                if not self.is_dragging and (abs(delta.x()) > self.drag_threshold or abs(delta.y()) > self.drag_threshold):
                    self.is_dragging = True
                if self.is_dragging:
                    new_x = self.window_start_pos.x() + delta.x()
                    self.move(new_x, self.window_start_pos.y()) 
        super().mouseMoveEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.logger.info("事件：左键双击")
            self.ToggleWindow()
        super().mouseDoubleClickEvent(event)
    


    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.is_dragging:
            screen = QApplication.primaryScreen().availableGeometry()
            window_center = self.pos().x() + self.width() / 2

            # 根据窗口中心相对于工作区宽度的比例划分区域（阈值需带上工作区 x 偏移）
            if window_center < screen.x() + screen.width() * 0.30:
                self.windowpos = "L"  
            elif window_center > screen.x() + screen.width() * 0.70:
                self.windowpos = "R"  
            else:
                self.windowpos = "M"  

            self.update_position()
        self.drag_start_pos = None
        self.is_dragging = False
        super().mouseReleaseEvent(event)

    @property
    def is_popping_hidden(self) -> bool:
        """是不是「已经收起来」或者「正在收起」。

        新通知要重新弹出小黑条时用它判断：只看 ``is_hidden`` 会漏掉「隐藏动画正在
        播放」的那 250ms —— 那时候 ``is_hidden`` 还是 False，但窗口马上就会滑走。
        """
        if self.is_hidden:
            return True
        return self.hide_animation.state() == QPropertyAnimation.State.Running

    def ShowWindow(self):
        self.logger.info("事件：显示")

        if self.is_hiding:
            # 隐藏动画正在播放：取消它，直接回到可见位置。
            # 不这么做的话，隐藏动画会把刚来的通知一起带走（窗口继续滑出去，
            # is_hidden 变 True，用户什么都看不到）。
            if self.hide_animation.state() == QPropertyAnimation.State.Running:
                self.hide_animation.stop()
                self.is_hiding = False
                self.is_hidden = False
                self.move(self._calc_target_pos())
                self.raise_()
            return

        if not self.is_hidden:
            return

        self.is_hiding = True
        screen = QApplication.primaryScreen().availableGeometry()
        target_pos = self._calc_target_pos()

        if self.windowpos == "L":
            # 从左侧隐藏位置开始，移动到正常位置
            initial_pos = QPoint(screen.x() - self.width() + self.edge_height, target_pos.y())
        elif self.windowpos == "R":
            # 从右侧隐藏位置开始，移动到正常位置
            initial_pos = QPoint(screen.x() + screen.width() - self.edge_height, target_pos.y())
        else:
            # 垂直方向处理保持不变
            initial_pos = QPoint(target_pos.x(), screen.y() - self.height() + self.edge_height)

        self.show_animation.setDuration(250)
        self.show_animation.setStartValue(initial_pos)
        self.show_animation.setEndValue(target_pos)
        self.show_animation.setEasingCurve(QEasingCurve.OutQuad)
        self.show_animation.start()

    def _on_show_animation_finished(self):
        self.is_hiding = False
        self.is_hidden = False

    def HideWindow(self):
        self.logger.info("事件：隐藏")
        if self.is_hiding:
            return
        
        self.is_hiding = True
        current_pos = self.pos()
        screen = QApplication.primaryScreen().availableGeometry()
        
        if self.windowpos in ["L", "R"]:
            if self.windowpos == "L":
                # 保留edge_height宽度可见
                target_x = screen.x() - (self.width() - self.edge_height)
            else:
                # 保留edge_height宽度可见
                target_x = screen.x() + screen.width() - self.edge_height
                
            target_pos = QPoint(target_x, current_pos.y())
        else:
            # 垂直方向保持原逻辑
            target_y = current_pos.y() - (self.height() - self.edge_height)
            target_pos = QPoint(current_pos.x(), target_y)
        
        self.hide_animation.setDuration(250)
        self.hide_animation.setStartValue(current_pos)
        self.hide_animation.setEndValue(target_pos)
        self.hide_animation.setEasingCurve(QEasingCurve.OutQuad)
        self.hide_animation.start()

    def _on_hide_animation_finished(self):
        self.is_hiding = False
        self.is_hidden = True

    def closeEvent(self, event):
        event.ignore()  # 忽略关闭事件

    def RefreshConfig(self):
        """刷新窗口（从 self.config 读取最新配置）"""
        self.edge_height = self.config.get("edge_height", self.edge_height)
        self.horizontal_edge_margin = self.config.get("horizontal_edge_margin", self.horizontal_edge_margin)
        self.windowpos = self.config.get("windowpos", self.windowpos)
        self.drag_threshold = self.config.get("drag_threshold", self.drag_threshold)

        # 通知显示方式 / 显示时间 / 开关
        presenter = getattr(self, "notify_presenter", None)
        if presenter is not None:
            presenter.apply_config(self.config or {})

        self.update_position()
        self.AutoSetSize()

    # ------------------------------------------------------------------ #
    # 通知提示
    # ------------------------------------------------------------------ #
    def _init_notifications(self):
        """创建通知部件并启动后台监听线程。"""
        self.notify_badge = Notify.NotificationBadge(self)
        self.time_visible = True        # 展开通知内容时会临时置 False
        self.notify_presenter = Notify.NotificationPresenter(
            self, self.notify_badge, dict(self.config or {}), self.logger,
            thread_manager=getattr(self, "thread_manager", None),
        )
        self._ensure_notify_widget()

        # 构造 presenter 时不起线程（便于单测），这里显式启动
        if self.notify_presenter.enabled:
            self.notify_presenter.start()

        app = QApplication.instance()
        if app is not None:
            # 退出时把后台线程停掉，避免进程挂在监听循环上
            app.aboutToQuit.connect(self._shutdown_notifications)

    def _ensure_notify_widget(self):
        """把通知部件放回布局（始终排在用户元素之后）。"""
        badge = getattr(self, "notify_badge", None)
        if badge is None:
            return
        if self.global_layout.indexOf(badge) < 0:
            self.global_layout.addWidget(badge)

    def set_time_visible(self, visible: bool):
        """显示通知内容时把时间藏起来，避免和通知抢地方。

        只显示 🔔 图标时时间照常显示；展开内容时才隐藏 —— 由
        :class:`~features.XHT.Lib.Notify.NotificationPresenter` 决定。
        """
        visible = bool(visible)
        if getattr(self, "time_visible", None) == visible:
            return
        self.time_visible = visible
        label = getattr(self, "time_label", None)
        if label is None:
            return
        label.setVisible(visible)
        self.AutoSetSize()

    def _shutdown_notifications(self):
        presenter = getattr(self, "notify_presenter", None)
        if presenter is not None:
            presenter.stop()

    def setElementList(self, elements: Element.ElementList = None):
        # 清除现有布局中的所有元素（通知部件不属于用户元素列表，保留不删）
        keep = getattr(self, "notify_badge", None)
        while self.global_layout.count():
            item = self.global_layout.takeAt(0)
            widget = item.widget()
            if widget and widget is not keep:
                widget.deleteLater()
        
        # 修改：正确处理传入的elements参数
        if elements is None or len(elements) == 0:
            self.default_element()
            elements = Element.ElementList()
            elements.addElement(self.time_label)
        # 修改：直接使用elements，因为ElementList本身就是list
        
        # 添加：确保elements是可迭代的
        for element in elements:
            self.global_layout.addWidget(element)
            element.show()
        self._ensure_notify_widget()
        self.AutoSetSize()


    def default_element(self):
        self.time_label = Element.LabelElement(self)
        self.time_label.setText(QTime.currentTime().toString("hh:mm"))
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.addTimer(1000, self.update_time)