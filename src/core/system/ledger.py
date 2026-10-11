# src/core/system/ledger.py
import sqlite3
import json
from typing import Optional, List, Dict, Any
from dataclasses import dataclass
from contextlib import contextmanager
from pathlib import Path

from src.core.system.path_manager import PathManager

DB_PATH = PathManager.LEDGER_DB


@dataclass
class ToolCall:
    """LLM 提出的工具调用意图（仅代表‘提议’，不代表已发生）"""
    call_id: str
    tool_name: str
    params: Dict[str, Any]
    has_side_effect: bool = True


@dataclass
class ToolResult:
    """底层物理函数真实执行后的原始返回结果"""
    call_id: str
    success: bool
    data: Any = None
    error: Optional[str] = None


@dataclass
class Receipt:
    """系统不可篡改的物理执行凭证 (Receipt)"""
    execution_id: str
    tool_name: str
    has_side_effect: bool
    status: str  # RUNNING is write-ahead/unknown outcome; terminal values include SUCCESS/FAILED/INTERCEPTED.
    params: Dict[str, Any]
    result: Any
    error: Optional[str]
    timestamp: float
    task_id: Optional[str] = None
    job_id: Optional[str] = None


class ExecutionLedger:
    """系统不可篡改的不可否定执行账本 (Execution Ledger)"""

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _get_connection(self):
        conn = sqlite3.connect(str(self.db_path), timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS execution_logs (
                    execution_id TEXT PRIMARY KEY,
                    tool_name TEXT NOT NULL,
                    has_side_effect INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    params TEXT NOT NULL,
                    result TEXT,
                    error TEXT,
                    timestamp REAL NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    task_id TEXT,
                    job_id TEXT
                )
            """)
            existing = {row[1] for row in cursor.execute("PRAGMA table_info(execution_logs)")}
            if "task_id" not in existing:
                cursor.execute("ALTER TABLE execution_logs ADD COLUMN task_id TEXT")
            if "job_id" not in existing:
                cursor.execute("ALTER TABLE execution_logs ADD COLUMN job_id TEXT")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_execution_task ON execution_logs(task_id, timestamp)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_execution_job ON execution_logs(job_id, timestamp)")
            conn.commit()

    def record_receipt(self, receipt: Receipt) -> None:
        """物理凭证强行入库"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO execution_logs
                (execution_id, tool_name, has_side_effect, status, params, result, error, timestamp, task_id, job_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(execution_id) DO UPDATE SET
                    status=excluded.status, result=excluded.result, error=excluded.error,
                    timestamp=excluded.timestamp
            """, (
                receipt.execution_id,
                receipt.tool_name,
                1 if receipt.has_side_effect else 0,
                receipt.status,
                json.dumps(receipt.params, ensure_ascii=False, default=str),
                json.dumps(receipt.result, ensure_ascii=False, default=str) if receipt.result is not None else None,
                receipt.error,
                receipt.timestamp,
                receipt.task_id,
                receipt.job_id,
            ))
            conn.commit()

    def get_task_receipts(self, task_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        """Return a task's tool-step timeline without exposing raw parameters/results."""
        with self._get_connection() as conn:
            rows = conn.execute(
                """SELECT execution_id, job_id, tool_name, has_side_effect, status, error, timestamp
                   FROM execution_logs WHERE task_id=? ORDER BY timestamp DESC, rowid DESC LIMIT ?""",
                (str(task_id), max(1, min(int(limit), 500))),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def get_job_receipts(self, job_id: str) -> List[Dict[str, Any]]:
        """Full chronological result set for one execution attempt, not older retries."""
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM execution_logs WHERE job_id=? ORDER BY timestamp ASC, rowid ASC",
                (str(job_id),),
            ).fetchall()
        return [dict(row) for row in rows]


# 全局账本单例
ledger = ExecutionLedger()
