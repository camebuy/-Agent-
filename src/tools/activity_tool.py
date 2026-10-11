"""日程与消息决策卡片的细粒度操作。

日程和卡片共享一个工具模块，但继续使用各自的持久化文件、锁和函数入口。
"""

import copy
import json
import os
import re
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timedelta
from functools import wraps
from typing import Any, Callable, Dict, List, Literal, Optional, TypeVar

from src.core.system.path_manager import PathManager
from src.core.tasks.task_context import get_current_task
from src.core.system.terminal_logger import log_event
from src.tools.database_tool import (
    create_task_record,
    delete_task_record,
    get_task_resources,
    resolve_file_ids,
)

# 统一由 PathManager 调度隔离存储路径
SCHEDULE_DIR = PathManager.SCHEDULE_DIR
SCHEDULE_FILE = PathManager.SCHEDULE_FILE
SCHEDULE_LOCK = threading.RLock()
# Do not replay any occurrence that was already due when the app started.
MISFIRE_GRACE_SECONDS = 0


def _schedule_locked(func):
    @wraps(func)
    def wrapped(*args, **kwargs):
        with SCHEDULE_LOCK:
            return func(*args, **kwargs)
    return wrapped

SCHEDULE_DIR.mkdir(parents=True, exist_ok=True)


def _load_schedules() -> dict:
    try:
        with open(SCHEDULE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"fixed_schedules": [], "recurring_schedules": []}
    except (OSError, ValueError) as exc:
        # An unreadable file is not an empty schedule list. Never overwrite it.
        raise RuntimeError(f"日程文件无法读取，已保留原文件：{exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("日程文件格式错误：顶层必须是对象，已保留原文件。")
    for key in ("fixed_schedules", "recurring_schedules"):
        items = data.setdefault(key, [])
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise RuntimeError(f"日程文件格式错误：{key} 必须是对象列表，已保留原文件。")
    return data


def _save_schedules(data: dict) -> None:
    SCHEDULE_DIR.mkdir(parents=True, exist_ok=True)
    temp = SCHEDULE_FILE.with_name(SCHEDULE_FILE.name + ".tmp")
    with SCHEDULE_LOCK:
        try:
            with temp.open('w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, SCHEDULE_FILE)
        except OSError as exc:
            temp.unlink(missing_ok=True)
            raise RuntimeError(f"保存日程失败: {exc}") from exc


def _validate_time_or_rule(kind: str, value: str) -> str:
    value = str(value).strip()
    if kind == "fixed":
        try:
            due = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("固定日程时间必须包含有效日期和时间，例如 2026-10-02 16:00。") from exc
        if "T" not in value and " " not in value:
            raise ValueError("固定日程必须指定时间，不能只有日期。")
        now = datetime.now().astimezone()
        due = due.replace(tzinfo=now.tzinfo) if due.tzinfo is None else due.astimezone(now.tzinfo)
        if due <= now:
            raise ValueError("固定日程必须设置为未来时间；过去的时间不会自动补跑。")
    else:
        value = value.lower()
        _next_recurring_after({"rule": value}, datetime.now().astimezone())
    return value


def _resolve_schedule_resources(
    resource_ids: Optional[List[str]], resource_query: Optional[str],
) -> List[str]:
    """Resolve references in the canonical user-file catalog, without copying files.

    A free-text query is intentionally allowed to select only one file. If several
    files match, the caller must inspect the catalog and supply explicit IDs.
    """
    if resource_ids is not None and not isinstance(resource_ids, list):
        raise ValueError("resource_ids 必须是 file_id 列表。")
    ids = resource_ids or []
    if resource_query is not None and not isinstance(resource_query, str):
        raise ValueError("resource_query 必须是文件名、路径、摘要或标签关键词。")
    query = (resource_query or "").strip()
    if resource_query is not None and not query:
        raise ValueError("resource_query 不能为空；不需要文件时请省略这个参数。")
    if ids and query:
        raise ValueError("resource_ids 和 resource_query 只能选择一种；多个文件请提供明确的 file_id 列表。")
    if not ids and not query:
        return []

    # Import lazily so callers creating an ordinary schedule need no catalog scan.
    from src.tools.database_tool import get_file_path, list_user_files, sync_user_file_catalog

    sync_user_file_catalog()
    if query:
        rows = list_user_files(query, limit=500)
        # SQL LIKE treats % and _ as wildcards. Require a literal metadata match
        # and refuse a truncated candidate list before selecting a file.
        if len(rows) == 500:
            raise ValueError("匹配文件过多；请先用 search_user_files 查找并提供明确的 file_id。")
        needle = query.casefold()
        rows = [
            row for row in rows
            if any(
                needle in str(value or "").casefold()
                for value in (
                    row.get("file_name"), row.get("rel_path"), row.get("summary"),
                    *(row.get("tags") or []),
                )
            )
        ]
        if not rows:
            raise ValueError(f"用户文件目录中没有匹配“{query}”的文件；请先登记文件或提供 file_id。")
        if len(rows) != 1:
            choices = ", ".join(f"{row['file_id']} ({row['file_name']})" for row in rows[:8])
            raise ValueError(
                f"“{query}”匹配到 {len(rows)} 个文件：{choices}。"
                "请先用 search_user_files 查看并提供明确的 file_id；没有自动选择。"
            )
        ids = [str(rows[0]["file_id"])]

    resolved = resolve_file_ids(file_ids=ids)
    for file_id in resolved:
        get_file_path(file_id)  # An active catalog entry can outlive a deleted file.
    return resolved


@_schedule_locked
def add_schedule(
    schedule_type: Literal["fixed", "recurring"],
    title: str,
    time_or_rule: str,
    owner: Literal["user", "agent"],
    description: str = "",
    resource_ids: Optional[List[str]] = None,
    resource_query: Optional[str] = None,
    initial_status: Literal["active", "paused"] = "active",
    end_time: Optional[str] = None,
) -> str:
    if schedule_type not in ("fixed", "recurring"):
        raise ValueError("schedule_type must be 'fixed' or 'recurring'")
    title = str(title).strip()
    time_or_rule = _validate_time_or_rule(schedule_type, time_or_rule)
    owner = str(owner or "").strip().lower()
    if owner not in ("user", "agent"):
        raise ValueError("owner must be 'user' or 'agent'")
    if initial_status not in ("active", "paused"):
        raise ValueError("initial_status must be 'active' or 'paused'")
    if not title or not time_or_rule:
        raise ValueError("title and time_or_rule are required")
    if end_time is not None:
        if schedule_type != "fixed" or owner != "user":
            raise ValueError("结束时间只适用于固定的个人日程。")
        start_at = datetime.fromisoformat(time_or_rule)
        try:
            end_at = datetime.fromisoformat(str(end_time).strip())
        except ValueError as exc:
            raise ValueError("结束时间必须为有效日期和时间。") from exc
        if end_at.tzinfo is None and start_at.tzinfo is not None:
            end_at = end_at.replace(tzinfo=start_at.tzinfo)
        if start_at.tzinfo is None and end_at.tzinfo is not None:
            start_at = start_at.replace(tzinfo=end_at.tzinfo)
        if not start_at < end_at:
            raise ValueError("结束时间必须晚于开始时间。")
        end_time = end_at.isoformat(sep=" ", timespec="minutes")
    resource_ids = _resolve_schedule_resources(resource_ids, resource_query)

    data = _load_schedules()
    bucket_key = "fixed_schedules" if schedule_type == "fixed" else "recurring_schedules"
    value_key = "time" if schedule_type == "fixed" else "rule"
    for existing in data.get(bucket_key, []):
        if (
            existing.get("status", "active") in ("active", "running", "paused")
            and str(existing.get("title", "")).strip() == title
            and str(existing.get("owner", "user")).strip() == owner
            and str(existing.get(value_key, "")).strip() == time_or_rule
            and str(existing.get("description", "")).strip() == str(description).strip()
            and str(existing.get("end_time") or "") == str(end_time or "")
        ):
            existing_status = existing.get("status", "active")
            if (initial_status == "paused") != (existing_status == "paused"):
                raise ValueError(
                    f"相同日程已存在（ID: {existing.get('id')}，状态: {existing_status}）；"
                    "请明确更新或处理原日程，不能通过重复创建改变其授权状态。"
                )
            if resource_ids:
                create_task_record(
                    str(existing.get("id")), title, "schedule", description,
                    file_ids=resource_ids, purpose="日程执行所需文件",
                )
            return (
                f"An equivalent active schedule already exists (ID: {existing.get('id')}); "
                "the existing schedule was reused."
            )

    schedule_id = f"sch_{uuid.uuid4().hex[:6]}"
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    new_item = {
        "id": schedule_id,
        "title": title,
        "description": description,
        "owner": owner,
        "status": initial_status,
        "created_at": now_str
    }

    if schedule_type == "fixed":
        new_item["time"] = time_or_rule
        if end_time:
            new_item["end_time"] = end_time
        data.setdefault("fixed_schedules", []).append(new_item)
        type_name = "固定一次性日程"
    else:
        new_item["rule"] = time_or_rule
        data.setdefault("recurring_schedules", []).append(new_item)
        type_name = "循环日程"

    if resource_ids:
        create_task_record(
            schedule_id, title, "schedule", description,
            file_ids=resource_ids, purpose="日程执行所需文件",
        )
    try:
        _save_schedules(data)
    except Exception:
        if resource_ids:
            delete_task_record(schedule_id)
        raise
    reference_note = f" 已引用文件 ID：{', '.join(resource_ids)}。" if resource_ids else ""
    return f"已成功创建【{type_name}】(ID: {schedule_id}, 归属: {owner}): '{title}'，时间/规则: {time_or_rule}。{reference_note}"


def list_schedules(schedule_type: Optional[str] = "all", owner_filter: Optional[str] = None) -> str:
    data = _load_schedules()
    output = []

    if schedule_type in ["fixed", "all"]:
        output.append("=== 📌 固定/一次性日程 ===")
        fixed_items = [
            i for i in data.get("fixed_schedules", [])
            if i.get("status") != "cancelled" and (not owner_filter or i.get("owner", "user") == owner_filter)
        ]
        if not fixed_items:
            output.append("（暂无固定日程）")
        for item in fixed_items:
            owner_tag = f"[{item.get('owner', 'user')}]"
            time_label = item['time'] + (f" 至 {item['end_time']}" if item.get('end_time') else "")
            output.append(f"• [{item['id']}] {owner_tag} {item['title']} | 时间: {time_label} | 备注: {item.get('description', '无')}")
            linked = get_task_resources(str(item.get("id", "")))
            if linked:
                output.append("  引用文件: " + ", ".join(f"{x['file_id']} ({x['file_name']})" for x in linked))

    if schedule_type in ["recurring", "all"]:
        output.append("\n=== 🔄 循环/周期日程 ===")
        recurring_items = [
            i for i in data.get("recurring_schedules", [])
            if i.get("status") != "cancelled" and (not owner_filter or i.get("owner", "user") == owner_filter)
        ]
        if not recurring_items:
            output.append("（暂无循环日程）")
        for item in recurring_items:
            owner_tag = f"[{item.get('owner', 'user')}]"
            output.append(f"• [{item['id']}] {owner_tag} {item['title']} | 规则: {item['rule']} | 备注: {item.get('description', '无')}")
            linked = get_task_resources(str(item.get("id", "")))
            if linked:
                output.append("  引用文件: " + ", ".join(f"{x['file_id']} ({x['file_name']})" for x in linked))

    return "\n".join(output)


@_schedule_locked
def get_schedule_record(schedule_id: str) -> Optional[dict]:
    """Return a detached schedule snapshot for approval and runtime checks."""
    wanted = str(schedule_id or "").strip()
    if not wanted:
        return None
    data = _load_schedules()
    for kind, key in (("fixed", "fixed_schedules"), ("recurring", "recurring_schedules")):
        for item in data.get(key, []):
            if item.get("id") == wanted:
                return deepcopy({**item, "schedule_type": kind})
    return None


def user_calendar_conflicts(start_at: datetime, end_at: datetime) -> list[dict]:
    """Return overlapping personal appointments; unknown end blocks the remainder of its day."""
    if start_at.tzinfo is None or end_at.tzinfo is None or end_at <= start_at:
        raise ValueError("请提供带时区的有效起止时间。")
    with SCHEDULE_LOCK:
        data = _load_schedules()
    conflicts = []
    for item in data.get("fixed_schedules", []):
        if item.get("owner", "user") != "user" or item.get("status") == "cancelled":
            continue
        try:
            start = datetime.fromisoformat(str(item["time"]))
            start = start.replace(tzinfo=start_at.tzinfo) if start.tzinfo is None else start.astimezone(start_at.tzinfo)
            estimated = not bool(item.get("end_time"))
            end = (datetime.fromisoformat(str(item["end_time"])) if not estimated else
                   datetime.combine(start.date() + timedelta(days=1), datetime.min.time(), tzinfo=start_at.tzinfo))
            end = end.replace(tzinfo=start_at.tzinfo) if end.tzinfo is None else end.astimezone(start_at.tzinfo)
        except (KeyError, ValueError, TypeError):
            conflicts.append({"id": item.get("id"), "title": item.get("title"), "uncertain": True,
                              "reason": "日程时间格式无法解析"})
            continue
        if start < end_at and end > start_at:
            conflicts.append({"id": item.get("id"), "title": item.get("title"),
                              "start": start.isoformat(timespec="minutes"),
                              "end": end.isoformat(timespec="minutes"), "uncertain": estimated})
    for item in data.get("recurring_schedules", []):
        if item.get("owner", "user") != "user" or item.get("status") in {"cancelled", "paused"}:
            continue
        match = re.fullmatch(r"every_(day|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+(\d{1,2}):(\d{2})",
                             str(item.get("rule") or ""), re.I)
        if not match:
            conflicts.append({"id": item.get("id"), "title": item.get("title"), "uncertain": True,
                              "reason": "循环日程规则无法解析"})
            continue
        day_name, hour, minute = match.groups()
        weekdays = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
        day = start_at.date()
        while day <= end_at.date():
            if day_name.lower() == "day" or weekdays[day.weekday()] == day_name.lower():
                start = datetime.combine(day, datetime.min.time().replace(hour=int(hour), minute=int(minute)),
                                         tzinfo=start_at.tzinfo)
                end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=start_at.tzinfo)
                if start < end_at and end > start_at:
                    conflicts.append({"id": item.get("id"), "title": item.get("title"),
                                      "start": start.isoformat(timespec="minutes"),
                                      "end": end.isoformat(timespec="minutes"), "uncertain": True})
            day += timedelta(days=1)
    return conflicts


def check_user_availability(start_time: str, end_time: str) -> str:
    """Read personal calendar without altering it or claiming HR has agreed."""
    try:
        start = datetime.fromisoformat(str(start_time).strip())
        end = datetime.fromisoformat(str(end_time).strip())
    except ValueError as exc:
        raise ValueError("请提供 YYYY-MM-DD HH:MM 格式的起止时间。") from exc
    zone = datetime.now().astimezone().tzinfo
    start = start.replace(tzinfo=zone) if start.tzinfo is None else start.astimezone(zone)
    end = end.replace(tzinfo=zone) if end.tzinfo is None else end.astimezone(zone)
    if end <= start or start <= datetime.now().astimezone():
        raise ValueError("查询时段必须是未来，且结束晚于开始。")
    conflicts = user_calendar_conflicts(start, end)
    return json.dumps({"start": start.isoformat(timespec="minutes"),
                       "end": end.isoformat(timespec="minutes"), "available": not conflicts,
                       "conflicts": conflicts}, ensure_ascii=False)


def find_interview_slots(day: str, duration_minutes: int = 60, limit: int = 3) -> str:
    """List free slots inside the user's explicitly configured interview window."""
    from src.core.system.runtime_settings import load_runtime_settings

    try:
        selected = datetime.strptime(str(day).strip(), "%Y-%m-%d").date()
        duration_minutes = int(duration_minutes)
        limit = int(limit)
    except (ValueError, TypeError) as exc:
        raise ValueError("日期使用 YYYY-MM-DD；时长和数量必须是整数。") from exc
    if not 15 <= duration_minutes <= 120 or not 1 <= limit <= 8:
        raise ValueError("面试时长须为 15 到 120 分钟，候选数量为 1 到 8。")
    window = load_runtime_settings()["interview_window"]
    if not window["start"]:
        raise ValueError("尚未在设置中明确允许自动提议的面试时段。")
    zone = datetime.now().astimezone().tzinfo
    start = datetime.fromisoformat(f"{selected.isoformat()} {window['start']}").replace(tzinfo=zone)
    end = datetime.fromisoformat(f"{selected.isoformat()} {window['end']}").replace(tzinfo=zone)
    now = datetime.now().astimezone()
    slots = []
    cursor = start
    while cursor + timedelta(minutes=duration_minutes) <= end and len(slots) < limit:
        finish = cursor + timedelta(minutes=duration_minutes)
        if cursor > now and not user_calendar_conflicts(cursor, finish):
            slots.append({"start": cursor.isoformat(timespec="minutes"),
                          "end": finish.isoformat(timespec="minutes")})
            cursor = finish
        else:
            cursor += timedelta(minutes=30)
    return json.dumps({"day": selected.isoformat(), "slots": slots,
                       "note": "这些只是当前日历可用候选；向 HR 提议后仍需等待明确确认。"},
                      ensure_ascii=False)


@_schedule_locked
def update_schedule(
    schedule_id: str,
    new_title: Optional[str] = None,
    new_time_or_rule: Optional[str] = None,
    new_description: Optional[str] = None,
    new_status: Optional[str] = None,
    resource_ids: Optional[List[str]] = None,
    resource_query: Optional[str] = None,
    new_end_time: Optional[str] = None,
) -> str:
    data = _load_schedules()
    target_item = None

    all_items = data.get("fixed_schedules", []) + data.get("recurring_schedules", [])
    for item in all_items:
        if item.get("id") == schedule_id:
            target_item = item
            break

    if not target_item:
        raise ValueError(f"未找到 ID 为 {schedule_id} 的日程。")

    if target_item.get("status") in ("running", "awaiting_human"):
        raise ValueError("这项日程正在执行或等待审批，请先处理对应任务，再修改日程。")
    if new_status is not None and new_status not in ("active", "paused", "cancelled"):
        raise ValueError("日程只能手动设为 active、paused 或 cancelled；执行结果由任务自动更新。")
    kind = "fixed" if "time" in target_item else "recurring"
    validated_time = _validate_time_or_rule(kind, new_time_or_rule) if new_time_or_rule is not None else None
    if new_end_time is not None:
        if kind != "fixed" or target_item.get("owner") != "user":
            raise ValueError("结束时间只适用于固定的个人日程。")
        start_at = datetime.fromisoformat(validated_time or str(target_item["time"]))
        end_at = datetime.fromisoformat(str(new_end_time).strip())
        if start_at.tzinfo is None and end_at.tzinfo is not None:
            start_at = start_at.replace(tzinfo=end_at.tzinfo)
        if end_at.tzinfo is None and start_at.tzinfo is not None:
            end_at = end_at.replace(tzinfo=start_at.tzinfo)
        if end_at <= start_at:
            raise ValueError("结束时间必须晚于开始时间。")
    if kind == "fixed" and new_status == "active" and validated_time is None:
        # Authorization may arrive after the requested time. Never turn that
        # stale one-time schedule into an immediate surprise execution.
        _validate_time_or_rule("fixed", str(target_item.get("time") or ""))
    if new_title is not None and not str(new_title).strip():
        raise ValueError("日程标题不能为空。")

    resolved_resource_ids = _resolve_schedule_resources(resource_ids, resource_query)

    if new_title is not None:
        target_item["title"] = str(new_title).strip()
    if new_description is not None:
        target_item["description"] = new_description
    if new_status:
        target_item["status"] = new_status
    if validated_time is not None:
        target_item["time" if kind == "fixed" else "rule"] = validated_time
        target_item.pop("retry_after", None)
        target_item.pop("error", None)
    if new_end_time is not None:
        target_item["end_time"] = end_at.isoformat(sep=" ", timespec="minutes")
    elif validated_time is not None and target_item.get("end_time"):
        old_start = datetime.fromisoformat(str(target_item["time"]))
        old_end = datetime.fromisoformat(str(target_item["end_time"]))
        if old_end <= old_start:
            raise ValueError("修改开始时间后，结束时间必须仍晚于开始时间。")
    if kind == "recurring" and (validated_time is not None or new_status == "active"):
        target_item["next_run_at"] = _next_recurring_after(
            target_item, datetime.now().astimezone(),
        ).isoformat(timespec="seconds")

    _save_schedules(data)
    if resolved_resource_ids:
        create_task_record(
            schedule_id, str(target_item.get("title") or schedule_id), "schedule",
            str(target_item.get("description") or ""), file_ids=resolved_resource_ids,
            purpose="日程执行所需文件",
        )
    return f"日程 [{schedule_id}] 更新成功！"


@_schedule_locked
def delete_schedule(schedule_id: str) -> str:
    data = _load_schedules()
    deleted = False

    for item in data.get("fixed_schedules", []) + data.get("recurring_schedules", []):
        if item.get("id") == schedule_id and item.get("status") in ("running", "awaiting_human"):
            raise ValueError("这项日程正在执行或等待审批，请先处理对应任务，再删除日程。")

    for key in ["fixed_schedules", "recurring_schedules"]:
        if key in data:
            initial_len = len(data[key])
            data[key] = [i for i in data[key] if i.get("id") != schedule_id]
            if len(data[key]) < initial_len:
                deleted = True
                break

    if deleted:
        _save_schedules(data)
        delete_task_record(schedule_id)
        return f"日程 [{schedule_id}] 已从数据库中彻底移除。"
    raise ValueError(f"未找到 ID 为 {schedule_id} 的日程。")


def _next_recurring_after(item: dict, base: datetime) -> datetime:
    match = re.fullmatch(
        r"every_(day|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+(\d{1,2}):(\d{2})",
        str(item.get("rule", "")),
        re.I,
    )
    if not match:
        raise ValueError(f"Unsupported recurring schedule rule: {item.get('rule')}")

    day_name, hour, minute = match.groups()
    weekdays = {
        name: i
        for i, name in enumerate(
            ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
        )
    }
    weekday = None if day_name.lower() == "day" else weekdays[day_name.lower()]

    for offset in range(8):
        day = base.date() + timedelta(days=offset)
        if weekday is None or day.weekday() == weekday:
            candidate = datetime.combine(
                day,
                datetime.min.time().replace(hour=int(hour), minute=int(minute)),
            ).replace(tzinfo=base.tzinfo)
            if candidate > base:
                return candidate
    raise ValueError("Could not calculate the next recurring schedule time")


def _due_datetime(item: dict, kind: str, now: datetime) -> datetime:
    if kind == "fixed":
        due = datetime.fromisoformat(str(item["time"]))
        return due.replace(tzinfo=now.tzinfo) if due.tzinfo is None else due.astimezone(now.tzinfo)

    if item.get("next_run_at"):
        due = datetime.fromisoformat(str(item["next_run_at"]))
        return due.replace(tzinfo=now.tzinfo) if due.tzinfo is None else due.astimezone(now.tzinfo)

    base_text = item.get("last_run_at") or item.get("created_at")
    if base_text:
        base = datetime.fromisoformat(str(base_text))
        base = base.replace(tzinfo=now.tzinfo) if base.tzinfo is None else base.astimezone(now.tzinfo)
    else:
        base = now
    return _next_recurring_after(item, base)

def claim_due_schedules(
    now: Optional[datetime] = None,
    apply_startup_misfire_policy: bool = False,
) -> list:
    now = (now or datetime.now().astimezone()).astimezone()
    claimed = []
    with SCHEDULE_LOCK:
        data = _load_schedules()
        changed = False
        for kind, key in (("fixed", "fixed_schedules"), ("recurring", "recurring_schedules")):
            for item in data.get(key, []):
                if item.get("status", "active") != "active":
                    continue
                try:
                    if not item.get("id") or not item.get("title"):
                        raise ValueError("日程缺少 id 或 title，已停止自动派发。")
                    retry = datetime.fromisoformat(item["retry_after"]) if item.get("retry_after") else None
                    if retry:
                        retry = retry.replace(tzinfo=now.tzinfo) if retry.tzinfo is None else retry.astimezone(now.tzinfo)
                    due_at = _due_datetime(item, kind, now)
                    if due_at <= now:
                        lateness = (now - due_at).total_seconds()
                        if apply_startup_misfire_policy and lateness > MISFIRE_GRACE_SECONDS:
                            if kind == "fixed":
                                item.update(
                                    status="missed",
                                    missed_at=now.isoformat(timespec="seconds"),
                                    error="Missed while the app was stopped; automatic replay was skipped.",
                                )
                            else:
                                item["last_missed_at"] = due_at.isoformat(timespec="seconds")
                                item["next_run_at"] = _next_recurring_after(item, now).isoformat(timespec="seconds")
                                item["error"] = "A missed occurrence was skipped; the next occurrence remains scheduled."
                            item.pop("retry_after", None)
                            item.pop("claimed_at", None)
                            changed = True
                            continue
                        if retry and retry > now:
                            continue
                        if kind == "recurring":
                            item["next_run_at"] = due_at.isoformat(timespec="seconds")
                        item.update(status="running", claimed_at=now.isoformat(timespec="seconds"))
                        claimed.append({**item, "schedule_type": kind})
                        changed = True
                except (KeyError, ValueError, TypeError, OverflowError) as exc:
                    item.update(status="failed", error=str(exc))
                    changed = True
        if changed:
            _save_schedules(data)
    return claimed


def get_running_schedules() -> list:
    data = _load_schedules()
    return [{**item, "schedule_type": kind}
            for kind, key in (("fixed", "fixed_schedules"), ("recurring", "recurring_schedules"))
            for item in data.get(key, []) if item.get("status") == "running"]


def set_schedule_run_state(
    schedule_id: str, status: str, *, expected_job_id: Optional[str] = None,
    expected_status: Optional[str] = None, expected_job_absent: bool = False, **fields,
) -> bool:
    with SCHEDULE_LOCK:
        data = _load_schedules()
        for key in ("fixed_schedules", "recurring_schedules"):
            for item in data.get(key, []):
                if item.get("id") == schedule_id:
                    if expected_job_absent and item.get("job_id"):
                        return False
                    if expected_job_id is not None and item.get("job_id") != expected_job_id:
                        return False
                    if expected_status is not None and item.get("status") != expected_status:
                        return False
                    item.update(status=status, **fields)
                    _save_schedules(data)
                    return True
    return False


def finish_schedule(
    schedule_id: str,
    success: bool,
    error: Optional[str] = None,
    cancelled: bool = False,
    missed: bool = False,
    expected_job_id: Optional[str] = None,
) -> bool:
    now = datetime.now().astimezone()
    with SCHEDULE_LOCK:
        data = _load_schedules()
        for kind, key in (("fixed", "fixed_schedules"), ("recurring", "recurring_schedules")):
            for item in data.get(key, []):
                if item.get("id") != schedule_id:
                    continue
                if expected_job_id is not None and (
                    item.get("job_id") != expected_job_id or item.get("status") != "running"
                ):
                    return False
                missed_occurrence = item.get("next_run_at") or item.get("time") or item.get("claimed_at")
                item["last_run_at"] = now.isoformat(timespec="seconds")
                item.pop("job_id", None)
                item.pop("task_id", None)
                item.pop("binding_key", None)
                item.pop("binding_at", None)
                item.pop("binding_previous_job_id", None)
                item.pop("binding_instance_id", None)
                item.pop("claimed_at", None)
                item.pop("retry_after", None)
                if success or cancelled:
                    item.pop("error", None)
                if missed and kind == "recurring":
                    item["status"] = "active"
                    item["last_missed_at"] = missed_occurrence or now.isoformat(timespec="seconds")
                    item["next_run_at"] = _next_recurring_after(item, now).isoformat(timespec="seconds")
                    item["error"] = error or "错过的循环日程已跳过，后续周期仍会执行。"
                elif missed:
                    item["status"] = "missed"
                    item["missed_at"] = now.isoformat(timespec="seconds")
                    item["error"] = error or "应用关闭期间错过；未自动补跑。"
                elif cancelled and kind == "recurring":
                    item["status"] = "active"
                    item.pop("next_run_at", None)
                    item["next_run_at"] = _due_datetime(item, kind, now).isoformat(timespec="seconds")
                    if error:
                        item["error"] = error
                elif cancelled:
                    item["status"] = "cancelled"
                    if error:
                        item["error"] = error
                elif success and kind == "recurring":
                    item["status"] = "active"
                    item.pop("next_run_at", None)
                    item["next_run_at"] = _due_datetime(item, kind, now).isoformat(timespec="seconds")
                    item.pop("error", None)
                elif kind == "recurring":
                    # A failed occurrence must not silently disable future polling.
                    item["status"] = "active"
                    item["last_failed_at"] = now.isoformat(timespec="seconds")
                    item["error"] = error or "本次执行失败；下一周期仍将运行。"
                    item["next_run_at"] = _next_recurring_after(item, now).isoformat(timespec="seconds")
                else:
                    item["status"] = "completed" if success else "failed"
                    if error:
                        item["error"] = error
                _save_schedules(data)
                return True
    return False


# 消息卡片路径由 PathManager 统一管理。
INBOX_FILE = PathManager.INBOX_FILE
_CARDS_LOCK = threading.RLock()
_T = TypeVar("_T")


def _read_cards_unlocked() -> List[Dict[str, Any]]:
    if not INBOX_FILE.exists():
        return []
    with INBOX_FILE.open("r", encoding="utf-8") as stream:
        data = json.load(stream)
    if not isinstance(data, list) or any(not isinstance(card, dict) for card in data):
        raise ValueError("消息决策中心数据损坏：应为卡片列表；原文件已保留。")
    return data


def read_cards() -> List[Dict[str, Any]]:
    """Read a detached snapshot. Corrupt data must never be replaced by an empty list."""
    with _CARDS_LOCK:
        return _read_cards_unlocked()


def mutate_cards(callback: Callable[[List[Dict[str, Any]]], _T]) -> _T:
    """Apply a mutation to the latest cards under one read/modify/write lock."""
    with _CARDS_LOCK:
        cards = _read_cards_unlocked()
        before = copy.deepcopy(cards)
        result = callback(cards)
        if cards != before:
            _save_cards(cards)
        return copy.deepcopy(result)


def _save_cards(cards: List[Dict[str, Any]]) -> None:
    """内部辅助函数：持久化卡片数据"""
    INBOX_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_file = INBOX_FILE.with_name(INBOX_FILE.name + f".{uuid.uuid4().hex}.tmp")
    try:
        with temp_file.open("w", encoding="utf-8") as f:
            json.dump(cards, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_file, INBOX_FILE)
    finally:
        temp_file.unlink(missing_ok=True)


def respond_to_card(card_id: str, choice: Any, comment: str = "") -> Dict[str, Any]:
    """Persist one valid decision; repeated UI clicks cannot rewrite an earlier vote."""
    def update(cards):
        card = next((item for item in cards if item.get("card_id") == card_id), None)
        if card is None:
            raise ValueError(f"卡片 {card_id} 已不存在，请刷新消息中心。")
        options = card.get("options") or []
        multiple = card.get("selection_mode", "single") == "multiple"
        choices = choice if isinstance(choice, list) else [choice]
        if not choices or any(value not in options for value in choices):
            raise ValueError("请选择卡片中提供的有效选项。")
        if len(set(choices)) != len(choices) or (not multiple and len(choices) != 1):
            raise ValueError("卡片选项数量无效。")
        saved_choice = choices if multiple else choices[0]
        saved_comment = str(comment or "").strip() or None
        if card.get("status") != "pending":
            if card.get("user_choice") == saved_choice and card.get("user_comment") == saved_comment:
                return card
            raise ValueError("此卡片已经处理，不能重复修改审批结果。")
        normalized = str(saved_choice).strip().lower() if not multiple else ""
        status = "responded"
        if normalized in ("approve", "approved", "yes", "同意"):
            status = "approved"
        elif normalized in ("reject", "rejected", "no", "拒绝"):
            status = "rejected"
        elif normalized in ("acknowledge", "ack", "noted", "知晓"):
            status = "acknowledged"
        card.update(status=status, user_choice=saved_choice, user_comment=saved_comment,
                    responded_at=datetime.now().isoformat(timespec="seconds"))
        return card
    return mutate_cards(update)


def send_user_request(
        title: str,
        content: str,
        options: Optional[List[str]] = None,
        action_type: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
        selection_mode: str = "single",
        resume_task_on_response: bool = True,
        idempotency_key: Optional[str] = None,
) -> str:
    """AI 主动向用户发送请求或通知卡片"""
    if options is None:
        options = ["approve", "reject"]
    if not isinstance(options, list) or not options or any(not isinstance(x, str) or not x.strip() for x in options):
        raise ValueError("options must be a nonempty list of text choices")
    if len(set(options)) != len(options):
        raise ValueError("options must be unique")
    selection_mode = str(selection_mode or "single").strip().lower()
    if selection_mode not in ("single", "multiple"):
        raise ValueError("selection_mode must be 'single' or 'multiple'")

    idempotency_key = str(idempotency_key or "").strip()
    card_id = f"card_{uuid.uuid4().hex[:8]}"

    new_card = {
        "card_id": card_id,
        "title": title,
        "content": content,
        "options": options,
        "selection_mode": selection_mode,
        "action_type": action_type,
        "payload": payload or {},
        "status": "pending",
        "user_choice": None,
        "user_comment": None,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    if idempotency_key:
        new_card["idempotency_key"] = idempotency_key
    task_context = get_current_task()
    if task_context.get("task_id"):
        new_card["workflow"] = {
            "task_id": str(task_context["task_id"]),
            "job_id": str(task_context.get("job_id") or ""),
            "conversation_id": str(task_context.get("conversation_id") or "default"),
            "schedule_id": task_context.get("schedule_id"),
            "resume_on_response": bool(resume_task_on_response),
        }

    def insert(cards):
        if idempotency_key:
            existing = next((card for card in cards if card.get("idempotency_key") == idempotency_key), None)
            if existing:
                return existing["card_id"]
        cards.insert(0, new_card)
        return card_id
    persisted_id = mutate_cards(insert)
    if persisted_id != card_id:
        return f"等价请求已存在，复用消息卡片 ID: {persisted_id}。"

    log_event("TOOL_CALL", f"下发决策卡片到消息中心: 【{title}】 (ID: {card_id})", "WARN")
    return f"请求已成功发送至用户消息中心，卡片 ID: {card_id}，正在等待用户决策..."


def delete_user_request(card_id: str) -> str:
    """删除消息决策中心指定的决策卡片"""
    def delete(cards):
        card = next((item for item in cards if item.get("card_id") == card_id), None)
        if not card:
            return False
        if (card.get("workflow") or {}).get("task_id"):
            raise ValueError("任务关联卡片需要保留审批记录，不能通过工具删除。")
        cards.remove(card)
        return True
    if mutate_cards(delete):
        log_event("TOOL_CALL", f"决策卡片 {card_id} 已成功删除", "INFO")
        return f"已成功删除卡片 {card_id}。"
    else:
        return f"未找到卡片 ID 为 {card_id} 的卡片。"


def clear_all_cards() -> str:
    """一键清空消息决策中心的所有卡片"""
    def clear(cards):
        initial = len(cards)
        cards[:] = [card for card in cards if (card.get("workflow") or {}).get("task_id")]
        return initial - len(cards)
    count = mutate_cards(clear)
    log_event("TOOL_CALL", f"已清理 {count} 张独立消息卡片，任务审批记录已保留", "WARN")
    return f"已清理 {count} 张独立消息卡片；任务关联卡片保留用于审批和恢复。"
