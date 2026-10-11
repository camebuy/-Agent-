"""Canonical locations for Agent state and user documents."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from config.settings import settings

logger = logging.getLogger(__name__)
_MIGRATION_LOCK = threading.RLock()


class PathManager:
    """Single source of truth for Agent-owned and user-owned data paths.

    User documents live directly under ``data/users``. Agent state is organized
    by operational domain under ``data/agent``. Legacy locations are migration
    sources only and are never used by runtime features.
    """

    # 下列相对路径以 settings.DATA_DIR 为起点；默认是项目根目录的 data/。
    DATA_ROOT: Path = settings.DATA_DIR  # data/｜统一数据根目录，可由 AACWORK_DATA_DIR 覆盖
    DATA_DIR: Path = DATA_ROOT  # data/｜旧调用方兼容别名

    AGENT_DIR: Path = DATA_ROOT / "agent"  # data/agent/｜Agent 自身数据
    USERS_DIR: Path = DATA_ROOT / "users"  # data/users/｜用户原始文件，不按任务复制

    CONFIG_DIR: Path = AGENT_DIR / "config"  # data/agent/config/｜运行配置
    RUNTIME_SETTINGS_FILE: Path = CONFIG_DIR / "runtime_settings.json"  # data/agent/config/runtime_settings.json｜模型及工具开关
    MEMORY_DIR: Path = AGENT_DIR / "memory"  # data/agent/memory/｜结构化记忆
    USER_PROFILE_FILE: Path = MEMORY_DIR / "user_profile.json"  # data/agent/memory/user_profile.json｜用户档案
    EPISODIC_MEMORY_FILE: Path = MEMORY_DIR / "episodic_memory.json"  # data/agent/memory/episodic_memory.json｜事件记忆
    ACTIVE_CONTEXT_FILE: Path = MEMORY_DIR / "active_context.json"  # data/agent/memory/active_context.json｜近期上下文
    SYSTEM_PROMPT_FILE: Path = CONFIG_DIR / "system_prompt.json"  # data/agent/config/system_prompt.json｜系统提示词

    LOG_DIR: Path = AGENT_DIR / "logs"  # data/agent/logs/｜可读运行日志
    TERMINAL_LOG_FILE: Path = LOG_DIR / "terminal_events.json"  # data/agent/logs/terminal_events.json｜终端事件
    CONVERSATIONS_DIR: Path = AGENT_DIR / "conversations"  # data/agent/conversations/｜对话与作业
    CHAT_RUNTIME_DB: Path = CONVERSATIONS_DIR / "chat_runtime.sqlite3"  # data/agent/conversations/chat_runtime.sqlite3｜聊天队列；同名 .lock 为进程锁
    CHAT_ARCHIVE_DIR: Path = CONVERSATIONS_DIR / "archive"  # data/agent/conversations/archive/｜预留归档目录

    AUDIT_DIR: Path = AGENT_DIR / "audit"  # data/agent/audit/｜工具执行凭证
    LEDGER_DB: Path = AUDIT_DIR / "execution_ledger.db"  # data/agent/audit/execution_ledger.db｜物理执行账本

    OPERATIONS_DIR: Path = AGENT_DIR / "operations"  # data/agent/operations/｜日程、卡片、招聘记录
    LEGACY_AGENT_SCHEDULE_FILE: Path = OPERATIONS_DIR / "schedules.json"  # data/agent/operations/schedules.json｜旧版导入来源
    LEGACY_AGENT_INBOX_FILE: Path = OPERATIONS_DIR / "inbox_cards.json"  # data/agent/operations/inbox_cards.json｜旧版导入来源
    DATA_CARDS_DIR: Path = OPERATIONS_DIR / "tasks"  # data/agent/operations/tasks/｜任务文件引用卡
    INBOX_DIR: Path = OPERATIONS_DIR / "decision_center"  # data/agent/operations/decision_center/｜决策卡目录
    INBOX_FILE: Path = INBOX_DIR / "inbox_cards.json"  # data/agent/operations/decision_center/inbox_cards.json｜决策卡
    SCHEDULE_DIR: Path = OPERATIONS_DIR / "schedules"  # data/agent/operations/schedules/｜日程目录
    SCHEDULE_FILE: Path = SCHEDULE_DIR / "schedules.json"  # data/agent/operations/schedules/schedules.json｜固定与循环日程
    RECRUITMENT_SYS_DIR: Path = OPERATIONS_DIR / "recruitment"  # data/agent/operations/recruitment/｜招聘状态目录
    RECRUITMENT_FILE: Path = RECRUITMENT_SYS_DIR / "recruitment.json"  # data/agent/operations/recruitment/recruitment.json｜岗位、投递及回复记录

    CATALOG_DIR: Path = AGENT_DIR / "catalog"  # data/agent/catalog/｜用户文件属性目录
    FILE_CATALOG_DB: Path = CATALOG_DIR / "file_catalog.sqlite3"  # data/agent/catalog/file_catalog.sqlite3｜文件 ID、摘要和标签
    MANIFEST_FILE: Path = CATALOG_DIR / "legacy_manifest.json"  # data/agent/catalog/legacy_manifest.json｜旧目录导入来源
    VECTOR_INDEX_DIR: Path = AGENT_DIR / "indexes" / "documents"  # data/agent/indexes/documents/｜通用 RAG 索引
    CARD_VECTOR_INDEX_DIR: Path = VECTOR_INDEX_DIR / "cards"  # data/agent/indexes/documents/cards/｜按任务卡隔离的索引
    STATS_DIR: Path = AGENT_DIR / "usage"  # data/agent/usage/｜用量统计
    TOKEN_FILE: Path = STATS_DIR / "token_stats.json"  # data/agent/usage/token_stats.json｜输入与输出 Token 统计
    DOCUMENT_OUTPUT_DIR: Path = AGENT_DIR / "artifacts" / "documents"  # data/agent/artifacts/documents/｜Agent 生成的文档及修订版
    DESKTOP_CAPTURE_DIR: Path = AGENT_DIR / "artifacts" / "screenshots"  # data/agent/artifacts/screenshots/｜用户请求的窗口截图
    BROWSER_PROFILE_DIR: Path = AGENT_DIR / "browser" / "profile"  # data/agent/browser/profile/｜旧浏览器资料迁移目标
    BROWSER_CONTEXT_DIR: Path = AGENT_DIR / "browser" / "context"  # data/agent/browser/context/｜旧浏览器上下文迁移目标
    RECOVERY_DIR: Path = AGENT_DIR / "recovery"  # data/agent/recovery/｜无法归类的旧数据留存

    # 以下 data/ 旧路径只供迁移读取；当前功能不再直接写入。
    LEGACY_INTERNAL_DIR: Path = DATA_ROOT / "_internal"  # data/_internal/｜旧内部目录
    LEGACY_MEMORY_DIR: Path = DATA_ROOT / "memory"  # data/memory/｜旧记忆目录
    LEGACY_LOG_DIR: Path = DATA_ROOT / "logs"  # data/logs/｜旧日志目录
    LEGACY_CHAT_RUNTIME_DB: Path = DATA_ROOT / "chat_runtime.sqlite3"  # data/chat_runtime.sqlite3｜旧聊天库
    LEGACY_CHAT_HISTORY_DIR: Path = DATA_ROOT / "chat_history"  # data/chat_history/｜旧对话记录
    LEGACY_DATA_CARDS_DIR: Path = DATA_ROOT / "data_cards"  # data/data_cards/｜旧任务卡
    LEGACY_INBOX_DIR: Path = DATA_ROOT / "inbox"  # data/inbox/｜旧决策卡目录
    LEGACY_INBOX_FILE: Path = LEGACY_INBOX_DIR / "inbox_cards.json"  # data/inbox/inbox_cards.json｜旧决策卡
    LEGACY_SCHEDULE_DIR: Path = DATA_ROOT / "schedules"  # data/schedules/｜旧日程目录
    LEGACY_STATS_DIR: Path = DATA_ROOT / "stats"  # data/stats/｜旧用量统计
    LEGACY_WORKSPACE_DIR: Path = DATA_ROOT / "workspace"  # data/workspace/｜旧文件工作区
    LEGACY_BROWSER_PROFILE_DIR: Path = DATA_ROOT / "browser-profile"  # data/browser-profile/｜旧浏览器资料
    LEGACY_BROWSER_CONTEXT_DIR: Path = DATA_ROOT / "browser_context"  # data/browser_context/｜旧浏览器上下文

    @classmethod
    def init_storage_structure(cls) -> None:
        for path in (
            cls.AGENT_DIR, cls.USERS_DIR, cls.CONFIG_DIR, cls.MEMORY_DIR,
            cls.LOG_DIR, cls.CONVERSATIONS_DIR, cls.CHAT_ARCHIVE_DIR,
            cls.AUDIT_DIR, cls.OPERATIONS_DIR, cls.DATA_CARDS_DIR,
            cls.INBOX_DIR, cls.SCHEDULE_DIR, cls.RECRUITMENT_SYS_DIR, cls.CATALOG_DIR,
            cls.VECTOR_INDEX_DIR, cls.STATS_DIR, cls.DOCUMENT_OUTPUT_DIR,
            cls.DESKTOP_CAPTURE_DIR,
        ):
            path.mkdir(parents=True, exist_ok=True)
        with _MIGRATION_LOCK:
            cls._migrate_legacy_layout()

    @staticmethod
    def _atomic_json(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
                temp = Path(stream.name)
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)

    @classmethod
    def _migration_inputs(cls, paths: Iterable[Path], destination: Path, expected: type) -> list[tuple[Path, Any]]:
        """Prefer active records and retain every unreadable legacy source for recovery."""
        sources = list(dict.fromkeys([destination, *paths]))
        if destination.exists() and not any(path.exists() for path in sources if path != destination):
            return []  # Normal startup must not rewrite live state.
        parsed = []
        for source in sources:
            if not source.exists():
                continue
            try:
                value = json.loads(source.read_text(encoding="utf-8"))
                if not isinstance(value, expected):
                    raise ValueError("unexpected record format")
                parsed.append((source, value))
            except (OSError, ValueError, TypeError) as exc:
                logger.warning("Migration retained unreadable file %s: %s", source, exc)
                if source == destination:
                    return []  # Never replace an unreadable active store with older state.
        return parsed

    @staticmethod
    def _retire_migrated(sources: Iterable[Path], destination: Path) -> None:
        for source in sources:
            if source != destination:
                try:
                    source.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Migration could not retire %s; retained for retry", source)

    @classmethod
    def _merge_record_lists(cls, paths: Iterable[Path], destination: Path, bucket_keys: tuple[str, ...] = ()) -> None:
        inputs = cls._migration_inputs(paths, destination, dict)
        if not inputs:
            return
        merged: dict[str, list] = {key: [] for key in bucket_keys}
        seen: dict[str, set[str]] = {key: set() for key in bucket_keys}
        consumed = []
        for source, data in inputs:
            if any(not isinstance(data.get(key, []), list) or
                   any(not isinstance(record, dict) for record in data.get(key, [])) for key in bucket_keys):
                logger.warning("Migration retained malformed records in %s", source)
                if source == destination:
                    return
                continue
            consumed.append(source)
            for key, value in data.items():
                if key not in bucket_keys:
                    merged.setdefault(key, value)
            for key in bucket_keys:
                records = data.get(key, [])
                if not isinstance(records, list):
                    continue
                for record in records:
                    if not isinstance(record, dict):
                        continue
                    identity = str(record.get("id") or record.get("card_id") or "").strip()
                    if not identity:
                        identity = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
                    if identity not in seen[key]:
                        merged[key].append(record)
                        seen[key].add(identity)
        if any(merged.values()) or not destination.exists():
            cls._atomic_json(destination, merged)
        cls._retire_migrated(consumed, destination)

    @classmethod
    def _merge_inbox(cls, sources: list[Path], destination: Path) -> None:
        inputs = cls._migration_inputs(sources, destination, list)
        if not inputs:
            return
        merged: list[dict] = []
        seen: set[str] = set()
        consumed = []
        for source, records in inputs:
            if any(not isinstance(record, dict) for record in records):
                logger.warning("Migration retained malformed records in %s", source)
                if source == destination:
                    return
                continue
            consumed.append(source)
            for record in records:
                if not isinstance(record, dict):
                    continue
                key = str(record.get("card_id") or "").strip()
                if not key:
                    key = hashlib.sha256(json.dumps(record, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
                if key not in seen:
                    merged.append(record)
                    seen.add(key)
        if merged or not destination.exists():
            cls._atomic_json(destination, merged)
        cls._retire_migrated(consumed, destination)

    @classmethod
    def _merge_token_stats(cls, sources: list[Path], destination: Path) -> None:
        inputs = cls._migration_inputs(sources, destination, dict)
        if not inputs:
            return
        merged: dict[str, Any] = {}
        daily: dict[str, dict] = {}
        for source, data in inputs:
            for key, value in data.items():
                if key == "daily_records" and isinstance(value, dict):
                    for day, row in value.items():
                        if isinstance(row, dict):
                            current = daily.setdefault(str(day), {})
                            for metric, amount in row.items():
                                if isinstance(amount, (int, float)):
                                    current[metric] = max(current.get(metric, 0), amount)
                                else:
                                    current.setdefault(metric, amount)
                elif key in ("daily", "daily_stats", "by_day") and isinstance(value, list):
                    for row in value:
                        if isinstance(row, dict):
                            day = str(row.get("date") or row.get("day") or "")
                            if day:
                                current = daily.setdefault(day, {})
                                for metric, amount in row.items():
                                    if isinstance(amount, (int, float)):
                                        current[metric] = max(current.get(metric, 0), amount)
                                    else:
                                        current.setdefault(metric, amount)
                elif isinstance(value, (int, float)):
                    merged[key] = max(merged.get(key, 0), value)
                else:
                    merged.setdefault(key, value)
        # Keep only the shape expected by the active tracker while preserving extra fields.
        merged.setdefault("total_all_time", 0)
        for row in daily.values():
            if "prompt_tokens" not in row and "input_tokens" in row:
                row["prompt_tokens"] = row["input_tokens"]
            if "completion_tokens" not in row and "output_tokens" in row:
                row["completion_tokens"] = row["output_tokens"]
            if "total_tokens" not in row:
                row["total_tokens"] = int(row.get("prompt_tokens", 0) or 0) + int(row.get("completion_tokens", 0) or 0)
            row.pop("input_tokens", None)
            row.pop("output_tokens", None)
            row.pop("date", None)
            row.pop("day", None)
        merged["daily_records"] = {day: daily[day] for day in sorted(daily)}
        if merged or not destination.exists():
            cls._atomic_json(destination, merged)
        cls._retire_migrated((path for path, _ in inputs), destination)

    @classmethod
    def _merge_legacy_chat_history(cls, source: Path) -> bool:
        """Import an old JSON chat transcript into the active SQLite history."""
        try:
            raw = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        messages = raw.get("messages", []) if isinstance(raw, dict) else raw
        if not isinstance(messages, list):
            return False
        normalized = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "").strip().lower()
            role = {"human": "user", "ai": "assistant"}.get(role, role)
            content = message.get("content")
            if role not in ("user", "assistant") or content is None:
                continue
            normalized.append((role, str(content), str(message.get("created_at") or datetime.now(timezone.utc).isoformat(timespec="seconds"))))
        if not normalized:
            return False
        try:
            cls.CHAT_RUNTIME_DB.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(cls.CHAT_RUNTIME_DB, timeout=30) as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS conversations (
                        conversation_id TEXT PRIMARY KEY,
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS chat_messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        conversation_id TEXT NOT NULL,
                        role TEXT NOT NULL,
                        content TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        job_id TEXT,
                        UNIQUE(job_id, role)
                    );
                    """
                )
                now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                db.execute("INSERT OR IGNORE INTO conversations(conversation_id,created_at) VALUES('default',?)", (now,))
                existing = {
                    (str(row[0]), str(row[1]))
                    for row in db.execute("SELECT role,content FROM chat_messages WHERE conversation_id='default'")
                }
                inserted = set()
                for role, content, created_at in normalized:
                    identity = (role, content)
                    if identity in existing or identity in inserted:
                        continue
                    db.execute(
                        "INSERT INTO chat_messages(conversation_id,role,content,created_at,job_id) VALUES('default',?,?,?,NULL)",
                        (role, content, created_at),
                    )
                    inserted.add(identity)
            return True
        except (OSError, sqlite3.Error):
            return False

    @classmethod
    def _move_tree(cls, source: Path, destination: Path) -> None:
        """Move legacy tree contents without overwriting; retryable when files are locked."""
        if not source.exists():
            return
        destination.mkdir(parents=True, exist_ok=True)
        for root, dirs, files in os.walk(source, topdown=True):
            root_path = Path(root)
            relative = root_path.relative_to(source)
            target_root = destination / relative
            target_root.mkdir(parents=True, exist_ok=True)
            dirs[:] = [name for name in dirs if not (root_path / name).is_symlink()]
            for name in files:
                old_file = root_path / name
                new_file = target_root / name
                if new_file.exists():
                    try:
                        if old_file.is_file() and new_file.is_file() and old_file.stat().st_size == new_file.stat().st_size:
                            if hashlib.sha256(old_file.read_bytes()).digest() == hashlib.sha256(new_file.read_bytes()).digest():
                                old_file.unlink()
                                continue
                    except OSError:
                        continue
                    # Preserve a distinct conflicting file under an explicit recovery area.
                    digest = hashlib.sha256(str(old_file).encode("utf-8", errors="replace")).hexdigest()[:8]
                    new_file = target_root / f"{old_file.stem}.legacy-{digest}{old_file.suffix}"
                    if new_file.exists():
                        continue
                try:
                    os.replace(old_file, new_file)
                except OSError:
                    try:
                        shutil.copy2(old_file, new_file)
                        if old_file.stat().st_size == new_file.stat().st_size:
                            old_file.unlink()
                    except OSError:
                        continue
        for root, dirs, files in os.walk(source, topdown=False):
            try:
                Path(root).rmdir()
            except OSError:
                pass

    @classmethod
    def _flatten_legacy_users(cls) -> None:
        source = cls.LEGACY_WORKSPACE_DIR
        if not source.exists():
            return
        for root, _, files in os.walk(source):
            for name in files:
                old_file = Path(root) / name
                if old_file.is_symlink() or name.startswith("."):
                    continue
                target = cls.USERS_DIR / name
                if target.exists():
                    try:
                        if target.stat().st_size == old_file.stat().st_size and hashlib.sha256(target.read_bytes()).digest() == hashlib.sha256(old_file.read_bytes()).digest():
                            old_file.unlink()
                            continue
                    except OSError:
                        continue
                    digest = hashlib.sha256(str(old_file).encode("utf-8", errors="replace")).hexdigest()[:8]
                    target = cls.USERS_DIR / f"{old_file.stem}__{digest}{old_file.suffix}"
                    if target.exists():
                        continue
                try:
                    os.replace(old_file, target)
                except OSError:
                    try:
                        shutil.copy2(old_file, target)
                        old_file.unlink()
                    except OSError:
                        pass
        for root, dirs, files in os.walk(source, topdown=False):
            try:
                Path(root).rmdir()
            except OSError:
                pass

    @staticmethod
    def _remove_empty_tree(root: Path) -> None:
        if not root.exists():
            return
        for current, _, _ in os.walk(root, topdown=False):
            try:
                Path(current).rmdir()
            except OSError:
                pass

    @classmethod
    def _migrate_legacy_layout(cls) -> None:
        # Merge structured records before moving any remaining legacy files.
        schedule_sources = [
            cls.LEGACY_INTERNAL_DIR / "schedules" / "schedules.json",
            cls.LEGACY_SCHEDULE_DIR / "schedules.json",
            cls.LEGACY_AGENT_SCHEDULE_FILE,
            cls.SCHEDULE_FILE,
        ]
        cls._merge_record_lists(schedule_sources, cls.SCHEDULE_FILE, ("fixed_schedules", "recurring_schedules"))

        inbox_sources = [
            cls.LEGACY_INTERNAL_DIR / "inbox" / "inbox_cards.json",
            cls.LEGACY_INBOX_FILE,
            cls.LEGACY_AGENT_INBOX_FILE,
            cls.INBOX_FILE,
        ]
        cls._merge_inbox(inbox_sources, cls.INBOX_FILE)
        old_inbox_marker = cls.LEGACY_INTERNAL_DIR / "inbox" / ".legacy_inbox_migrated"
        try:
            old_inbox_marker.unlink(missing_ok=True)
        except OSError:
            pass

        stats_sources = [
            cls.LEGACY_INTERNAL_DIR / "stats" / "token_stats.json",
            cls.LEGACY_STATS_DIR / "token_usage.json",
            cls.TOKEN_FILE,
        ]
        cls._merge_token_stats(stats_sources, cls.TOKEN_FILE)

        # Move single-owner Agent state into its canonical domain directory.
        cls._move_tree(cls.LEGACY_MEMORY_DIR, cls.MEMORY_DIR)
        # Older system prompt was stored alongside memory; keep it in config.
        old_prompt = cls.MEMORY_DIR / "system_prompt.json"
        if old_prompt.exists():
            new_prompt = cls.SYSTEM_PROMPT_FILE
            if not new_prompt.exists():
                try:
                    os.replace(old_prompt, new_prompt)
                except OSError:
                    pass
            else:
                try:
                    if old_prompt.read_bytes() == new_prompt.read_bytes():
                        old_prompt.unlink()
                    else:
                        # A legacy customization can differ from the active
                        # prompt. Keep it available for manual comparison.
                        cls.RECOVERY_DIR.mkdir(parents=True, exist_ok=True)
                        target = cls.RECOVERY_DIR / "legacy_system_prompt.json"
                        if target.exists():
                            target = cls.RECOVERY_DIR / f"legacy_system_prompt_{hashlib.sha256(old_prompt.read_bytes()).hexdigest()[:12]}.json"
                        if not target.exists():
                            os.replace(old_prompt, target)
                except OSError:
                    pass
        cls._move_tree(cls.LEGACY_LOG_DIR, cls.LOG_DIR)

        old_db = cls.LEGACY_CHAT_RUNTIME_DB
        # Sidecar files belong to their original database. Never attach an old
        # WAL to an already existing destination database.
        for suffix in (() if cls.CHAT_RUNTIME_DB.exists() else ("", "-wal", "-shm")):
            source_db = Path(str(old_db) + suffix)
            target_db = Path(str(cls.CHAT_RUNTIME_DB) + suffix)
            if source_db.exists() and not target_db.exists():
                try:
                    os.replace(source_db, target_db)
                except OSError:
                    pass
        # Preserve the old browser session transcript as an archive, not as a second active store.
        old_session = cls.LEGACY_CHAT_HISTORY_DIR / "web_session.json"
        if not old_session.exists():
            archived_session = cls.CHAT_ARCHIVE_DIR / "web_session.json"
            if archived_session.exists():
                old_session = archived_session
        imported_legacy_chat = old_session.exists() and cls._merge_legacy_chat_history(old_session)
        if imported_legacy_chat:
            try:
                old_session.unlink(missing_ok=True)
            except OSError:
                pass
            try:
                cls.LEGACY_CHAT_HISTORY_DIR.rmdir()
            except OSError:
                pass
        elif cls.LEGACY_CHAT_HISTORY_DIR.exists():
            cls._move_tree(cls.LEGACY_CHAT_HISTORY_DIR, cls.CHAT_ARCHIVE_DIR)

        cls._move_tree(cls.LEGACY_INTERNAL_DIR / "ledger", cls.AUDIT_DIR)
        cls._move_tree(cls.LEGACY_INTERNAL_DIR / "cards", cls.DATA_CARDS_DIR)
        cls._move_tree(cls.LEGACY_INTERNAL_DIR / "manifest", cls.CATALOG_DIR)
        moved_manifest = cls.CATALOG_DIR / "global_manifest.json"
        if moved_manifest.exists() and not cls.MANIFEST_FILE.exists():
            try:
                os.replace(moved_manifest, cls.MANIFEST_FILE)
            except OSError:
                pass
        cls._move_tree(cls.LEGACY_INTERNAL_DIR / "vector_store", cls.VECTOR_INDEX_DIR)
        cls._move_tree(cls.LEGACY_INTERNAL_DIR / "recruitment", cls.RECRUITMENT_SYS_DIR)
        cls._move_tree(cls.LEGACY_INTERNAL_DIR / "schedules", cls.SCHEDULE_DIR)
        cls._move_tree(cls.LEGACY_INTERNAL_DIR / "stats", cls.STATS_DIR)
        cls._move_tree(cls.LEGACY_DATA_CARDS_DIR, cls.DATA_CARDS_DIR)
        cls._move_tree(cls.LEGACY_SCHEDULE_DIR, cls.SCHEDULE_DIR)
        cls._move_tree(cls.LEGACY_STATS_DIR, cls.STATS_DIR)
        if cls.LEGACY_INBOX_DIR.exists():
            try:
                if any(cls.LEGACY_INBOX_DIR.iterdir()):
                    cls._move_tree(cls.LEGACY_INBOX_DIR, cls.INBOX_DIR / "archive")
            except OSError:
                pass

        # User documents stay together. Legacy categorized paths are flattened once;
        # naming collisions get a stable suffix so no file is overwritten.
        cls._flatten_legacy_users()

        # Browser profile data belongs to the Agent runtime. A running browser can
        # lock it; in that case the old path is retained and this migration retries.
        if cls.LEGACY_BROWSER_PROFILE_DIR.exists() and not cls.BROWSER_PROFILE_DIR.exists():
            lock_names = {"SingletonLock", "SingletonCookie", "SingletonSocket"}
            try:
                profile_locked = any(item.name in lock_names for item in cls.LEGACY_BROWSER_PROFILE_DIR.rglob("*"))
            except OSError:
                profile_locked = True
            if not profile_locked:
                cls.BROWSER_PROFILE_DIR.parent.mkdir(parents=True, exist_ok=True)
                try:
                    # A same-volume directory rename keeps a live profile consistent.
                    os.replace(cls.LEGACY_BROWSER_PROFILE_DIR, cls.BROWSER_PROFILE_DIR)
                except OSError:
                    # A running browser may hold the profile; retry on a later app start.
                    pass
        elif cls.LEGACY_BROWSER_PROFILE_DIR.exists() and cls.BROWSER_PROFILE_DIR.exists():
            cls._move_tree(cls.LEGACY_BROWSER_PROFILE_DIR, cls.BROWSER_PROFILE_DIR)
        if cls.LEGACY_BROWSER_CONTEXT_DIR.exists():
            cls._move_tree(cls.LEGACY_BROWSER_CONTEXT_DIR, cls.BROWSER_CONTEXT_DIR)

        # Sweep unrecognized Agent-owned legacy files into one recovery folder.
        # This is a move-only fallback; it never deletes unknown content.
        if cls.LEGACY_INTERNAL_DIR.exists():
            try:
                has_files = any(
                    item.is_file() and not item.is_symlink()
                    for item in cls.LEGACY_INTERNAL_DIR.rglob("*")
                )
                if has_files:
                    cls._move_tree(cls.LEGACY_INTERNAL_DIR, cls.RECOVERY_DIR / "legacy_internal")
            except OSError:
                pass
        recovery_internal = cls.RECOVERY_DIR / "legacy_internal"
        if recovery_internal.exists():
            try:
                has_recovery_files = any(
                    item.is_file() and not item.is_symlink()
                    for item in recovery_internal.rglob("*")
                )
                if not has_recovery_files:
                    cls._remove_empty_tree(recovery_internal)
            except OSError:
                pass

        # Remove only empty legacy containers. Non-empty/locked data is left for retry.
        for old_dir in (
            cls.LEGACY_INTERNAL_DIR / "inbox", cls.LEGACY_INTERNAL_DIR,
            cls.LEGACY_INBOX_DIR, cls.LEGACY_SCHEDULE_DIR, cls.LEGACY_STATS_DIR,
            cls.LEGACY_DATA_CARDS_DIR,
        ):
            try:
                old_dir.rmdir()
            except OSError:
                pass


PathManager.init_storage_structure()
