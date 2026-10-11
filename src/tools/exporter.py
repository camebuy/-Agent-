# src/tools/exporter.py
import logging
from typing import List, Optional, Callable
from functools import wraps
from llama_index.core import VectorStoreIndex
from llama_index.core.tools import BaseTool, FunctionTool

from src.tools.executor import Executor

# 1. 引入所有业务子系统的底层物理工具函数
from src.tools.database_tool import (
    scan_and_get_global_manifest,
    create_task_data_card,
    delete_task_data_card,
    search_user_files,
    read_catalog_file,
    describe_user_file,
    link_user_files_to_task,
    list_task_file_resources,
    update_user_file_tags,
    search_personal_knowledge,
    build_rag_tool,
)

from src.tools.activity_tool import (
    add_schedule,
    list_schedules,
    update_schedule,
    delete_schedule,
    check_user_availability,
    find_interview_slots,
    send_user_request,
    delete_user_request,
    clear_all_cards,
)

from src.tools.browser_atomic_tools import (
    web_use_open_tab,
    web_navigate,
    web_click,
    web_input,
    web_snapshot,
    web_extract,
    web_scroll,
    web_wait,
    web_upload,
    web_verify_page,
    web_list_controls,
)
from src.tools.email_tool import (
    qq_mail_open_compose, qq_mail_write_draft, qq_mail_list_unread,
    qq_mail_prepare_send, qq_mail_send_approved,
)
from src.tools.desktop_chat_tool import (
    desktop_chat_windows, desktop_chat_snapshot, desktop_chat_open_conversation,
    desktop_chat_read_visible, desktop_chat_check_unread, desktop_chat_send_message,
)
from src.tools.document_tool import (
    document_create_text, document_create_spreadsheet, document_revise_text,
    document_append_text, document_append_spreadsheet_rows,
    document_update_spreadsheet_cells, document_list_outputs, document_read_output,
)
from src.tools.desktop_tool import (
    desktop_list_windows, desktop_inspect_window, desktop_focus_window,
    desktop_invoke_control, desktop_fill_control, desktop_launch_application,
    desktop_open_user_file, desktop_read_clipboard, desktop_write_clipboard,
    desktop_capture_window,
)

from src.tools.recruitment_tool import (
    parse_and_process_jobs,
    get_recruitment_summary,
    listen_hr_messages,
    prepare_job_application,
    submit_job_application,
    get_application_status,
    prepare_scheduled_auto_apply,
    reconcile_application_status,
    prepare_scheduled_hr_auto_reply,
    auto_reply_hr_message,
    prepare_hr_message_response,
    prepare_hr_reply_draft,
    record_confirmed_interview,
    get_hr_auto_reply_status,
    get_hr_auto_reply_summary,
)
from src.core.agent.memory_manager import search_user_memory, remember_user_memory
from src.core.system.runtime_settings import load_runtime_settings

logger = logging.getLogger(__name__)

TOOL_GROUPS = {
    "user_files": {
        "sync_user_file_catalog", "search_user_files", "read_user_file", "describe_user_file",
        "set_user_file_tags", "link_files_to_task", "list_task_file_resources",
        "create_task_data_card", "delete_task_data_card", "web_upload",
        "desktop_open_user_file",
    },
    "memory": {"search_user_memory", "remember_user_memory"},
    "schedules": {"schedule_list_tool", "schedule_add_tool", "schedule_update_tool", "schedule_delete_tool", "check_user_availability", "find_interview_slots", "record_confirmed_interview", "prepare_scheduled_auto_apply", "prepare_scheduled_hr_auto_reply"},
    "decision_cards": {"send_user_request", "delete_user_request", "clear_all_cards", "parse_and_process_jobs", "listen_hr_messages", "prepare_job_application", "submit_job_application", "prepare_scheduled_auto_apply", "prepare_scheduled_hr_auto_reply", "prepare_hr_message_response", "prepare_hr_reply_draft", "auto_reply_hr_message", "record_confirmed_interview", "qq_mail_prepare_send", "qq_mail_send_approved"},
    "browser": {"web_snapshot", "web_extract", "web_list_controls", "web_wait", "web_use_open_tab", "web_navigate", "web_click", "web_input", "web_scroll", "web_upload", "web_verify_page", "submit_job_application", "reconcile_application_status", "auto_reply_hr_message", "record_confirmed_interview", "qq_mail_open_compose", "qq_mail_write_draft", "qq_mail_list_unread", "qq_mail_prepare_send", "qq_mail_send_approved"},
    "email": {"qq_mail_open_compose", "qq_mail_write_draft", "qq_mail_list_unread", "qq_mail_prepare_send", "qq_mail_send_approved"},
    "desktop_chat": {
        "desktop_chat_windows", "desktop_chat_snapshot", "desktop_chat_open_conversation",
        "desktop_chat_read_visible", "desktop_chat_check_unread", "desktop_chat_send_message",
    },
    "documents": {
        "document_create_text", "document_create_spreadsheet", "document_revise_text",
        "document_append_text", "document_append_spreadsheet_rows",
        "document_update_spreadsheet_cells", "document_list_outputs", "document_read_output",
    },
    "desktop": {
        "desktop_list_windows", "desktop_inspect_window", "desktop_focus_window",
        "desktop_invoke_control", "desktop_fill_control", "desktop_launch_application",
        "desktop_open_user_file", "desktop_read_clipboard", "desktop_write_clipboard",
        "desktop_capture_window",
    },
    "recruitment": {"get_recruitment_summary", "parse_and_process_jobs", "listen_hr_messages", "prepare_job_application", "submit_job_application", "get_application_status", "prepare_scheduled_auto_apply", "reconcile_application_status", "prepare_scheduled_hr_auto_reply", "prepare_hr_message_response", "prepare_hr_reply_draft", "auto_reply_hr_message", "record_confirmed_interview", "get_hr_auto_reply_status", "get_hr_auto_reply_summary"},
    "knowledge_search": {"personal_knowledge_search"},
}


def is_tool_enabled(name: str, permissions: dict) -> bool:
    required = [group for group, names in TOOL_GROUPS.items() if name in names]
    if name == "personal_knowledge_search":
        required.append("user_files")
    return all(permissions.get(group, True) for group in required)


def _wrap_tool(fn: Callable, name: str, description: str, has_side_effect: bool = True) -> BaseTool:
    """
    核心控制平面包装器：强行拦截所有 Agent 发起的 Function Call，
    必须通过 Executor.execute() 进行物理记录、审计并生成不可篡改的 Receipt 凭证。
    """

    @wraps(fn)
    def executor_wrapped_fn(**kwargs):
        logger.info("[ControlPlane] 拦截工具调用: %s", name)

        def permitted_call(**params):
            # Settings can change while an existing task/agent is still alive.
            if not is_tool_enabled(name, load_runtime_settings()["tool_permissions"]):
                raise PermissionError(f"工具 {name} 已在控制设置中停用。")
            return fn(**params)

        tool_result, receipt = Executor.execute(
            tool_name=name,
            func=permitted_call,
            params=kwargs,
            has_side_effect=has_side_effect
        )

        if not tool_result.success:
            logger.error(f"[ControlPlane] 物理工具 [{name}] 执行异常: {tool_result.error}")
            raise RuntimeError(f"物理工具 [{name}] 执行失败: {tool_result.error}")

        return tool_result.data

    executor_wrapped_fn._aacwork_executor_wrapped = True
    executor_wrapped_fn._aacwork_has_side_effect = has_side_effect
    return FunctionTool.from_defaults(
        fn=executor_wrapped_fn,
        name=name,
        description=description
    )


def get_agent_tools(index: Optional[VectorStoreIndex] = None) -> List[BaseTool]:
    """
    导出全量接轨控制平面的工具链列表
    """
    tools: List[BaseTool] = [
        # ==================== 1. 数据卡与物理文件管理 ====================
        _wrap_tool(
            fn=scan_and_get_global_manifest,
            name="sync_user_file_catalog",
            description="同步 data/users 的文件属性目录并返回 file_id、路径、类型、大小与摘要。文件保持在单一用户文件区。",
            has_side_effect=True
        ),
        _wrap_tool(
            fn=search_user_files,
            name="search_user_files",
            description="按名称、路径、摘要或标签搜索 data/users 中的个人文件，返回稳定 file_id。涉及简历或其他用户资料的任务应先搜索。",
            has_side_effect=True
        ),
        _wrap_tool(
            fn=read_catalog_file,
            name="read_user_file",
            description="使用文件目录返回的 file_id 读取该个人文件内容；仅允许 data/users 中登记的文件。",
            has_side_effect=False
        ),
        _wrap_tool(
            fn=describe_user_file,
            name="describe_user_file",
            description="生成并保存已登记用户文件的摘要属性。",
            has_side_effect=True
        ),
        _wrap_tool(
            fn=update_user_file_tags,
            name="set_user_file_tags",
            description="设置用户文件属性目录中的标签，便于之后搜索；不会移动或复制文件。",
            has_side_effect=True
        ),
        _wrap_tool(
            fn=link_user_files_to_task,
            name="link_files_to_task",
            description="将一个或多个 file_id 关联到 task_id；同一个文件可被多个任务复用，不会复制用户文件。",
            has_side_effect=True
        ),
        _wrap_tool(
            fn=list_task_file_resources,
            name="list_task_file_resources",
            description="查看 task_id 已关联的用户文件 ID 和用途。",
            has_side_effect=False
        ),
        _wrap_tool(
            fn=create_task_data_card,
            name="create_task_data_card",
            description="创建任务元数据，并用稳定 file_id 引用用户文件，不复制文件。",
            has_side_effect=True
        ),
        _wrap_tool(
            fn=delete_task_data_card,
            name="delete_task_data_card",
            description="删除任务元数据与文件引用；不会删除用户原始文件。",
            has_side_effect=True
        ),
        _wrap_tool(
            fn=search_user_memory,
            name="search_user_memory",
            description="在用户明确保存的结构化记忆中按关键词检索偏好、背景和约定。",
            has_side_effect=False,
        ),
        _wrap_tool(
            fn=remember_user_memory,
            name="remember_user_memory",
            description="仅在用户明确要求记住某条长期事实或偏好时保存；不要保存密钥、密码或支付信息。",
            has_side_effect=True,
        ),

        # ==================== 2. 日程与任务管理工具链 ====================
        _wrap_tool(
            fn=list_schedules,
            name="schedule_list_tool",
            description="获取当前数据库中保存的所有固定日程与重复循环日程列表。",
            has_side_effect=False
        ),
        _wrap_tool(
            fn=add_schedule,
            name="schedule_add_tool",
            description="Create a fixed or recurring schedule; owner is required. Use owner='user' for personal reminders and owner='agent' for automatic work. Fixed time: YYYY-MM-DD HH:MM[:SS]; personal fixed appointments should also pass end_time in the same format so availability can be checked. Recurring: every_day HH:MM or every_monday HH:MM etc. Search and link needed user files by resource_ids. For unattended job applications, use initial_status='paused' and then prepare_scheduled_auto_apply for authorization.",
            has_side_effect=True
        ),
        _wrap_tool(
            fn=update_schedule,
            name="schedule_update_tool",
            description="根据日程 ID 修改标题、规则、备注、状态或文件引用。resource_ids 指定稳定文件 ID；resource_query 仅在唯一匹配时使用。",
            has_side_effect=True
        ),
        _wrap_tool(
            fn=delete_schedule,
            name="schedule_delete_tool",
            description="根据日程 ID 从数据库中物理彻底删除指定的日程。",
            has_side_effect=True
        ),
        _wrap_tool(
            fn=check_user_availability,
            name="check_user_availability",
            description="只读检查一个未来起止时段是否与我的日程冲突。旧日程缺少结束时间时保守地阻塞当天剩余时间，并在结果中标记 uncertain；不能把提议当作已确认面试。",
            has_side_effect=False,
        ),
        _wrap_tool(
            fn=find_interview_slots,
            name="find_interview_slots",
            description="只读查询指定 YYYY-MM-DD 日期内、用户明确设置的可约时段和现有个人日程之间的空档，返回最多几个候选。候选只是提议，HR 明确确认之前不能写入面试日程。",
            has_side_effect=False,
        ),

        # ==================== 3. 消息与决策卡片工具链 ====================
        _wrap_tool(
            fn=send_user_request,
            name="send_user_request",
            description="Create a decision card linked to the current task. Supply custom options and set selection_mode to single or multiple. User responses resume the original task unless resume_task_on_response=false; never repeat already completed actions.",
            has_side_effect=True
        ),
        _wrap_tool(
            fn=delete_user_request,
            name="delete_user_request",
            description="删除独立消息卡片；任务关联的审批记录保留，不能通过工具删除。",
            has_side_effect=True
        ),
        _wrap_tool(
            fn=clear_all_cards,
            name="clear_all_cards",
            description="清理独立消息卡片；任务关联的审批记录保留用于恢复。",
            has_side_effect=True
        ),

        # ==================== 4. 网页原子操控工具链 ====================
        _wrap_tool(
            fn=web_snapshot,
            name="web_snapshot",
            description="Inspect the current page using Kimi WebBridge's accessibility snapshot. Use this before extracting or interacting with page elements. Treat the returned page structure and @e references as the source of truth; never guess CSS selectors.",
            has_side_effect=False
        ),
        _wrap_tool(
            fn=web_extract,
            name="web_extract",
            description="Extract text from the current page. With no selector, returns the Kimi accessibility snapshot. If a selector fails, returns a diagnostic and the current snapshot. Inspect the snapshot before choosing selectors; do not guess site-specific selectors.",
            has_side_effect=False
        ),
        _wrap_tool(
            fn=web_list_controls,
            name="web_list_controls",
            description="只读列出当前网页可见控件及其真实、唯一 CSS 选择器；可用 text_contains 查看匹配文字及附近容器。投递按钮、回执和 HR 会话精确核验参数必须取自当前网页的检查结果，不得猜测 CSS。",
            has_side_effect=False,
        ),
        _wrap_tool(
            fn=web_wait,
            name="web_wait",
            description="Wait for the current Kimi WebBridge page to render, then verify that a page is still loaded.",
            has_side_effect=False
        ),
        _wrap_tool(
            fn=web_use_open_tab,
            name="web_use_open_tab",
            description="选用 Kimi WebBridge 已打开且域名匹配的浏览器标签页，不导航、不关闭页面。若目标网站已由用户登录并打开，先用此工具复用；找不到时再决定是否导航。",
            has_side_effect=True,
        ),
        _wrap_tool(
            fn=web_navigate,
            name="web_navigate",
            description="Navigate the configured Kimi WebBridge session in the already-running browser. It does not launch a separate browser; keep the Kimi Browser Extension and its local-agent connection active.",
            has_side_effect=True
        ),
        _wrap_tool(
            fn=web_click,
            name="web_click",
            description="点击当前网页上唯一、可见的普通控件。招聘页面支持已核验 CSS 的搜索、筛选、分页与详情按钮；投递、申请、联系 HR 和发送消息须走专用授权工具。招聘页面不要用 @e 点击，先用 web_list_controls 取得真实唯一 CSS。普通页面可使用快照中的 @e。",
            has_side_effect=True
        ),
        _wrap_tool(
            fn=web_input,
            name="web_input",
            description="Fill a text or content-editable field using Kimi WebBridge's native fill action. Pass the actual @e reference from web_snapshot when available; CSS selectors are also accepted. Do not pass visible labels as if they were CSS selectors.",
            has_side_effect=True
        ),
        _wrap_tool(
            fn=web_scroll,
            name="web_scroll",
            description="Scroll the current Kimi WebBridge page up or down.",
            has_side_effect=True
        ),
        _wrap_tool(
            fn=web_upload,
            name="web_upload",
            description="在普通网页将 data/users 中登记的一个 file_id 附加到真实文件输入控件；招聘网页上的简历上传必须使用已授权的 submit_job_application。浏览器上传不表示网站已接收投递。",
            has_side_effect=True,
        ),
        _wrap_tool(
            fn=web_verify_page,
            name="web_verify_page",
            description="Read the current WebBridge page and require expected_text and/or expected_url_contains to be present. Use for an observed page marker; it alone does not prove a platform accepted an application.",
            has_side_effect=False,
        ),
        _wrap_tool(
            fn=qq_mail_open_compose,
            name="qq_mail_open_compose",
            description="复用用户已登录的 QQ 邮箱网页版标签页并打开空白写信页；不填写、不发送，不覆盖已写内容。仅支持当前观察到的新版 QQ 邮箱界面。",
            has_side_effect=True,
        ),
        _wrap_tool(
            fn=qq_mail_write_draft,
            name="qq_mail_write_draft",
            description="在已登录 QQ 邮箱的空白写信页填写主题、正文和可选的单个收件人邮箱；保留现有签名，不点击发送、不覆盖旧草稿。只确认页面已填写，不能宣称已保存到草稿箱。先调用 qq_mail_open_compose。",
            has_side_effect=True,
        ),
        _wrap_tool(
            fn=qq_mail_list_unread,
            name="qq_mail_list_unread",
            description="查看 QQ 邮箱当前收件箱页面的未读发件人、主题、时间和摘要；不打开或标记邮件已读。若写信页正在使用，会另开邮箱标签页。",
            has_side_effect=True,
        ),
        _wrap_tool(
            fn=qq_mail_prepare_send,
            name="qq_mail_prepare_send",
            description="读取当前 QQ 邮箱已填写且有唯一确认收件人的写信页，生成含准确正文的发送确认卡；此步骤不会发送。仅当用户明确要求寄出邮件时调用。",
            has_side_effect=True,
        ),
        _wrap_tool(
            fn=qq_mail_send_approved,
            name="qq_mail_send_approved",
            description="仅在 QQ 邮件发送确认卡批准后的原任务续办中，对比写信页内容并尝试发送一次。结果 UNKNOWN 时检查已发送文件夹，不得自动重试。",
            has_side_effect=True,
        ),

        # QQ and WeChat desktop clients; separate from browser and webmail tools.
        _wrap_tool(
            fn=desktop_chat_windows, name="desktop_chat_windows",
            description="只读列出 QQ 或微信桌面软件的当前窗口及 handle。platform 使用 qq 或 wechat；不会启动软件。",
            has_side_effect=False,
        ),
        _wrap_tool(
            fn=desktop_chat_snapshot, name="desktop_chat_snapshot",
            description="只读检查 QQ 或微信桌面窗口公开的可访问控件结构。客户端改版或无法读取时据此诊断；不要猜控件。",
            has_side_effect=False,
        ),
        _wrap_tool(
            fn=desktop_chat_open_conversation, name="desktop_chat_open_conversation",
            description="按精确联系人或群名称搜索并打开 QQ/微信桌面会话，必须核对会话标题；不会发送消息。",
            has_side_effect=True,
        ),
        _wrap_tool(
            fn=desktop_chat_read_visible, name="desktop_chat_read_visible",
            description="读取已核对的 QQ/微信桌面会话中当前可见的消息区域文本。不能据此宣称已读全历史或确定发送者。",
            has_side_effect=True,
        ),
        _wrap_tool(
            fn=desktop_chat_check_unread, name="desktop_chat_check_unread",
            description="只读检查 QQ/微信桌面会话列表里真正暴露的未读标记。结果 UNKNOWN 表示无法判断，不能视为没有新消息。",
            has_side_effect=False,
        ),
        _wrap_tool(
            fn=desktop_chat_send_message, name="desktop_chat_send_message",
            description="仅响应用户当前对话明确要求，向唯一且已核对标题的 QQ/微信桌面会话发送一条消息。需要精确 contact 和 message；不能由日程代发。UNKNOWN 时先人工核对，绝不重试。",
            has_side_effect=True,
        ),

        # Independent document production and bounded general desktop actions.
        _wrap_tool(
            fn=document_create_text, name="document_create_text",
            description="在 Agent 成品目录新建 TXT、Markdown、Word DOCX 或 PDF。name 只写文件名和后缀；不会覆盖现有文件。正文由 content 提供。",
            has_side_effect=True,
        ),
        _wrap_tool(
            fn=document_create_spreadsheet, name="document_create_spreadsheet",
            description="根据 headers 和 rows 新建 XLSX 表格；单元格以文字保存，不执行公式，不覆盖同名文件。",
            has_side_effect=True,
        ),
        _wrap_tool(
            fn=document_revise_text, name="document_revise_text",
            description="对一个 Agent 成品 source_name 或一个已登记用户文件 source_file_id 中的 TXT/MD/DOCX 作精确文字替换，另存 output_name。两种来源只填一种；不会修改原文件。Word 中跨格式片段的替换会停止。",
            has_side_effect=True,
        ),
        _wrap_tool(
            fn=document_append_text, name="document_append_text",
            description="向 Agent 成品 source_name 或已登记用户 TXT/MD/DOCX 尾部追加文字，另存 output_name；原文件不变。",
            has_side_effect=True,
        ),
        _wrap_tool(
            fn=document_append_spreadsheet_rows, name="document_append_spreadsheet_rows",
            description="向 Agent 成品 source_name 或已登记用户 XLSX 的末尾追加 rows，并另存 output_name；原表格不变。",
            has_side_effect=True,
        ),
        _wrap_tool(
            fn=document_update_spreadsheet_cells, name="document_update_spreadsheet_cells",
            description="用 updates 中的 cell、expected、value 精确更新 XLSX 单元格并另存 output_name；当前值与 expected 不符时停止，支持用 sheet_name 指定工作表。",
            has_side_effect=True,
        ),
        _wrap_tool(
            fn=document_list_outputs, name="document_list_outputs",
            description="只读列出 Agent 已生成的文档成品文件名和路径。",
            has_side_effect=False,
        ),
        _wrap_tool(
            fn=document_read_output, name="document_read_output",
            description="只读查看 Agent 已生成的 TXT、MD、DOCX、XLSX 或 PDF 内容，用于核对成品；用户原件继续用 read_user_file。",
            has_side_effect=False,
        ),
        _wrap_tool(
            fn=desktop_list_windows, name="desktop_list_windows",
            description="只读列出当前 Windows 桌面上可见的应用窗口及 handle。",
            has_side_effect=False,
        ),
        _wrap_tool(
            fn=desktop_inspect_window, name="desktop_inspect_window",
            description="只读检查指定 window_handle 的可访问控件，返回两分钟有效的 snapshot_id 和 control_ref。QQ、微信及浏览器须使用各自专用工具。",
            has_side_effect=False,
        ),
        _wrap_tool(
            fn=desktop_focus_window, name="desktop_focus_window",
            description="在当前对话任务中，将已列出的普通 Windows 应用窗口切到前台；不可操作 QQ、微信或浏览器。",
            has_side_effect=True,
        ),
        _wrap_tool(
            fn=desktop_invoke_control, name="desktop_invoke_control",
            description="在当前对话任务中触发一个刚刚检查到的具名低风险按钮、菜单、标签或列表项。使用 snapshot_id 和 control_ref，不猜坐标；拒绝发送、提交、支付及删除类控件。",
            has_side_effect=True,
        ),
        _wrap_tool(
            fn=desktop_fill_control, name="desktop_fill_control",
            description="在当前对话任务中填写刚检查到的普通 Edit 控件，核对结果。已有内容时默认不覆盖；明确需要覆盖才传 replace_existing=true。",
            has_side_effect=True,
        ),
        _wrap_tool(
            fn=desktop_launch_application, name="desktop_launch_application",
            description="在当前对话任务中启动 Windows 记事本、计算器或文件资源管理器。app 使用 notepad、calculator 或 explorer。启动请求后用 desktop_list_windows 核实。",
            has_side_effect=True,
        ),
        _wrap_tool(
            fn=desktop_open_user_file, name="desktop_open_user_file",
            description="在当前对话任务中，按文件目录中的 file_id 用系统默认软件打开普通文档或图片；不执行脚本。",
            has_side_effect=True,
        ),
        _wrap_tool(
            fn=desktop_read_clipboard, name="desktop_read_clipboard",
            description="在当前对话任务中读取 Windows 剪贴板里的文字，最多返回 10000 字；不会把内容写入执行凭证。",
            has_side_effect=False,
        ),
        _wrap_tool(
            fn=desktop_write_clipboard, name="desktop_write_clipboard",
            description="在当前对话任务中把 text 写到 Windows 剪贴板；不会自动粘贴或发送。",
            has_side_effect=True,
        ),
        _wrap_tool(
            fn=desktop_capture_window, name="desktop_capture_window",
            description="在当前对话任务中保存指定普通窗口的可见像素截图，返回本地文件路径；遮挡区域可能出现在截图里。",
            has_side_effect=True,
        ),

        # ==================== 5. 招聘巡航业务工具链 ====================
        _wrap_tool(
            fn=get_recruitment_summary,
            name="get_recruitment_summary",
            description="获取招聘岗位、投递状态以及 HR 自动回复记录的汇总统计。",
            has_side_effect=False
        ),
        _wrap_tool(
            fn=parse_and_process_jobs,
            name="parse_and_process_jobs",
            description="保存从真实网页提取的岗位字典；若要之后按 JD 生成首次 HR 回复，请同时提供网页观察到的 jd_text 正文。默认下发岗位初审卡；具体投递可设 create_review_card=false，再用 prepare_job_application 绑定岗位和简历。此工具不会投递。",
            has_side_effect=True
        ),
        _wrap_tool(
            fn=listen_hr_messages,
            name="listen_hr_messages",
            description="把已经从网页读取并传入的 HR 消息列表做成提醒卡片；本工具本身不主动轮询网站，也不发送回复。",
            has_side_effect=True
        ),
        _wrap_tool(
            fn=prepare_scheduled_auto_apply,
            name="prepare_scheduled_auto_apply",
            description="为已暂停的 Agent 日程创建自动投递范围授权卡，绑定岗位关键词、可选地点、网站域名、单次数量上限和简历交付方式。file_upload 模式绑定已关联的 file_id；platform_profile 模式绑定网站上观察到的在线简历准确名称和唯一选择器。只有用户批准后日程才启用。",
            has_side_effect=True,
        ),
        _wrap_tool(
            fn=prepare_job_application,
            name="prepare_job_application",
            description="为已记录岗位准备实际投递并固定简历交付方式。file_upload 需要目录中的简历 file_id；platform_profile 需要网页中观察到的在线简历准确名称和唯一选择器，不表示上传了本地简历。默认逐岗位审批；已批准的定时授权可核对范围后直接准备。",
            has_side_effect=True,
        ),
        _wrap_tool(
            fn=submit_job_application,
            name="submit_job_application",
            description="执行已准备且获授权的最终岗位投递。仅用当前页观察到的选择器：file_upload 需 upload_selector 和上传后可见的 attachment_ready_text；platform_profile 需在线简历准确名称、唯一选择器和可核验的选中状态。receipt_selector 必填，回执元素须同时包含成功文字、岗位名、公司名；无法确认记 UNKNOWN 并停止。",
            has_side_effect=True,
        ),
        _wrap_tool(
            fn=get_application_status,
            name="get_application_status",
            description="读取一个岗位申请的准备、批准、已核实提交、结果未知或失败状态与证据；APPROVED 只表示批准，并非已投递。",
            has_side_effect=False,
        ),
        _wrap_tool(
            fn=reconcile_application_status,
            name="reconcile_application_status",
            description="读取招聘网站投递历史，并用具体岗位和申请编号的页面证据更新本地 UNKNOWN/ATTEMPTING 为 SUBMITTED 或 NOT_SUBMITTED；不会点击投递。",
            has_side_effect=True,
        ),
        _wrap_tool(
            fn=prepare_scheduled_hr_auto_reply,
            name="prepare_scheduled_hr_auto_reply",
            description="为 Agent 日程申请 HR 回复授权卡，限定网站、消息关键词和单次上限。新任务优先用 reply_mode='draft'：delivery_mode='observe' 只记录草稿、'review' 逐条审批、'auto' 仅在证据及规则通过时自动发送。exact 与 guided 仅兼容旧授权。",
            has_side_effect=True,
        ),
        _wrap_tool(
            fn=prepare_hr_message_response,
            name="prepare_hr_message_response",
            description="旧版 guided 授权兼容工具：按固定类别准备回复；新日程请使用 prepare_hr_reply_draft 自由拟稿。",
            has_side_effect=True,
        ),
        _wrap_tool(
            fn=prepare_hr_reply_draft,
            name="prepare_hr_reply_draft",
            description="模型先根据真实 HR 原文拟 draft_text，再逐项提交 evidence：hr_message 原文、job 已登记字段、当前日程关联的 user_file 原文、或明确保存的 preference。工具核对来源并保存草稿；observe 只记录，review 发逐条审批卡，auto 仅在低风险且证据有效时返回 PREPARED。面试提议还需 proposed_start/proposed_end。返回 PREPARED 后才可发送；审批草稿获准后可以直接用 auto_reply_hr_message 发送已保存的原文。",
            has_side_effect=True,
        ),
        _wrap_tool(
            fn=auto_reply_hr_message,
            name="auto_reply_hr_message",
            description="仅在当前批准的 Agent 日程运行中，核对真实 HR 会话、入站消息和控件后发送。draft 模式使用已保存且仍通过证据、授权和审批核验的模型草稿；observe 禁止发送。结果不明记 UNKNOWN，禁止自动重试。",
            has_side_effect=True,
        ),
        _wrap_tool(
            fn=record_confirmed_interview,
            name="record_confirmed_interview",
            description="仅在同一真实 HR 会话中核验发件人和明确包含同一日期、开始时间的确认原文；此前必须有页面确认已发的面试提议。再次查我的日程无冲突后写入个人面试日程并发通知卡。须提供页面观察到的 CSS 选择器。",
            has_side_effect=True,
        ),
        _wrap_tool(
            fn=get_hr_auto_reply_status,
            name="get_hr_auto_reply_status",
            description="按 HR 消息指纹读取自动回复的保存状态与网页证据；OBSERVED_SENT 只表示页面观察到发送结果。",
            has_side_effect=False,
        ),
        _wrap_tool(
            fn=get_hr_auto_reply_summary,
            name="get_hr_auto_reply_summary",
            description="查看 HR 自动回复的状态统计，可按日程筛选。",
            has_side_effect=False,
        ),
    ]

    permissions = load_runtime_settings()["tool_permissions"]
    tools = [tool for tool in tools if is_tool_enabled(tool.metadata.name, permissions)]

    # ==================== 动态挂载可选知识库检索工具 ====================
    if permissions.get("knowledge_search", True) and permissions.get("user_files", True):
        if index is None:
            tools.append(_wrap_tool(
                fn=search_personal_knowledge,
                name="personal_knowledge_search",
                description="按问题对 data/users 的个人资料做语义检索。首次调用时才构建索引；文件变化时刷新索引。需要找准确文件名或 file_id 时先用 search_user_files。",
                has_side_effect=True,
            ))
        else:
            tools.append(build_rag_tool(index))

    return tools
