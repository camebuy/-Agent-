"""One Agent conversation and its evidence-based completion status."""
import asyncio
import uuid
import re
from datetime import datetime
from typing import Optional, Set, Dict
from enum import Enum

from llama_index.core.llms import ChatMessage, MessageRole

from config.settings import settings
from config.prompts import (
    load_system_prompt, BROWSER_TOOL_POLICY, DESKTOP_CHAT_POLICY, DOCUMENT_AND_DESKTOP_POLICY, PLATFORM_AND_SCHEDULE_POLICY,
    FILE_RESOURCE_POLICY, RECRUITMENT_WORKFLOW_POLICY, MEMORY_POLICY,
)
from src.core.agent.agent_engine import create_native_tool_agent, get_llm
from src.core.agent.memory_manager import MemoryManager
from src.tools.exporter import get_agent_tools
from src.core.system.terminal_logger import log_event
from src.core.system.ledger import ledger
from src.tools.activity_tool import read_cards
from src.core.tasks.task_context import set_current_task, reset_current_task
from src.core.system.runtime_settings import load_runtime_settings


class TaskState(Enum):
    INIT = "INIT"                        # 任务初始化
    ANALYZING = "ANALYZING"              # 意图分析与规划中
    EXECUTING = "EXECUTING"              # 工具执行中 ( Executor 监控)
    AWAITING_HUMAN = "AWAITING_HUMAN"    # 等待人类卡片审批
    COMPLETED = "COMPLETED"              # 成功完成 (必须核验 Receipt)
    FAILED = "FAILED"                    # 执行失败/拦截终止
    CANCELLED = "CANCELLED"              # 用户请求停止


class InvalidStateTransitionError(Exception):
    """非法状态跳转异常"""
    pass


class TaskStateMachine:
    """任务严格有限状态机 (Finite State Machine)"""

    # 允许的法定跳转路径规约
    ALLOWED_TRANSITIONS: Dict[TaskState, Set[TaskState]] = {
        TaskState.INIT: {TaskState.ANALYZING, TaskState.FAILED, TaskState.CANCELLED},
        TaskState.ANALYZING: {TaskState.EXECUTING, TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED},
        TaskState.EXECUTING: {TaskState.AWAITING_HUMAN, TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED},
        TaskState.AWAITING_HUMAN: {TaskState.EXECUTING, TaskState.FAILED, TaskState.CANCELLED},
        TaskState.COMPLETED: set(),  # 终态
        TaskState.FAILED: set(),     # 终态
        TaskState.CANCELLED: set(),  # 终态
    }

    def __init__(self, task_id: str):
        self.task_id = task_id
        self.current_state = TaskState.INIT
        self.has_executed_tool = False

    def transition_to(self, new_state: TaskState, has_receipt: bool = False):
        """严格核验并流转状态"""
        # 1. 检查跳转路径合法性
        allowed = self.ALLOWED_TRANSITIONS.get(self.current_state, set())
        if new_state not in allowed:
            raise InvalidStateTransitionError(
                f"[状态机拦截] 非法跳状态！禁止从 [{self.current_state.value}] 直接跳转至 [{new_state.value}]"
            )

        # 2. 校验硬规约：如涉及副作用但没有物理 Receipt，绝对禁止进入 COMPLETED
        if new_state == TaskState.COMPLETED and self.has_executed_tool and not has_receipt:
            raise InvalidStateTransitionError(
                "[状态机硬防御] 无法进入 COMPLETED 状态：任务标记有副作用，但缺乏有效的物理 Receipt 凭证！"
            )

        old_state = self.current_state
        self.current_state = new_state
        log_event("STATE_MACHINE", f"任务 [{self.task_id[:8]}] 状态变更: {old_state.value} -> {new_state.value}", "INFO")


def strip_activity_markup(text: str) -> str:
    """Discard legacy generated UI blocks; never render model-provided HTML."""
    return re.sub(r"(?s)<!--AGENT_ACTIVITY_BEGIN-->.*?<!--AGENT_ACTIVITY_END-->", "", text).strip()


def _application_run_outcomes(job_ids: set[str] | frozenset[str]) -> tuple[str, bool]:
    """Read recorded application states independently of the model's final prose."""
    try:
        from src.tools.recruitment_tool import JobRecordManager
        rows = []
        for job in JobRecordManager().get_all_jobs().values():
            if not isinstance(job, dict):
                continue
            application = job.get("application") or {}
            if not isinstance(application, dict) or application.get("run_job_id") not in job_ids:
                continue
            rows.append((job, application))
    except Exception as exc:
        log_event("TASK", f"投递结果汇总暂不可用：{exc}", "WARN")
        return "", False
    if not rows:
        return "", False
    labels = {
        "SUBMITTED": "网页确认已提交",
        "UNKNOWN": "提交结果待核对，禁止自动重试",
        "ATTEMPTING": "提交可能已开始，结果待核对",
        "FAILED": "提交前失败",
        "PREPARED": "已准备，尚未提交",
        "PENDING_APPROVAL": "等待逐岗位批准",
        "APPROVED": "已获批准，尚未提交",
        "REJECTED": "已拒绝",
        "NOT_SUBMITTED": "站内记录显示未提交，需重新授权后处理",
    }
    lines = ["投递执行记录（依据保存的岗位状态）："]
    for job, application in rows[:20]:
        title = " ".join(str(job.get("title") or "未命名岗位").split())[:80]
        company = " ".join(str(job.get("company") or "未知公司").split())[:80]
        state = str(application.get("status") or "未记录")
        lines.append(f"- {company} / {title}：{labels.get(state, state)}")
    if len(rows) > 20:
        lines.append(f"- 另有 {len(rows) - 20} 条，请在岗位记录中查看。")
    # A user's explicit rejection closes that application decision cleanly.
    # Every other non-submitted state still needs work or result checking.
    return "\n".join(lines), any(
        application.get("status") not in {"SUBMITTED", "REJECTED"}
        for _, application in rows
    )


def _hr_run_outcomes(job_ids: set[str] | frozenset[str]) -> tuple[str, bool, bool]:
    """Read the saved HR send states instead of trusting the model's summary."""
    try:
        from src.tools.recruitment_tool import hr_reply_run_outcomes
        summaries = []
        incomplete = False
        observed_send = False
        for job_id in sorted(job_ids):
            summary, part_incomplete, part_observed = hr_reply_run_outcomes(job_id)
            if summary:
                summaries.append(summary)
            incomplete = incomplete or part_incomplete
            observed_send = observed_send or part_observed
        return "\n".join(summaries), incomplete, observed_send
    except Exception as exc:
        log_event("TASK", f"HR 回复结果汇总暂不可用：{exc}", "WARN")
        return "", False, False


class ChatManager:
    """控制平面核心组件：只相信 Receipt 物理账本与状态机，不相信 LLM 文本"""

    def __init__(self, index=None):
        self.index = index
        self.memory_mgr = MemoryManager()
        self.tools = get_agent_tools(index=index)
        self.last_task_status = "completed"
        self.last_task_error = None

    def _build_system_prompt(self, history=None) -> str:
        custom_system_prompt = load_system_prompt()
        runtime = load_runtime_settings()
        memory_context = (
            self.memory_mgr.get_memory_context_for_prompt()
            if runtime["tool_permissions"].get("memory", True)
            else "【灰风结构化记忆】用户已在设置中关闭记忆读取。"
        )

        now = datetime.now().astimezone()
        weekday_map = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
        now_str = f"{now.strftime('%Y-%m-%d %H:%M:%S')} ({weekday_map[now.weekday()]})"

        return f"""【系统绝对时间】
当前真实的本地时间是：{now_str}。

【控制平面核心宪法（硬约束）】
1. 你仅是系统的【提议者 (Proposer)】，绝非实际物理状态的【裁判员 (Judge)】。
2. 物理状态的变化必须且仅能通过调用工具触发现实 Executor。
3. 如果未发生物理工具调用，绝对禁止在回复中宣称“已为您完成”、“已成功更新”、“已删除”或“已保存”。
4. 禁止自行伪造卡片与凭证。

【核心指令】
{BROWSER_TOOL_POLICY}

{DESKTOP_CHAT_POLICY}

{DOCUMENT_AND_DESKTOP_POLICY}

{PLATFORM_AND_SCHEDULE_POLICY}

{FILE_RESOURCE_POLICY}

{RECRUITMENT_WORKFLOW_POLICY}

{MEMORY_POLICY}

{custom_system_prompt}

{memory_context}
"""

    def _pending_card_ids(self, task_id: str) -> set[str]:
        return {
            str(card.get("card_id")) for card in read_cards()
            if card.get("status") == "pending"
            and str((card.get("workflow") or {}).get("task_id") or "") == task_id
            and (card.get("workflow") or {}).get("resume_on_response", True)
        }

    async def chat(
        self, user_message: str, history=None, task_id: Optional[str] = None,
        job_id: Optional[str] = None, conversation_id: str = "default",
        source: str = "manual", schedule_id: Optional[str] = None,
        parent_job_id: Optional[str] = None, cancellation_check=None,
        progress_callback=None, approval_card_ids=None,
    ) -> str:
        self.last_task_status = "completed"
        self.last_task_error = None
        task_id = str(task_id or f"task_{uuid.uuid4().hex[:8]}")
        job_id = str(job_id or f"job_{uuid.uuid4().hex}")
        source = str(source or "manual")
        fsm = TaskStateMachine(task_id=task_id)
        fsm.transition_to(TaskState.ANALYZING)
        def report_progress(message: str) -> None:
            if callable(progress_callback):
                try:
                    progress_callback(message)
                except Exception:
                    pass
        report_progress("我正在理解你的请求，确认接下来需要做什么。")
        log_event("USER_INPUT", f'收到指令: "{user_message}"', "CMD")
        cancelled = lambda: callable(cancellation_check) and cancellation_check()
        if cancelled():
            fsm.transition_to(TaskState.CANCELLED)
            self.last_task_status = "cancelled"
            return "任务已取消，尚未开始调用模型或工具。"

        context_token = set_current_task({
            "task_id": task_id, "job_id": job_id, "conversation_id": conversation_id,
            "user_request": user_message,
            "source": source, "schedule_id": schedule_id, "parent_job_id": parent_job_id,
            "approval_card_ids": list(approval_card_ids or []),
            "cancel_check": cancellation_check, "progress_callback": progress_callback,
        })
        execution_error = None
        reply = ""
        fsm.transition_to(TaskState.EXECUTING)
        try:
            report_progress("我正在组织处理步骤；需要操作时会调用相应工具。")
            system_prompt = self._build_system_prompt(history=history)
            llm = get_llm()  # One consistent model for this entire run.
            messages = []
            for item in history or []:
                if item.get("role") in ("user", "assistant") and item.get("content"):
                    # Old activity markup is presentation data, not conversation context.
                    content = strip_activity_markup(str(item["content"]))
                    messages.append(ChatMessage(role=item["role"], content=content))
            self.tools = get_agent_tools(index=self.index)
            if self.tools:
                # Let the same tool-aware model answer casual conversation without
                # calling tools. A separate classifier loses short follow-up intents.
                agent = create_native_tool_agent(self.tools, system_prompt=system_prompt, llm=llm)
                if hasattr(agent, "run"):
                    response = await agent.run(
                        user_msg=user_message, chat_history=messages,
                        max_iterations=settings.MAX_ITERATIONS, early_stopping_method="force",
                    )
                elif hasattr(agent, "achat"):
                    response = await agent.achat(user_message, chat_history=messages)
                else:
                    response = await asyncio.to_thread(agent.chat, user_message, chat_history=messages)
                reply = str(response)
            else:
                response = await llm.achat([
                    ChatMessage(role=MessageRole.SYSTEM, content=system_prompt), *messages,
                    ChatMessage(role=MessageRole.USER, content=user_message),
                ])
                reply = str(response.message.content or "")
        except Exception as exc:
            execution_error = f"{type(exc).__name__}: {exc}"
            log_event("AGENT", f"Agent 执行异常中断: {execution_error}", "ERROR")
        finally:
            reset_current_task(context_token)

        report_progress("我正在核对操作结果，并整理给你的回复。")
        # Read this job's complete receipts, rather than a truncated task lifetime.
        try:
            receipts = ledger.get_job_receipts(job_id)
            pending_card = bool(self._pending_card_ids(task_id))
        except Exception as exc:
            receipts, pending_card = [], False
            execution_error = execution_error or f"无法读取任务结果记录：{exc}"
        latest = {}
        for receipt in receipts:  # Ledger returns chronological order; newest wins.
            latest[str(receipt.get("tool_name") or "unknown")] = receipt
        failures = {name: item for name, item in latest.items() if item.get("status") != "SUCCESS"}
        has_receipt = any(r.get("status") == "SUCCESS" and r.get("has_side_effect") for r in receipts)
        has_application_step = any(
            receipt.get("tool_name") in {"prepare_job_application", "submit_job_application"}
            for receipt in receipts
        )
        has_hr_reply_step = any(
            receipt.get("tool_name") == "auto_reply_hr_message" for receipt in receipts
        )
        lineage = None
        lineage_error = ""
        if schedule_id and source in {"schedule", "approval", "recovery"}:
            try:
                from src.core.tasks.schedule_runner import get_schedule_lineage
                lineage = get_schedule_lineage(schedule_id, job_id, source, parent_job_id)
            except Exception as exc:
                lineage_error = f"无法核对日程执行批次：{exc}"
        lineage_missing = bool(schedule_id and source in {"schedule", "approval", "recovery"}
                               and not lineage)
        followup_job_ids = {job_id}
        if source in {"approval", "recovery"} and parent_job_id:
            followup_job_ids.add(str(parent_job_id))
        occurrence_job_ids = lineage[1] if lineage else frozenset(followup_job_ids)
        application_summary, application_incomplete = (
            _application_run_outcomes(occurrence_job_ids)
            if lineage or source in {"schedule", "approval", "recovery"} or has_application_step
            else ("", False)
        )
        hr_summary, hr_incomplete, hr_observed_send = (
            _hr_run_outcomes(occurrence_job_ids)
            if lineage or source in {"schedule", "approval", "recovery"} or has_hr_reply_step
            else ("", False, False)
        )
        submission_incomplete = bool(lineage or source in {"schedule", "approval", "recovery"}) and (
            application_incomplete or hr_incomplete
        )
        scheduled_application_request = bool(lineage or source in {"schedule", "approval", "recovery"}) and bool(re.search(
            r"投递|应聘|申请(?:岗位|职位|工作)|提交简历|发送简历", user_message,
        ))
        unsupported_submission_claim = bool(
            scheduled_application_request and not application_summary and re.search(
                r"(?:成功|完成|已(?:经)?)(?:自动)?(?:投递|应聘|申请(?:岗位|职位|工作)|提交简历|发送简历)",
                reply,
            )
        )
        scheduled_hr_request = bool(lineage or source in {"schedule", "approval", "recovery"}) and bool(re.search(
            r"HR|回复|联系(?:招聘者|面试官|人事)|发(?:送)?消息|回消息", user_message, re.I,
        ))
        observed_recruitment_page = any(
            receipt.get("status") == "SUCCESS"
            and receipt.get("tool_name") in {
                "web_snapshot", "web_extract", "web_list_controls", "web_verify_page",
            }
            for receipt in receipts
        )
        unverified_empty_recruitment = bool(
            (scheduled_application_request and not application_summary
             and not (observed_recruitment_page or any(
                 receipt.get("status") == "SUCCESS"
                 and receipt.get("tool_name") == "submit_job_application"
                 for receipt in receipts
             )))
            or (scheduled_hr_request and not hr_summary
                and not (observed_recruitment_page or any(
                    receipt.get("status") == "SUCCESS"
                    and receipt.get("tool_name") == "auto_reply_hr_message"
                    for receipt in receipts
                )))
        )
        unsupported_hr_reply_claim = bool(
            scheduled_hr_request and not hr_observed_send and re.search(
                r"(?:已|已经|成功|完成)(?:自动)?(?:回复|联系(?:招聘者|面试官|人事)|发(?:送)?消息)",
                reply, re.I,
            )
        )
        unverified_claim = bool(
            re.search(r"已(?:经)?(?:为你|为您)?(?:成功)?(?:创建|添加|设置|删除|更新|修改|保存|发送|打开|投递)", reply)
            and not has_receipt
        )
        action_requested = bool(lineage or source == "schedule") or bool(re.search(
            r"(?:帮我|请|现在|立即|给我).{0,30}(?:创建|添加|设置|删除|更新|修改|保存|发送|打开|投递|执行)",
            user_message,
        ))
        model_reported_failure = bool(action_requested and not receipts and re.search(
            r"(?:无法|未能|不能完成|执行失败|抱歉)", reply,
        ))
        fsm.has_executed_tool = has_receipt
        if cancelled():
            status, state = "cancelled", TaskState.CANCELLED
            self.last_task_error = "用户请求取消任务。"
            reply = (reply + "\n\n任务已停止。已完成的操作不会自动撤销。").strip()
        elif pending_card:
            # A model timeout after a real card was saved must not orphan approval.
            status, state = "awaiting_human", TaskState.AWAITING_HUMAN
            if execution_error:
                reply = "决策卡片已保存，请在消息决策中心处理。本次模型回复中断，审批记录仍然有效。"
                self.last_task_error = execution_error
        elif (execution_error or failures or lineage_missing or model_reported_failure or
              (unverified_claim and action_requested) or submission_incomplete or
              unsupported_submission_claim or unsupported_hr_reply_claim or
              unverified_empty_recruitment):
            status, state = "failed", TaskState.FAILED
            if execution_error:
                self.last_task_error = execution_error
            elif failures:
                self.last_task_error = "未恢复的工具调用失败：" + "；".join(
                    f"{name}: {item.get('error') or item.get('status')}" for name, item in failures.items()
                )
            elif lineage_missing:
                self.last_task_error = lineage_error or "无法证明此任务仍属于当前日程执行批次。"
            elif submission_incomplete:
                self.last_task_error = "任务存在未完成或未确认的岗位投递或 HR 回复。"
            elif unsupported_hr_reply_claim:
                self.last_task_error = "模型声称已回复 HR，但本次没有页面观察到的发送结果。"
            elif unsupported_submission_claim:
                self.last_task_error = "模型声称已投递，但本次没有岗位投递记录。"
            elif unverified_empty_recruitment:
                self.last_task_error = "招聘任务没有可核对的网页观察或执行记录。"
            elif model_reported_failure:
                self.last_task_error = "模型报告无法完成操作，但本次没有可核对的工具记录。"
            else:
                self.last_task_error = "模型声称执行了操作，但本次没有可核对的成功工具记录。"
            if execution_error:
                reply = f"任务执行中断：{execution_error}"
            elif failures:
                reply += "\n\n任务未完整完成：以下工具最后一次调用未成功：" + "、".join(failures) + "。请查看运行日志中的错误详情。"
            elif lineage_missing:
                reply += "\n\n无法核对这项工作与当前日程执行批次的关系；本次结果未标为完成。"
            elif unverified_empty_recruitment:
                reply += "\n\n本次没有可核对的招聘网页观察或执行记录，不能确认巡查已经发生。"
            if unverified_claim:
                reply += "\n\n本次没有可核对的成功变更记录；上文的完成描述未经验证，请查看运行日志。"
        else:
            status, state = "completed", TaskState.COMPLETED
        self.last_task_status = status
        fsm.transition_to(state, has_receipt=has_receipt)
        if status == "completed":
            await self._capture_explicit_memory(user_message, reply, source)
        reply = reply or ("正在等待您处理消息决策中心的卡片。" if pending_card else "本次未收到模型回复。")
        if application_summary:
            reply += "\n\n" + application_summary
        elif scheduled_application_request:
            reply += "\n\n本次任务没有新增岗位投递记录；没有可核对的投递成功结果。"
        if hr_summary:
            reply += "\n\n" + hr_summary
        if unsupported_hr_reply_claim:
            reply += "\n\n本次没有页面观察到的 HR 发送结果；上文的回复完成描述未经核对。"
        return reply

    async def _capture_explicit_memory(self, user_message: str, assistant_reply: str, source: str) -> None:
        try:
            runtime = load_runtime_settings()
            if (not runtime.get("memory_capture_enabled", True)
                    or not runtime["tool_permissions"].get("memory", True)):
                return
            await self.memory_mgr.analyze_and_extract_memory(user_message, assistant_reply, source=source)
        except Exception as exc:
            log_event("MEMORY", f"记忆整理暂不可用：{exc}", "WARN")
