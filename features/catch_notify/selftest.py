"""本库的自检 / 回归测试。

::

    python -m features.catch_notify.selftest
    python -m features.catch_notify.selftest --workdir D:\\tmp

做两件事：

1. 纯函数检查：实体解码、``<text>`` 提取、payload 编码识别、FILETIME 转换、
   :meth:`Notification.to_dict` / :meth:`Notification.from_dict` 往返。
2. 端到端：把真实通知库复制到工作目录，往**副本**里插一条带完整 XML 的通知，
   再用 :meth:`NotificationDatabase.watch` 确认能抓到，并校验 ``xml_bytes`` 与
   插入内容逐字节一致 —— 全程不碰系统真实通知库。

没有真实通知库的机器上（从未收到过通知）第 2 部分会标记 SKIP，不算失败。
"""

from __future__ import annotations

import argparse
import datetime as dt
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

from .database import NotificationDatabase, default_database_path
from .errors import CatchNotifyError
from .records import (
    Notification,
    decode_payload,
    extract_texts,
    filetime_to_datetime,
    unescape_xml,
)
from .toast import ToastAction

FAKE_APP_ID = "CatchNotify.SelfTest"
FAKE_APP_NAME = "catch_notify 自检"
FAKE_TITLE = "自检标题"
FAKE_XML = (
    '<toast activationType="foreground" launch="action=open&amp;id=42">'
    '<visual><binding template="ToastGeneric">'
    f"<text>{FAKE_TITLE}</text>"
    '<text hint-style="base">&#34;转义&#34; &amp; 实体</text>'
    "</binding></visual>"
    '<actions><action content="打开" arguments="open"/></actions>'
    "</toast>"
)

_RESULTS: list = []


def check(name: str, passed, detail: str = "") -> None:
    _RESULTS.append((name, passed, detail))
    mark = "SKIP" if passed is None else ("PASS" if passed else "FAIL")
    print(f"  [{mark}] {name}" + (f"  {detail}" if detail else ""))


# --------------------------------------------------------------------------- #
# 1) 纯函数
# --------------------------------------------------------------------------- #
def check_pure_functions() -> None:
    print("=== 纯函数 ===")

    check("unescape_xml 命名实体", unescape_xml("a &amp; b &lt;c&gt; &quot;d&quot;") == 'a & b <c> "d"')
    check("unescape_xml 十进制引用", unescape_xml("&#34;引号&#34;") == '"引号"')
    check("unescape_xml 十六进制引用", unescape_xml("&#x27;x&#x27;") == "'x'")
    check("unescape_xml 未知实体原样保留", unescape_xml("&nbsp;") == "&nbsp;")

    texts = extract_texts(FAKE_XML)
    check("extract_texts 取到两条", len(texts) == 2, f"texts={texts}")
    check("extract_texts 解实体", texts[1] == '"转义" & 实体' if len(texts) > 1 else False,
          texts[1] if len(texts) > 1 else "")
    check("extract_texts 忽略属性", extract_texts('<text hint-style="base">A</text>') == ("A",))

    raw = "中文通知".encode("utf-8")
    check("decode_payload utf-8", decode_payload(raw) == "中文通知")
    check("decode_payload utf-16 BOM", decode_payload("中文".encode("utf-16")) == "中文")
    check("decode_payload gbk 回退", decode_payload("中文".encode("gbk")) == "中文")
    check("decode_payload 空值", decode_payload(None) == "")

    # FILETIME: 2026-10-02T18:30:43+08:00 附近
    moment = filetime_to_datetime(134354106430000000)
    check("filetime_to_datetime", isinstance(moment, dt.datetime), str(moment))
    check("filetime_to_datetime 空值", filetime_to_datetime(0) is None)

    item = Notification(id=7, app_id="A", xml=FAKE_XML, texts=extract_texts(FAKE_XML),
                        xml_bytes=FAKE_XML.encode("utf-8"))
    restored = Notification.from_dict(item.to_dict(with_xml_bytes=True))
    check("to_dict/from_dict 往返", restored.id == 7 and restored.texts == item.texts
          and restored.xml_bytes == item.xml_bytes)
    check("title/body 便捷属性", item.title == FAKE_TITLE and item.body == '"转义" & 实体')
    check("display_app 回退到 app_id", item.display_app == "A")

    check_content_elements(item)


def check_content_elements(item: Notification) -> None:
    """Toast 内容元素解析与按钮激活规划（纯函数，不碰系统）。"""
    print("\n=== 内容元素 ===")
    from .activation import (
        STRATEGY_ACTIVATE_APP, STRATEGY_COM, STRATEGY_DISMISS, STRATEGY_PROTOCOL,
        STRATEGY_UNSUPPORTED, aumid_candidates, build_plan, hresult_message,
    )
    from .toast import local_image_path, parse_toast

    content = parse_toast(FAKE_XML)
    check("解析出模板", content.template == "ToastGeneric", content.template)
    check("标题 / 正文按角色取", content.title == FAKE_TITLE
          and content.body_lines == ('"转义" & 实体',), content.body_lines)
    check("文本角色序列", item.text_roles == ("title", "body"), item.text_roles)
    check("按钮解析出 arguments",
          len(content.actions) == 1 and content.actions[0].label == "打开"
          and content.actions[0].arguments == "open", content.actions)
    check("content 结果被缓存", item.content is item.content)
    check("to_dict 带内容元素", item.to_dict()["content"]["template"] == "ToastGeneric")

    check("空内容不抛异常", parse_toast(None).title == ""
          and parse_toast("<toast><visual/></toast>").body == "")
    check("旧模板按 id 取标题", parse_toast(
        '<toast><visual><binding template="ToastText02"><text id="2">正文</text>'
        '<text id="1">标题</text></binding></visual></toast>').title == "标题")
    check("归属文本不算正文", parse_toast(
        '<text>甲</text><text placement="attribution">来自 X</text>').attribution == "来自 X")
    check("AUMID 写法变体", len(aumid_candidates("A\\b.exe")) == 4, aumid_candidates("A\\b.exe"))

    class _Lookup:
        def clsid_for(self, aumid):
            return "{11111111-2222-3333-4444-555555555555}"

        def server_command(self, clsid):
            return '"C:\\x\\x.exe" -ToastActivated'

    plan = build_plan(item, action=content.actions[0], lookup=_Lookup())
    check("有激活器时规划为 COM 调用", plan.strategy == STRATEGY_COM
          and plan.request.arguments == "open", plan.to_dict())

    class _NoActivator:
        def clsid_for(self, aumid):
            return ""

        def server_command(self, clsid):
            return ""

    check("没有激活器的前台按钮 → 尽力按 AUMID 拉起应用",
          build_plan(item, action=content.actions[0],
                     lookup=_NoActivator()).strategy == STRATEGY_ACTIVATE_APP)
    check("没有激活器的通知本体 → 按 AUMID 拉起应用",
          build_plan(item, lookup=_NoActivator()).strategy == STRATEGY_ACTIVATE_APP)
    check("没有激活器的后台动作 → 明确不支持",
          build_plan(item, action=ToastAction(content="暂停", arguments="action=pause",
                                              activation_type="background"),
                     lookup=_NoActivator()).strategy == STRATEGY_UNSUPPORTED)
    check("系统动作规划为关闭",
          build_plan(item, action=ToastAction(arguments="dismiss"),
                     lookup=_Lookup()).strategy == STRATEGY_DISMISS)
    check("协议动作规划为 ShellExecute",
          build_plan(item, action=ToastAction(content="打开网页",
                                              arguments="https://example.com",
                                              activation_type="protocol"),
                     lookup=_Lookup()).strategy == STRATEGY_PROTOCOL)
    check("HRESULT 人话", hresult_message(0).startswith("成功"))
    check("图片路径解析", local_image_path("file:///C:/a/b.png").endswith("b.png")
          and local_image_path("https://x/y.png") is None)


# --------------------------------------------------------------------------- #
# 2) 端到端
# --------------------------------------------------------------------------- #
def _insert_fake_notification(path: Path) -> None:
    connection = sqlite3.connect(str(path))
    try:
        cursor = connection.cursor()
        cursor.execute(
            "INSERT INTO NotificationHandler (PrimaryId, HandlerType, CreatedTime, ModifiedTime) "
            "VALUES (?, 'app:desktop', datetime('now'), datetime('now'))",
            (FAKE_APP_ID,),
        )
        handler_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO HandlerAssets (HandlerId, AssetKey, AssetValue) "
            "VALUES (?, 'DisplayName', ?)",
            (handler_id, FAKE_APP_NAME),
        )
        order = cursor.execute(
            "SELECT COALESCE(MAX([Order]), 0) + 1 FROM Notification"
        ).fetchone()[0]
        identifier = cursor.execute(
            "SELECT COALESCE(MAX(Id), 0) + 1 FROM Notification"
        ).fetchone()[0]
        cursor.execute(
            "INSERT INTO Notification ([Order], Id, HandlerId, Type, Payload, [Tag], [Group], "
            "ArrivalTime, ExpiryTime, PayloadType, BootId, ExpiresOnReboot) "
            "VALUES (?, ?, ?, 'toast', ?, 'selftest-tag', '', ?, 0, 'Xml', 0, 0)",
            (order, identifier, handler_id, FAKE_XML.encode("utf-8"),
             134354106430000000),
        )
        connection.commit()
    finally:
        connection.close()


def check_end_to_end(workdir) -> None:
    print("\n=== 端到端（在通知库副本上做） ===")

    if workdir is None:
        check("端到端监听", None, "跳过：没有可写的工作目录（受限沙箱环境）")
        return

    real = default_database_path()
    if not real.is_file():
        check("端到端监听", None, f"跳过：找不到 {real}")
        return

    copy = workdir / "selftest-wpndatabase.db"
    if copy.exists():
        copy.unlink()
    shutil.copy2(real, copy)
    for suffix in ("-wal", "-shm"):
        stale = Path(str(copy) + suffix)
        if stale.exists():
            stale.unlink()

    database = NotificationDatabase(copy)
    check("打开副本（只读直连或快照）", True, f"mode={'snapshot' if database.using_snapshot else 'direct'}")

    stop_event = threading.Event()
    collected: list = []
    failures: list = []

    def reader() -> None:
        try:
            for item in database.watch(skip_existing=True, interval=0.2, stop_event=stop_event):
                collected.append(item)
        except Exception as exc:  # noqa: BLE001 - 自检要报告任何异常
            failures.append(exc)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    time.sleep(0.8)
    _insert_fake_notification(copy)

    deadline = time.time() + 8.0
    while time.time() < deadline and not collected and not failures:
        time.sleep(0.1)
    stop_event.set()
    thread.join(timeout=5)
    reread = database.read(kinds="toast")
    kinds = database.kinds()
    database.close()

    if failures:
        check("监听线程无异常", False, repr(failures[0]))
        return
    check("监听线程无异常", True)
    check("抓到新通知", len(collected) == 1, f"collected={len(collected)}")
    if len(collected) != 1:
        return

    item = collected[0]
    check("应用名解析自 HandlerAssets", item.app_name == FAKE_APP_NAME, item.app_name)
    check("AUMID 解析自 NotificationHandler", item.app_id == FAKE_APP_ID, item.app_id)
    check("标题/正文提取正确", item.title == FAKE_TITLE, item.title)
    check("XML 完整（含 <actions> 与转义实体）",
          "<actions>" in item.xml and 'arguments="open"' in item.xml)
    check("xml_bytes 与写入内容逐字节一致", item.xml_bytes == FAKE_XML.encode("utf-8"))
    check("tag 读取正确", item.tag == "selftest-tag", item.tag)
    check("到达时间是 datetime", isinstance(item.arrived_at, dt.datetime), item.arrived_at_iso)

    check("read() 能再取到同一条", any(x.id == item.id for x in reread), f"共 {len(reread)} 条")
    check("kinds() 统计到 toast", kinds.get("toast", 0) >= 1, str(kinds))

    # 关闭后再读应当明确报错，而不是静默返回空 —— 这是调用方依赖的契约。
    try:
        database.read()
        closed_guard = False
    except CatchNotifyError:
        closed_guard = True
    check("关闭后读取会明确报错", closed_guard)

    # dump 出来的文件必须与数据库里的原始字节逐字节一致
    from .cli import dump_xml

    # 直接落到工作目录里（受限环境不允许新建子目录，反正结束就删）
    dump_dir = workdir
    before = {path.name for path in dump_dir.glob("*.xml")}
    dump_xml(item, dump_dir)
    dumped = sorted(path for path in dump_dir.glob("*.xml") if path.name not in before)
    check("dump_xml 落盘逐字节一致",
          len(dumped) == 1 and dumped[0].read_bytes() == FAKE_XML.encode("utf-8"),
          dumped[0].name if dumped else "没有产出文件")
    for path in dumped:
        path.unlink()

    for stale in (copy, Path(str(copy) + "-wal"), Path(str(copy) + "-shm")):
        if stale.exists():
            stale.unlink()


# --------------------------------------------------------------------------- #
def _writable(directory: Path) -> bool:
    """这个目录现在能不能写文件（只试写一个文件，不建子目录）。"""
    probe = directory / ".catch_notify_selftest_probe"
    try:
        probe.write_text("x", encoding="utf-8")
    except OSError:
        return False
    try:
        probe.unlink()
    except OSError:
        pass
    return True


def prepare_workdir(explicit: str | None) -> Path | None:
    """选一个真的能写的工作目录；一个都没有时返回 ``None``（跳过端到端）。

    受限环境里系统临时目录的**新建子目录**可能不可写，所以这里只往已存在的
    目录里写，并且逐个探测而不是假定。
    """
    candidates: list = []
    if explicit:
        candidates.append(Path(explicit))
    try:
        candidates.append(Path(tempfile.mkdtemp(prefix="catch-notify-selftest-")))
    except OSError:
        pass
    candidates.append(Path(tempfile.gettempdir()))
    candidates.append(Path(__file__).resolve().parent)
    candidates.append(Path.cwd())

    for candidate in candidates:
        try:
            if candidate.is_dir() and _writable(candidate):
                return candidate
        except OSError:
            continue
    return None


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(prog="python -m features.catch_notify.selftest")
    parser.add_argument("--workdir", metavar="DIR",
                        help="临时文件目录（默认系统临时目录）")
    args = parser.parse_args(argv)

    workdir = prepare_workdir(args.workdir)
    print(f"工作目录：{workdir if workdir else '（没有可写目录）'}\n")

    check_pure_functions()
    check_end_to_end(workdir)

    failed = [name for name, passed, _ in _RESULTS if passed is False]
    skipped = [name for name, passed, _ in _RESULTS if passed is None]
    print(f"\n=== 汇总：{len(_RESULTS) - len(failed) - len(skipped)} 通过 / "
          f"{len(failed)} 失败 / {len(skipped)} 跳过 ===")
    for name in failed:
        print(f"  FAIL: {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
