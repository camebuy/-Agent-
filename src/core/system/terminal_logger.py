# src/core/system/terminal_logger.py
import json
import os
import re
import threading
import uuid
from datetime import datetime
from src.core.system.path_manager import PathManager

LOG_DIR = PathManager.LOG_DIR
LOG_DIR.mkdir(parents=True, exist_ok=True)
TERMINAL_LOG_FILE = PathManager.TERMINAL_LOG_FILE
_LOG_LOCK = threading.RLock()

TOOL_LABELS = {
    "web_use_open_tab": "复用已打开网页", "web_navigate": "打开网页",
    "web_snapshot": "查看页面结构", "web_extract": "读取页面内容",
    "web_list_controls": "检查网页控件", "web_wait": "等待页面加载",
    "web_scroll": "滚动页面", "web_click": "点击页面元素",
    "web_input": "填写页面内容", "web_upload": "附加用户文件",
    "web_verify_page": "核对网页结果", "parse_and_process_jobs": "整理岗位信息",
    "qq_mail_open_compose": "打开 QQ 邮箱写信页",
    "qq_mail_write_draft": "填写 QQ 邮箱草稿",
    "qq_mail_list_unread": "查看 QQ 邮箱未读邮件",
    "qq_mail_prepare_send": "提交 QQ 邮件发送审批",
    "qq_mail_send_approved": "发送已批准的 QQ 邮件",
    "desktop_chat_windows": "查看桌面聊天窗口",
    "desktop_chat_snapshot": "检查桌面聊天控件",
    "desktop_chat_open_conversation": "打开桌面会话",
    "desktop_chat_read_visible": "读取桌面会话",
    "desktop_chat_check_unread": "检查桌面未读提示",
    "desktop_chat_send_message": "发送桌面聊天消息",
    "document_create_text": "创建文本文档",
    "document_create_spreadsheet": "创建表格",
    "document_revise_text": "修订文档",
    "document_append_text": "补充文档内容",
    "document_append_spreadsheet_rows": "追加表格行",
    "document_update_spreadsheet_cells": "修改表格单元格",
    "document_list_outputs": "查看生成的文件",
    "document_read_output": "读取生成的文件",
    "desktop_list_windows": "查看桌面窗口",
    "desktop_inspect_window": "检查窗口控件",
    "desktop_focus_window": "切换桌面窗口",
    "desktop_invoke_control": "操作桌面控件",
    "desktop_fill_control": "填写桌面输入框",
    "desktop_launch_application": "启动桌面程序",
    "desktop_open_user_file": "打开个人文件",
    "desktop_read_clipboard": "读取剪贴板",
    "desktop_write_clipboard": "写入剪贴板",
    "desktop_capture_window": "保存窗口截图",
    "prepare_scheduled_auto_apply": "申请定时投递授权",
    "prepare_job_application": "准备岗位投递",
    "submit_job_application": "提交岗位投递",
    "reconcile_application_status": "核对岗位投递结果",
    "prepare_scheduled_hr_auto_reply": "申请 HR 回复授权",
    "prepare_hr_message_response": "准备 HR 回复",
    "prepare_hr_reply_draft": "拟定 HR 回复草稿",
    "auto_reply_hr_message": "回复 HR 消息",
    "record_confirmed_interview": "登记已确认面试",
    "get_hr_auto_reply_status": "查看 HR 回复状态",
    "get_hr_auto_reply_summary": "查看 HR 回复汇总",
    "get_application_status": "查看投递状态",
    "send_user_request": "发送决策卡片", "schedule_add_tool": "创建日程",
    "schedule_list_tool": "查看日程", "schedule_update_tool": "更新日程",
    "check_user_availability": "检查可约时间",
    "find_interview_slots": "查找面试空档",
    "schedule_delete_tool": "删除日程", "search_user_files": "搜索个人文件",
    "read_user_file": "读取个人文件", "search_user_memory": "搜索记忆",
    "remember_user_memory": "保存记忆", "sync_user_file_catalog": "同步文件目录",
    "describe_user_file": "概括个人文件", "set_user_file_tags": "设置文件标签",
    "link_files_to_task": "关联任务文件", "list_task_file_resources": "查看任务文件",
    "create_task_data_card": "创建任务文件卡", "delete_task_data_card": "删除任务文件卡",
    "delete_user_request": "删除决策卡片", "clear_all_cards": "清理决策卡片",
    "listen_hr_messages": "整理 HR 消息", "get_recruitment_summary": "查看招聘记录",
    "personal_knowledge_search": "搜索个人知识库",
}


def humanize_event(module: str, message: str, level: str = "INFO") -> tuple[str, str]:
    """Return a concise home-terminal label and message; keep raw logs for diagnostics."""
    module = str(module or "SYSTEM").upper()
    message = str(message or "")
    level = str(level or "INFO").upper()
    module_labels = {
        "USER_INPUT": "任务", "STATE_MACHINE": "任务", "EXECUTOR": "工具",
        "SCHEDULE": "日程", "TOOL_CALL": "审批", "TASK": "任务",
        "AGENT": "Agent", "GUARD": "安全检查", "MEMORY": "记忆",
    }
    label = module_labels.get(module, "系统")

    if module == "USER_INPUT":
        return label, "收到新的个人任务，正在整理目标。"
    if module == "STATE_MACHINE":
        target = re.search(r"->\s*([A-Z_]+)", message)
        states = {
            "ANALYZING": "正在分析任务", "EXECUTING": "正在执行任务",
            "AWAITING_HUMAN": "任务等待你的审批", "COMPLETED": "任务已完成",
            "FAILED": "任务执行失败", "CANCELLED": "任务已取消", "INIT": "任务已创建",
        }
        if target:
            return label, states.get(target.group(1), "任务状态已更新")
    if module == "EXECUTOR":
        match = re.search(r"\[([A-Za-z0-9_]+)\]", message)
        tool_label = TOOL_LABELS.get(match.group(1), "工具操作") if match else "工具操作"
        if "物理执行失败" in message or level == "ERROR":
            return label, f"{tool_label}遇到问题；可在运行日志中查看详情。"
        if "物理执行成功" in message:
            return label, f"{tool_label}已完成。"
        if "接收到副作用执行请求" in message:
            return label, f"正在{tool_label}。"
    if module == "SCHEDULE":
        if "执行结束" in message:
            title = re.search(r"日程\s*\[(.*?)\]", message)
            lower_message = message.lower()
            status = (
                "已错过" if "missed" in lower_message
                else "已取消" if "cancelled" in lower_message
                else "失败" if "failed" in lower_message or "interrupted" in lower_message
                else "完成"
            )
            return label, f"日程任务{status}：{title.group(1)}" if title else f"日程任务{status}。"
        if "后台调度循环异常" in message:
            return label, "调度遇到问题；可在运行日志中查看详情。"
        title = re.search(r"日程\s*\[(.*?)\]", message)
        if title:
            return label, f"正在处理日程：{title.group(1)}"
    if module == "TOOL_CALL" and "下发决策卡片" in message:
        title = re.search(r"【(.*?)】", message)
        return label, f"已发送审批卡片：{title.group(1)}" if title else "已发送审批卡片，等待你的选择。"
    if module == "AGENT" and level == "ERROR":
        return label, "任务执行遇到问题；可在运行日志中查看详情。"
    if module == "GUARD" and level in ("WARN", "ERROR"):
        return label, "检测到需要关注的状态；可在运行日志中查看详情。"
    if module == "TASK":
        if "恢复失败" in message:
            return label, "审批已记录，但任务恢复失败；可在运行日志中查看详情。"
        if "继续执行" in message:
            return label, "已收到审批，正在继续原任务。"
    if level == "ERROR":
        return label, "运行遇到问题；可在运行日志中查看详情。"
    return label, message[:180]


def log_event(module: str, message: str, level: str = "INFO"):
    """Append one bounded diagnostic event using an atomic file replacement."""
    with _LOG_LOCK:
        TERMINAL_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        events = []
        if TERMINAL_LOG_FILE.exists():
            try:
                with TERMINAL_LOG_FILE.open("r", encoding="utf-8") as handle:
                    loaded = json.load(handle)
                if not isinstance(loaded, list) or any(not isinstance(row, dict) for row in loaded):
                    raise ValueError("terminal log must contain event objects")
                events = loaded
            except (OSError, json.JSONDecodeError, ValueError):
                # Keep the damaged file for diagnosis rather than replacing it
                # with an apparently clean log.
                return

        module_name = str(module).upper()
        message_text = str(message)
        display_module, display_message = humanize_event(module_name, message_text, level)
        events.append({
            "timestamp": datetime.now().strftime("%H:%M:%S"),
            "occurred_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "module": module_name,
            "display_module": display_module,
            "message": message_text,
            "display_message": display_message,
            "level": str(level).upper(),
        })
        events = events[-500:]

        temp_file = TERMINAL_LOG_FILE.with_name(TERMINAL_LOG_FILE.name + f".{uuid.uuid4().hex}.tmp")
        try:
            with temp_file.open("w", encoding="utf-8") as handle:
                json.dump(events, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_file, TERMINAL_LOG_FILE)
        except OSError:
            temp_file.unlink(missing_ok=True)
