from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, File, Image, Plain, Record, Reply, Video
from astrbot.api.star import Context, Star, register
from astrbot.api.web import error_response, file_response, json_response, request
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.utils.astrbot_path import (
    get_astrbot_plugin_data_path,
    get_astrbot_temp_path,
)

PLUGIN_NAME = "astrbot_plugin_chat_console"
MAX_MESSAGES = 5000
MAX_SEND_QUEUE = 64
SEND_WORKER_COUNT = 4
SEND_TIMEOUT_SECONDS = 30
GROUP_INFO_CACHE_SECONDS = 300
GROUP_INFO_ERROR_CACHE_SECONDS = 30
DATA_DIR = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
DATABASE_FILE = DATA_DIR / "chat_console.db"
LEGACY_MESSAGES_FILE = DATA_DIR / "messages.json"
PINS_FILE = DATA_DIR / "pinned_groups.json"
SETTINGS_FILE = DATA_DIR / "group_settings.json"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _message_chain_size(chain: Any) -> int:
    try:
        return len(json.dumps(chain, ensure_ascii=False))
    except (TypeError, ValueError):
        return 0


def _write_json_file(path: Path, data: Any):
    """Write JSON atomically so interrupted writes do not corrupt plugin data.

    Args:
        path: Destination file.
        data: JSON-serializable value to persist.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def _connect_database() -> sqlite3.Connection:
    """Open the plugin SQLite database with a row-oriented result format.

    Returns:
        A SQLite connection owned by the caller.
    """
    connection = sqlite3.connect(DATABASE_FILE, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def _ensure_database() -> None:
    """Create the message table and indexes when the plugin starts."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with closing(_connect_database()) as connection, connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL UNIQUE,
                created_at REAL NOT NULL,
                platform_id TEXT NOT NULL DEFAULT '',
                platform_name TEXT NOT NULL DEFAULT '',
                umo TEXT NOT NULL DEFAULT '',
                session_id TEXT NOT NULL DEFAULT '',
                message_type TEXT NOT NULL DEFAULT '',
                group_id TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL DEFAULT 'user',
                sender_id TEXT NOT NULL DEFAULT '',
                sender_name TEXT NOT NULL DEFAULT '',
                avatar TEXT NOT NULL DEFAULT '',
                group_name TEXT NOT NULL DEFAULT '',
                group_avatar TEXT NOT NULL DEFAULT '',
                chain_json TEXT NOT NULL DEFAULT '[]',
                is_mention INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS plugin_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_messages_session_sequence
                ON messages(platform_id, session_id, sequence);
            """
        )
        existing_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(messages)").fetchall()
        }
        for column in ("group_name", "group_avatar"):
            if column not in existing_columns:
                connection.execute(
                    f"ALTER TABLE messages ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                )


def _is_group_record(record: dict[str, Any]) -> bool:
    message_type = _text(record.get("message_type")).upper().replace("_", "")
    return "GROUPMESSAGE" in message_type


def _group_bucket_key(adapter_id: str, group_id: str) -> str:
    return f"{adapter_id}::group::{group_id}"


def _conversation_key(record: dict[str, Any], merge_sessions: bool) -> str:
    adapter_id = _text(record.get("platform_id"))
    group_id = _text(record.get("group_id"))
    session_id = _text(record.get("session_id"))
    if merge_sessions and group_id:
        return _group_bucket_key(adapter_id, group_id)
    return f"{adapter_id}::{session_id}"


def _http_url(value: Any) -> str:
    candidate = _text(value)
    return candidate if candidate.startswith(("https://", "http://")) else ""


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(int(value), maximum))
    except (TypeError, ValueError):
        return default


def _read_plugin_state(key: str) -> Any:
    _ensure_database()
    with closing(_connect_database()) as connection:
        row = connection.execute(
            "SELECT value FROM plugin_state WHERE key = ?", (key,)
        ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["value"])
    except (TypeError, ValueError):
        return None


def _write_plugin_state(key: str, value: Any) -> None:
    _ensure_database()
    with closing(_connect_database()) as connection, connection:
        connection.execute(
            "INSERT INTO plugin_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )


def _write_database_records(
    records: list[dict[str, Any]], max_messages: int = MAX_MESSAGES
) -> None:
    """Insert message records and trim the database to the retention limit.

    Args:
        records: Normalized message records to insert or replace.
    """
    if not records:
        return
    _ensure_database()
    values = [
        (
            _text(record.get("id")),
            float(record.get("created_at", 0) or 0),
            _text(record.get("platform_id")),
            _text(record.get("platform_name")),
            _text(record.get("umo")),
            _text(record.get("session_id")),
            _text(record.get("message_type")),
            _text(record.get("group_id")),
            _text(record.get("role")) or "user",
            _text(record.get("sender_id")),
            _text(record.get("sender_name")),
            _text(record.get("avatar")),
            _text(record.get("group_name")),
            _http_url(record.get("group_avatar")),
            json.dumps(record.get("chain", []), ensure_ascii=False),
            int(bool(record.get("is_mention", False))),
        )
        for record in records
        if _text(record.get("id"))
    ]
    if not values:
        return
    with closing(_connect_database()) as connection, connection:
        connection.executemany(
            """
            INSERT OR IGNORE INTO messages (
                id, created_at, platform_id, platform_name, umo, session_id,
                message_type, group_id, role, sender_id, sender_name, avatar,
                group_name, group_avatar, chain_json, is_mention
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        connection.execute(
            """
            DELETE FROM messages
            WHERE id IN (
                SELECT id FROM messages
                ORDER BY sequence DESC
                LIMIT -1 OFFSET ?
            )
            """,
            (max_messages,),
        )


def _update_group_metadata_database(
    adapter_id: str, group_id: str, group_name: str, group_avatar: str
) -> None:
    if not group_name and not group_avatar:
        return
    _ensure_database()
    with closing(_connect_database()) as connection, connection:
        connection.execute(
            "UPDATE messages SET "
            "group_name = CASE WHEN ? != '' THEN ? ELSE group_name END, "
            "group_avatar = CASE WHEN ? != '' THEN ? ELSE group_avatar END "
            "WHERE platform_id = ? AND group_id = ?",
            (
                group_name,
                group_name,
                group_avatar,
                group_avatar,
                adapter_id,
                group_id,
            ),
        )


def _sanitize_chain(chain: Any) -> tuple[list[dict[str, Any]], bool]:
    """Remove accidental inline media payloads from persisted chat history."""
    changed = False
    output: list[dict[str, Any]] = []
    for item in chain if isinstance(chain, list) else []:
        if not isinstance(item, dict):
            changed = True
            continue
        copied = dict(item)
        kind = _text(copied.get("type"))
        url = _text(copied.get("url"))
        if kind in {"image", "video", "record", "file"} and (
            url.startswith("base64://")
            or url.startswith("data:")
            or len(url) > 2 * 1024 * 1024
        ):
            copied["url"] = ""
            copied["label"] = "图片数据已省略"
            changed = True
        if kind == "reply":
            nested, nested_changed = _sanitize_chain(copied.get("chain", []))
            copied["chain"] = nested
            changed = changed or nested_changed
        output.append(copied)
    return output, changed


def _avatar_for_sender(sender_id: str, sender: Any = None) -> str:
    platform_avatar = _text(getattr(sender, "avatar", ""))
    if platform_avatar.startswith(("http://", "https://")):
        return platform_avatar
    if not sender_id or not sender_id.isdigit():
        return ""
    return f"https://q1.qlogo.cn/g?b=qq&nk={sender_id}&s=640"


def _raw_sender(message_obj: Any) -> Any:
    raw = getattr(message_obj, "raw_message", None)
    return getattr(raw, "author", None) or getattr(message_obj, "sender", None)


def _component_to_dict(component: Any) -> dict[str, Any]:
    if isinstance(component, Plain):
        return {"type": "text", "text": _text(getattr(component, "text", ""))}
    if isinstance(component, At):
        return {
            "type": "at",
            "user_id": _text(getattr(component, "qq", "")),
            "name": _text(getattr(component, "name", "")),
        }
    if isinstance(component, Reply):
        return {
            "type": "reply",
            "message_id": _text(getattr(component, "id", "")),
            "sender_name": _text(getattr(component, "sender_nickname", "")),
            "text": _text(getattr(component, "message_str", "")),
            "chain": [
                _component_to_dict(item)
                for item in (getattr(component, "chain", None) or [])
            ],
        }
    if isinstance(component, Image):
        return {
            "type": "image",
            "url": _text(getattr(component, "url", ""))
            or _text(getattr(component, "file", ""))
            or _text(getattr(component, "path", "")),
        }
    if isinstance(component, Video):
        return {
            "type": "video",
            "url": _text(getattr(component, "url", ""))
            or _text(getattr(component, "file", ""))
            or _text(getattr(component, "path", "")),
        }
    if isinstance(component, Record):
        return {
            "type": "record",
            "url": _text(getattr(component, "url", ""))
            or _text(getattr(component, "file", ""))
            or _text(getattr(component, "path", "")),
        }
    if isinstance(component, File):
        return {
            "type": "file",
            "name": _text(getattr(component, "name", "")),
            "url": _text(getattr(component, "url", ""))
            or _text(getattr(component, "file", ""))
            or _text(getattr(component, "path", "")),
        }
    return {"type": "other", "text": _text(getattr(component, "type", "消息"))}


def _chain_to_dicts(event: AstrMessageEvent) -> list[dict[str, Any]]:
    chain = [_component_to_dict(item) for item in (event.get_messages() or [])]
    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
    public_urls = _raw_media_urls(raw)
    cursor = iter(public_urls)

    def replace_media(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        output = []
        for part in parts:
            copied = dict(part)
            if copied.get("type") in {"image", "video", "record", "file"}:
                current = _text(copied.get("url"))
                if not current.startswith(("http://", "https://")):
                    copied["url"] = next(cursor, current)
            if copied.get("type") == "reply":
                copied["chain"] = replace_media(copied.get("chain", []))
            output.append(copied)
        return output

    return replace_media(chain)


def _raw_media_urls(raw: Any) -> list[str]:
    urls: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key in ("url", "download_url", "media_url"):
                candidate = _text(value.get(key))
                if candidate.startswith(("http://", "https://")):
                    urls.append(candidate)
            for item in value.values():
                if isinstance(item, (dict, list, tuple)):
                    walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)
        else:
            candidate = _text(getattr(value, "url", ""))
            if candidate.startswith(("http://", "https://")):
                urls.append(candidate)
            for attr in ("attachments", "msg_elements", "quoted_message"):
                nested = getattr(value, attr, None)
                if nested:
                    walk(nested)

    walk(getattr(raw, "raw_data", None) or raw)
    return list(dict.fromkeys(urls))


@register(PLUGIN_NAME, "BvzRays", "在 WebUI 查看并控制各适配器群聊消息", "1.1.0")
class ChatConsolePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.context = context
        self.config = config or AstrBotConfig(
            {
                "dark_mode": True,
                "merge_isolated_sessions": False,
                "show_onebot_profile": True,
                "message_poll_interval_seconds": 3,
                "group_poll_interval_seconds": 10,
                "send_timeout_seconds": SEND_TIMEOUT_SECONDS,
            }
        )
        self.max_messages = _bounded_int(
            self.config.get("max_messages", MAX_MESSAGES), MAX_MESSAGES, 500, 50000
        )
        self.merge_isolated_sessions = bool(
            self.config.get("merge_isolated_sessions", False)
        )
        self.show_onebot_profile = bool(self.config.get("show_onebot_profile", True))
        self.message_poll_interval_seconds = _bounded_int(
            self.config.get("message_poll_interval_seconds", 3), 3, 2, 60
        )
        self.group_poll_interval_seconds = _bounded_int(
            self.config.get("group_poll_interval_seconds", 10), 10, 5, 120
        )
        self.send_timeout_seconds = _bounded_int(
            self.config.get("send_timeout_seconds", SEND_TIMEOUT_SECONDS),
            SEND_TIMEOUT_SECONDS,
            5,
            120,
        )
        self._lock = asyncio.Lock()
        self._messages: list[dict[str, Any]] = self._initialize_database()
        self._messages_by_session: dict[str, list[dict[str, Any]]] = {}
        self._messages_by_group: dict[str, list[dict[str, Any]]] = {}
        for item in self._messages:
            key = f"{item.get('platform_id', '')}::{item.get('session_id', '')}"
            self._messages_by_session.setdefault(key, []).append(item)
            if _is_group_record(item):
                group_key = _group_bucket_key(
                    _text(item.get("platform_id")),
                    _text(item.get("group_id")) or _text(item.get("session_id")),
                )
                self._messages_by_group.setdefault(group_key, []).append(item)
        self._pinned_groups: set[str] = self._load_pinned_groups()
        self._group_settings: dict[str, dict[str, Any]] = self._load_group_settings()
        self._group_info_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._group_info_tasks: dict[str, asyncio.Task[dict[str, Any] | None]] = {}
        self._group_info_queue: asyncio.Queue[tuple[str, str] | None] = asyncio.Queue(maxsize=32)
        self._group_info_enqueued: set[str] = set()
        self._group_info_workers: list[asyncio.Task] = []
        self._persist_queue: asyncio.Queue[object] = asyncio.Queue(maxsize=1)
        self._persist_task: asyncio.Task | None = None
        self._pending_database_records: list[dict[str, Any]] = []
        self._send_queue: asyncio.Queue[
            tuple[str, str, MessageChain, asyncio.Future[tuple[bool, str, int]]] | None
        ] = asyncio.Queue(maxsize=MAX_SEND_QUEUE)
        self._send_workers: list[asyncio.Task] = []
        self._send_locks: dict[str, asyncio.Lock] = {}
        self._stopping = False
        self._persistence_closing = False
        self._register_api("adapters", self.api_adapters, ["GET"], "获取适配器列表")
        self._register_api("settings", self.api_settings, ["GET"], "获取聊天控制台设置")
        self._register_api("ui_state", self.api_ui_state, ["GET", "POST"], "保存聊天窗口选择")
        self._register_api("groups", self.api_groups, ["GET"], "获取群聊列表")
        self._register_api("group_info", self.api_group_info, ["GET"], "读取 OneBot 群资料")
        self._register_api("member_info", self.api_member_info, ["GET"], "读取 OneBot 群成员资料")
        self._register_api("messages", self.api_messages, ["GET"], "获取群聊消息")
        self._register_api("send", self.api_send, ["POST"], "发送群聊消息")
        self._register_api("send_media", self.api_send_media, ["POST"], "发送聊天媒体")
        self._register_api("media", self.api_media, ["GET"], "访问聊天媒体")
        self._register_api("pins", self.api_pins, ["GET", "POST"], "读取或修改群聊置顶")
        self._register_api(
            "group_settings",
            self.api_group_settings,
            ["GET", "POST"],
            "读取或修改群聊设置",
        )

    def _register_api(self, suffix: str, handler: Any, methods: list[str], desc: str):
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/{suffix}", handler, methods, desc
        )

    def _initialize_database(self) -> list[dict[str, Any]]:
        """Initialize SQLite and import legacy JSON history once.

        Returns:
            Retained messages in insertion order.
        """
        _ensure_database()
        with closing(_connect_database()) as connection:
            migrated = connection.execute(
                "SELECT value FROM plugin_state WHERE key = 'messages_json_migrated'"
            ).fetchone()
        if not migrated:
            try:
                legacy_messages = json.loads(
                    LEGACY_MESSAGES_FILE.read_text(encoding="utf-8")
                )
            except FileNotFoundError:
                legacy_messages = []
            except Exception as exc:
                logger.warning("chat_console: legacy message import failed: %s", exc)
                legacy_messages = None
            if isinstance(legacy_messages, list):
                cleaned = []
                for item in legacy_messages[-self.max_messages :]:
                    if isinstance(item, dict):
                        copied = dict(item)
                        copied["chain"] = _sanitize_chain(copied.get("chain", []))[0]
                        if not _text(copied.get("id")):
                            copied["id"] = uuid.uuid4().hex
                        cleaned.append(copied)
                _write_database_records(cleaned, self.max_messages)
                with closing(_connect_database()) as connection, connection:
                    connection.execute(
                        "INSERT OR REPLACE INTO plugin_state (key, value) VALUES (?, ?)",
                        ("messages_json_migrated", "1"),
                    )
                if cleaned:
                    logger.info(
                        "chat_console: imported %s legacy messages into SQLite",
                        len(cleaned),
                    )
                    migrated_path = LEGACY_MESSAGES_FILE.with_suffix(
                        LEGACY_MESSAGES_FILE.suffix + ".migrated"
                    )
                    try:
                        if not migrated_path.exists():
                            LEGACY_MESSAGES_FILE.replace(migrated_path)
                    except OSError as exc:
                        logger.warning(
                            "chat_console: could not archive legacy message file: %s",
                            exc,
                        )
        with closing(_connect_database()) as connection:
            rows = connection.execute(
                """
                SELECT id, created_at, platform_id, platform_name, umo, session_id,
                       message_type, group_id, role, sender_id, sender_name, avatar,
                       group_name, group_avatar, chain_json, is_mention
                FROM messages
                ORDER BY sequence DESC
                LIMIT ?
                """,
                (self.max_messages,),
            ).fetchall()
        messages = []
        for row in reversed(rows):
            try:
                chain = json.loads(row["chain_json"])
            except (TypeError, ValueError):
                chain = []
            messages.append(
                {
                    "id": row["id"],
                    "created_at": row["created_at"],
                    "platform_id": row["platform_id"],
                    "platform_name": row["platform_name"],
                    "umo": row["umo"],
                    "session_id": row["session_id"],
                    "message_type": row["message_type"],
                    "group_id": row["group_id"],
                    "role": row["role"],
                    "sender_id": row["sender_id"],
                    "sender_name": row["sender_name"],
                    "avatar": row["avatar"],
                    "group_name": row["group_name"],
                    "group_avatar": row["group_avatar"],
                    "chain": chain,
                    "is_mention": bool(row["is_mention"]),
                }
            )
        return messages

    def _load_pinned_groups(self) -> set[str]:
        try:
            data = json.loads(PINS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return {_text(item) for item in data if _text(item)}
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.warning("chat_console: load pinned groups failed: %s", exc)
        return set()

    async def _save_pinned_groups(self):
        await asyncio.to_thread(
            _write_json_file, PINS_FILE, sorted(self._pinned_groups)
        )

    def _load_group_settings(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): v for k, v in data.items() if isinstance(v, dict)}
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.warning("chat_console: load group settings failed: %s", exc)
        return {}

    async def _save_group_settings(self):
        await asyncio.to_thread(
            _write_json_file, SETTINGS_FILE, dict(self._group_settings)
        )

    async def _save_messages(self):
        records = list(self._pending_database_records)
        if not records:
            return
        await asyncio.to_thread(
            _write_database_records, records, self.max_messages
        )
        del self._pending_database_records[: len(records)]

    async def _persistence_worker(self):
        while True:
            signal = await self._persist_queue.get()
            closing = signal is None
            self._persist_queue.task_done()
            if not closing:
                await asyncio.sleep(0.1)
            while True:
                try:
                    signal = self._persist_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                self._persist_queue.task_done()
                closing = closing or signal is None
            try:
                await self._save_messages()
            except Exception:
                logger.exception("chat_console: persist messages failed")
                if not closing:
                    await asyncio.sleep(1)
                    try:
                        self._persist_queue.put_nowait(True)
                    except asyncio.QueueFull:
                        pass
            if closing:
                return

    def _queue_persistence(self):
        if self._persistence_closing:
            return
        if self._persist_task is None or self._persist_task.done():
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return
            self._persist_task = asyncio.create_task(self._persistence_worker())
        try:
            self._persist_queue.put_nowait(True)
        except asyncio.QueueFull:
            # One pending signal is enough because the worker snapshots the latest list.
            pass

    async def _store_message(self, record: dict[str, Any]):
        self._messages.append(record)
        key = f"{record.get('platform_id', '')}::{record.get('session_id', '')}"
        self._messages_by_session.setdefault(key, []).append(record)
        if _is_group_record(record):
            group_key = _group_bucket_key(
                _text(record.get("platform_id")),
                _text(record.get("group_id")) or _text(record.get("session_id")),
            )
            self._messages_by_group.setdefault(group_key, []).append(record)
        if len(self._messages) > self.max_messages:
            removed = self._messages[: -self.max_messages]
            del self._messages[: -self.max_messages]
            for old in removed:
                old_key = f"{old.get('platform_id', '')}::{old.get('session_id', '')}"
                session_messages = self._messages_by_session.get(old_key)
                if session_messages:
                    session_messages.pop(0)
                    if not session_messages:
                        self._messages_by_session.pop(old_key, None)
                if _is_group_record(old):
                    old_group_key = _group_bucket_key(
                        _text(old.get("platform_id")),
                        _text(old.get("group_id")) or _text(old.get("session_id")),
                    )
                    group_messages = self._messages_by_group.get(old_group_key)
                    if group_messages:
                        group_messages.pop(0)
                        if not group_messages:
                            self._messages_by_group.pop(old_group_key, None)
        self._pending_database_records.append(record)
        self._queue_persistence()

    async def initialize(self):
        self._persist_task = asyncio.create_task(self._persistence_worker())
        self._send_workers = [
            asyncio.create_task(self._send_worker()) for _ in range(SEND_WORKER_COUNT)
        ]
        self._group_info_workers = [
            asyncio.create_task(self._group_info_worker()) for _ in range(2)
        ]

    async def _send_worker(self):
        while True:
            job = await self._send_queue.get()
            if job is None:
                self._send_queue.task_done()
                return
            session_key, umo, message_chain, result = job
            lock = self._send_locks.setdefault(session_key, asyncio.Lock())
            try:
                async def send_in_order() -> bool:
                    async with lock:
                        return await self.context.send_message(umo, message_chain)

                sent = await asyncio.wait_for(
                    send_in_order(), timeout=self.send_timeout_seconds
                )
                if not result.done():
                    result.set_result(
                        (
                            bool(sent),
                            ""
                            if sent
                            else self._send_error_message(
                                umo, "AstrBot 未找到支持该会话发送的适配器。"
                            ),
                            502,
                        )
                    )
            except asyncio.TimeoutError:
                logger.warning("chat_console: queued send timed out for %s", umo)
                if not result.done():
                    result.set_result(
                        (
                            False,
                            self._send_error_message(
                                umo,
                                "OneBot API 响应超时，送达状态未确认；请先检查群聊再重试",
                            ),
                            504,
                        )
                    )
            except Exception as exc:
                if exc.__class__.__name__ == "ApiNotAvailable":
                    logger.warning("chat_console: send API unavailable for %s", umo)
                else:
                    logger.exception("chat_console: queued send failed")
                if not result.done():
                    status = 503 if exc.__class__.__name__ == "ApiNotAvailable" else 500
                    result.set_result(
                        (False, self._send_error_message(umo, exc), status)
                    )
            finally:
                self._send_queue.task_done()

    def _send_error_message(self, umo: str, error: Any) -> str:
        """Describe send failures with the adapter instance involved."""
        adapter_id = _text(umo).split(":", 1)[0]
        adapter_name = adapter_id or "当前适配器"
        for platform in self._platforms():
            try:
                if _text(getattr(platform.meta(), "id", "")) == adapter_id:
                    adapter_name = self._adapter_instance_name(platform, platform.meta())
                    break
            except Exception:
                continue
        if isinstance(error, str):
            detail = error
        elif error.__class__.__name__ == "ApiNotAvailable":
            detail = "OneBot API 通道不可用，请检查该实例的反向 WebSocket API 连接"
        else:
            detail = f"发送失败：{error}"
        return (
            f"适配器“{adapter_name}”发送失败：{detail}。"
            "请确认当前聊天窗口选中的是预期的适配器实例。"
        )

    async def _queue_send(
        self,
        session_key: str,
        umo: str,
        message_chain: MessageChain,
    ) -> tuple[bool, str, int]:
        if self._stopping or not self._send_workers:
            return False, "消息发送服务尚未就绪。", 503
        future = asyncio.get_running_loop().create_future()
        try:
            self._send_queue.put_nowait((session_key, umo, message_chain, future))
        except asyncio.QueueFull:
            return False, "发送队列已满，请稍后重试。", 429
        return await asyncio.shield(future)

    async def _append_message(
        self,
        event: AstrMessageEvent,
        role: str,
        chain: list[dict[str, Any]],
        sender_id: str,
        sender_name: str,
        sender: Any = None,
    ):
        if not chain:
            return
        session = event.session
        platform_id = event.get_platform_id()
        group = getattr(getattr(event, "message_obj", None), "group", None)
        record = {
            "id": uuid.uuid4().hex,
            "created_at": time.time(),
            "platform_id": platform_id,
            "platform_name": event.get_platform_name(),
            "umo": event.unified_msg_origin,
            "session_id": event.session_id,
            "message_type": _text(
                getattr(getattr(session, "message_type", ""), "value", "")
            )
            or _text(getattr(session, "message_type", "")),
            "group_id": _text(event.get_group_id())
            or _text(getattr(event.message_obj, "group_id", "")),
            "group_name": _text(getattr(group, "group_name", "")),
            "group_avatar": _http_url(
                getattr(group, "group_avatar", "") or getattr(group, "avatar", "")
            ),
            "role": role,
            "sender_id": sender_id,
            "sender_name": sender_name or ("机器人" if role == "bot" else "未知用户"),
            "avatar": _avatar_for_sender(sender_id, sender) if role != "bot" else "",
            "chain": _sanitize_chain(chain)[0],
            "is_mention": False,
        }
        if role == "user":
            self_id = _text(event.get_self_id())
            record["is_mention"] = bool(
                self_id
                and any(
                    part.get("type") == "at" and _text(part.get("user_id")) == self_id
                    for part in record["chain"]
                )
            )
        await self._store_message(record)

    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def on_message(self, event: AstrMessageEvent):
        message_obj = getattr(event, "message_obj", None)
        sender = _raw_sender(message_obj)
        sender_id = _text(getattr(sender, "user_id", "") or getattr(sender, "id", ""))
        sender_id = sender_id or _text(getattr(sender, "member_openid", ""))
        sender_name = _text(event.get_sender_name())
        await self._append_message(
            event,
            "user",
            _chain_to_dicts(event),
            sender_id,
            sender_name,
            sender,
        )

    @filter.after_message_sent(priority=-100)
    async def after_message_sent(self, event: AstrMessageEvent):
        result = event.get_result()
        if not result or not result.chain:
            return
        await self._append_message(
            event,
            "bot",
            [_component_to_dict(item) for item in result.chain],
            "",
            "机器人",
        )

    def _platforms(self) -> list[Any]:
        return list(self.context.platform_manager.get_insts())

    @staticmethod
    def _adapter_instance_name(platform: Any, meta: Any) -> str:
        config = getattr(platform, "config", {})
        if not isinstance(config, dict):
            config = {}
        for field in (
            "adapter_display_name",
            "display_name",
            "adapter_name",
            "account_name",
            "alias",
            "name",
        ):
            candidate = _text(config.get(field))
            if candidate and candidate.casefold() != _text(getattr(meta, "name", "")).casefold():
                return candidate
        return _text(config.get("id")) or _text(getattr(meta, "id", "")) or _text(
            getattr(meta, "adapter_display_name", "")
        ) or _text(getattr(meta, "name", "适配器"))

    def _onebot_platform(self, adapter_id: str) -> Any | None:
        for platform in self._platforms():
            try:
                meta = platform.meta()
                if _text(getattr(meta, "id", "")) != adapter_id:
                    continue
                platform_type = _text(getattr(meta, "name", "")).casefold()
                class_name = type(platform).__name__.casefold()
                if platform_type not in {"aiocqhttp", "onebot", "onebot11", "onebot_v11", "napcat", "snowluna"} and not any(
                    marker in class_name for marker in ("aiocqhttp", "onebot", "napcat", "snowluna")
                ):
                    return None
                client = getattr(platform, "bot", None) or getattr(platform, "client", None)
                if not callable(getattr(client, "call_action", None)):
                    getter = getattr(platform, "get_client", None)
                    client = getter() if callable(getter) else client
                if callable(getattr(client, "call_action", None)):
                    return platform
            except Exception:
                continue
        return None

    def _schedule_group_info(self, adapter_id: str, group_id: str) -> None:
        key = f"{adapter_id}::{group_id}"
        if key in self._group_info_enqueued:
            return
        cache = self._group_info_cache.get(f"group::{adapter_id}::{group_id}::")
        if cache and cache[0] > time.monotonic():
            return
        self._group_info_enqueued.add(key)
        try:
            self._group_info_queue.put_nowait((adapter_id, group_id))
        except asyncio.QueueFull:
            self._group_info_enqueued.discard(key)

    async def _group_info_worker(self) -> None:
        while True:
            item = await self._group_info_queue.get()
            if item is None:
                self._group_info_queue.task_done()
                return
            adapter_id, group_id = item
            queue_key = f"{adapter_id}::{group_id}"
            try:
                info = await self._onebot_action("group", adapter_id, group_id)
                if info:
                    group_key = _group_bucket_key(adapter_id, group_id)
                    for message in self._messages_by_group.get(group_key, []):
                        message["group_name"] = _text(info.get("group_name"))
                        message["group_avatar"] = _http_url(info.get("group_avatar"))
                    await asyncio.to_thread(
                        _update_group_metadata_database,
                        adapter_id,
                        group_id,
                        _text(info.get("group_name")),
                        _http_url(info.get("group_avatar")),
                    )
            except Exception:
                logger.debug("chat_console: OneBot group metadata refresh failed")
            finally:
                self._group_info_enqueued.discard(queue_key)
                self._group_info_queue.task_done()

    async def _onebot_action(
        self,
        kind: str,
        adapter_id: str,
        group_id: str,
        user_id: str = "",
    ) -> dict[str, Any] | None:
        cache_key = "::".join((kind, adapter_id, group_id, user_id))
        now = time.monotonic()
        cached = self._group_info_cache.get(cache_key)
        if cached and cached[0] > now:
            return dict(cached[1]) if cached[1] else None
        task = self._group_info_tasks.get(cache_key)
        if task is None:
            async def fetch() -> dict[str, Any] | None:
                platform = self._onebot_platform(adapter_id)
                if platform is None:
                    return None
                client = getattr(platform, "bot", None) or getattr(platform, "client", None)
                if not callable(getattr(client, "call_action", None)):
                    getter = getattr(platform, "get_client", None)
                    client = getter() if callable(getter) else client
                call_action = getattr(client, "call_action", None)
                if not callable(call_action):
                    return None
                group_value: int | str = int(group_id) if group_id.isdigit() else group_id
                params: dict[str, Any] = {"group_id": group_value}
                if kind == "member":
                    params["user_id"] = int(user_id) if user_id.isdigit() else user_id
                action = "get_group_info" if kind == "group" else "get_group_member_info"
                try:
                    response = await asyncio.wait_for(
                        call_action(action=action, **params), timeout=6
                    )
                except Exception as exc:
                    logger.debug("chat_console: OneBot %s lookup failed: %s", kind, exc)
                    return None
                if isinstance(response, dict) and "data" in response and (
                    "status" in response or "retcode" in response
                ):
                    response = response.get("data")
                if not isinstance(response, dict):
                    return None
                if kind == "group":
                    fields = {
                        "group_name": _text(response.get("group_name")),
                        "member_count": response.get("member_count"),
                        "max_member_count": response.get("max_member_count"),
                        "group_avatar": _http_url(
                            response.get("group_avatar")
                            or response.get("avatar_url")
                            or response.get("avatar")
                        ),
                    }
                else:
                    role = _text(response.get("role")).lower()
                    fields = {
                        "user_id": _text(response.get("user_id")) or user_id,
                        "nickname": _text(response.get("nickname")),
                        "card": _text(response.get("card")),
                        "sex": _text(response.get("sex")),
                        "age": response.get("age"),
                        "area": _text(response.get("area")),
                        "role": role,
                        "level": _text(response.get("level")),
                        "title": _text(response.get("title")),
                        "join_time": response.get("join_time"),
                        "last_sent_time": response.get("last_sent_time"),
                        "avatar": (
                            f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"
                            if user_id.isdigit()
                            else ""
                        ),
                    }
                return fields

            task = asyncio.create_task(fetch())
            self._group_info_tasks[cache_key] = task
        try:
            result = await task
        finally:
            if self._group_info_tasks.get(cache_key) is task:
                self._group_info_tasks.pop(cache_key, None)
        ttl = GROUP_INFO_CACHE_SECONDS if result else GROUP_INFO_ERROR_CACHE_SECONDS
        self._group_info_cache[cache_key] = (time.monotonic() + ttl, result or {})
        return dict(result) if result else None

    async def api_settings(self):
        """Return the configured initial theme for the chat console.

        Returns:
            JSON containing the configured dark-mode default.
        """
        global_config = self.context.get_config()
        platform_settings = global_config.get("platform_settings", {})
        isolation_enabled = bool(platform_settings.get("unique_session", False))
        return json_response(
            {
                "dark_mode": bool(self.config.get("dark_mode", True)),
                "merge_isolated_sessions": self.merge_isolated_sessions,
                "show_onebot_profile": self.show_onebot_profile,
                "message_poll_interval_seconds": self.message_poll_interval_seconds,
                "group_poll_interval_seconds": self.group_poll_interval_seconds,
                "send_timeout_seconds": self.send_timeout_seconds,
                "max_messages": self.max_messages,
                "session_isolation_enabled": isolation_enabled,
            }
        )

    async def api_adapters(self):
        items = []
        for platform in self._platforms():
            try:
                meta = platform.meta()
                platform_config = getattr(platform, "config", {})
                items.append(
                    {
                        "id": _text(getattr(meta, "id", "")),
                        "name": self._adapter_instance_name(platform, meta),
                        "display_name": self._adapter_instance_name(platform, meta),
                        "adapter_type": _text(getattr(meta, "name", "适配器")),
                        "status": _text(
                            getattr(getattr(platform, "status", ""), "value", "")
                        )
                        or _text(getattr(platform, "status", "unknown")),
                        "account_name": _text(
                            platform_config.get("account_name", "")
                            if isinstance(platform_config, dict)
                            else ""
                        ),
                    }
                )
            except Exception as exc:
                logger.warning("chat_console: skip broken adapter metadata: %s", exc)
        return json_response({"adapters": items})

    async def api_groups(self):
        adapter_id = _text(request.query.get("adapter_id", ""))
        groups: dict[str, dict[str, Any]] = {}
        merge_sessions = self.merge_isolated_sessions
        global_config = self.context.get_config()
        isolation_enabled = bool(
            global_config.get("platform_settings", {}).get("unique_session", False)
        )
        source = self._messages_by_group if merge_sessions else self._messages_by_session
        for source_key, source_messages in source.items():
            session_messages = list(source_messages)
            if not session_messages:
                continue
            item = session_messages[-1]
            if adapter_id and item.get("platform_id") != adapter_id:
                continue
            session_id = _text(item.get("session_id"))
            if not session_id or not _is_group_record(item):
                continue
            group_id = _text(item.get("group_id")) or session_id
            key = source_key
            settings = self._group_settings.get(key, {})
            hidden_at = float(settings.get("hidden_at", 0) or 0)
            if hidden_at and float(item.get("created_at", 0) or 0) <= hidden_at:
                continue
            last_read = float(
                settings.get("last_read", 0) or 0
            )
            group_name = next(
                (_text(message.get("group_name")) for message in reversed(session_messages) if _text(message.get("group_name"))),
                "",
            )
            group_avatar = next(
                (_http_url(message.get("group_avatar")) for message in reversed(session_messages) if _http_url(message.get("group_avatar"))),
                "",
            )
            group_info_key = f"group::{_text(item.get('platform_id'))}::{group_id}::"
            cached_info = self._group_info_cache.get(group_info_key)
            if cached_info and cached_info[0] > time.monotonic():
                group_name = _text(cached_info[1].get("group_name")) or group_name
                group_avatar = _http_url(cached_info[1].get("group_avatar")) or group_avatar
            groups[key] = {
                "key": key,
                "adapter_id": item.get("platform_id", ""),
                "adapter_name": item.get("platform_name", ""),
                "group_id": group_id,
                "group_name": group_name,
                "group_avatar": group_avatar,
                "session_id": session_id,
                "session_ids": sorted(
                    {
                        _text(message.get("session_id"))
                        for message in session_messages
                        if _text(message.get("session_id"))
                    }
                ),
                "last_message_at": item.get("created_at", 0),
                "last_preview": self._preview(item.get("chain", [])),
                "pinned": key in self._pinned_groups,
                "can_send": self._can_send(item.get("platform_id", "")),
                "active_reply_enabled": self._active_reply_enabled(item.get("umo", "")),
                "group_id_kind": "group_openid",
                "session_isolated": any(
                    _text(message.get("session_id")) != group_id
                    for message in session_messages
                ),
                "isolation_notice": isolation_enabled,
                "merge_enabled": merge_sessions,
                "profile_supported": bool(
                    self.config.get("show_onebot_profile", True)
                    and self._onebot_platform(_text(item.get("platform_id")))
                ),
                "remark": _text(settings.get("remark")),
                "unread": sum(
                    message.get("role") == "user"
                    and float(message.get("created_at", 0) or 0) > last_read
                    for message in session_messages
                ),
                "unread_mentions": sum(
                    message.get("role") == "user"
                    and message.get("is_mention", False)
                    and float(message.get("created_at", 0) or 0) > last_read
                    for message in session_messages
                ),
                "unread_start_id": next(
                    (
                        message.get("id", "")
                        for message in session_messages
                        if message.get("role") == "user"
                        and float(message.get("created_at", 0) or 0) > last_read
                    ),
                    "",
                ),
                "unread_mention_id": next(
                    (
                        message.get("id", "")
                        for message in session_messages
                        if message.get("role") == "user"
                        and message.get("is_mention", False)
                        and float(message.get("created_at", 0) or 0) > last_read
                    ),
                    "",
                ),
            }
        if self.show_onebot_profile:
            for group in groups.values():
                if group.get("profile_supported") and not group.get("group_name"):
                    self._schedule_group_info(group["adapter_id"], group["group_id"])
        return json_response(
            {
                "groups": sorted(
                    groups.values(),
                    key=lambda x: (not x["pinned"], -float(x["last_message_at"] or 0)),
                )
            }
        )

    def _can_send(self, adapter_id: str) -> bool:
        for platform in self._platforms():
            try:
                if _text(getattr(platform.meta(), "id", "")) == adapter_id:
                    return bool(
                        getattr(platform.meta(), "support_proactive_message", False)
                    )
            except Exception:
                continue
        return False

    def _find_send_target(
        self, adapter_id: str, session_id: str, group_id: str = ""
    ) -> dict[str, Any] | None:
        if self.merge_isolated_sessions and group_id:
            records = self._messages_by_group.get(
                _group_bucket_key(adapter_id, group_id), []
            )
            if records:
                return records[-1]
        records = self._messages_by_session.get(
            f"{adapter_id}::{session_id}", []
        )
        return records[-1] if records else None

    def _active_reply_enabled(self, umo: str) -> bool | None:
        """Read the active-reply flag bound to one unified message origin.

        Args:
            umo: Unified message origin for the group session.

        Returns:
            The session setting, or None when it cannot be read.
        """
        if not umo:
            return None
        try:
            config = self.context.get_config(umo=umo)
            ltm = config.get("provider_ltm_settings", {})
            active_reply = ltm.get("active_reply", {}) if isinstance(ltm, dict) else {}
            if not isinstance(active_reply, dict) or "enable" not in active_reply:
                return None
            value = active_reply["enable"]
            if isinstance(value, bool):
                return value
            if isinstance(value, int) and value in {0, 1}:
                return bool(value)
            if isinstance(value, str):
                normalized = value.strip().lower()
                if normalized in {"true", "1", "yes", "on"}:
                    return True
                if normalized in {"false", "0", "no", "off"}:
                    return False
            return None
        except Exception as exc:
            logger.debug(
                "chat_console: read active-reply config failed for %s: %s", umo, exc
            )
            return None

    def _decorate_chain(
        self, chain: list[dict[str, Any]], names: dict[str, str]
    ) -> list[dict[str, Any]]:
        result = []
        for part in chain:
            copied = dict(part)
            if copied.get("type") == "text":
                text = _text(copied.get("text"))
                for user_id, name in names.items():
                    if name:
                        text = text.replace(f"<@{user_id}>", f"@{name}")
                        text = text.replace(f"<@!{user_id}>", f"@{name}")
                        text = text.replace(f"[At:{user_id}]", f"@{name}")
                copied["text"] = text
            if copied.get("type") == "at" and not _text(copied.get("name")):
                copied["name"] = names.get(_text(copied.get("user_id")), "")
            if copied.get("type") == "reply":
                copied["chain"] = self._decorate_chain(copied.get("chain", []), names)
            result.append(copied)
        return result

    @staticmethod
    def _browser_media_url(url: str) -> str:
        if not url:
            return ""
        if url.startswith(("/AstrBot/data/", "/AstrBot/data\\", "upload://")):
            path = url.removeprefix("upload://")
            if path.startswith("/AstrBot/data/"):
                return (
                    f"/api/v1/plugins/extensions/{PLUGIN_NAME}/media?path={quote(path)}"
                )
            return f"/api/v1/plugins/extensions/{PLUGIN_NAME}/media?path={quote(f'/AstrBot/data/plugin_data/{PLUGIN_NAME}/uploads/{path}')}"
        return url

    async def api_pins(self):
        if request.method == "GET":
            return json_response({"pinned": sorted(self._pinned_groups)})
        payload = await request.json(default={})
        key = _text(payload.get("key")) if isinstance(payload, dict) else ""
        pinned = bool(payload.get("pinned")) if isinstance(payload, dict) else False
        if not key:
            return error_response("群聊 key 不能为空。", status_code=400)
        async with self._lock:
            if pinned:
                self._pinned_groups.add(key)
            else:
                self._pinned_groups.discard(key)
            await self._save_pinned_groups()
        return json_response({"key": key, "pinned": pinned})

    async def api_group_settings(self):
        payload = await request.json(default={}) if request.method == "POST" else {}
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象。", status_code=400)
        key = (
            _text(payload.get("key"))
            if request.method == "POST"
            else _text(request.query.get("key", ""))
        )
        if not key:
            return error_response("群聊 key 不能为空。", status_code=400)
        async with self._lock:
            settings = self._group_settings.setdefault(key, {})
            if request.method == "POST":
                if "remark" in payload:
                    settings["remark"] = _text(payload.get("remark"))[:80]
                if payload.get("read"):
                    settings["last_read"] = time.time()
                if payload.get("delete"):
                    latest = max(
                        (
                            float(message.get("created_at", 0) or 0)
                            for message in self._messages
                            if _conversation_key(message, self.merge_isolated_sessions)
                            == key
                        ),
                        default=time.time(),
                    )
                    settings["hidden_at"] = latest
                await self._save_group_settings()
        return json_response(
            {
                "key": key,
                "remark": _text(settings.get("remark")),
                "last_read": settings.get("last_read", 0),
                "deleted": bool(settings.get("hidden_at")),
            }
        )

    async def api_ui_state(self):
        if request.method == "GET":
            try:
                client_id = uuid.UUID(
                    _text(request.query.get("client_id", ""))
                ).hex
            except (ValueError, AttributeError):
                return error_response("client_id 无效。", status_code=400)
            state_key = f"ui_state:{client_id}"
            state = await asyncio.to_thread(_read_plugin_state, state_key)
            return json_response(state or {"adapter_id": "", "groups_by_adapter": {}})
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象。", status_code=400)
        try:
            client_id = uuid.UUID(_text(payload.get("client_id"))).hex
        except (ValueError, AttributeError):
            return error_response("client_id 无效。", status_code=400)
        state_key = f"ui_state:{client_id}"
        adapter_id = _text(payload.get("adapter_id"))[:200]
        group_key = _text(payload.get("group_key"))[:512]
        state = await asyncio.to_thread(_read_plugin_state, state_key) or {}
        groups_by_adapter = state.get("groups_by_adapter", {})
        if not isinstance(groups_by_adapter, dict):
            groups_by_adapter = {}
        groups_by_adapter = {
            _text(key)[:200]: _text(value)[:512]
            for key, value in groups_by_adapter.items()
            if _text(key) and _text(value)
        }
        if adapter_id and group_key:
            groups_by_adapter[adapter_id] = group_key
        if adapter_id and payload.get("clear_group"):
            groups_by_adapter.pop(adapter_id, None)
        state = {"adapter_id": adapter_id, "groups_by_adapter": groups_by_adapter}
        await asyncio.to_thread(_write_plugin_state, state_key, state)
        return json_response(state)

    async def api_group_info(self):
        adapter_id = _text(request.query.get("adapter_id", ""))
        group_id = _text(request.query.get("group_id", ""))
        if not adapter_id or not group_id:
            return error_response("adapter_id 和 group_id 不能为空。", status_code=400)
        if not self.show_onebot_profile:
            return error_response("OneBot 资料查询已关闭。", status_code=403)
        info = await self._onebot_action("group", adapter_id, group_id)
        if not info:
            return error_response("当前适配器不支持 OneBot 群资料查询。", status_code=404)
        return json_response(info)

    async def api_member_info(self):
        adapter_id = _text(request.query.get("adapter_id", ""))
        group_id = _text(request.query.get("group_id", ""))
        user_id = _text(request.query.get("user_id", ""))
        if not adapter_id or not group_id or not user_id:
            return error_response(
                "adapter_id、group_id 和 user_id 不能为空。", status_code=400
            )
        if not self.show_onebot_profile:
            return error_response("OneBot 资料查询已关闭。", status_code=403)
        info = await self._onebot_action("member", adapter_id, group_id, user_id)
        if not info:
            return error_response("当前适配器不支持 OneBot 成员资料查询。", status_code=404)
        return json_response(info)

    async def api_messages(self):
        adapter_id = _text(request.query.get("adapter_id", ""))
        session_id = _text(request.query.get("session_id", ""))
        group_id = _text(request.query.get("group_id", ""))
        after_id = _text(request.query.get("after_id", ""))
        from_id = _text(request.query.get("from_id", ""))
        try:
            limit = max(1, min(int(request.query.get("limit", 200)), 500))
        except (TypeError, ValueError):
            limit = 200
        if self.merge_isolated_sessions and adapter_id and group_id:
            messages = self._messages_by_group.get(
                _group_bucket_key(adapter_id, group_id), []
            )
        elif adapter_id and session_id:
            messages = self._messages_by_session.get(f"{adapter_id}::{session_id}", [])
        else:
            messages = [
                item
                for item in self._messages
                if (not adapter_id or item.get("platform_id") == adapter_id)
                and (not session_id or item.get("session_id") == session_id)
                and (not group_id or item.get("group_id") == group_id)
            ]
        reset = False
        if from_id:
            start = next(
                (
                    index
                    for index, item in enumerate(messages)
                    if item.get("id") == from_id
                ),
                -1,
            )
            if start < 0:
                reset = True
                page = messages[-limit:]
            else:
                page = messages[start : start + limit]
        elif after_id:
            cursor = next(
                (
                    index
                    for index, item in enumerate(messages)
                    if item.get("id") == after_id
                ),
                -1,
            )
            if cursor < 0:
                reset = True
                page = messages[-limit:]
            else:
                page = messages[cursor + 1 : cursor + 1 + limit]
        else:
            page = messages[-limit:]
        names = {
            _text(item.get("sender_id")): _text(item.get("sender_name"))
            for item in messages
            if _text(item.get("sender_id")) and _text(item.get("sender_name"))
        }
        output = []
        for item in page:
            copied = dict(item)
            copied["chain"] = self._decorate_chain(item.get("chain", []), names)
            for part in copied["chain"]:
                if part.get("type") in {"image", "video", "record", "file"}:
                    part["url"] = self._browser_media_url(_text(part.get("url")))
            output.append(copied)
        return json_response(
            {
                "messages": output,
                "reset": reset,
                "last_id": output[-1].get("id", "") if output else after_id,
            }
        )

    async def api_media(self):
        raw_path = unquote(_text(request.query.get("path", "")))
        target = Path(raw_path).resolve(strict=False)
        allowed_roots = [Path(get_astrbot_temp_path()).resolve()]
        if not target.is_file() or not any(
            target == root or root in target.parents for root in allowed_roots
        ):
            return error_response("媒体不存在或路径不允许访问。", status_code=404)
        return file_response(target)

    async def api_send_media(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象。", status_code=400)
        adapter_id = _text(payload.get("adapter_id"))
        session_id = _text(payload.get("session_id"))
        group_id = _text(payload.get("group_id"))
        image_data = _text(payload.get("data"))
        if not adapter_id or not session_id or not image_data:
            return error_response(
                "adapter_id、session_id 和 data 不能为空。", status_code=400
            )
        if image_data.startswith("data:") and ";base64," in image_data:
            image_data = image_data.split(";base64,", 1)[1]
        try:
            decoded_size = len(base64.b64decode(image_data, validate=True))
        except (ValueError, base64.binascii.Error):
            return error_response("图片数据不是有效的 Base64。", status_code=400)
        if decoded_size > 12 * 1024 * 1024:
            return error_response("图片不能超过 12 MB。", status_code=413)
        target = self._find_send_target(adapter_id, session_id, group_id)
        if not target:
            return error_response("未找到该群聊会话。", status_code=404)
        if not self._can_send(adapter_id):
            return error_response("当前适配器不支持主动发言。", status_code=403)
        sent, error, status = await self._queue_send(
            f"{adapter_id}::{target.get('group_id') or session_id}",
            _text(target.get("umo")),
            MessageChain([Image.fromBase64(image_data)]),
        )
        if not sent:
            return error_response(error, status_code=status)
        await self._store_message(
            {
                "id": uuid.uuid4().hex,
                "created_at": time.time(),
                "platform_id": adapter_id,
                "platform_name": target.get("platform_name", ""),
                "umo": target.get("umo", ""),
                "session_id": session_id,
                "message_type": target.get("message_type", "GroupMessage"),
                "group_id": target.get("group_id", session_id),
                "group_name": target.get("group_name", ""),
                "group_avatar": target.get("group_avatar", ""),
                "role": "bot",
                "sender_id": "",
                "sender_name": "机器人",
                "avatar": "",
                "chain": [{"type": "image", "url": "", "label": "控制台发送的图片"}],
            }
        )
        return json_response({"sent": True})

    async def api_send(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象。", status_code=400)
        adapter_id = _text(payload.get("adapter_id"))
        session_id = _text(payload.get("session_id"))
        group_id = _text(payload.get("group_id"))
        content = _text(payload.get("content"))
        image_data = _text(payload.get("image_data"))
        if not adapter_id or not session_id or (not content and not image_data):
            return error_response(
                "adapter_id、session_id 和 content 或 image_data 不能为空。",
                status_code=400,
            )
        target = self._find_send_target(adapter_id, session_id, group_id)
        if not target:
            return error_response(
                "未找到该适配器的群聊会话，请先让机器人收到该群消息。", status_code=404
            )
        if not self._can_send(adapter_id):
            return error_response("当前适配器不支持主动发言。", status_code=403)
        outgoing = []
        if image_data:
            if image_data.startswith("data:") and ";base64," in image_data:
                image_data = image_data.split(";base64,", 1)[1]
            try:
                decoded_size = len(base64.b64decode(image_data, validate=True))
            except (ValueError, base64.binascii.Error):
                return error_response("图片数据不是有效的 Base64。", status_code=400)
            if decoded_size > 15 * 1024 * 1024:
                return error_response("图片不能超过 15 MB。", status_code=413)
            outgoing.append(Image.fromBase64(image_data))
        if content:
            outgoing.append(Plain(content))
        umo = _text(target.get("umo"))
        sent, error, status = await self._queue_send(
            f"{adapter_id}::{target.get('group_id') or session_id}",
            umo,
            MessageChain(outgoing),
        )
        if not sent:
            return error_response(error, status_code=status)
        await self._store_message(
            {
                "id": uuid.uuid4().hex,
                "created_at": time.time(),
                "platform_id": adapter_id,
                "platform_name": target.get("platform_name", ""),
                "umo": umo,
                "session_id": session_id,
                "message_type": target.get("message_type", "GroupMessage"),
                "group_id": target.get("group_id", session_id),
                "group_name": target.get("group_name", ""),
                "group_avatar": target.get("group_avatar", ""),
                "role": "bot",
                "sender_id": "",
                "sender_name": "机器人",
                "avatar": "",
                "chain": (
                    [{"type": "image", "url": "", "label": "控制台发送的图片"}]
                    if image_data
                    else []
                )
                + ([{"type": "text", "text": content}] if content else []),
            }
        )
        return json_response({"sent": True})

    @staticmethod
    def _preview(chain: list[dict[str, Any]]) -> str:
        parts = []
        for item in chain:
            kind = item.get("type")
            if kind == "text":
                parts.append(item.get("text", ""))
            elif kind == "at":
                parts.append(f"@{item.get('name') or item.get('user_id')}")
            elif kind == "reply":
                parts.append(f"[引用] {item.get('text', '')}")
            else:
                parts.append(f"[{kind}]")
        return " ".join(str(part) for part in parts if part)[:120]

    async def terminate(self):
        self._stopping = True
        if self._group_info_workers:
            for _ in self._group_info_workers:
                await self._group_info_queue.put(None)
            await asyncio.gather(*self._group_info_workers, return_exceptions=True)
            self._group_info_workers.clear()
        if self._send_workers:
            await self._send_queue.join()
            for _ in self._send_workers:
                await self._send_queue.put(None)
            await asyncio.gather(*self._send_workers, return_exceptions=True)
            self._send_workers.clear()
        self._persistence_closing = True
        if self._persist_task and not self._persist_task.done():
            await self._persist_queue.put(None)
            await self._persist_task
        else:
            await self._save_messages()
