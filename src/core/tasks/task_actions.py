"""Task actions shared by the UI and unattended schedule runner."""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any, Optional

from chat_job_manager import DEFAULT_CONVERSATION_ID
from src.core.system.ledger import ledger
from src.core.tasks.schedule_runner import schedule_occurrence_key
from src.core.system.terminal_logger import log_event
from src.tools.activity_tool import (
    get_schedule_record, mutate_cards, read_cards, respond_to_card, set_schedule_run_state,
)

_RESUME_LOCK = threading.RLock()
_RESOLVED = {"approved", "rejected", "acknowledged", "responded"}


def _workflow(card: dict) -> dict:
    return card.get("workflow") or {}


def _schedule_origin_job(manager: Any, job: dict) -> Optional[dict]:
    """Find the scheduled occurrence behind approval/recovery follow-ups."""
    seen = set()
    for _ in range(20):
        if not job:
            return None
        if job.get("source") == "schedule":
            return job
        parent_id = str(job.get("parent_job_id") or "")
        if not parent_id or parent_id in seen:
            return None
        seen.add(parent_id)
        job = manager.get_job(parent_id) or {}
    return None


def _same_schedule_occurrence(schedule: dict, origin: dict) -> bool:
    return (
        str(origin.get("schedule_id") or "") == str(schedule.get("id") or "")
        and str(origin.get("idempotency_key") or "")
        == schedule_occurrence_key(schedule)
    )


class _ScheduleFollowupLink:
    """Bind a follow-up before enqueue, with guarded cross-store repair."""

    def __init__(self, manager: Any, schedule_id: str, previous_job_id: str,
                 task_id: str, prior_job: dict, allowed_statuses: set[str],
                 idempotency_key: str):
        self.manager = manager
        self.schedule_id = str(schedule_id or "")
        self.previous_job_id = str(previous_job_id or "")
        self.task_id = str(task_id)
        self.idempotency_key = str(idempotency_key)
        self.origin = _schedule_origin_job(manager, prior_job) if self.schedule_id else None
        self.is_schedule_run = bool(
            self.origin and str(self.origin.get("schedule_id") or "") == self.schedule_id
        )
        self.snapshot = None
        self.expected_job_id = self.previous_job_id
        self.attempted_job_id = None
        if not self.is_schedule_run:
            return

        self._repair_orphan()
        schedule = get_schedule_record(self.schedule_id)
        if (schedule and _same_schedule_occurrence(schedule, self.origin)
                and schedule.get("status") in allowed_statuses):
            linked_to_previous = str(schedule.get("job_id") or "") == self.previous_job_id
            finished_without_job = (
                schedule.get("status") == "failed" and not schedule.get("job_id")
            )
            if linked_to_previous or finished_without_job:
                self.snapshot = schedule
                self.expected_job_id = self.previous_job_id if linked_to_previous else None

    @property
    def submitted_schedule_id(self) -> Optional[str]:
        # A past occurrence can be resumed as a task, but must not control the
        # schedule's current occurrence.
        return self.schedule_id if self.snapshot or not self.is_schedule_run else None

    def _repair_orphan(self) -> None:
        schedule = get_schedule_record(self.schedule_id)
        if not schedule or not _same_schedule_occurrence(schedule, self.origin):
            return
        orphan_id = str(schedule.get("job_id") or "")
        if (schedule.get("status") != "running" or not orphan_id
                or orphan_id == self.previous_job_id):
            return
        binding_at = str(schedule.get("binding_at") or "")
        manager_instance = str(getattr(self.manager, "instance_id", "") or "")
        if (binding_at and manager_instance
                and str(schedule.get("binding_instance_id") or "") == manager_instance):
            try:
                age = (datetime.now().astimezone() - datetime.fromisoformat(binding_at).astimezone()).total_seconds()
                if 0 <= age < 30:
                    return  # A concurrent SQLite submit may still be committing.
            except ValueError:
                pass
        if self.manager.get_job(orphan_id) or ledger.get_job_receipts(orphan_id):
            return
        # The JSON write may have survived a rolled-back SQLite submit. Only
        # restore this exact occurrence while it still names the orphan ID.
        prior = self.manager.get_job(self.previous_job_id) or {}
        status = "awaiting_human" if prior.get("status") == "awaiting_human" else "running"
        set_schedule_run_state(
            self.schedule_id, status, expected_job_id=orphan_id,
            expected_status="running", job_id=self.previous_job_id,
            task_id=self.task_id,
            binding_key=prior.get("idempotency_key"), binding_at=None,
            binding_previous_job_id=None,
            binding_instance_id=None,
            error="等待你在消息决策中心处理卡片。" if status == "awaiting_human" else "",
        )

    def before_enqueue(self, job_id: str, task_id: str) -> None:
        self.attempted_job_id = str(job_id)
        schedule = get_schedule_record(self.schedule_id)
        if (not schedule or not _same_schedule_occurrence(schedule, self.origin)
                or schedule.get("status") != self.snapshot.get("status")
                or str(schedule.get("job_id") or "") != str(self.expected_job_id or "")):
            raise RuntimeError("日程已转到其他执行批次；旧任务不能覆盖当前日程。")
        if not set_schedule_run_state(
            self.schedule_id, "running", expected_job_id=self.expected_job_id,
            expected_job_absent=not bool(self.expected_job_id),
            expected_status=self.snapshot["status"], job_id=job_id,
            task_id=task_id, error="", binding_key=self.idempotency_key,
            binding_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            binding_previous_job_id=self.previous_job_id,
            binding_instance_id=getattr(self.manager, "instance_id", None),
        ):
            raise RuntimeError("日程状态已改变，恢复任务没有入队。")

    def restore_if_uncommitted(self) -> None:
        if not self.snapshot or not self.attempted_job_id:
            return
        schedule = get_schedule_record(self.schedule_id)
        if not schedule or not _same_schedule_occurrence(schedule, self.origin):
            return
        set_schedule_run_state(
            self.schedule_id, self.snapshot["status"],
            expected_job_id=self.attempted_job_id, expected_status="running",
            job_id=self.snapshot.get("job_id"), task_id=self.snapshot.get("task_id"),
            error=self.snapshot.get("error"),
            binding_key=self.snapshot.get("binding_key"),
            binding_at=self.snapshot.get("binding_at"),
            binding_previous_job_id=self.snapshot.get("binding_previous_job_id"),
            binding_instance_id=self.snapshot.get("binding_instance_id"),
        )


def respond_to_task_card(manager: Any, card_id: str, choice: Any, comment: str = "") -> dict:
    """Save the vote before any external or background follow-up begins."""
    card = respond_to_card(card_id, choice, comment)
    if card.get("action_type") == "JOB_APPROVE" and card.get("status") in {"approved", "rejected"}:
        from src.tools.recruitment_tool import JobRecordManager
        job_id = str((card.get("payload") or {}).get("job_id") or "")
        if job_id:
            error = None
            try:
                updated = JobRecordManager().update_job_status(
                    job_id, "APPROVED" if card["status"] == "approved" else "REJECTED"
                )
                if not updated:
                    raise RuntimeError(f"岗位记录 {job_id} 不存在或保存失败")
            except Exception as exc:
                error = str(exc)
                log_event("TASK", f"审批已保存，岗位记录更新失败：{error}", "ERROR")

            def record_error(cards):
                current = next((item for item in cards if item.get("card_id") == card_id), None)
                if current:
                    if error:
                        current["action_error"] = error
                    else:
                        current.pop("action_error", None)
            mutate_cards(record_error)
    if card.get("action_type") in {"JOB_SUBMISSION", "SCHEDULE_AUTO_APPLY", "SCHEDULE_HR_AUTO_REPLY"}:
        error = None
        try:
            if card.get("action_type") == "JOB_SUBMISSION":
                from src.tools.recruitment_tool import set_application_approval_from_card
                set_application_approval_from_card(card)
            elif card.get("action_type") == "SCHEDULE_AUTO_APPLY":
                from src.tools.recruitment_tool import activate_scheduled_auto_apply
                activate_scheduled_auto_apply(card)
            else:
                from src.tools.recruitment_tool import activate_scheduled_hr_auto_reply
                activate_scheduled_hr_auto_reply(card)
        except Exception as exc:
            error = str(exc)
            log_event("TASK", f"招聘授权已记录，但关联操作失败：{error}", "ERROR")

        def record_recruitment_error(cards):
            current = next((item for item in cards if item.get("card_id") == card_id), None)
            if current:
                if error:
                    current["action_error"] = error
                else:
                    current.pop("action_error", None)
        mutate_cards(record_recruitment_error)
    if card.get("action_type") == "HR_DRAFT_APPROVE" and card.get("status") in {"approved", "rejected"}:
        error = None
        try:
            from src.tools.recruitment_tool import record_hr_draft_decision_from_card
            record_hr_draft_decision_from_card(card)
        except Exception as exc:
            error = str(exc)
            log_event("TASK", f"HR 草稿审批已保存，但回复状态核对失败：{error}", "ERROR")

        def record_hr_draft_error(cards):
            current = next((item for item in cards if item.get("card_id") == card_id), None)
            if current:
                if error:
                    current["action_error"] = error
                else:
                    current.pop("action_error", None)
        mutate_cards(record_hr_draft_error)
    workflow = _workflow(card)
    if workflow.get("task_id") and workflow.get("resume_on_response", True):
        resume_task_after_approval(manager, str(workflow["task_id"]))
    return card


def resume_task_after_approval(manager: Any, task_id: str) -> Optional[str]:
    """Resume one resolved batch exactly once; retry after a crash is safe."""
    with _RESUME_LOCK:
        task_id = str(task_id or "")
        task = manager.get_task(task_id)
        if not task or task.get("status") not in {
            "awaiting_human", "running", "queued", "completed"
        }:
            return None
        cards = read_cards()
        unlinked = [card for card in cards if
                    str(_workflow(card).get("task_id") or "") == task_id
                    and _workflow(card).get("resume_on_response", True)
                    and not _workflow(card).get("response_job_id")]
        resolved = [card for card in unlinked if card.get("status") in _RESOLVED]
        if not resolved:
            return None
        origin_job_id = str(_workflow(resolved[0]).get("job_id") or "")
        if not origin_job_id:
            return None
        batch = [card for card in unlinked if str(_workflow(card).get("job_id") or "") == origin_job_id]
        if not batch or any(card.get("status") not in _RESOLVED for card in batch):
            return None
        card_ids = sorted(str(card["card_id"]) for card in batch)
        idempotency_key = f"approval:{task_id}:{','.join(card_ids)}"
        existing = manager.get_job_by_idempotency_key(idempotency_key)
        if task.get("status") not in {"awaiting_human", "running"} and not existing:
            return None

        response_lines = []
        for card in batch:
            choice = card.get("user_choice")
            selected = "、".join(map(str, choice)) if isinstance(choice, list) else str(choice)
            response_lines.append(
                f"卡片 ID：{card.get('card_id')}\n事项：{card.get('title') or '待确认事项'}\n"
                f"用户选择：{selected}\n补充说明：{card.get('user_comment') or '无'}"
            )
        receipts = ledger.get_job_receipts(origin_job_id)
        steps = [f"- {item.get('tool_name')}: {item.get('status')}"
                 for item in receipts[-30:]]
        request = str(task.get("request_text") or task.get("title") or "")
        policy_result = ""
        if any(card.get("action_type") == "SCHEDULE_AUTO_APPLY" for card in batch):
            policy_result = (
                "本批次包含定时自动投递范围授权。审批处理器已经按卡片选择启用或保持暂停原日程；"
                "不要重复创建日程或再次提交同一授权卡，只汇报日程及授权状态。\n"
            )
        approved_hr_drafts = [
            card for card in batch
            if card.get("action_type") == "HR_DRAFT_APPROVE" and card.get("status") == "approved"
        ]
        if approved_hr_drafts:
            fingerprints = "、".join(
                str((card.get("payload") or {}).get("message_fingerprint") or "")
                for card in approved_hr_drafts
            )
            policy_result += (
                f"本批次已逐条批准 HR 草稿（消息指纹：{fingerprints}）。"
                "如原任务需要审批后发送，请先重读当前 HR 会话，核对原文、发件人和页面选择器，"
                "再调用 auto_reply_hr_message 发送已保存的原草稿；不得重新拟稿或重复发送。"
                "若日程授权、页面或证据已变化，就停止发送并说明原因。\n"
            )
        approved_mail = [
            card for card in batch
            if card.get("action_type") == "QQ_MAIL_SEND" and card.get("status") == "approved"
        ]
        if approved_mail:
            mail_ids = "、".join(str(card.get("card_id")) for card in approved_mail)
            policy_result += (
                f"已批准 QQ 邮件发送卡片（ID：{mail_ids}）。若原请求明确要求发送，"
                "请在当前写信页调用 qq_mail_send_approved(card_id)；该工具会核对审批时的账户、"
                "收件人、主题、正文与签名，并且最多尝试发送一次。不要调用通用网页点击发送；"
                "若结果 UNKNOWN，停止并请用户核对已发送文件夹。\n"
            )
        continuation = (
            "用户已处理决策卡片。请结合原始请求、审批结果和已有步骤接续此任务。"
            "核对外部现状，只执行尚未完成且明确要求的动作；不得重复已成功的副作用操作。"
            "拒绝的事项不得执行。若原请求要求卡片下发后停止，只汇报审批结果并结束。\n\n"
            + policy_result
            + f"原始请求：\n{request}\n\n审批结果：\n" + "\n\n".join(response_lines)
            + "\n\n已记录步骤：\n" + ("\n".join(steps) or "- 无")
        )
        schedule_id = _workflow(batch[0]).get("schedule_id") or task.get("schedule_id")
        try:
            prior_job = (manager.get_job(origin_job_id) or {}) if schedule_id else {}
            schedule_link = _ScheduleFollowupLink(
                manager, schedule_id, origin_job_id, task_id, prior_job,
                {"running", "awaiting_human"}, idempotency_key,
            )
            try:
                job_id = manager.submit(
                    str(task.get("conversation_id") or DEFAULT_CONVERSATION_ID), continuation,
                    task_id=task_id, source="approval", parent_job_id=origin_job_id,
                    schedule_id=schedule_link.submitted_schedule_id,
                    display_message=f"审批回复：{batch[0].get('title') or '待审批事项'}",
                    allow_queue=True, idempotency_key=idempotency_key, approval_card_ids=card_ids,
                    before_enqueue=schedule_link.before_enqueue if schedule_link.snapshot else None,
                )
            except Exception:
                committed = manager.get_job_by_idempotency_key(idempotency_key)
                if committed:
                    job_id = str(committed["job_id"])
                else:
                    schedule_link.restore_if_uncommitted()
                    raise

            def link(cards_now):
                for card in cards_now:
                    if card.get("card_id") in card_ids:
                        wf = card.setdefault("workflow", {})
                        wf["response_job_id"] = job_id
                        wf["response_card_ids"] = card_ids
                        wf.pop("resume_error", None)
            mutate_cards(link)
            log_event("TASK", f"任务 [{task_id}] 已收到审批并继续执行。", "INFO")
            return job_id
        except Exception as exc:
            def record_error(cards_now):
                for card in cards_now:
                    if card.get("card_id") in card_ids:
                        card.setdefault("workflow", {})["resume_error"] = str(exc)
            mutate_cards(record_error)
            log_event("TASK", f"任务 [{task_id}] 审批已保存，但恢复失败：{exc}", "ERROR")
            return None


def reconcile_approval_responses(manager: Any) -> None:
    """Recover votes saved before a UI crash or a temporary worker failure."""
    task_ids = {
        str(_workflow(card).get("task_id")) for card in read_cards()
        if card.get("status") in _RESOLVED and _workflow(card).get("task_id")
        and _workflow(card).get("resume_on_response", True)
        and not _workflow(card).get("response_job_id")
    }
    for task_id in task_ids:
        resume_task_after_approval(manager, task_id)


def resume_interrupted_task(manager: Any, task_id: str) -> str:
    with _RESUME_LOCK:
        return _resume_interrupted_task_locked(manager, task_id)


def _resume_interrupted_task_locked(manager: Any, task_id: str) -> str:
    task = manager.get_task(task_id)
    if not task or task.get("status") not in {"failed", "interrupted"}:
        raise ValueError("这项任务当前不能从中断点恢复。")
    previous_job_id = str(task.get("latest_job_id") or "")
    prior_job = manager.get_job(previous_job_id) or {}
    steps = ledger.get_job_receipts(previous_job_id)
    step_lines = [f"- {item.get('tool_name')}：{item.get('status')}"
                  for item in steps[-100:]]
    request = str(task.get("request_text") or task.get("title") or "")
    recovery_prompt = (
        "用户请求核对进度并恢复任务。先通过只读方式核对当前外部状态。"
        "不要重复已经成功的有副作用操作。中断时 RUNNING 的工具结果未知；"
        "无法确认结果时发卡片询问用户。此前用户明确拒绝的事项仍不得执行。\n\n"
        f"原任务：\n{request}\n\n已有工具步骤：\n"
        + ("\n".join(step_lines) or "- 暂无记录")
        + f"\n\n上次错误：{task.get('error') or '无'}"
    )
    import json
    approval_ids = json.loads(prior_job.get("approval_card_ids") or "[]")
    idempotency_key = f"recovery:{task_id}:{previous_job_id}"
    schedule_link = _ScheduleFollowupLink(
        manager, task.get("schedule_id"), previous_job_id, task_id,
        prior_job, {"running", "failed"}, idempotency_key,
    )
    if schedule_link.snapshot:
        from src.tools.recruitment_tool import (
            approved_schedule_policy_context, approved_hr_auto_reply_context,
        )
        recovery_prompt += (
            approved_schedule_policy_context(schedule_link.schedule_id)
            + approved_hr_auto_reply_context(schedule_link.schedule_id)
        )
    try:
        job_id = manager.submit(
            str(task.get("conversation_id") or DEFAULT_CONVERSATION_ID), recovery_prompt,
            task_id=task_id, source="recovery",
            parent_job_id=prior_job.get("parent_job_id") or previous_job_id,
            schedule_id=schedule_link.submitted_schedule_id,
            display_message="我已请求灰风核对进度并恢复这项任务。",
            allow_queue=True, idempotency_key=idempotency_key,
            approval_card_ids=approval_ids,
            before_enqueue=schedule_link.before_enqueue if schedule_link.snapshot else None,
        )
    except Exception:
        committed = manager.get_job_by_idempotency_key(idempotency_key)
        if committed:
            job_id = str(committed["job_id"])
        else:
            schedule_link.restore_if_uncommitted()
            raise
    return job_id


def cancel_task(manager: Any, task_id: str) -> str:
    result = manager.cancel_task(task_id)
    def close(cards):
        for card in cards:
            if str(_workflow(card).get("task_id") or "") == task_id and card.get("status") == "pending":
                card["status"] = "cancelled"
                card["user_comment"] = "关联任务已取消。"
    mutate_cards(close)
    return result
