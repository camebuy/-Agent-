# src/core/executor.py
import uuid
import time
import logging
import threading
import hashlib
import json
from typing import Callable, Any, Dict, Optional, Tuple

from src.core.system.ledger import ledger, ToolCall, ToolResult, Receipt
from src.tools.activity_tool import read_cards
from src.core.system.terminal_logger import log_event
from src.core.tasks.task_context import get_current_task

logger = logging.getLogger("Executor")
_EXECUTION_LOCK = threading.RLock()

# Explicit metadata: a tool called "delete_search_history" is not read-only.
TOOL_EFFECTS = {
    name: False for name in (
        "read_user_file", "list_task_file_resources", "search_user_memory",
        "schedule_list_tool", "web_snapshot", "web_extract", "web_list_controls", "web_wait",
        "web_verify_page", "get_recruitment_summary", "get_application_status",
        "get_hr_auto_reply_status", "get_hr_auto_reply_summary",
        "desktop_chat_windows", "desktop_chat_snapshot", "desktop_chat_check_unread",
        "document_list_outputs", "document_read_output",
        "desktop_list_windows", "desktop_inspect_window", "desktop_read_clipboard",
        "knowledge_search",
    )
}
TOOL_EFFECTS.update({name: True for name in (
    "sync_user_file_catalog", "search_user_files", "describe_user_file",
    "set_user_file_tags", "link_files_to_task", "create_task_data_card",
    "delete_task_data_card", "remember_user_memory", "schedule_add_tool",
    "schedule_update_tool", "schedule_delete_tool", "send_user_request",
    "delete_user_request", "clear_all_cards", "web_use_open_tab", "web_navigate", "web_click",
    "web_input", "web_scroll", "web_upload", "parse_and_process_jobs",
    "listen_hr_messages", "prepare_scheduled_auto_apply",
    "prepare_job_application", "submit_job_application", "reconcile_application_status",
    "prepare_scheduled_hr_auto_reply", "prepare_hr_reply_draft", "auto_reply_hr_message",
    "qq_mail_open_compose", "qq_mail_write_draft", "qq_mail_list_unread",
    "qq_mail_prepare_send", "qq_mail_send_approved",
    "desktop_chat_open_conversation", "desktop_chat_read_visible", "desktop_chat_send_message",
    "document_create_text", "document_create_spreadsheet", "document_revise_text",
    "document_append_text", "document_append_spreadsheet_rows", "document_update_spreadsheet_cells",
    "desktop_focus_window", "desktop_invoke_control",
    "desktop_fill_control", "desktop_launch_application", "desktop_open_user_file",
    "desktop_write_clipboard", "desktop_capture_window",
    "personal_knowledge_search",
)})


def tool_has_side_effect(tool_name: str) -> bool:
    return TOOL_EFFECTS.get(tool_name, True)


def _receipt_params(tool_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Avoid duplicating private email and chat contents in the ledger."""
    if tool_name == "personal_knowledge_search":
        query = str(params.get("query") or "")
        return {"query_length": len(query), "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest()}
    if tool_name.startswith("document_") or tool_name.startswith("desktop_") and not tool_name.startswith("desktop_chat_"):
        safe_keys = {"name", "source_name", "source_file_id", "output_name", "file_id", "sheet_name",
                     "window_handle", "snapshot_id", "control_ref", "app", "replace_existing", "max_chars"}
        result = {key: params[key] for key in safe_keys if key in params}
        for key in ("content", "text", "find_text", "replacement", "headers", "rows", "updates"):
            if key in params:
                value = json.dumps(params[key], ensure_ascii=False, default=str)
                result[key + "_length"] = len(value)
                result[key + "_sha256"] = hashlib.sha256(value.encode("utf-8")).hexdigest()
        return result
    if tool_name.startswith("desktop_chat_"):
        result = {"platform": str(params.get("platform") or "")[:20]}
        if params.get("window_handle"):
            result["window_handle"] = int(params["window_handle"])
        for name in ("contact", "message"):
            if name in params:
                value = str(params.get(name) or "")
                result[name + "_length"] = len(value)
                result[name + "_sha256"] = hashlib.sha256(value.encode("utf-8")).hexdigest()
        return result
    if tool_name != "qq_mail_write_draft":
        return params
    result = {}
    for name in ("recipient_email", "subject", "body"):
        value = str(params.get(name) or "")
        result[name + "_length"] = len(value)
        result[name + "_sha256"] = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return result


def _receipt_result(tool_name: str, result: Any) -> Any:
    """Agent receives the result; the durable ledger stores only chat metadata."""
    if tool_name == "personal_knowledge_search":
        return {"result_length": len(str(result or ""))}
    if tool_name.startswith("document_") or tool_name.startswith("desktop_") and not tool_name.startswith("desktop_chat_"):
        try:
            parsed = json.loads(result) if isinstance(result, str) else result
        except (TypeError, ValueError):
            parsed = {}
        if not isinstance(parsed, dict):
            parsed = {}
        return {
            "status": str(parsed.get("status") or "RETURNED")[:32],
            "name": str(parsed.get("name") or "")[:120],
            "window_count": len(parsed.get("windows") or []),
            "control_count": len(parsed.get("controls") or []),
            "file_count": len(parsed.get("files") or []),
            "text_length": len(str(parsed.get("text") or "")),
        }
    if tool_name == "qq_mail_list_unread":
        try:
            parsed = json.loads(result) if isinstance(result, str) else result
        except (TypeError, ValueError):
            parsed = {}
        if not isinstance(parsed, dict):
            parsed = {}
        return {"status": parsed.get("status"), "unread_on_page": parsed.get("unread_on_page")}
    if not tool_name.startswith("desktop_chat_"):
        return result
    try:
        parsed = json.loads(result) if isinstance(result, str) else result
    except (TypeError, ValueError):
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    return {
        "status": str(parsed.get("status") or "RETURNED")[:32],
        "platform": str(parsed.get("platform") or "")[:20],
        "window_count": len(parsed.get("windows") or []),
        "control_count": len(parsed.get("controls") or []),
        "item_count": len(parsed.get("items") or []),
        "indicator_count": len(parsed.get("indicators") or []),
    }


def _task_approval_gate_reason(
    task_id: Optional[str], source: str, has_side_effect: bool,
    *, job_id: Optional[str] = None, parent_job_id: Optional[str] = None,
    approval_card_ids: Optional[list] = None, tool_name: str = "",
) -> Optional[str]:
    if not task_id:
        return None
    try:
        cards = read_cards()
        related_cards = []
        for card in cards:
            if not isinstance(card, dict):
                continue
            workflow = card.get("workflow")
            if (
                isinstance(workflow, dict)
                and str(workflow.get("task_id") or "") == task_id
                and workflow.get("resume_on_response", True)
            ):
                related_cards.append(card)
        if any(card.get("status") == "pending" for card in related_cards):
            return "此任务已有待审批卡片；用户回复前已阻止后续工具操作。"

        # A quick UI response must not let the original execution race past its
        # own decision. New unrelated runs of this task are not frozen forever.
        if job_id and any((card.get("workflow") or {}).get("job_id") == job_id for card in related_cards):
            return "审批结果已提交；原执行链已冻结，后续操作必须由审批续办任务处理。"
        if source != "approval" and not approval_card_ids:
            return None
        requested_ids = set(approval_card_ids or [])
        resolved = [card for card in related_cards if card.get("card_id") in requested_ids]
        if (
            not requested_ids or len(resolved) != len(requested_ids)
            or any(card.get("status") not in ("approved", "rejected", "acknowledged", "responded") for card in resolved)
            or (parent_job_id and any((card.get("workflow") or {}).get("job_id") != parent_job_id for card in resolved))
        ):
            return "本次续办对应的审批记录不完整，已阻止工具执行。"
        def rejected(card):
            choice = card.get("user_choice")
            choices = choice if isinstance(choice, list) else [choice]
            return card.get("status") == "rejected" or any(
                str(value).strip().lower() in ("reject", "rejected", "no", "拒绝") for value in choices
            )
        # Custom selections/acknowledgments are valid answers, not rejections.
        # After an explicit rejection allow reading and asking for a revised
        # decision, but do not start external writes or defer them in a schedule.
        if has_side_effect and tool_name != "send_user_request" and any(rejected(card) for card in resolved):
            return "用户拒绝了本次待执行操作；可读取信息或发起新的决策卡，已阻止其他变更。"
        return None
    except (OSError, TypeError, ValueError) as exc:
        # Approval state is a safety boundary: if it cannot be checked, fail closed.
        raise RuntimeError(f"无法确认该任务是否仍在等待审批：{exc}") from exc


class Executor:
    """副作用唯一控制执行器 (Side-Effect Executor)"""

    @staticmethod
    def execute(tool_name: str, func: Callable, params: Dict[str, Any], has_side_effect: bool = True) -> Tuple[
        ToolResult, Receipt]:
        # Serialize check + action even if a model emits parallel tool calls.
        with _EXECUTION_LOCK:
            return Executor._execute(tool_name, func, params, has_side_effect)

    @staticmethod
    def _execute(tool_name: str, func: Callable, params: Dict[str, Any], has_side_effect: bool) -> Tuple[ToolResult, Receipt]:
        """
        拦截并执行物理副作用，自动生成 Receipt 并写入 Ledger 账本
        """
        execution_id = f"exec_{uuid.uuid4().hex[:10]}"
        call_id = f"call_{uuid.uuid4().hex[:8]}"
        receipt_params = _receipt_params(tool_name, params)

        tool_call = ToolCall(
            call_id=call_id,
            tool_name=tool_name,
            params=receipt_params,
            has_side_effect=has_side_effect
        )

        log_event("EXECUTOR", f"接收到副作用执行请求: [{tool_name}] (ID: {execution_id})", "CMD")

        task_context = get_current_task()
        task_id = str(task_context.get("task_id") or "") or None
        job_id = str(task_context.get("job_id") or "") or None
        progress = task_context.get("progress_callback")
        result_data = None
        error_msg = None
        is_success = False

        started = Receipt(
            execution_id=execution_id,
            tool_name=tool_name,
            has_side_effect=has_side_effect,
            status="RUNNING",
            params=receipt_params,
            result=None,
            error=None,
            timestamp=time.time(),
            task_id=task_id,
            job_id=job_id,
        )
        try:
            # Write ahead of the physical action. If the audit store is unavailable,
            # do not perform an untracked side effect.
            ledger.record_receipt(started)
        except Exception as exc:
            error_msg = f"执行账本不可写，已阻止工具执行：{exc}"
            log_event("EXECUTOR", error_msg, "ERROR")
            failed = Receipt(
                execution_id, tool_name, has_side_effect, "FAILED",
                receipt_params, None, error_msg, time.time(), task_id, job_id,
            )
            return ToolResult(call_id=call_id, success=False, error=error_msg), failed

        if callable(progress):
            try:
                progress(f"正在执行：{tool_name}")
            except Exception:
                pass

        try:
            cancel_check = task_context.get("cancel_check")
            if callable(cancel_check) and cancel_check():
                error_msg = "任务已收到取消请求；该工具调用未执行。"
                receipt = Receipt(
                    execution_id, tool_name, has_side_effect, "INTERCEPTED",
                    receipt_params, None, error_msg, time.time(), task_id, job_id,
                )
                ledger.record_receipt(receipt)
                log_event("EXECUTOR", f"工具 [{tool_name}] 已拦截：任务取消后未执行", "WARN")
                return ToolResult(call_id=call_id, success=False, error=error_msg), receipt
            try:
                approval_block = _task_approval_gate_reason(
                    task_id, str(task_context.get("source") or "manual"), has_side_effect,
                    job_id=job_id, parent_job_id=task_context.get("parent_job_id"),
                    approval_card_ids=task_context.get("approval_card_ids"), tool_name=tool_name,
                )
            except RuntimeError as exc:
                error_msg = f"{exc}；为避免越过审批，已阻止本次工具操作。"
                receipt = Receipt(
                    execution_id, tool_name, has_side_effect, "INTERCEPTED",
                    receipt_params, None, error_msg, time.time(), task_id, job_id,
                )
                ledger.record_receipt(receipt)
                log_event("EXECUTOR", f"工具 [{tool_name}] 已拦截：无法核验审批状态", "WARN")
                return ToolResult(call_id=call_id, success=False, error=error_msg), receipt
            if approval_block:
                error_msg = approval_block
                receipt = Receipt(
                    execution_id, tool_name, has_side_effect, "INTERCEPTED",
                    receipt_params, None, error_msg, time.time(), task_id, job_id,
                )
                ledger.record_receipt(receipt)
                log_event("EXECUTOR", f"工具 [{tool_name}] 已拦截：任务审批门槛未通过", "WARN")
                return ToolResult(call_id=call_id, success=False, error=error_msg), receipt
            # 真实触发物理副作用函数
            result_data = func(**params)
            is_success = True
            log_event("EXECUTOR", f"工具 [{tool_name}] 物理执行成功", "SUCCESS")
        except Exception as exc:
            error_msg = str(exc)
            is_success = False
            log_event("EXECUTOR", f"工具 [{tool_name}] 物理执行失败: {error_msg}", "ERROR")

        # 1. 组装物理结果 ToolResult
        tool_result = ToolResult(
            call_id=call_id,
            success=is_success,
            data=result_data,
            error=error_msg
        )

        # 2. 强行生成系统 Receipt 凭证
        receipt = Receipt(
            execution_id=execution_id,
            tool_name=tool_name,
            has_side_effect=has_side_effect,
            status="SUCCESS" if is_success else "FAILED",
            params=receipt_params,
            result=_receipt_result(tool_name, result_data),
            error=error_msg,
            timestamp=time.time(),
            task_id=task_id,
            job_id=job_id,
        )

        # Update the write-ahead record with the physical result. If this fails,
        # the earlier RUNNING row remains as an explicit unknown-outcome checkpoint.
        try:
            ledger.record_receipt(receipt)
        except Exception as exc:
            error_msg = f"工具已返回，但执行结果未能写入账本，外部状态需要核对：{exc}"
            receipt = Receipt(
                execution_id, tool_name, has_side_effect, "RUNNING",
                receipt_params, None, error_msg, time.time(), task_id, job_id,
            )
            tool_result = ToolResult(call_id=call_id, success=False, error=error_msg)
            is_success = False
            log_event("EXECUTOR", error_msg, "ERROR")

        if callable(progress):
            try:
                progress(f"已完成工具步骤：{tool_name}" if is_success else f"工具步骤失败：{tool_name}")
            except Exception:
                pass

        # 4. 要求⑤：若执行涉及到卡片/UI 状态变更，直接基于 Receipt 触发事件更新落盘，不经由 LLM 文本

        return tool_result, receipt

