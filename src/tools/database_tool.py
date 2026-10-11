"""User file catalog, task file references, document indexes, and RAG tools.

The SQLite catalog stays lightweight at import time. Document parsing, OCR, and
vector search dependencies are loaded only when their functions are called.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import sqlite3
import uuid
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, List, Optional

from config.settings import settings
from src.core.system.path_manager import PathManager

if TYPE_CHECKING:
    from llama_index.core import VectorStoreIndex
    from llama_index.core.tools import QueryEngineTool


SUPPORTED_SUMMARY_BYTES = 20 * 1024 * 1024
MAX_READ_CHARS = 24000
_SYNC_LOCK = threading.RLock()
_INDEX_LOCK = threading.RLock()
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp"}
_INDEXABLE_SUFFIXES = _IMAGE_SUFFIXES | {
    ".txt", ".md", ".pdf", ".docx", ".xlsx", ".csv", ".json",
    ".html", ".xml", ".yaml", ".yml", ".py",
}
_ocr_reader = None
_knowledge_index = None
_knowledge_signature = None
_embedding_model_name = ""


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@contextmanager
def _connect():
    PathManager.FILE_CATALOG_DB.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(PathManager.FILE_CATALOG_DB), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def _init_db() -> None:
    with _connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS files (
                file_id TEXT PRIMARY KEY,
                rel_path TEXT NOT NULL UNIQUE,
                file_name TEXT NOT NULL,
                extension TEXT NOT NULL DEFAULT '',
                mime_type TEXT NOT NULL DEFAULT 'application/octet-stream',
                size_bytes INTEGER NOT NULL DEFAULT 0,
                mtime_ns INTEGER NOT NULL DEFAULT 0,
                sha256 TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                tags_json TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_files_active_name ON files(status, file_name);
            CREATE INDEX IF NOT EXISTS idx_files_hash ON files(sha256);
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                task_name TEXT NOT NULL,
                task_type TEXT NOT NULL DEFAULT 'task',
                description TEXT NOT NULL DEFAULT '',
                expires_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS task_files (
                task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                file_id TEXT NOT NULL REFERENCES files(file_id) ON DELETE RESTRICT,
                purpose TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                PRIMARY KEY(task_id, file_id)
            );
            CREATE INDEX IF NOT EXISTS idx_task_files_file ON task_files(file_id);
            """
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_user_path(rel_path: str) -> Path:
    raw = Path(str(rel_path))
    if raw.is_absolute():
        raise ValueError("只允许引用 data/users 中的相对路径。")
    base = PathManager.USERS_DIR.resolve()
    candidate = (base / raw).resolve(strict=False)
    if candidate != base and base not in candidate.parents:
        raise ValueError("文件路径超出用户文件区。")
    return candidate


def _file_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    try:
        item["tags"] = json.loads(item.pop("tags_json", "[]"))
    except (TypeError, ValueError):
        item["tags"] = []
        item.pop("tags_json", None)
    item["path"] = item.get("rel_path")
    return item


def sync_user_file_catalog() -> str:
    """Refresh file metadata without moving, copying, or semantically sorting files."""
    with _SYNC_LOCK:
        return _sync_user_file_catalog()


def _sync_user_file_catalog() -> str:
    _init_db()
    root = PathManager.USERS_DIR
    root.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    scan_errors: list[OSError] = []
    for current, dirs, names in os.walk(root, onerror=scan_errors.append):
        dirs[:] = sorted(name for name in dirs if not name.startswith(".") and not (Path(current) / name).is_symlink())
        for name in names:
            path = Path(current) / name
            if name.startswith(".") or path.is_symlink() or not path.is_file():
                continue
            paths.append(path)

    added = changed = 0
    now = _now()
    active_ids: set[str] = set()
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        for path in paths:
            rel_path = path.relative_to(root).as_posix()
            existing = db.execute("SELECT * FROM files WHERE rel_path=?", (rel_path,)).fetchone()
            if existing:
                active_ids.add(str(existing["file_id"]))
            try:
                stat = path.stat()
                unchanged = bool(
                    existing
                    and int(existing["size_bytes"]) == stat.st_size
                    and int(existing["mtime_ns"]) == stat.st_mtime_ns
                )
                if unchanged:
                    file_id = str(existing["file_id"])
                    db.execute(
                        "UPDATE files SET status='active', updated_at=? WHERE file_id=?",
                        (now, file_id),
                    )
                else:
                    digest = _sha256(path)
                    file_id = str(existing["file_id"]) if existing else ""
                    # Reuse the ID only when a previously cataloged file was moved or renamed.
                    if not file_id:
                        moved_candidates = db.execute(
                            "SELECT * FROM files WHERE sha256=? ORDER BY updated_at DESC",
                            (digest,),
                        ).fetchall()
                        for moved in moved_candidates:
                            if str(moved["file_id"]) not in active_ids and not _safe_user_path(str(moved["rel_path"])).is_file():
                                file_id = str(moved["file_id"])
                                existing = moved
                                break
                        if not file_id:
                            file_id = f"file_{uuid.uuid4().hex[:12]}"
                    created_at = str(existing["created_at"]) if existing else now
                    same_content = bool(existing and str(existing["sha256"] or "") == digest)
                    summary = str(existing["summary"] or "") if same_content else ""
                    tags_json = str(existing["tags_json"] or "[]") if existing else "[]"
                    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                    db.execute(
                        """INSERT INTO files(file_id,rel_path,file_name,extension,mime_type,size_bytes,mtime_ns,sha256,summary,tags_json,status,created_at,updated_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,'active',?,?)
                           ON CONFLICT(file_id) DO UPDATE SET rel_path=excluded.rel_path,file_name=excluded.file_name,
                           extension=excluded.extension,mime_type=excluded.mime_type,size_bytes=excluded.size_bytes,
                           mtime_ns=excluded.mtime_ns,sha256=excluded.sha256,summary=excluded.summary,
                           status='active',updated_at=excluded.updated_at""",
                        (file_id, rel_path, path.name, path.suffix.lower(), mime_type, stat.st_size,
                         stat.st_mtime_ns, digest, summary, tags_json, created_at, now),
                    )
                    if existing:
                        changed += 1
                    else:
                        added += 1
                active_ids.add(file_id)
            except OSError:
                continue

        # An unreadable folder is not evidence that its files were deleted.
        # Compare in Python to avoid SQLite's variable limit for large catalogs.
        if not scan_errors:
            missing = [(now, row["file_id"]) for row in db.execute("SELECT file_id FROM files WHERE status='active'")
                       if str(row["file_id"]) not in active_ids]
            db.executemany("UPDATE files SET status='missing', updated_at=? WHERE file_id=?", missing)

    _import_legacy_manifest()
    warning = " 部分目录暂时无法读取，已保留原有文件记录。" if scan_errors else ""
    return f"用户文件目录已同步：新增 {added} 个、更新 {changed} 个，当前发现 {len(paths)} 个文件。文件仍保存在同一个用户文件区。{warning}"


def _import_legacy_manifest() -> None:
    """Import legacy summaries into the catalog once, then retire the JSON copy."""
    legacy = PathManager.MANIFEST_FILE
    if not legacy.exists():
        return
    try:
        data = json.loads(legacy.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    entries = data.get("files", {}) if isinstance(data, dict) else {}
    if not isinstance(entries, dict):
        return
    _init_db()
    with _connect() as db:
        current = db.execute("SELECT file_id,rel_path,file_name FROM files WHERE status='active'").fetchall()
        by_path = {str(row["rel_path"]).replace("\\", "/"): row for row in current}
        by_name: dict[str, list[sqlite3.Row]] = {}
        for row in current:
            by_name.setdefault(str(row["file_name"]).casefold(), []).append(row)
        for old_path, record in entries.items():
            if not isinstance(record, dict):
                continue
            key = str(record.get("rel_path") or old_path).replace("\\", "/")
            row = by_path.get(key)
            if row is None:
                matches = by_name.get(str(record.get("file_name") or Path(key).name).casefold(), [])
                row = matches[0] if len(matches) == 1 else None
            summary = str(record.get("summary") or "").strip()
            if row and summary:
                db.execute("UPDATE files SET summary=?, updated_at=? WHERE file_id=? AND (summary='' OR summary IS NULL)",
                           (summary, _now(), row["file_id"]))
            elif row is None:
                old_name = str(record.get("file_name") or Path(key).name or "未命名文件")
                safe_rel = Path(key)
                if safe_rel.is_absolute() or ".." in safe_rel.parts:
                    legacy_suffix = hashlib.sha256(key.encode("utf-8", errors="replace")).hexdigest()[:8]
                    safe_rel = Path("legacy_missing") / f"{legacy_suffix}__{old_name}"
                rel_path = safe_rel.as_posix()
                # Keep historical metadata for a file that is currently absent.
                # Its stable legacy ID can later be reactivated when the file returns.
                file_id = "file_legacy_" + hashlib.sha256(key.encode("utf-8", errors="replace")).hexdigest()[:12]
                db.execute(
                    """INSERT OR IGNORE INTO files(file_id,rel_path,file_name,extension,mime_type,size_bytes,mtime_ns,
                       sha256,summary,tags_json,status,created_at,updated_at)
                       VALUES(?,?,?,?,?,0,0,'',?,'[]','missing',?,?)""",
                    (file_id, rel_path, old_name, Path(old_name).suffix.lower(),
                     mimetypes.guess_type(old_name)[0] or "application/octet-stream", summary,
                     str(record.get("updated_at") or _now()), _now()),
                )
    try:
        legacy.unlink()
    except OSError:
        pass


def list_user_files(query: str = "", limit: int = 100) -> list[dict[str, Any]]:
    _init_db()
    query = str(query or "").strip()
    limit = max(1, min(int(limit or 100), 500))
    with _connect() as db:
        if query:
            pattern = f"%{query}%"
            rows = db.execute(
                """SELECT * FROM files WHERE status='active' AND
                   (file_name LIKE ? OR rel_path LIKE ? OR summary LIKE ? OR tags_json LIKE ?)
                   ORDER BY file_name COLLATE NOCASE LIMIT ?""",
                (pattern, pattern, pattern, pattern, limit),
            ).fetchall()
        else:
            rows = db.execute("SELECT * FROM files WHERE status='active' ORDER BY file_name COLLATE NOCASE LIMIT ?", (limit,)).fetchall()
    return [_file_row_to_dict(row) for row in rows]


def search_user_files(query: str, limit: int = 8) -> str:
    """Find candidate user files by filename, path, summary, or tags."""
    sync_user_file_catalog()
    rows = list_user_files(query, limit=max(1, min(int(limit or 8), 20)))
    if not rows and str(query or "").strip():
        # For files with opaque names, create a small batch of summaries once,
        # then retry metadata search. This stays lazy and avoids reading every file
        # on every request.
        candidates = [row for row in list_user_files(limit=50) if not str(row.get("summary") or "").strip()]
        for row in candidates[:20]:
            try:
                summarize_user_file(row["file_id"])
            except Exception:
                continue
        rows = list_user_files(query, limit=max(1, min(int(limit or 8), 20)))
    if not rows:
        return "没有找到匹配的用户文件。"
    return "\n".join(
        f"- file_id={row['file_id']} | {row['file_name']} | path={row['rel_path']} | 摘要={row.get('summary') or '尚无摘要'}"
        for row in rows
    )


def set_user_file_tags(file_id: str, tags: list[str]) -> str:
    _init_db()
    clean_tags = list(dict.fromkeys(str(tag).strip() for tag in (tags or []) if str(tag).strip()))
    if any(len(tag) > 64 for tag in clean_tags):
        raise ValueError("单个标签不能超过 64 个字符。")
    with _connect() as db:
        row = db.execute("SELECT 1 FROM files WHERE file_id=? AND status='active'", (str(file_id),)).fetchone()
        if not row:
            raise FileNotFoundError(f"用户文件 ID {file_id} 不存在或已移除。")
        db.execute("UPDATE files SET tags_json=?,updated_at=? WHERE file_id=?",
                   (json.dumps(clean_tags, ensure_ascii=False), _now(), str(file_id)))
    return f"已更新文件 {file_id} 的标签：{', '.join(clean_tags) if clean_tags else '已清空'}。"


def get_file_path(file_id: str) -> Path:
    _init_db()
    with _connect() as db:
        row = db.execute("SELECT rel_path,status FROM files WHERE file_id=?", (str(file_id),)).fetchone()
    if not row or row["status"] != "active":
        raise FileNotFoundError(f"用户文件 ID {file_id} 不存在或已移除。")
    path = _safe_user_path(str(row["rel_path"]))
    if not path.is_file():
        raise FileNotFoundError(f"目录中找不到文件 ID {file_id} 对应的文件。")
    return path


def read_user_file(file_id: str, max_chars: int = MAX_READ_CHARS) -> str:
    """Read one explicitly selected user file; never accepts an arbitrary disk path."""
    max_chars = max(1, min(int(max_chars), MAX_READ_CHARS))
    path = get_file_path(file_id)
    if path.stat().st_size > SUPPORTED_SUMMARY_BYTES:
        raise ValueError("文件超过 20 MiB，已阻止一次性读取；请先拆分或缩小文件。")
    text = ""
    try:
        from llama_index.core import SimpleDirectoryReader
        docs = SimpleDirectoryReader(input_files=[str(path)]).load_data()
        text = "\n\n".join(str(doc.text or "") for doc in docs)
    except Exception:
        if path.suffix.lower() in {".txt", ".md", ".csv", ".json", ".py", ".html", ".xml", ".yaml", ".yml"}:
            text = path.read_text(encoding="utf-8", errors="replace")
        else:
            raise ValueError(f"当前环境无法解析 {path.suffix or '该类型'} 文件。")
    text = text.strip()
    if not text:
        return f"文件 {path.name} 没有可提取的文本内容。"
    if len(text) > int(max_chars):
        text = text[:int(max_chars)] + "\n\n[内容已截断；如需更多内容请按章节读取。]"
    return f"文件：{path.name}\nfile_id：{file_id}\n\n{text}"


def summarize_user_file(file_id: str) -> str:
    path = get_file_path(file_id)
    before = path.stat()
    source_digest = _sha256(path)
    if path.stat().st_size > SUPPORTED_SUMMARY_BYTES:
        return "文件过大，已跳过摘要生成。"
    try:
        text = read_user_file(file_id, max_chars=10000)
    except Exception as exc:
        text = f"文件名：{path.name}\n读取失败：{exc}"
    summary = ""
    try:
        from llama_index.core import Settings
        response = Settings.llm.complete(
            "请用一到两句话准确概括这个用户文件，说明主题和可能用途，不要猜测缺失信息。\n\n" + text[:10000]
        )
        summary = " ".join(str(response).split())[:500]
    except Exception:
        summary = " ".join(text.split())[:220]
    _init_db()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("文件在生成摘要期间发生变化，请同步目录后重试。")
    with _connect() as db:
        db.execute("UPDATE files SET summary=?, updated_at=? WHERE file_id=? AND sha256=? AND status='active'",
                   (summary, _now(), str(file_id), source_digest))
    return summary or f"文件：{path.name}"


def create_task_record(task_id: str, task_name: str, task_type: str, description: str,
                       expires_at: Optional[str] = None, file_ids: Iterable[str] = (), purpose: str = "") -> str:
    _init_db()
    task_id = str(task_id).strip()
    if not task_id:
        raise ValueError("task_id 不能为空。")
    ids = list(dict.fromkeys(str(item).strip() for item in (file_ids or []) if str(item).strip()))
    now = _now()
    with _connect() as db:
        db.execute(
            """INSERT INTO tasks(task_id,task_name,task_type,description,expires_at,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET task_name=excluded.task_name,
               task_type=excluded.task_type,description=excluded.description,expires_at=excluded.expires_at,updated_at=excluded.updated_at""",
            (task_id, str(task_name), str(task_type or "task"), str(description or ""), expires_at, now, now),
        )
        for file_id in ids:
            exists = db.execute("SELECT 1 FROM files WHERE file_id=? AND status='active'", (file_id,)).fetchone()
            if not exists:
                raise ValueError(f"文件 ID {file_id} 不存在或已移除。先同步用户文件目录并确认 ID。")
            db.execute("INSERT OR IGNORE INTO task_files(task_id,file_id,purpose,created_at) VALUES(?,?,?,?)",
                       (task_id, file_id, str(purpose or ""), now))
    return task_id


def link_files_to_task(task_id: str, file_ids: list[str], purpose: str = "") -> str:
    _init_db()
    task_id = str(task_id or "").strip()
    if not task_id:
        raise ValueError("task_id 不能为空。")
    now = _now()
    ids = list(dict.fromkeys(str(item).strip() for item in (file_ids or []) if str(item).strip()))
    if not ids:
        return "没有提供 file_id，未添加文件引用。"
    with _connect() as db:
        if not db.execute("SELECT 1 FROM tasks WHERE task_id=?", (str(task_id),)).fetchone():
            db.execute("INSERT INTO tasks(task_id,task_name,task_type,description,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                       (str(task_id), str(task_id), "task", "", now, now))
        for file_id in ids:
            if not db.execute("SELECT 1 FROM files WHERE file_id=? AND status='active'", (file_id,)).fetchone():
                raise ValueError(f"文件 ID {file_id} 不存在或已移除。")
            db.execute("INSERT OR IGNORE INTO task_files(task_id,file_id,purpose,created_at) VALUES(?,?,?,?)",
                       (str(task_id), file_id, str(purpose or ""), now))
    return f"已将 {len(ids)} 个文件 ID 关联到任务 {task_id}；只保存引用，没有复制用户文件。"


def get_task_resources(task_id: str) -> list[dict[str, Any]]:
    _init_db()
    with _connect() as db:
        rows = db.execute(
            """SELECT f.*, tf.purpose, tf.created_at AS linked_at FROM task_files tf
               JOIN files f ON f.file_id=tf.file_id WHERE tf.task_id=? ORDER BY f.file_name COLLATE NOCASE""",
            (str(task_id),),
        ).fetchall()
    return [_file_row_to_dict(row) for row in rows]


def list_task_resource_links() -> list[dict[str, Any]]:
    _init_db()
    with _connect() as db:
        rows = db.execute(
            """SELECT t.task_id,t.task_name,t.task_type,t.description,tf.file_id,tf.purpose,
                      f.file_name,f.rel_path,f.status
               FROM task_files tf JOIN tasks t ON t.task_id=tf.task_id
               JOIN files f ON f.file_id=tf.file_id
               ORDER BY t.updated_at DESC,f.file_name COLLATE NOCASE"""
        ).fetchall()
    return [dict(row) for row in rows]


def get_task_file_paths(task_id: str) -> list[Path]:
    paths = []
    for item in get_task_resources(task_id):
        if item.get("status") == "active":
            try:
                paths.append(get_file_path(item["file_id"]))
            except (FileNotFoundError, ValueError):
                continue
    return paths


def delete_task_record(task_id: str) -> bool:
    _init_db()
    with _connect() as db:
        cursor = db.execute("DELETE FROM tasks WHERE task_id=?", (str(task_id),))
        return cursor.rowcount > 0


def resolve_file_ids(file_ids: Optional[list[str]] = None, file_paths: Optional[list[str]] = None) -> list[str]:
    _init_db()
    ids = list(dict.fromkeys(str(item).strip() for item in (file_ids or []) if str(item).strip()))
    path_refs: list[str] = []
    for raw_path in (file_paths or []):
        candidate = _safe_user_path(raw_path)
        if not candidate.is_file():
            raise FileNotFoundError(f"用户文件不存在：{raw_path}")
        path_refs.append(candidate.relative_to(PathManager.USERS_DIR.resolve()).as_posix())
    if path_refs:
        sync_user_file_catalog()
    with _connect() as db:
        for rel_path in path_refs:
            row = db.execute("SELECT file_id FROM files WHERE rel_path=? AND status='active'", (rel_path,)).fetchone()
            if row:
                ids.append(str(row["file_id"]))
            else:
                raise ValueError(f"文件尚未成功登记到目录：{rel_path}，请确认文件可读后重试。")
    ids = list(dict.fromkeys(ids))
    with _connect() as db:
        for file_id in ids:
            if not db.execute("SELECT 1 FROM files WHERE file_id=? AND status='active'", (file_id,)).fetchone():
                raise ValueError(f"文件 ID {file_id} 不存在或已移除。")
    return ids


def catalog_rows_for_ui(query: str = "") -> list[dict[str, Any]]:
    """Read the catalog; UI controls synchronization explicitly to avoid rescans on rerun."""
    return list_user_files(query, limit=500)



def _migrate_legacy_task_cards() -> None:
    """Move old JSON task-card records into the catalog's canonical SQLite store."""
    _init_db()
    for path in PathManager.DATA_CARDS_DIR.glob("*.json"):
        try:
            card = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(card, dict):
                continue
            card_id = str(card.get("card_id") or path.stem)
            file_ids = list(card.get("file_ids") or [])
            file_paths = list(card.get("file_paths") or [])
            if file_ids or file_paths:
                try:
                    ids = resolve_file_ids(file_ids=file_ids, file_paths=file_paths)
                except (ValueError, FileNotFoundError):
                    # Old cards sometimes stored workspace-relative or absolute paths.
                    # After flattening, migrate only an unambiguous basename match.
                    sync_user_file_catalog()
                    rows = list_user_files(limit=500)
                    ids = list(file_ids)
                    for old_path in file_paths:
                        old_name = str(old_path).replace("\\", "/").rsplit("/", 1)[-1].casefold()
                        matches = [row for row in rows if row["file_name"].casefold() == old_name]
                        if len(matches) != 1:
                            raise ValueError(f"旧文件路径无法唯一匹配：{old_path}")
                        ids.append(matches[0]["file_id"])
                    ids = list(dict.fromkeys(ids))
            else:
                ids = []
            create_task_record(
                card_id,
                str(card.get("card_name") or card_id),
                str(card.get("card_type") or "task"),
                str(card.get("description") or ""),
                card.get("expire_at"),
                ids,
                purpose=str(card.get("description") or ""),
            )
            path.unlink()
        except Exception:
            # Keep malformed or unresolved records so a future migration can retry.
            continue


def scan_and_get_global_manifest() -> str:
    """Refresh the catalog and return file IDs with concise property summaries."""
    sync_result = sync_user_file_catalog()
    _migrate_legacy_task_cards()
    rows = list_user_files(limit=500)
    view = [sync_result, "用户文件属性目录（文件 ID 可由多个任务复用）："]
    for row in rows:
        summary = str(row.get("summary") or "").strip()
        if not summary:
            summary = summarize_user_file(row["file_id"])
        size = int(row.get("size_bytes") or 0)
        view.append(
            f"- file_id={row['file_id']} | {row['file_name']} | path={row['rel_path']} | "
            f"{row.get('mime_type', '')} | {size} bytes | 摘要={summary}"
        )
    if not rows:
        view.append("当前没有用户文件。把文件放入 data/users 后可再次同步。")
    return "\n".join(view)


def create_task_data_card(
    card_id: str,
    card_name: str,
    card_type: str,
    description: str,
    file_paths: Optional[List[str]] = None,
    expire_days: Optional[int] = None,
    file_ids: Optional[List[str]] = None,
) -> str:
    """Create a task record and link stable user-file IDs; documents are never copied."""
    sync_user_file_catalog()
    _migrate_legacy_task_cards()
    ids = resolve_file_ids(file_ids=file_ids or [], file_paths=file_paths or [])
    expires_at = None
    if str(card_type).lower() == "short_term" and expire_days:
        expires_at = (datetime.now().astimezone() + timedelta(days=int(expire_days))).isoformat(timespec="seconds")
    create_task_record(card_id, card_name, card_type, description, expires_at, ids, purpose=description)
    return f"任务 {card_name} 已登记（ID: {card_id}），引用 {len(ids)} 个文件 ID；没有复制文件。"


def delete_task_data_card(card_id: str) -> str:
    """Delete task metadata and its links, leaving original user files untouched."""
    if delete_task_record(card_id):
        return f"任务 {card_id} 及其文件引用已删除；用户文件本身保留。"
    # One-time compatibility for an old JSON card that has not been migrated yet.
    base = PathManager.DATA_CARDS_DIR.resolve()
    legacy = (base / f"{card_id}.json").resolve()
    if legacy.parent != base:
        raise ValueError("旧任务卡 ID 不能包含目录路径。")
    if legacy.exists():
        legacy.unlink()
        return f"旧任务卡 {card_id} 已删除；文件本身保留。"
    return f"未找到任务 ID {card_id}。"


def describe_user_file(file_id: str) -> str:
    """Generate/update the catalog summary for one file ID."""
    return summarize_user_file(file_id)


def update_user_file_tags(file_id: str, tags: List[str]) -> str:
    """Set optional searchable tags on one catalog entry."""
    return set_user_file_tags(file_id, tags)


def link_user_files_to_task(task_id: str, file_ids: List[str], purpose: str = "") -> str:
    """Attach existing user file IDs to an existing or new task record."""
    sync_user_file_catalog()
    return link_files_to_task(task_id, file_ids, purpose)


def list_task_file_resources(task_id: str) -> str:
    """Return files already linked to a task or schedule ID."""
    rows = get_task_resources(task_id)
    if not rows:
        return f"任务 {task_id} 当前没有关联文件。"
    return "\n".join(
        f"- {row['file_id']} | {row['file_name']} | {row['rel_path']} | 用途={row.get('purpose') or '未注明'}"
        for row in rows
    )


def read_catalog_file(file_id: str) -> str:
    """Read one selected user file by its catalog ID, never by an arbitrary path."""
    return read_user_file(file_id)


def _load_ocr_image_reader_class():
    """Create the original BaseReader subclass only when OCR is requested."""
    with _INDEX_LOCK:
        existing = globals().get("OCRImageReader")
        if existing is not None:
            return existing

        from llama_index.core import Document
        from llama_index.core.readers.base import BaseReader

        class OCRImageReader(BaseReader):
            """Load OCR lazily only when an indexed document is an image."""

            def __init__(self):
                import easyocr

                self.reader = easyocr.Reader(["ch_sim", "en"], gpu=False)

            def load_data(self, file_path, extra_info=None):
                results = self.reader.readtext(str(file_path), detail=0)
                metadata = dict(extra_info or {})
                metadata["file_path"] = str(file_path)
                return [Document(text="\n".join(results), metadata=metadata)]

        globals()["OCRImageReader"] = OCRImageReader
        return OCRImageReader


def __getattr__(name: str):
    if name == "OCRImageReader":
        return _load_ocr_image_reader_class()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _get_file_extractor(paths=()):
    global _ocr_reader
    if not any(Path(path).suffix.lower() in _IMAGE_SUFFIXES for path in paths):
        return {}
    with _INDEX_LOCK:
        if _ocr_reader is None:
            _ocr_reader = _load_ocr_image_reader_class()()
        return {suffix: _ocr_reader for suffix in _IMAGE_SUFFIXES}


def _sync_index(paths: list[Path], storage_dir: Path):
    """Refresh changed documents and remove deleted documents from the same index."""
    with _INDEX_LOCK:
        index_file = storage_dir / "index_store.json"
        if not paths and not index_file.is_file():
            return None

        from llama_index.core import (
            SimpleDirectoryReader,
            StorageContext,
            VectorStoreIndex,
            load_index_from_storage,
        )

        documents = []
        if paths:
            # An unreadable file must not silently remove its old indexed nodes.
            documents = SimpleDirectoryReader(
                input_files=[str(path) for path in paths], filename_as_id=True,
                file_extractor=_get_file_extractor(paths), raise_on_error=True,
            ).load_data()
        if index_file.is_file():
            context = StorageContext.from_defaults(persist_dir=str(storage_dir))
            index = load_index_from_storage(context)
            active_doc_ids = {doc.get_doc_id() for doc in documents}
            obsolete = set(index.ref_doc_info) - active_doc_ids
            for doc_id in obsolete:
                index.delete_ref_doc(doc_id, delete_from_docstore=True)
            refreshed = index.refresh_ref_docs(documents) if documents else []
            if obsolete or any(refreshed):
                index.storage_context.persist(persist_dir=str(storage_dir))
        elif documents:
            # A child task-index directory alone does not mean a global index exists.
            if any((storage_dir / name).exists() for name in ("docstore.json", "default__vector_store.json")):
                raise ValueError(f"向量索引不完整，已保留文件，请先修复索引目录：{storage_dir}")
            storage_dir.mkdir(parents=True, exist_ok=True)
            index = VectorStoreIndex.from_documents(documents)
            index.storage_context.persist(persist_dir=str(storage_dir))
        else:
            return None
        return index if documents else None


def load_or_build_index():
    """Index only user-owned files, including removal of deleted documents."""
    global _embedding_model_name
    with _INDEX_LOCK:
        if _embedding_model_name != settings.EMBED_MODEL:
            from llama_index.core import Settings
            from llama_index.embeddings.huggingface import HuggingFaceEmbedding

            Settings.embed_model = HuggingFaceEmbedding(model_name=settings.EMBED_MODEL)
            _embedding_model_name = settings.EMBED_MODEL
    docs_dir = PathManager.USERS_DIR.resolve()
    docs_dir.mkdir(parents=True, exist_ok=True)
    paths = []

    def scan_error(error):
        raise error

    for current, directories, files in os.walk(docs_dir, onerror=scan_error):
        directories[:] = sorted(name for name in directories
                                if not name.startswith(".") and not (Path(current) / name).is_symlink())
        for name in sorted(files):
            path = Path(current) / name
            if name.startswith(".") or path.is_symlink():
                continue
            resolved = path.resolve()
            if docs_dir not in resolved.parents:
                raise ValueError("文档路径超出用户文件区。")
            if (path.is_file() and path.suffix.lower() in _INDEXABLE_SUFFIXES
                    and path.stat().st_size <= SUPPORTED_SUMMARY_BYTES):
                paths.append(path)
    model_dir = PathManager.VECTOR_INDEX_DIR / f"model_{hashlib.sha256(settings.EMBED_MODEL.encode('utf-8')).hexdigest()[:12]}"
    return _sync_index(paths, model_dir)


def search_personal_knowledge(query: str) -> str:
    """Build the private index on first use and refresh it only when files change."""
    query = str(query or "").strip()
    if not query or len(query) > 2000:
        raise ValueError("知识检索问题须为 1–2000 字。")

    # The catalog scan observes additions and edits without moving user files.
    sync_user_file_catalog()
    with _connect() as db:
        file_signature = tuple(
            (str(row["rel_path"]), int(row["size_bytes"]), int(row["mtime_ns"]))
            for row in db.execute(
                "SELECT rel_path,size_bytes,mtime_ns FROM files WHERE status='active' ORDER BY rel_path"
            ) if Path(str(row["rel_path"])).suffix.lower() in _INDEXABLE_SUFFIXES
            and int(row["size_bytes"]) <= SUPPORTED_SUMMARY_BYTES
        )
    if not file_signature:
        return "用户文件区尚无可索引的文档；请检查 data/users 中的文件格式和大小。"
    signature = (settings.EMBED_MODEL, file_signature)

    global _knowledge_index, _knowledge_signature
    with _INDEX_LOCK:
        if signature != _knowledge_signature or _knowledge_index is None:
            index = load_or_build_index()
            _knowledge_index = index
            _knowledge_signature = signature
        else:
            index = _knowledge_index
    if index is None:
        return "没有建立可用的个人知识索引；请检查文件格式和索引目录。"
    response = index.as_query_engine(similarity_top_k=6).query(query)
    return str(response)


def load_index_by_data_card(card_id: str):
    """Build an optional task index using stable file references in the catalog."""
    base = PathManager.CARD_VECTOR_INDEX_DIR.resolve()
    card_storage_dir = (base / str(card_id)).resolve()
    if card_storage_dir.parent != base or not str(card_id).strip():
        raise ValueError("任务 ID 不能包含目录路径。")
    sync_user_file_catalog()
    paths = get_task_file_paths(card_id)
    return _sync_index(paths, card_storage_dir)


@lru_cache(maxsize=1)
def _get_reranker():
    from llama_index.core.postprocessor import SentenceTransformerRerank

    return SentenceTransformerRerank(model="BAAI/bge-reranker-base", top_n=3)


def build_rag_tool(index: VectorStoreIndex, use_reranker: bool = False) -> QueryEngineTool:
    """Build the personal-knowledge query tool for an existing vector index."""
    from llama_index.core.tools import QueryEngineTool, ToolMetadata

    # Basic lookup does not need a second model download for re-ranking.
    query_engine = index.as_query_engine(
        similarity_top_k=10,
        node_postprocessors=[_get_reranker()] if use_reranker else [],
    )
    return QueryEngineTool(
        query_engine=query_engine,
        metadata=ToolMetadata(
            name="personal_knowledge_search",
            description="用于检索个人本地知识库中的私有文档、笔记、资料及历史记录。",
        ),
    )


