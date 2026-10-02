"""手动验证：把一条示例通知按内容元素渲染出来，导出成 PNG 看排版。

    python tests/manual_toast_render.py [输出文件]

用它检查「小黑条上到底长什么样」：标题 / 多行正文 / 归属 / 场景 / 进度 / 应用图标 /
横幅图 / 可点击按钮是否各就各位。默认写到 ``docs/toast_content_demo.png``。

只依赖 Qt（离屏渲染），不改动系统状态、不联网、不发通知。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget  # noqa: E402

from features.XHT.Lib.Notify import NotificationBadge  # noqa: E402
from features.catch_notify.records import Notification, extract_texts  # noqa: E402

SAMPLE_XML = """<toast launch="action=open&amp;conversationId=42" scenario="reminder"
       duration="long" activationType="foreground" displayTimestamp="2026-10-02T20:00:00Z">
  <visual>
    <binding template="ToastGeneric">
      <text>小明的消息</text>
      <text>晚上一起吃饭吗？</text>
      <text>老地方见，我大概七点到</text>
      <text placement="attribution">来自 微信</text>
      <image placement="hero" src="{hero}"/>
      <image placement="appLogoOverride" hint-crop="circle" src="{logo}"/>
      <progress title="同步中" status="进行中" value="0.42"/>
    </binding>
  </visual>
  <actions>
    <input id="reply" type="text" placeHolderContent="回复…" defaultInput="好"/>
    <action content="回复" arguments="action=reply" hint-inputId="reply" hint-buttonStyle="Success"/>
    <action content="打开" arguments="action=open" placement="contextMenu"/>
    <action content="忽略" arguments="dismiss" activationType="system"/>
  </actions>
  <audio silent="true"/>
  <header id="h" title="提醒" subtitle="今天 19:30" arguments="action=header"/>
</toast>"""


def make_image(path: Path, width: int, height: int, top: QColor, bottom: QColor) -> None:
    """造一张竖向渐变图，充当横幅图 / 应用图标。"""
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    for y in range(height):
        ratio = y / float(max(height - 1, 1))
        color = QColor(
            int(top.red() + (bottom.red() - top.red()) * ratio),
            int(top.green() + (bottom.green() - top.green()) * ratio),
            int(top.blue() + (bottom.blue() - top.blue()) * ratio),
        )
        for x in range(width):
            image.setPixelColor(x, y, color)
    image.save(str(path))


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    output = Path(argv[0]) if argv else ROOT / "docs" / "toast_content_demo.png"

    scratch = Path(tempfile.mkdtemp(prefix="xht-toast-render-"))
    hero = scratch / "hero.png"
    logo = scratch / "logo.png"
    make_image(hero, 400, 200, QColor("#39C5BB"), QColor("#2A9A91"))
    make_image(logo, 64, 64, QColor("#80E0D7"), QColor("#39C5BB"))

    xml = SAMPLE_XML.format(hero=str(hero).replace("\\", "\\\\"),
                            logo="file:///" + str(logo).replace("\\", "/"))
    item = Notification(id=1, app_id="WeChat", app_name="微信", xml=xml,
                        texts=extract_texts(xml))

    app = QApplication.instance() or QApplication(sys.argv)

    # 模仿 XHT 的黑底圆角条
    window = QWidget()
    window.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    window.setStyleSheet("background-color: black; border-radius: 24px;")
    layout = QVBoxLayout(window)
    layout.setContentsMargins(16, 10, 16, 10)

    badge = NotificationBadge(window)
    badge.show_content(item.content, unread=3, app_name="微信")
    layout.addWidget(badge)
    # 富文本会按宽度换行，尺寸由 NotificationBadge 的 sizeHint/heightForWidth 给出
    badge.setFixedWidth(420)
    layout.activate()
    window.adjustSize()
    layout.activate()
    app.processEvents()

    image = window.grab().toImage()
    output.parent.mkdir(parents=True, exist_ok=True)
    if not image.save(str(output)):
        print("❌ 渲染结果写不进去：%s" % output)
        return 1

    print("✅ 渲染到 %s（%dx%d）" % (output, image.width(), image.height()))
    print()
    print("内容元素解析结果：")
    print("  模板 / 场景 : %s / %s" % (item.content.template, item.content.scenario_label))
    print("  标题        : %s" % item.content.title)
    print("  正文        : %s" % list(item.content.body_lines))
    print("  归属        : %s" % item.content.attribution)
    print("  按钮        : %s" % [action.label for action in item.content.button_actions])
    print("  输入框      : %s" % [entry.id for entry in item.content.inputs])
    shutil.rmtree(scratch, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
