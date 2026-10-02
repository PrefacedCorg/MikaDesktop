"""数据源一：Windows 通知库（``wpndatabase.db``）。

这是本库的**默认数据源**，也是唯一不需要任何额外条件的一个：

* 不需要 Windows SDK、不需要编译器、不需要管理员权限；
* 不需要「访问通知」授权（对比 WinRT 数据源）；
* 只依赖 Windows 自带的通知平台 + Python 标准库 ``sqlite3``。

其中 ``Notification.Payload`` 就是应用当初提交的**原始 XML 字节**（含
``launch`` / ``arguments`` / ``actions`` 等全部内容），所以这里的 XML 是逐字节
原件，而不是重建出来的近似品。
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import tempfile
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Iterator

from .errors import CatchNotifyError, DatabaseUnavailable, SchemaError
from .records import (
    PROVENANCE_DATABASE,
    Notification,
    decode_payload,
    extract_texts,
    filetime_to_datetime,
)

logger = logging.getLogger(__name__)

__all__ = ["NotificationDatabase", "default_database_path", "build_query"]

#: Notification 表里缺了就没法工作的列
REQUIRED_COLUMNS = ("Order", "Id", "Payload")

ENV_SNAPSHOT_DIR = "NOTIFICATION_DB_SNAPSHOT_DIR"

#: 已上报通知的身份表上限。通知中心自己有条数上限（toast 默认 20、condensed 80），
#: 被挤掉的旧条目不可能再回到表里，所以只要记住最近这么多条就够了。
SEEN_LIMIT = 256


def notification_identity(item: Notification) -> tuple:
    """判断「是不是同一条通知」的键。

    **不能只用 ``[Order]``**：它是 ``INTEGER PRIMARY KEY``（SQLite 的 rowid），平台插入
    新行时拿的是「当前表里最大 Order + 1」。较新的行一旦被删（用户点掉、清空通知中心、
    到期、通知中心上限挤掉），新行就会拿到比历史更小的 Order —— 只用 Order 当游标会
    整段漏报。加上 ``Id`` 与到达时间，既能识别同一条，又能在原行被更新时重新上报。
    """
    return (int(item.order or 0), int(item.id or 0), item.arrived_at_iso or "")



def default_database_path() -> Path:
    """当前用户的通知库路径（``%LOCALAPPDATA%`` 是每个用户独立的）。"""
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    return base / "Microsoft" / "Windows" / "Notifications" / "wpndatabase.db"


def build_query(connection: sqlite3.Connection) -> str:
    """按实际存在的列拼查询语句。

    必需列缺失 → :class:`SchemaError`；可选列缺失 → 补 ``NULL AS [列名]``，
    这样即便将来 Windows 改了库结构，最坏情况也只是少几个字段而不是直接崩。
    """

    def columns(table: str) -> set:
        try:
            return {row[1] for row in connection.execute("PRAGMA table_info(%s)" % table)}
        except sqlite3.Error:
            return set()

    notification = columns("Notification")
    missing = [name for name in REQUIRED_COLUMNS if name not in notification]
    if missing:
        raise SchemaError("通知库的结构与预期不符：Notification 表缺少列 " + ", ".join(missing))

    handler = columns("NotificationHandler")
    assets = columns("HandlerAssets")
    join_handler = "HandlerId" in notification and {"RecordId", "PrimaryId"} <= handler
    has_app_name = join_handler and {"HandlerId", "AssetKey", "AssetValue"} <= assets

    def optional(column: str) -> str:
        return "n.[%s]" % column if column in notification else "NULL AS [%s]" % column

    fields = [
        "n.[Order]",
        "n.[Id]",
        optional("Type"),
        "n.[Payload]",
        optional("PayloadType"),
        optional("Tag"),
        optional("Group"),
        optional("ArrivalTime"),
        optional("ExpiryTime"),
        optional("BootId"),
        "h.[PrimaryId]" if join_handler else "NULL AS [PrimaryId]",
    ]
    if has_app_name:
        fields.append(
            "(SELECT a.[AssetValue] FROM HandlerAssets a "
            "WHERE a.[HandlerId] = h.[RecordId] AND a.[AssetKey] = 'DisplayName') AS DisplayName"
        )
    else:
        fields.append("NULL AS DisplayName")

    query = "SELECT " + ", ".join(fields) + " FROM Notification n"
    if join_handler:
        query += " LEFT JOIN NotificationHandler h ON h.[RecordId] = n.[HandlerId]"
    return query + " WHERE n.[Order] > ? ORDER BY n.[Order] ASC"


def _normalize_kinds(kinds: Any) -> set | None:
    if kinds is None:
        return None
    if isinstance(kinds, str):
        return {kinds.strip().lower()}
    return {str(item).strip().lower() for item in kinds}


class NotificationDatabase:
    """只读访问 Windows 通知库。

    典型用法::

        with NotificationDatabase() as db:
            for item in db.read(kinds="toast"):
                print(item.summary, item.xml)

        for item in NotificationDatabase().watch(stop_event=event):
            handle(item)

    说明
    ----
    * 优先只读直连；直连失败（权限 / WAL 锁）时自动退化为**快照副本**：把主库和
      ``-wal`` 复制到临时目录再读，因此永远看不到「打不开」这种硬失败。
    * 刻意**不用** ``?immutable=1``：那等于告诉 SQLite「此库永不变化」，会跳过
      WAL，监听模式下永远收不到新通知。快照副本则在源库变化时重新复制。
    * ``check_same_thread=False`` + 内部锁：可以在工作线程里 watch、同时在 UI
      线程里 read；但请勿并发调用 :meth:`close`。
    """

    def __init__(self, path: Any = None, *, snapshot_dir: Any = None):
        self.path = Path(path) if path else default_database_path()
        self._snapshot_dir_override = Path(snapshot_dir) if snapshot_dir else None
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None
        self._query = ""
        self._snapshot_path: Path | None = None
        self._snapshot_dir: Path | None = None
        self._source_signature: tuple | None = None
        self._has_type_column = False
        self._connect()

    # -- 只读属性 ---------------------------------------------------------- #
    @property
    def using_snapshot(self) -> bool:
        """当前是不是在读快照副本。"""
        return self._snapshot_path is not None

    @property
    def query(self) -> str:
        """实际使用的 SELECT（排障时有用，列名漂移一眼可见）。"""
        return self._query

    # -- 连接 -------------------------------------------------------------- #
    @staticmethod
    def _signature(path: Path) -> tuple:
        signature = []
        for suffix in ("", "-wal"):
            side = Path(str(path) + suffix)
            try:
                stat = side.stat()
                signature.append((stat.st_size, stat.st_mtime_ns))
            except OSError:
                signature.append((None, None))
        return tuple(signature)

    def _connect(self) -> None:
        if not self.path.is_file():
            raise DatabaseUnavailable(
                "找不到通知库：%s\n"
                "  可能原因：这台机器还没收到过任何通知（该文件由通知平台在第一条通知时创建）；"
                "当前进程运行在别的用户账户下（%%LOCALAPPDATA%% 每用户独立）；或文件被清理过。"
                % self.path
            )

        connection = None
        direct_error = None
        try:
            # connect 本身是惰性的，必须 execute 一次才知道能不能读。
            connection = sqlite3.connect(
                "file:%s?mode=ro" % self.path.as_posix(),
                uri=True,
                timeout=3.0,
                check_same_thread=False,
            )
            connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        except sqlite3.Error as exc:
            direct_error = exc
            if connection is not None:
                connection.close()
            connection = None

        if connection is None:
            self._snapshot_path = self._copy_snapshot()
            self._source_signature = self._signature(self.path)
            connection = sqlite3.connect(
                str(self._snapshot_path), timeout=3.0, check_same_thread=False
            )
            logger.info("只读直连失败（%s），已改用快照副本：%s", direct_error, self._snapshot_path)

        self._connection = connection
        try:
            self._query = build_query(connection)
            self._has_type_column = "Type" in {
                row[1] for row in connection.execute("PRAGMA table_info(Notification)")
            }
        except SchemaError:
            connection.close()
            self._connection = None
            self._drop_snapshot()
            raise

    def _copy_snapshot(self) -> Path:
        """把主库（和 ``-wal``）复制到临时目录。

        ``-shm`` 只是派生缓存，SQLite 会自己重建，故意不复制，避免把不一致的
        索引缓存带过去。
        """
        if self._snapshot_dir is None:
            override = self._snapshot_dir_override or os.environ.get(ENV_SNAPSHOT_DIR)
            self._snapshot_dir = (
                Path(override) if override else Path(tempfile.mkdtemp(prefix="wpn-snapshot-"))
            )
            self._snapshot_dir.mkdir(parents=True, exist_ok=True)

        target = self._snapshot_dir / self.path.name
        shutil.copy2(self.path, target)
        side = Path(str(self.path) + "-wal")
        if side.is_file():
            try:
                shutil.copy2(side, Path(str(target) + "-wal"))
            except OSError as exc:  # pragma: no cover - 只在极端情况下发生
                logger.debug("复制 -wal 失败：%s", exc)
        return target

    def _drop_snapshot(self) -> None:
        if self._snapshot_dir is not None:
            shutil.rmtree(self._snapshot_dir, ignore_errors=True)
        self._snapshot_dir = None
        self._snapshot_path = None

    def refresh(self) -> None:
        """快照模式下，源库有变化就换一份新快照；直连模式下什么都不做。"""
        with self._lock:
            if self._snapshot_path is None or self._connection is None:
                return
            signature = self._signature(self.path)
            if signature == self._source_signature:
                return

            self._connection.close()
            for stale in self._snapshot_dir.glob(self.path.name + "*"):
                try:
                    stale.unlink()
                except OSError:
                    pass
            self._snapshot_path = self._copy_snapshot()
            self._connection = sqlite3.connect(
                str(self._snapshot_path), timeout=3.0, check_same_thread=False
            )
            self._source_signature = signature

    def close(self) -> None:
        """关闭连接并清理临时快照。可重复调用。"""
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            self._drop_snapshot()

    def __enter__(self) -> "NotificationDatabase":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- 读取 -------------------------------------------------------------- #
    def count(self) -> int:
        """当前通知条数。"""
        with self._lock:
            self._require_connection()
            return int(self._connection.execute("SELECT COUNT(*) FROM Notification").fetchone()[0])

    def kinds(self) -> dict:
        """``{类型: 条数}``，例如 ``{'toast': 12, 'tile': 3}``。"""
        with self._lock:
            self._require_connection()
            try:
                rows = self._connection.execute(
                    "SELECT Type, COUNT(*) FROM Notification GROUP BY Type"
                ).fetchall()
            except sqlite3.Error:
                return {}
            return {(row[0] or "?"): int(row[1]) for row in rows}

    def max_order(self) -> int:
        """最大的 ``[Order]``（增量游标的起点）。"""
        with self._lock:
            self._require_connection()
            row = self._connection.execute(
                "SELECT COALESCE(MAX([Order]), 0) FROM Notification"
            ).fetchone()
            return int(row[0] or 0)

    def metadata(self) -> dict:
        """通知库的 ``Metadata`` 表（``Key -> Value``）。

        里面是平台自己的元数据，例如 ``toast:maxCount``（通知中心最多留多少条）、
        ``CurrentNotificationId``。
        """
        with self._lock:
            self._require_connection()
            try:
                rows = self._connection.execute("SELECT Key, Value FROM Metadata").fetchall()
            except sqlite3.Error:
                return {}
            return {str(key): value for key, value in rows}

    def handler_settings(self, aumid: str | None = None) -> dict:
        """每个应用的平台开关：``{AUMID: {SettingKey: Value}}``。

        这就是设置界面里「单应用通知开关」的后端，键的含义见
        :mod:`~features.catch_notify.settings`（``s:toast`` / ``s:banner`` /
        ``s:listenerEnabled`` …）。指定 ``aumid`` 只取那一个应用。
        """
        with self._lock:
            self._require_connection()
            try:
                rows = self._connection.execute(
                    "SELECT h.[PrimaryId], s.[SettingKey], s.[Value] "
                    "FROM HandlerSettings s "
                    "JOIN NotificationHandler h ON h.[RecordId] = s.[HandlerId]"
                ).fetchall()
            except sqlite3.Error:
                return {}

        settings: dict = {}
        for primary_id, setting_key, value in rows:
            if aumid is not None and primary_id != aumid:
                continue
            settings.setdefault(primary_id or "", {})[str(setting_key)] = value
        return settings

    def read(
        self,
        *,
        kinds: Any = None,
        since_order: int = 0,
        limit: int | None = None,
        include_xml: bool = True,
    ) -> list:
        """读取通知，按 `[Order]` 升序返回 :class:`~.records.Notification` 列表。

        参数
        ----
        kinds         ``None`` = 全部；也可以传 ``"toast"`` 或 ``("toast", "tile")``
        since_order   只取 ``[Order]`` 大于该值的记录（增量拉取）。

                      **注意**：``[Order]`` 是 SQLite 的 rowid，平台插入新行时取的是
                      「当前表里最大 Order + 1」。较新的行被删掉之后，新行会拿到比历史
                      更小的 Order —— 所以「只记一个递增游标」会漏报。要长期监听请用
                      :meth:`watch`（它会检测 Order 回退并重扫去重），或者自己记录
                      已上报过的通知身份（见 :func:`notification_identity`）。
        limit         最多返回多少条（取最新的 N 条）
        include_xml   只想看标题正文时置 False，可省掉解码和文本提取
        """
        wanted = _normalize_kinds(kinds)
        records = []
        with self._lock:
            self._require_connection()
            for row in self._connection.execute(self._query, (int(since_order),)):
                (order, ident, kind, payload, payload_type, tag, group,
                 arrival, expiry, boot_id, app_id, display_name) = row

                kind = (kind or "").lower()
                # 老版本库没有 Type 列时不要误过滤；有列但值不认识时按「不匹配」处理。
                if wanted is not None and kind and kind not in wanted:
                    continue

                raw = bytes(payload) if isinstance(payload, (bytes, bytearray)) else None
                xml = decode_payload(payload) if include_xml else ""
                texts = extract_texts(xml) if include_xml else ()

                records.append(Notification(
                    id=int(ident or 0),
                    kind=kind,
                    app_name=display_name or "",
                    app_id=app_id or "",
                    xml=xml,
                    texts=texts,
                    tag=tag or "",
                    group=group or "",
                    arrived_at=filetime_to_datetime(arrival),
                    expires_at=filetime_to_datetime(expiry),
                    source=PROVENANCE_DATABASE,
                    order=int(order or 0),
                    payload_type=payload_type or "",
                    xml_is_original=True,
                    xml_bytes=raw,
                    extra={"boot_id": boot_id},
                ))

        if limit is not None and limit >= 0 and len(records) > limit:
            records = records[-limit:]
        return records

    # -- 监听 -------------------------------------------------------------- #
    def watch(
        self,
        *,
        kinds: Any = None,
        interval: float = 0.8,
        skip_existing: bool = False,
        stop_event: Any = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> Iterator[Notification]:
        """持续产出新通知的生成器。

        新通知的判定**不能只靠 ``[Order]`` 递增**（它是 rowid，见
        :func:`notification_identity`），这里的做法是：

        * 平时用 ``[Order] >= cursor`` 增量读（绝大多数轮询只读到 0~1 行，够快）；
        * 一旦发现表里的最大 Order **小于**游标（说明较新的行被删了、Order 回退了），
          就退回从头重扫一遍，靠「已上报身份表」去重；
        * 身份表在 ``skip_existing=True`` 时用启动瞬间的现有条目做种，所以重扫不会把
          通知中心里的旧通知重新报一遍。

        ``stop_event`` 传一个 :class:`threading.Event` 就能立刻（最多
        ``interval`` 秒内）结束循环 —— GUI 里退出线程用得上。
        """
        cursor = self.max_order() if skip_existing else 0
        seen: "OrderedDict[tuple, None]" = OrderedDict()
        if skip_existing:
            try:
                for item in self.read(include_xml=False):
                    seen[notification_identity(item)] = None
            except CatchNotifyError as exc:  # pragma: no cover - 打开后立刻坏掉
                logger.debug("记录已有通知失败：%s", exc)
        delay = max(float(interval), 0.05)

        while True:
            if stop_event is not None and stop_event.is_set():
                return

            rescan = False
            highest = cursor
            try:
                self.refresh()
                highest = self.max_order()
                if highest < cursor:
                    logger.debug("通知库最大 Order 从 %s 回退到 %s，重扫一遍", cursor, highest)
                    rescan = True
                    floor = 0
                else:
                    # 多读一条（Order >= cursor）是为了覆盖「新行的 Order 正好撞上旧游标」
                    floor = max(cursor - 1, 0)
                batch = self.read(kinds=kinds, since_order=floor)
            except CatchNotifyError as exc:
                if on_error is not None:
                    on_error(exc)
                else:
                    logger.warning("读取通知失败：%s", exc)
                batch = []
            except sqlite3.Error as exc:  # pragma: no cover - 运行期损坏等少见情况
                if on_error is not None:
                    on_error(exc)
                else:
                    logger.warning("数据库读取异常：%s", exc)
                batch = []

            if rescan:
                # 重扫一次就把水位拉回当前值，别每轮都全表重扫
                cursor = max(highest, 0)

            for item in batch:
                key = notification_identity(item)
                if key in seen:
                    continue
                seen[key] = None
                while len(seen) > SEEN_LIMIT:
                    seen.popitem(last=False)
                cursor = max(cursor, item.order)
                yield item

            if stop_event is not None:
                stop_event.wait(delay)
            else:
                time.sleep(delay)

    # -- 内部 -------------------------------------------------------------- #
    def _require_connection(self) -> None:
        if self._connection is None:
            raise DatabaseUnavailable("数据库连接已关闭。")

    def __repr__(self) -> str:
        mode = "snapshot" if self.using_snapshot else "direct"
        return f"<NotificationDatabase {self.path} mode={mode}>"
