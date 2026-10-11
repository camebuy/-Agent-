"""Dispatch due schedules through the durable chat job manager."""
import atexit
import sqlite3
import threading
import uuid
import time
from datetime import datetime, timedelta
from typing import Optional
from src.core.system.path_manager import PathManager
from src.core.system.terminal_logger import log_event
from src.tools.activity_tool import (
    claim_due_schedules, finish_schedule, get_running_schedules,
    get_schedule_record, set_schedule_run_state, send_user_request,
)
from src.tools.database_tool import get_task_resources


def schedule_occurrence_key(schedule: dict) -> str:
    """Stable identity of one fixed or recurring schedule occurrence."""
    schedule_id = str(schedule.get("id") or "")
    if not schedule_id:
        raise ValueError("日程缺少 ID，无法确认执行批次。")
    occurrence = (
        schedule.get("time") if schedule.get("schedule_type") == "fixed"
        else schedule.get("next_run_at") or schedule.get("claimed_at") or "pending"
    )
    return f"schedule:{schedule_id}:{occurrence}"


def get_schedule_lineage(
    schedule_id: str, job_id: str, source: str,
    parent_job_id: str | None = None,
) -> Optional[tuple[str, frozenset[str]]]:
    """Return (scheduled origin job, all same-occurrence job IDs) if bound now."""
    schedule_id = str(schedule_id or "")
    job_id = str(job_id or "")
    source = str(source or "")
    if not schedule_id or not job_id or source not in {"schedule", "approval", "recovery"}:
        return None
    schedule = get_schedule_record(schedule_id)
    if (not schedule or schedule.get("owner") != "agent"
            or schedule.get("status") != "running"
            or str(schedule.get("job_id") or "") != job_id):
        return None
    occurrence_key = schedule_occurrence_key(schedule)
    db_path = PathManager.CHAT_RUNTIME_DB
    if not db_path.exists():
        return None
    try:
        with sqlite3.connect(str(db_path), timeout=3) as conn:
            conn.row_factory = sqlite3.Row
            records = conn.execute(
                """SELECT job_id, parent_job_id, source, schedule_id, idempotency_key
                   FROM chat_jobs WHERE schedule_id=?""",
                (schedule_id,),
            ).fetchall()
    except sqlite3.Error:
        return None
    rows = {str(row["job_id"]): dict(row) for row in records}
    current = rows.get(job_id)
    if not current or str(current.get("source") or "") != source:
        return None
    if parent_job_id and str(current.get("parent_job_id") or "") != str(parent_job_id):
        return None

    def origin_for(candidate_id: str) -> Optional[dict]:
        seen: set[str] = set()
        current_id = candidate_id
        for _ in range(24):
            if not current_id or current_id in seen:
                return None
            seen.add(current_id)
            row = rows.get(current_id)
            if not row:
                return None
            if row.get("source") == "schedule":
                return row
            current_id = str(row.get("parent_job_id") or "")
        return None

    origin = origin_for(job_id)
    if (not origin or str(origin.get("schedule_id") or "") != schedule_id
            or str(origin.get("idempotency_key") or "") != occurrence_key):
        return None
    origin_id = str(origin["job_id"])
    related = frozenset(
        candidate_id for candidate_id in rows
        if (candidate_origin := origin_for(candidate_id)) is not None
        and str(candidate_origin.get("job_id") or "") == origin_id
    )
    return origin_id, related


class ScheduleRunner:
    def __init__(self, job_manager, conversation_id="default", poll_seconds=1):
        self.jobs = job_manager
        self.conversation_id = conversation_id
        self.interval = max(0.25, float(poll_seconds))
        self._startup_misfire_check = True
        self._health_lock = threading.RLock()
        self._last_poll_at = None
        self._last_error = ""
        self._approval_error = ""
        self._last_approval_reconcile = 0.0
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name="schedule-runner", daemon=True)
        self.thread.start()
        atexit.register(self.stop)

    def _run(self):
        while not self.stop_event.is_set():
            try:
                if time.monotonic() - self._last_approval_reconcile >= 5:
                    try:
                        from src.core.tasks.task_actions import reconcile_approval_responses
                        reconcile_approval_responses(self.jobs)
                        with self._health_lock:
                            self._approval_error = ""
                    except Exception as exc:
                        with self._health_lock:
                            self._approval_error = f"{type(exc).__name__}: {exc}"
                        log_event("TASK", f"审批恢复暂不可用：{exc}", "ERROR")
                    self._last_approval_reconcile = time.monotonic()
                self._poll()
                self._dispatch()
                with self._health_lock:
                    self._last_poll_at = datetime.now().astimezone()
                    self._last_error = ""
            except Exception as exc:
                with self._health_lock:
                    self._last_error = f"{type(exc).__name__}: {exc}"
                log_event("SCHEDULE", f"后台调度循环异常: {exc}", "ERROR")
            self.stop_event.wait(self.interval)

    def health_snapshot(self) -> dict:
        with self._health_lock:
            last_poll = self._last_poll_at
            last_error = self._last_error
            approval_error = self._approval_error
        age = None
        if last_poll is not None:
            age = max(0.0, (datetime.now().astimezone() - last_poll).total_seconds())
        return {
            "thread_alive": self.thread.is_alive(),
            "last_poll_at": last_poll.isoformat(timespec="seconds") if last_poll else None,
            "last_poll_age_seconds": age,
            "last_error": last_error,
            "approval_error": approval_error,
            "healthy": self.thread.is_alive() and not last_error and (age is None or age < max(30, self.interval * 10)),
        }

    def _poll(self):
        for item in get_running_schedules():
            job_id = item.get("job_id")
            binding_key = str(item.get("binding_key") or schedule_occurrence_key(item))
            if not job_id:
                # Recover occurrences left by older versions at the enqueue/save boundary.
                existing = self.jobs.get_job_by_idempotency_key(binding_key)
                if existing:
                    job_id = existing["job_id"]
                    self._attach_job(item, job_id, existing.get("task_id"), binding_key=binding_key)
                else:
                    # No action was queued. The next dispatch applies startup /
                    # sleep misfire policy immediately, without a delayed replay.
                    reset = set_schedule_run_state(
                        item["id"], "active", job_id=None, task_id=None,
                        binding_key=None, binding_at=None,
                        binding_previous_job_id=None, binding_instance_id=None,
                        expected_status="running", expected_job_absent=True,
                    )
                    if reset:
                        self._startup_misfire_check = True
                    continue
            job = self.jobs.get_job(job_id)
            if not job:
                # The schedule JSON is written inside submit's SQLite
                # transaction, but a concurrent reader may see that JSON
                # before the transaction commits. Leave a short window for
                # the commit instead of relinking an older occurrence.
                if self._binding_is_recent(item):
                    continue
                existing = self.jobs.get_job_by_idempotency_key(binding_key)
                if existing:
                    job_id = existing["job_id"]
                    if str(existing["job_id"]) != str(item.get("job_id")):
                        self._attach_job(item, job_id, existing.get("task_id"), binding_key=binding_key)
                    job = existing
                else:
                    previous_job_id = str(item.get("binding_previous_job_id") or "")
                    if previous_job_id:
                        previous = self.jobs.get_job(previous_job_id)
                        if previous:
                            prior_status = (
                                "awaiting_human" if previous.get("status") == "awaiting_human"
                                else "running"
                            )
                            set_schedule_run_state(
                                item["id"], prior_status, job_id=previous_job_id,
                                task_id=previous.get("task_id"),
                                error=("等待你在消息决策中心处理卡片。"
                                       if prior_status == "awaiting_human" else ""),
                                binding_key=previous.get("idempotency_key"),
                                binding_at=None, binding_previous_job_id=None,
                                binding_instance_id=None,
                                expected_job_id=job_id, expected_status="running",
                            )
                            continue
                        set_schedule_run_state(
                            item["id"], "failed", job_id=None, task_id=None,
                            error="审批或恢复任务的关联作业未提交，原作业也已丢失；请手动核对。",
                            binding_key=None, binding_at=None,
                            binding_previous_job_id=None, binding_instance_id=None,
                            expected_job_id=job_id, expected_status="running",
                        )
                    else:
                        # A new occurrence was never committed. Treat this as
                        # a startup recovery, so an overdue run is skipped.
                        reset = set_schedule_run_state(
                            item["id"], "active", job_id=None, task_id=None,
                            error="关联任务未提交，将重新检查此日程。",
                            binding_key=None, binding_at=None,
                            binding_previous_job_id=None, binding_instance_id=None,
                            expected_job_id=job_id, expected_status="running",
                        )
                        if reset:
                            self._startup_misfire_check = True
                    continue
            if job and job.get("status") == "awaiting_human":
                set_schedule_run_state(
                    item["id"], "awaiting_human", job_id=job_id,
                    task_id=job.get("task_id"), error="等待你在消息决策中心处理卡片。",
                    expected_job_id=job_id, expected_status="running",
                )
            elif job and job.get("status") in ("completed", "failed", "interrupted", "cancelled", "missed"):
                ok = job.get("status") == "completed"
                cancelled = job.get("status") == "cancelled"
                missed = job.get("status") == "missed"
                finished = finish_schedule(
                    item["id"], ok,
                    None if ok else job.get("error"),
                    cancelled=cancelled,
                    missed=missed,
                    expected_job_id=job_id,
                )
                if finished:
                    log_event("SCHEDULE", f"日程 [{item.get('title')}] 执行结束: {job.get('status')}",
                              "SUCCESS" if ok else ("WARN" if missed or cancelled else "ERROR"))
                    if (item.get("schedule_type") == "recurring" and not ok
                            and not cancelled and not missed):
                        from src.tools.activity_tool import send_user_request
                        try:
                            send_user_request(
                                title=f"循环日程本次执行失败：{item.get('title') or item['id']}",
                                content=(f"日程 ID：{item['id']}\n本次任务：{job_id}\n"
                                         f"错误：{job.get('error') or '未提供错误详情'}\n"
                                         "下一周期仍会按原规则运行，请检查运行日志。"),
                                options=["acknowledge"], action_type="SCHEDULE_RUN_FAILED",
                                payload={"schedule_id": item["id"], "job_id": job_id},
                                resume_task_on_response=False,
                                idempotency_key=f"schedule-failed:{item['id']}:{job_id}",
                            )
                        except Exception as exc:
                            log_event("SCHEDULE", f"失败提醒卡未能保存：{exc}", "ERROR")

    def _binding_is_recent(self, item):
        current_instance = str(getattr(self.jobs, "instance_id", "") or "")
        if (not current_instance or
                str(item.get("binding_instance_id") or "") != current_instance):
            return False
        value = str(item.get("binding_at") or "")
        if not value:
            return False
        try:
            bound_at = datetime.fromisoformat(value)
            if bound_at.tzinfo is None:
                bound_at = bound_at.astimezone()
            age = (datetime.now().astimezone() - bound_at).total_seconds()
            return 0 <= age < 30
        except ValueError:
            return False

    def _attach_job(self, item, job_id, task_id, *, binding_key=None):
        old_job_id = item.get("job_id")
        binding_key = binding_key or item.get("binding_key") or schedule_occurrence_key(item)
        if not set_schedule_run_state(
            item["id"], "running", job_id=job_id, task_id=task_id,
            error="", expected_status="running",
            expected_job_id=old_job_id if old_job_id else None,
            expected_job_absent=not bool(old_job_id),
            binding_key=binding_key,
            binding_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            binding_previous_job_id=None,
            binding_instance_id=getattr(self.jobs, "instance_id", None),
        ):
            raise RuntimeError("日程状态已改变，无法关联后台任务。")

    def _dispatch(self):
        now = datetime.now().astimezone()
        resumed_after_pause = self._last_poll_at is not None and (
            now - self._last_poll_at
        ).total_seconds() > max(30, self.interval * 10)
        claimed_schedules = claim_due_schedules(
            now,
            apply_startup_misfire_policy=self._startup_misfire_check or resumed_after_pause,
        )
        self._startup_misfire_check = False
        for item in claimed_schedules:
            owner = str(item.get("owner") or "user").strip().lower()
            idempotency_key = schedule_occurrence_key(item)
            if owner != "agent":
                try:
                    when = item.get("time") or item.get("rule") or "已到时间"
                    details = str(item.get("description") or "")
                    content = f"时间：{when}\n事项：{item.get('title', '未命名日程')}"
                    if details:
                        content += f"\n备注：{details}"
                    send_user_request(
                        title=f"日程提醒：{item.get('title', '未命名日程')}",
                        content=content,
                        options=["acknowledge"],
                        action_type="SCHEDULE_REMINDER",
                        payload={"schedule_id": str(item.get("id") or "")},
                        resume_task_on_response=False,
                        idempotency_key=idempotency_key,
                    )
                    finish_schedule(str(item["id"]), True)
                    log_event("SCHEDULE", f"已发送个人日程提醒 [{item.get('title', '')}]", "SUCCESS")
                except Exception as exc:
                    set_schedule_run_state(
                        item["id"], "active",
                        retry_after=(now + timedelta(seconds=5)).isoformat(timespec="seconds"),
                        error=f"日程提醒发送失败，将重试：{exc}",
                    )
                continue
            # An earlier attempt may already own this occurrence. Link it even
            # when it is the active job, instead of deferring the schedule.
            existing = self.jobs.get_job_by_idempotency_key(idempotency_key)
            if existing:
                self._attach_job(item, existing["job_id"], existing.get("task_id"),
                                 binding_key=idempotency_key)
                continue
            if self.jobs.get_active_job(self.conversation_id):
                set_schedule_run_state(
                    item["id"], "active", job_id=None, task_id=None,
                    retry_after=(now + timedelta(seconds=2)).isoformat(timespec="seconds"),
                    expected_status="running", expected_job_absent=True,
                )
                continue
            task_id = f"task_{uuid.uuid4().hex[:10]}"
            bound_job_id = None
            try:
                resources = get_task_resources(str(item.get("id", "")))
                resource_context = ""
                if resources:
                    refs = "\n".join(
                        f"- file_id={row['file_id']} | {row['file_name']} | {row['rel_path']}"
                        for row in resources
                    )
                    resource_context = (
                        "\n\n此任务已关联以下用户文件。需要文件内容时，使用 read_user_file(file_id) 读取；"
                        "不要复制、移动或重新分类这些文件：\n" + refs
                    )
                from src.tools.recruitment_tool import (
                    approved_schedule_policy_context,
                    approved_hr_auto_reply_context,
                )
                schedule_id = str(item.get("id") or "")
                policy_context = (
                    approved_schedule_policy_context(schedule_id)
                    + approved_hr_auto_reply_context(schedule_id)
                )
                prompt = (f"定时任务已到执行时间。请按要求实际执行并返回详细结果。\n"
                          f"任务：{item.get('title', '')}\n执行步骤：\n"
                          f"{item.get('description') or item.get('title', '')}"
                          f"{resource_context}{policy_context}")
                def bind_before_enqueue(job_id, actual_task_id):
                    nonlocal bound_job_id
                    bound_job_id = job_id
                    self._attach_job(item, job_id, actual_task_id, binding_key=idempotency_key)

                self.jobs.submit(
                    self.conversation_id, prompt,
                    task_id=task_id, source="schedule", schedule_id=str(item["id"]),
                    idempotency_key=idempotency_key,
                    before_enqueue=bind_before_enqueue,
                )
            except Exception as exc:
                # A commit can fail after the schedule file is written. Check
                # the idempotency record before clearing the saved job ID.
                try:
                    existing = self.jobs.get_job_by_idempotency_key(idempotency_key)
                except Exception as lookup_exc:
                    log_event("SCHEDULE", f"日程 [{item.get('title', '')}] 提交状态暂无法核对：{lookup_exc}", "ERROR")
                    continue
                if existing:
                    current = get_schedule_record(str(item["id"]))
                    if not current or current.get("job_id") != existing["job_id"]:
                        self._attach_job(item, existing["job_id"], existing.get("task_id"),
                                         binding_key=idempotency_key)
                    continue
                set_schedule_run_state(
                    item["id"], "active", job_id=None, task_id=None,
                    retry_after=(now + timedelta(seconds=5)).isoformat(timespec="seconds"),
                    error=f"后台队列暂不可用，将重试：{exc}",
                    binding_key=None, binding_at=None, binding_previous_job_id=None,
                    binding_instance_id=None,
                    expected_status="running",
                    expected_job_id=bound_job_id if bound_job_id else None,
                    expected_job_absent=not bool(bound_job_id),
                )
                continue
            log_event("SCHEDULE", f"开始执行日程 [{item.get('title', '')}]", "INFO")

    def stop(self):
        self.stop_event.set()
        if self.thread.is_alive() and threading.current_thread() is not self.thread:
            self.thread.join(timeout=2)
