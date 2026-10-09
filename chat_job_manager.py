from __future__ import annotations

import asyncio
import json
import os
import queue
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional


ACTIVE_STATUSES = ("queued", "running")
TERMINAL_STATUSES = ("completed", "failed", "interrupted", "cancelled", "missed")
DEFAULT_CONVERSATION_ID = "default"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class JobRequest:
    job_id: str
    conversation_id: str
    user_message: str
    task_id: str
    source: str = "manual"
    parent_job_id: Optional[str] = None
    schedule_id: Optional[str] = None
    approval_card_ids: tuple[str, ...] = ()


class ChatJobManager:
    """把 AI 请求从 Streamlit 脚本生命周期中剥离出来。

    设计原则：
    1. UI 线程只负责提交任务和读取状态，不直接等待 LLM。
    2. 独立 worker thread + 持久 Event Loop，页面 rerun 不会终止任务。
    3. SQLite 保存任务和消息，切页、刷新后仍能找回结果。
    4. 单 worker 串行执行，避免共享 ChatManager/工具链发生并发竞态。
    5. 启动时只恢复从未开始的 queued job；running job 标记为 interrupted，避免盲目重放副作用。
    """

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.instance_id = uuid.uuid4().hex
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._instance_lock = None
        self._acquire_instance_lock()
        self._queue: queue.Queue[JobRequest | None] = queue.Queue()
        self._stop_event = threading.Event()
        self._worker_ready = threading.Event()
        self._worker = threading.Thread(
            target=self._worker_main,
            name="hui-feng-chat-worker",
            daemon=True,
        )
        try:
            self._init_db()
            recovered_jobs = self._recover_incomplete_jobs()
        except BaseException:
            self._release_instance_lock()
            raise
        for recovered_job in recovered_jobs:
            self._queue.put(recovered_job)
        try:
            self._worker.start()
        except BaseException:
            self._release_instance_lock()
            raise
        self._worker_ready.wait(timeout=10)

    # ------------------------------------------------------------------
    # SQLite helpers
    # ------------------------------------------------------------------
    def _acquire_instance_lock(self) -> None:
        """Only one worker may recover/execute jobs in this runtime database."""
        handle = self.db_path.with_suffix(self.db_path.suffix + ".lock").open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError) as exc:
            handle.close()
            raise RuntimeError("任务数据库已由另一个应用实例使用，请先关闭另一个 Streamlit 服务。") from exc
        self._instance_lock = handle

    def _release_instance_lock(self) -> None:
        handle = getattr(self, "_instance_lock", None)
        if handle is not None:
            # Closing the descriptor releases the OS lock, including after a crash.
            handle.close()
            self._instance_lock = None

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA synchronous=NORMAL")
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
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

                CREATE TABLE IF NOT EXISTS chat_jobs (
                    job_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    user_message TEXT NOT NULL,
                    task_id TEXT,
                    source TEXT NOT NULL DEFAULT 'manual',
                    parent_job_id TEXT,
                    schedule_id TEXT,
                    idempotency_key TEXT,
                    approval_card_ids TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL,
                    heartbeat_at TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    reply TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_messages_conversation
                    ON chat_messages(conversation_id, id);
                CREATE INDEX IF NOT EXISTS idx_jobs_conversation
                    ON chat_jobs(conversation_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_jobs_active
                    ON chat_jobs(conversation_id, status);

                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    request_text TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'manual',
                    schedule_id TEXT,
                    status TEXT NOT NULL,
                    latest_job_id TEXT,
                    progress_message TEXT,
                    heartbeat_at TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_updated ON tasks(updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
                """
            )
            existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(chat_jobs)")}
            for column, definition in (
                ("task_id", "TEXT"),
                ("source", "TEXT NOT NULL DEFAULT 'manual'"),
                ("parent_job_id", "TEXT"),
                ("schedule_id", "TEXT"),
                ("idempotency_key", "TEXT"),
                ("heartbeat_at", "TEXT"),
                ("cancel_requested", "INTEGER NOT NULL DEFAULT 0"),
                ("approval_card_ids", "TEXT NOT NULL DEFAULT '[]'"),
            ):
                if column not in existing_columns:
                    conn.execute(f"ALTER TABLE chat_jobs ADD COLUMN {column} {definition}")
            task_columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
            for column, definition in (
                ("progress_message", "TEXT"),
                ("heartbeat_at", "TEXT"),
                ("cancel_requested", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if column not in task_columns:
                    conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} {definition}")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_idempotency "
                "ON chat_jobs(idempotency_key) WHERE idempotency_key IS NOT NULL"
            )

    def _recover_incomplete_jobs(self) -> list[JobRequest]:
        """Requeue jobs known not to have started; quarantine jobs that may have side effects."""
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE chat_jobs
                SET status = 'interrupted',
                    error = COALESCE(error, 'Application restarted before this task completed.'),
                    finished_at = COALESCE(finished_at, ?), heartbeat_at=NULL
                WHERE status = 'running'
                """,
                (now,),
            )
            interrupted = conn.execute(
                """
                SELECT j.job_id, j.conversation_id, j.user_message, j.task_id,
                       j.source, j.schedule_id, j.created_at, t.task_id AS linked_task_id
                FROM chat_jobs AS j
                LEFT JOIN tasks AS t ON t.latest_job_id = j.job_id
                WHERE j.status = 'interrupted'
                ORDER BY j.created_at DESC
                """
            ).fetchall()
            for row in interrupted:
                task_id = str(row["task_id"] or row["linked_task_id"] or f"task_{uuid.uuid4().hex[:10]}")
                if not row["task_id"]:
                    conn.execute("UPDATE chat_jobs SET task_id=? WHERE job_id=?", (task_id, row["job_id"]))
                conn.execute(
                    """INSERT OR IGNORE INTO tasks(
                           task_id, conversation_id, title, request_text, source, schedule_id,
                           status, latest_job_id, progress_message, created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, 'interrupted', ?,
                                 '应用退出时任务中断；执行中的外部操作结果需要核对。', ?, ?)""",
                    (task_id, row["conversation_id"], str(row["user_message"] or "")[:120],
                     row["user_message"], row["source"] or "manual", row["schedule_id"],
                     row["job_id"], row["created_at"] or now, now),
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO chat_messages(
                        conversation_id, role, content, created_at, job_id
                    ) VALUES (?, 'assistant', ?, ?, ?)
                    """,
                    (
                        row["conversation_id"],
                        "任务在应用退出时中断。请先核对已发生的工具操作，再从最近进度恢复。",
                        now,
                        row["job_id"],
                    ),
                )
            conn.execute(
                """UPDATE tasks SET status='interrupted',
                          error=COALESCE(error, 'Application restarted before this task completed.'),
                          progress_message='应用退出时任务中断。请查看工具步骤并核对外部状态后恢复。',
                          heartbeat_at=NULL, cancel_requested=0, updated_at=?
                   WHERE latest_job_id IN (SELECT job_id FROM chat_jobs WHERE status='interrupted')""",
                (now,),
            )
            queued_rows = conn.execute(
                "SELECT * FROM chat_jobs WHERE status='queued' ORDER BY created_at ASC"
            ).fetchall()
            recovered = []
            missed_schedule_jobs = []
            for row in queued_rows:
                task_id = str(row["task_id"] or f"task_{uuid.uuid4().hex[:10]}")
                if not row["task_id"]:
                    conn.execute("UPDATE chat_jobs SET task_id=? WHERE job_id=?", (task_id, row["job_id"]))
                if row["source"] == "schedule":
                    reason = "日程在应用关闭期间到期；按错过即跳过策略，启动时没有自动补跑。"
                    conn.execute(
                        """UPDATE chat_jobs SET status='missed', error=?, reply=?, finished_at=?,
                                  heartbeat_at=NULL WHERE job_id=? AND status='queued'""",
                        (reason, reason, now, row["job_id"]),
                    )
                    conn.execute(
                        """INSERT OR IGNORE INTO tasks(
                               task_id, conversation_id, title, request_text, source, schedule_id,
                               status, latest_job_id, progress_message, error, created_at, updated_at
                           ) VALUES (?, ?, ?, ?, ?, ?, 'missed', ?, ?, ?, ?, ?)""",
                        (task_id, row["conversation_id"], str(row["user_message"] or "")[:120],
                         row["user_message"], row["source"] or "schedule", row["schedule_id"],
                         row["job_id"], reason, reason, row["created_at"], now),
                    )
                    conn.execute(
                        """UPDATE tasks SET status='missed', progress_message=?, error=?,
                                  heartbeat_at=NULL, cancel_requested=0, updated_at=?
                           WHERE task_id=? AND latest_job_id=?""",
                        (reason, reason, now, task_id, row["job_id"]),
                    )
                    conn.execute(
                        """INSERT OR IGNORE INTO chat_messages(
                               conversation_id, role, content, created_at, job_id
                           ) VALUES (?, 'assistant', ?, ?, ?)""",
                        (row["conversation_id"], reason, now, row["job_id"]),
                    )
                    if row["schedule_id"]:
                        missed_schedule_jobs.append((str(row["schedule_id"]), str(row["job_id"])))
                    continue
                conn.execute(
                    """INSERT OR IGNORE INTO tasks(
                           task_id, conversation_id, title, request_text, source, schedule_id,
                           status, latest_job_id, created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)""",
                    (task_id, row["conversation_id"], str(row["user_message"] or "")[:120],
                     row["user_message"], row["source"] or "manual", row["schedule_id"],
                     row["job_id"], row["created_at"], now),
                )
                recovered.append(JobRequest(
                    job_id=row["job_id"],
                    conversation_id=row["conversation_id"],
                    user_message=row["user_message"],
                    task_id=task_id,
                    source=row["source"] or "manual",
                    parent_job_id=row["parent_job_id"],
                    schedule_id=row["schedule_id"],
                    approval_card_ids=tuple(json.loads(row["approval_card_ids"] or "[]")),
                ))
        if missed_schedule_jobs:
            from src.core.system.terminal_logger import log_event
            from src.tools.activity_tool import finish_schedule
            for schedule_id, queued_job_id in sorted(set(missed_schedule_jobs)):
                try:
                    changed = finish_schedule(
                        schedule_id, False,
                        "应用重启时日程任务仍在排队；已跳过此过期执行，不会自动补跑。",
                        missed=True, expected_job_id=queued_job_id,
                    )
                    if changed:
                        log_event("SCHEDULE", f"日程 [{schedule_id}] 在启动恢复时标记为错过，未自动补跑。", "WARN")
                except Exception as exc:
                    log_event("SCHEDULE", f"无法更新错过日程 [{schedule_id}] 的状态：{exc}", "ERROR")
        return recovered

    def submit(
        self,
        conversation_id: str,
        user_message: str,
        *,
        task_id: Optional[str] = None,
        source: str = "manual",
        parent_job_id: Optional[str] = None,
        schedule_id: Optional[str] = None,
        display_message: Optional[str] = None,
        allow_queue: bool = False,
        idempotency_key: Optional[str] = None,
        approval_card_ids: Optional[list[str]] = None,
        before_enqueue: Optional[Callable[[str, str], None]] = None,
    ) -> str:
        """Persist a job, then expose it to the FIFO worker queue.

        When supplied, before_enqueue receives the actual job/task IDs inside
        the SQLite write transaction. It also runs for an idempotency hit before
        that existing ID is returned. Callers should use it only to persist
        external state needed by the worker; raising prevents a new enqueue.
        """
        if self._stop_event.is_set() or not self._worker.is_alive():
            raise RuntimeError("The AI worker is not running. Restart the Streamlit app.")

        conversation_id = str(conversation_id)
        user_message = str(user_message).strip()
        if not user_message:
            raise ValueError("user_message cannot be empty")

        job_id = uuid.uuid4().hex
        task_id = str(task_id or f"task_{uuid.uuid4().hex[:10]}")
        source = str(source or "manual")
        idempotency_key = str(idempotency_key or "").strip() or None
        approval_ids = tuple(dict.fromkeys(str(value) for value in (approval_card_ids or []) if value))
        now = utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if idempotency_key:
                existing = conn.execute(
                    "SELECT job_id, task_id FROM chat_jobs WHERE idempotency_key=?",
                    (idempotency_key,),
                ).fetchone()
                if existing:
                    existing_job_id = str(existing["job_id"])
                    if before_enqueue is not None:
                        before_enqueue(existing_job_id, str(existing["task_id"] or task_id))
                    return existing_job_id
            active = conn.execute(
                """
                SELECT 1 FROM chat_jobs
                WHERE conversation_id = ? AND status IN ('queued', 'running')
                LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
            if active and not allow_queue:
                raise RuntimeError("This conversation already has a task in progress.")

            conn.execute(
                "INSERT OR IGNORE INTO conversations(conversation_id, created_at) VALUES (?, ?)",
                (conversation_id, now),
            )
            conn.execute(
                """
                INSERT INTO chat_messages(
                    conversation_id, role, content, created_at, job_id
                ) VALUES (?, 'user', ?, ?, ?)
                """,
                (conversation_id, str(display_message or user_message), now, job_id),
            )
            conn.execute(
                """
                INSERT INTO chat_jobs(
                    job_id, conversation_id, user_message, task_id, source,
                    parent_job_id, schedule_id, idempotency_key, status, created_at
                    , approval_card_ids
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
                """,
                (job_id, conversation_id, user_message, task_id, source,
                 parent_job_id, schedule_id, idempotency_key, now, json.dumps(approval_ids)),
            )
            conn.execute(
                """INSERT INTO tasks(
                       task_id, conversation_id, title, request_text, source, schedule_id,
                       status, latest_job_id, progress_message, heartbeat_at, cancel_requested,
                       created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, '已排入执行队列。', NULL, 0, ?, ?)
                   ON CONFLICT(task_id) DO UPDATE SET
                       status='queued', latest_job_id=excluded.latest_job_id,
                       schedule_id=COALESCE(excluded.schedule_id, tasks.schedule_id),
                       progress_message=excluded.progress_message, heartbeat_at=NULL,
                       cancel_requested=0, error=NULL, updated_at=excluded.updated_at""",
                (task_id, conversation_id, str(user_message).strip()[:120], user_message,
                 source, schedule_id, job_id, now, now),
            )

            # A caller with separately persisted state (a schedule) can bind
            # the actual job ID before the worker is allowed to claim it.
            # An exception rolls back this job and leaves nothing in the queue.
            if before_enqueue is not None:
                before_enqueue(job_id, task_id)

            # Enqueue while holding the SQLite write transaction so concurrent
            # submitters preserve commit order. The worker's claim waits for
            # commit and skips this queue item if the transaction rolls back.
            self._queue.put(JobRequest(
                job_id, conversation_id, user_message, task_id, source, parent_job_id, schedule_id, approval_ids,
            ))
        return job_id
    def get_job(self, job_id: str) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM chat_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_job_by_idempotency_key(self, key: str) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM chat_jobs WHERE idempotency_key=?", (str(key),)).fetchone()
        return dict(row) if row else None

    def get_task(self, task_id: str) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (str(task_id),)).fetchone()
        return dict(row) if row else None

    def get_tasks(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY updated_at DESC LIMIT ?",
                (max(1, min(int(limit), 200)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_task_activity(self, task_id: str, limit: int = 100) -> list[dict[str, Any]]:
        from src.core.system.ledger import ledger
        return ledger.get_task_receipts(task_id, limit=limit)

    def update_task_progress(self, job_id: str, task_id: str, message: str) -> None:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                "UPDATE chat_jobs SET heartbeat_at=? WHERE job_id=? AND status='running'",
                (now, job_id),
            )
            conn.execute(
                """UPDATE tasks SET progress_message=?, heartbeat_at=?, updated_at=?
                   WHERE task_id=? AND latest_job_id=? AND status='running'""",
                (str(message)[:240], now, now, task_id, job_id),
            )

    def is_cancel_requested(self, job_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT cancel_requested FROM chat_jobs WHERE job_id=?",
                (str(job_id),),
            ).fetchone()
        return bool(row and row["cancel_requested"])

    def health_snapshot(self) -> dict[str, Any]:
        snapshot: dict[str, Any] = {
            "worker_alive": self._worker.is_alive(),
            "queued_in_memory": self._queue.qsize(),
            "database_ok": False,
            "database_error": None,
            "active_job": None,
            "heartbeat_age_seconds": None,
        }
        try:
            with self._connect() as conn:
                conn.execute("SELECT 1").fetchone()
                row = conn.execute(
                    "SELECT * FROM chat_jobs WHERE status IN ('queued','running') "
                    "ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END, created_at ASC LIMIT 1"
                ).fetchone()
            snapshot["database_ok"] = True
            snapshot["active_job"] = dict(row) if row else None
            if row and row["heartbeat_at"]:
                stamp = datetime.fromisoformat(row["heartbeat_at"])
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                snapshot["heartbeat_age_seconds"] = max(0, (datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds())
        except Exception as exc:
            snapshot["database_error"] = str(exc)
        return snapshot

    def cancel_task(self, task_id: str) -> str:
        now = utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            task = conn.execute("SELECT * FROM tasks WHERE task_id=?", (str(task_id),)).fetchone()
            if not task:
                raise ValueError("没有找到这项任务。")
            if task["status"] not in ("queued", "running"):
                raise ValueError("这项任务已经结束，不能取消。")
            active_jobs = conn.execute(
                "SELECT * FROM chat_jobs WHERE task_id=? AND status IN ('queued','running') ORDER BY created_at",
                (str(task_id),),
            ).fetchall()
            if not active_jobs:
                raise ValueError("没有找到任务对应的执行记录。")
            running_jobs = [row for row in active_jobs if row["status"] == "running"]
            queued_jobs = [row for row in active_jobs if row["status"] == "queued"]
            for job in queued_jobs:
                reply = "任务已取消，尚未开始此执行步骤。"
                conn.execute(
                    "UPDATE chat_jobs SET status='cancelled', error=?, reply=?, finished_at=? WHERE job_id=? AND status='queued'",
                    ("用户取消了排队中的任务。", reply, now, job["job_id"]),
                )
                conn.execute(
                    """INSERT OR IGNORE INTO chat_messages(
                           conversation_id, role, content, created_at, job_id
                       ) VALUES (?, 'assistant', ?, ?, ?)""",
                    (job["conversation_id"], reply, now, job["job_id"]),
                )
            for job in running_jobs:
                conn.execute(
                    "UPDATE chat_jobs SET cancel_requested=1 WHERE job_id=? AND status='running'",
                    (job["job_id"],),
                )
            if running_jobs:
                conn.execute(
                    """UPDATE tasks SET status='running', cancel_requested=1, error=NULL,
                           progress_message=?, updated_at=? WHERE task_id=?""",
                    ("已收到取消请求；当前工具调用结束后会停止后续步骤。", now, task_id),
                )
                return "已请求取消；当前正在执行的工具调用可能会先完成，之后会停止后续步骤。"
            conn.execute(
                """UPDATE tasks SET status='cancelled', cancel_requested=1,
                       error='用户取消了排队中的任务。', progress_message='任务已取消。',
                       heartbeat_at=NULL, updated_at=? WHERE task_id=?""",
                (now, task_id),
            )
        return "任务已取消，尚未开始执行。"

    def get_active_job(self, conversation_id: str) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM chat_jobs
                WHERE conversation_id = ? AND status IN ('queued', 'running')
                ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END, rowid ASC
                LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_jobs(self, conversation_id: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM chat_jobs
                WHERE conversation_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (conversation_id, int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_messages(self, conversation_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, conversation_id, role, content, created_at, job_id
                FROM (
                    SELECT * FROM chat_messages
                    WHERE conversation_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                )
                ORDER BY id ASC
                """,
                (conversation_id, int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_history_before_job(self, job: JobRequest, limit: int = 12) -> list[dict[str, Any]]:
        """Exclude later queued user requests while retaining earlier jobs' replies.

        An approval response may be queued before its parent's final reply is
        saved, so compare the jobs' user-message order, not the reply timestamp.
        """
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM (
                       SELECT m.* FROM chat_messages AS m
                       WHERE m.conversation_id=? AND m.job_id IS NOT ?
                         AND (
                           (m.job_id IS NULL AND m.id < (
                               SELECT id FROM chat_messages WHERE job_id=? AND role='user'))
                           OR m.job_id IN (
                               SELECT job_id FROM chat_messages WHERE conversation_id=? AND role='user'
                               AND id < (SELECT id FROM chat_messages WHERE job_id=? AND role='user'))
                         )
                       ORDER BY m.id DESC LIMIT ?
                   ) ORDER BY id ASC""",
                (job.conversation_id, job.job_id, job.job_id,
                 job.conversation_id, job.job_id, max(1, int(limit))),
            ).fetchall()
        return [dict(row) for row in rows]

    def is_busy(self) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM chat_jobs WHERE status IN ('queued', 'running') LIMIT 1"
            ).fetchone()
        return row is not None

    def new_conversation(self) -> str:
        conversation_id = uuid.uuid4().hex
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO conversations(conversation_id, created_at) VALUES (?, ?)",
                (conversation_id, utc_now()),
            )
        return conversation_id

    def clear_conversation(self, conversation_id: str) -> None:
        # 如果还有任务正在执行，不强杀它；只删除已落盘的展示数据会造成用户误解。
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            active = conn.execute(
                "SELECT 1 FROM chat_jobs WHERE conversation_id=? AND status IN ('queued','running') LIMIT 1",
                (conversation_id,),
            ).fetchone()
            if active:
                raise RuntimeError("当前会话仍有任务执行中，请等待完成后再新建会话。")
            waiting = conn.execute(
                "SELECT 1 FROM tasks WHERE conversation_id=? AND status='awaiting_human' LIMIT 1",
                (conversation_id,),
            ).fetchone()
            if waiting:
                raise RuntimeError("当前会话有任务正在等待卡片审批，请先处理审批后再清理会话。")
            conn.execute(
                "DELETE FROM chat_messages WHERE conversation_id = ?",
                (conversation_id,),
            )
            conn.execute(
                "DELETE FROM chat_jobs WHERE conversation_id = ?",
                (conversation_id,),
            )
            conn.execute("DELETE FROM tasks WHERE conversation_id=?", (conversation_id,))
            conn.execute(
                "DELETE FROM conversations WHERE conversation_id = ?",
                (conversation_id,),
            )

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------
    def _set_running(self, job: JobRequest) -> bool:
        now = utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                """UPDATE chat_jobs SET status='running', started_at=?, heartbeat_at=?
                   WHERE job_id=? AND status='queued'""",
                (now, now, job.job_id),
            )
            if cursor.rowcount != 1:
                return False
            conn.execute(
                """UPDATE tasks SET status='running', error=NULL, cancel_requested=0,
                       progress_message='正在准备任务。', heartbeat_at=?, updated_at=?
                   WHERE task_id=? AND latest_job_id=?""",
                (now, now, job.task_id, job.job_id),
            )
        return True

    def _touch_heartbeat(self, job: JobRequest) -> None:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                "UPDATE chat_jobs SET heartbeat_at=? WHERE job_id=? AND status='running'",
                (now, job.job_id),
            )
            conn.execute(
                """UPDATE tasks SET heartbeat_at=?, updated_at=?
                   WHERE task_id=? AND latest_job_id=? AND status='running'""",
                (now, now, job.task_id, job.job_id),
            )

    def _heartbeat_loop(self, job: JobRequest, stop_event: threading.Event) -> None:
        while not stop_event.wait(5):
            try:
                self._touch_heartbeat(job)
            except Exception:
                pass

    def _set_completed(self, job: JobRequest, reply: str) -> None:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE chat_jobs
                SET status='completed', reply=?, finished_at=?, heartbeat_at=NULL
                WHERE job_id=?
                """,
                (reply, now, job.job_id),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO chat_messages(
                    conversation_id, role, content, created_at, job_id
                ) VALUES (?, 'assistant', ?, ?, ?)
                """,
                (job.conversation_id, reply, now, job.job_id),
            )
            conn.execute(
                """UPDATE tasks SET status='completed', error=NULL, cancel_requested=0,
                       progress_message='任务已完成。', heartbeat_at=NULL, updated_at=?
                   WHERE task_id=? AND latest_job_id=?""",
                (now, job.task_id, job.job_id),
            )

    def _set_awaiting_human(self, job: JobRequest, reply: str) -> None:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                "UPDATE chat_jobs SET status='awaiting_human', reply=?, finished_at=?, heartbeat_at=NULL WHERE job_id=?",
                (reply, now, job.job_id),
            )
            conn.execute(
                """INSERT OR IGNORE INTO chat_messages(
                       conversation_id, role, content, created_at, job_id
                   ) VALUES (?, 'assistant', ?, ?, ?)""",
                (job.conversation_id, reply, now, job.job_id),
            )
            conn.execute(
                """UPDATE tasks SET status='awaiting_human', error=NULL, cancel_requested=0,
                       progress_message='等待你处理决策卡片。', heartbeat_at=NULL, updated_at=?
                   WHERE task_id=? AND latest_job_id=?""",
                (now, job.task_id, job.job_id),
            )

    def _set_failed(
        self,
        job: JobRequest,
        exc: BaseException,
        assistant_reply: Optional[str] = None,
    ) -> None:
        now = utc_now()
        error_text = f"{type(exc).__name__}: {exc}"
        assistant_content = assistant_reply or f"任务执行失败：{error_text}"
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE chat_jobs
                SET status='failed', reply=?, error=?, finished_at=?, heartbeat_at=NULL
                WHERE job_id=?
                """,
                (assistant_reply, error_text, now, job.job_id),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO chat_messages(
                    conversation_id, role, content, created_at, job_id
                ) VALUES (?, 'assistant', ?, ?, ?)
                """,
                (job.conversation_id, assistant_content, now, job.job_id),
            )
            conn.execute(
                """UPDATE tasks SET status='failed', error=?, progress_message='任务执行失败。',
                       heartbeat_at=NULL, updated_at=? WHERE task_id=? AND latest_job_id=?""",
                (error_text, now, job.task_id, job.job_id),
            )

    def _set_cancelled(self, job: JobRequest, reply: Optional[str] = None) -> None:
        now = utc_now()
        content = reply or "任务已按你的请求停止。请查看已完成的步骤；中断中的外部操作需要核对后再继续。"
        with self._connect() as conn:
            conn.execute(
                """UPDATE chat_jobs SET status='cancelled', reply=?, error=?, finished_at=?, heartbeat_at=NULL
                   WHERE job_id=?""",
                (content, "用户请求取消任务。", now, job.job_id),
            )
            conn.execute(
                """INSERT OR IGNORE INTO chat_messages(
                       conversation_id, role, content, created_at, job_id
                   ) VALUES (?, 'assistant', ?, ?, ?)""",
                (job.conversation_id, content, now, job.job_id),
            )
            conn.execute(
                """UPDATE tasks SET status='cancelled', cancel_requested=1, error='用户请求取消任务。',
                       progress_message='任务已停止。', heartbeat_at=NULL, updated_at=?
                   WHERE task_id=? AND latest_job_id=?""",
                (now, job.task_id, job.job_id),
            )

    def _worker_main(self) -> None:
        # 创建并挂载专属于该后台守护线程的事件循环（兼容 Python < 3.11 环境）
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        chat_manager = None
        self._worker_ready.set()

        try:
            while not self._stop_event.is_set():
                try:
                    job = self._queue.get(timeout=0.5)
                except queue.Empty:
                    continue

                if job is None:
                    self._queue.task_done()
                    break

                heartbeat_stop = None
                heartbeat_thread = None
                try:
                    if not self._set_running(job):
                        continue
                    heartbeat_stop = threading.Event()
                    heartbeat_thread = threading.Thread(
                        target=self._heartbeat_loop,
                        args=(job, heartbeat_stop),
                        name=f"job-heartbeat-{job.job_id[:8]}",
                        daemon=True,
                    )
                    heartbeat_thread.start()
                    if chat_manager is None:
                        from src.core.agent.chat_manager import ChatManager
                        # 只有真正第一次提交 AI 任务时才初始化工具链/MemoryManager。
                        # 这样打开首页、日志页、消息页时不会为了“显示状态”加载整套 Agent。
                        chat_manager = ChatManager()

                    # 当前 user 消息已经先写入数据库；传给模型时只取它之前的历史，
                    # 避免重复注入当前问题。
                    history = self.get_history_before_job(job, limit=12)
                    reply = loop.run_until_complete(
                        chat_manager.chat(
                            job.user_message,
                            history=history,
                            task_id=job.task_id,
                            job_id=job.job_id,
                            conversation_id=job.conversation_id,
                            source=job.source,
                            schedule_id=job.schedule_id,
                            parent_job_id=job.parent_job_id,
                            approval_card_ids=list(job.approval_card_ids),
                            cancellation_check=lambda: self.is_cancel_requested(job.job_id),
                            progress_callback=lambda message: self.update_task_progress(
                                job.job_id, job.task_id, message,
                            ),
                        )
                    )
                    reply_text = str(reply)
                    if self.is_cancel_requested(job.job_id) or getattr(chat_manager, "last_task_status", "completed") == "cancelled":
                        self._set_cancelled(job, reply_text)
                    elif getattr(chat_manager, "last_task_status", "completed") == "failed":
                        error_text = (
                            getattr(chat_manager, "last_task_error", None)
                            or "Agent 报告存在未恢复的工具调用失败。"
                        )
                        self._set_failed(job, RuntimeError(error_text), assistant_reply=reply_text)
                    elif getattr(chat_manager, "last_task_status", "completed") == "awaiting_human":
                        self._set_awaiting_human(job, reply_text)
                    else:
                        self._set_completed(job, reply_text)
                except BaseException as exc:
                    try:
                        if self.is_cancel_requested(job.job_id):
                            self._set_cancelled(job)
                        else:
                            self._set_failed(job, exc)
                    except Exception as persistence_exc:
                        # Preserve the RUNNING record for recovery; do not let
                        # a failing database error handler kill the whole worker.
                        from src.core.system.terminal_logger import log_event
                        log_event("EXECUTOR", f"任务结果无法保存，需核对后恢复：{persistence_exc}", "ERROR")
                finally:
                    if heartbeat_stop is not None:
                        heartbeat_stop.set()
                    if heartbeat_thread is not None and heartbeat_thread.is_alive():
                        heartbeat_thread.join(timeout=1)
                    self._queue.task_done()
        finally:
            loop.close()
            self._release_instance_lock()

    def shutdown(self) -> None:
        self._stop_event.set()
        self._queue.put(None)
        if self._worker.is_alive():
            self._worker.join(timeout=2)
