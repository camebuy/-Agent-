import html
import json
import os
import re
import socket
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import streamlit as st

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import settings
from config.prompts import load_system_prompt, save_system_prompt
from src.core.system.path_manager import PathManager
from src.core.system.terminal_logger import TERMINAL_LOG_FILE, TOOL_LABELS, humanize_event
from src.core.tasks.schedule_runner import ScheduleRunner
from src.tools.activity_tool import _load_schedules, add_schedule, delete_schedule, update_schedule
from src.tools.database_tool import (
    _migrate_legacy_task_cards, catalog_rows_for_ui, get_task_resources,
    list_task_resource_links, set_user_file_tags, summarize_user_file,
    sync_user_file_catalog,
)
from src.tools.token_tracker import get_today_stats
from src.core.agent.memory_manager import MemoryManager
from src.core.system.runtime_settings import load_runtime_settings, save_runtime_settings
from chat_job_manager import ChatJobManager, DEFAULT_CONVERSATION_ID


st.set_page_config(page_title="灰风 · Personal AI", page_icon="G", layout="wide", initial_sidebar_state="expanded")


@st.cache_data(ttl=10, show_spinner=False)
def check_local_tcp_port(host: str, port: int) -> bool:
    try:
        with socket.create_connection((str(host), int(port)), timeout=0.35):
            return True
    except (OSError, ValueError, TypeError):
        return False


@st.cache_data(max_entries=3, show_spinner=False)
def generated_document_bytes(path_text: str, mtime_ns: int, size_bytes: int) -> bytes:
    """Cache a selected download across Streamlit reruns without retaining stale versions."""
    path = Path(path_text)
    if (path.is_symlink() or path.resolve().parent != PathManager.DOCUMENT_OUTPUT_DIR.resolve()
            or path.stat().st_mtime_ns != mtime_ns or path.stat().st_size != size_bytes):
        raise ValueError("成品文件已变化，请刷新页面后重试下载。")
    return path.read_bytes()


@st.cache_data(ttl=10, show_spinner=False)
def webbridge_connection_status() -> dict:
    """Read daemon and extension status without opening or changing browser tabs."""
    executable = Path.home() / ".kimi-webbridge" / "bin" / (
        "kimi-webbridge.exe" if os.name == "nt" else "kimi-webbridge"
    )
    if executable.is_file():
        try:
            result = subprocess.run(
                [str(executable), "status"], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=3,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode == 0:
                state = json.loads(result.stdout)
                if isinstance(state, dict):
                    return {
                        "running": bool(state.get("running")),
                        "extension_connected": bool(state.get("extension_connected")),
                        "port": state.get("port"),
                        "addr": state.get("addr"),
                        "source": "daemon",
                    }
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    return {
        "running": check_local_tcp_port(settings.KIMI_WEBBRIDGE_HOST, settings.KIMI_WEBBRIDGE_PORT),
        "extension_connected": None,
        "port": None,
        "addr": None,
        "source": "port",
    }


_saved_runtime = load_runtime_settings()
if not st.session_state.get("_legacy_task_cards_migrated"):
    _migrate_legacy_task_cards()
    st.session_state["_legacy_task_cards_migrated"] = True
st.markdown("""
<style>
:root{
  --ink:#d6d8dc;--muted:#9da2a8;--line:#484b50;--canvas:#2b2d30;
  --surface:#33363a;--sidebar:#303236;--field:#3b3e42;
  --accent:#589878;--accent-soft:#3b5045;
  --text-color:#d6d8dc;--background-color:#2b2d30;
  --secondary-background-color:#33363a;--primary-color:#589878;
  color-scheme:dark;
}
html,body,.stApp,[data-testid="stAppViewContainer"],[data-testid="stMain"],[data-testid="stMainBlockContainer"]{
  background-color:var(--canvas)!important;color:var(--ink)!important;
}
[data-testid="stHeader"]{background:var(--canvas)!important}
[data-testid="stMainBlockContainer"]{max-width:1240px;padding-top:2rem;padding-bottom:4rem}
[data-testid="stSidebar"],[data-testid="stSidebar"]>div,[data-testid="stSidebar"]>div>div{
  background-color:var(--sidebar)!important;color:var(--ink)!important;
}
[data-testid="stSidebar"]>div:first-child{padding-top:1.4rem}
.brand-title{font-size:1.35rem;font-weight:720;letter-spacing:-.03em;color:var(--ink)}
.brand-subtitle{color:var(--muted);font-size:.82rem;margin-top:.2rem}
.nav-caption{color:var(--muted);font-size:.72rem;letter-spacing:.1em;text-transform:uppercase;margin:1.8rem 0 .45rem}
[data-testid="stSidebar"] [role="radiogroup"]{gap:.22rem}
[data-testid="stSidebar"] [role="radiogroup"] label{padding:.48rem .6rem;border-radius:.65rem;color:#c3c7cc}
[data-testid="stSidebar"] [role="radiogroup"] label:has(input:checked){background:var(--accent-soft);color:#d7e7de;font-weight:650}
h1,h2,h3,h4{color:var(--ink)!important;letter-spacing:-.025em}
.stMarkdown,.stMarkdown p,.stMarkdown li,[data-testid="stMarkdownContainer"]{color:var(--ink)}
.page-kicker{color:var(--muted);font-size:.92rem;margin-top:-.35rem;margin-bottom:1.15rem}
[data-testid="stVerticalBlockBorderWrapper"]{border-color:var(--line)!important;border-radius:14px;background:var(--surface)!important}
[data-testid="stTextInput"] input,[data-testid="stTextArea"] textarea,[data-testid="stNumberInput"] input,[data-testid="stDateInput"] input,[data-testid="stTimeInput"] input,[data-testid="stChatInput"] textarea,[data-baseweb="input"] input,[data-baseweb="textarea"] textarea,[data-baseweb="select"]>div{
  background:var(--field)!important;color:var(--ink)!important;border-color:var(--line)!important;
}
input::placeholder,textarea::placeholder{color:#9da2a8!important}
div.stButton>button{background:var(--surface)!important;border-radius:9px;border-color:var(--line);color:var(--ink);font-weight:560}
div.stButton>button:hover{border-color:var(--accent);color:#b8e0c9}
div.stButton>button[kind="primary"]{background:var(--accent)!important;border-color:var(--accent);color:#f2f7f4}
[data-baseweb="popover"],[role="listbox"],[role="dialog"]{background:var(--surface)!important;color:var(--ink)!important}
[data-testid="stTabs"] button{color:var(--muted)}
[data-testid="stTabs"] button[aria-selected="true"]{color:var(--ink);border-color:var(--accent)}
[data-testid="stNotification"]{background:var(--surface)!important;color:var(--ink)!important}
.home-stat{border:1px solid var(--line);border-radius:14px;background:var(--surface);min-height:112px;padding:17px 19px}
.home-stat-label{color:var(--muted);font-size:.82rem;margin-bottom:.45rem}
.home-stat-value{color:var(--ink);font-size:1.4rem;line-height:1.2;font-weight:700;overflow-wrap:anywhere}
.home-stat-note{color:var(--muted);font-size:.77rem;margin-top:.42rem}
.terminal-window{background:#050706;border:1px solid #17241c;border-radius:13px;padding:14px 18px 18px;box-shadow:0 12px 32px rgba(0,0,0,.24)}
.terminal-bar{color:#91a69a;font:12px/1.2 Consolas,"Cascadia Code",monospace;padding-bottom:12px;border-bottom:1px solid #1d2a22;margin-bottom:12px}
.terminal-dots{color:#5c6f63;letter-spacing:.24em;margin-right:10px}
.terminal-output{color:#55dc84;font:13px/1.7 Consolas,"Cascadia Code","Courier New",monospace;white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word;max-height:62vh;min-height:280px;overflow:auto;scrollbar-color:#294b35 #050706}
.terminal-output .log-time{color:#79b28b}.terminal-output .log-success{color:#69f39a}.terminal-output .log-warn{color:#f3c66d}.terminal-output .log-cmd{color:#83d7a0}
.agent-activity{margin-top:12px}
.activity-heading{font-size:.86rem;font-weight:680;color:#d5ded8;margin-bottom:9px}
.activity-note{color:var(--muted);font-size:.74rem;font-weight:450;margin-left:.4rem}
.activity-list{display:flex;flex-direction:column;gap:7px}
.activity-step{display:flex;align-items:center;gap:10px;border:1px solid var(--line);background:#373a3e;border-radius:10px;padding:10px 11px;min-width:0}
.activity-mark{width:23px;height:23px;border-radius:50%;display:grid;place-items:center;flex:0 0 auto;font-size:.75rem;font-weight:700}
.activity-mark.ok{color:#a9e4bd;background:#314b3b}.activity-mark.error{color:#f0aaaa;background:#503537}
.activity-mark.running{color:#f3d895;background:#4c4430}.activity-mark.blocked{color:#d2c9b7;background:#444039}
.activity-title{color:var(--ink);font-size:.82rem;font-weight:620;overflow-wrap:anywhere}
.activity-tool{color:var(--muted);font-size:.75rem;line-height:1.4;margin-top:2px}
.event-meta{color:var(--muted);font-size:.77rem}
@media(max-width:760px){[data-testid="stMainBlockContainer"]{padding-left:1rem;padding-right:1rem}.terminal-output{max-height:55vh;font-size:12px}}
</style>
""", unsafe_allow_html=True)


def normalize_schedules(raw):
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
    if isinstance(raw, list):
        return [dict(x) for x in raw if isinstance(x, dict)]
    if not isinstance(raw, dict):
        return []
    result = []
    for key, kind in (("fixed_schedules", "fixed"), ("recurring_schedules", "recurring"), ("schedules", "fixed"), ("items", "fixed")):
        values = raw.get(key, [])
        for item in values if isinstance(values, list) else []:
            if isinstance(item, dict):
                item = dict(item)
                item.setdefault("type", kind)
                item.setdefault("status", "active")
                result.append(item)
    return result


def schedule_matches_date(item: dict, selected: date) -> bool:
    kind = item.get("type") or item.get("schedule_type") or ("recurring" if "rule" in item else "fixed")
    if kind == "fixed":
        value = str(item.get("time") or item.get("datetime") or "")
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date() == selected
        except ValueError:
            return value[:10] == selected.isoformat()
    match = re.fullmatch(r"every_(day|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+\d{1,2}:\d{2}", str(item.get("rule") or "").strip().lower())
    if not match:
        return False
    day = match.group(1)
    weekdays = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    return day == "day" or day == weekdays[selected.weekday()]


def schedule_display_time(item: dict) -> str:
    kind = item.get("type") or item.get("schedule_type") or ("recurring" if "rule" in item else "fixed")
    if kind == "fixed":
        value = str(item.get("time") or "")
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%H:%M")
        except ValueError:
            return value
    match = re.fullmatch(r"every_(day|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+(\d{1,2}:\d{2})", str(item.get("rule") or "").strip(), re.I)
    if not match:
        return str(item.get("rule") or "周期日程")
    labels = {"day":"每天","monday":"每周一","tuesday":"每周二","wednesday":"每周三","thursday":"每周四","friday":"每周五","saturday":"每周六","sunday":"每周日"}
    return f"{labels[match.group(1).lower()]} · {match.group(2)}"


def load_cards() -> list:
    from src.tools.activity_tool import read_cards
    return read_cards()


def ensure_file_catalog_synced() -> None:
    if not st.session_state.get("_file_catalog_synced"):
        sync_user_file_catalog()
        st.session_state["_file_catalog_synced"] = True


def choice_label(value) -> str:
    text = str(value)
    return {"approve":"同意","approved":"同意","yes":"同意","reject":"拒绝","rejected":"拒绝","no":"拒绝","acknowledge":"知晓","ack":"知晓","noted":"知晓"}.get(text.strip().lower(), text)


def apply_card_choice(card: dict, choice, comment: str = "") -> None:
    from src.core.tasks.task_actions import respond_to_task_card
    respond_to_task_card(get_chat_job_manager(), str(card["card_id"]), choice, comment)


def resume_task_after_approval(task_id: str) -> Optional[str]:
    from src.core.tasks.task_actions import resume_task_after_approval as resume
    return resume(get_chat_job_manager(), task_id)


def resume_interrupted_task(task_id: str) -> str:
    from src.core.tasks.task_actions import resume_interrupted_task as resume
    return resume(get_chat_job_manager(), task_id)


def cancel_task_from_ui(task_id: str) -> str:
    from src.core.tasks.task_actions import cancel_task
    return cancel_task(get_chat_job_manager(), task_id)


def load_log_events() -> list:
    try:
        data = json.loads(TERMINAL_LOG_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, list) or any(not isinstance(x, dict) for x in data):
            raise ValueError("日志数据应为事件列表")
        return data
    except FileNotFoundError:
        return []
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError(f"日志文件无法读取，原文件已保留：{exc}") from exc


@st.cache_data(ttl=30, show_spinner=False)
def job_receipts_for_display(job_id: str) -> list:
    from src.core.system.ledger import ledger
    return ledger.get_job_receipts(job_id)


def build_activity_panel(receipts: list[dict], *, live: bool) -> str:
    """Show recorded tool calls without exposing their raw parameters or results."""
    if not receipts:
        return ""
    steps = []
    for receipt in receipts[-20:]:
        tool_name = str(receipt.get("tool_name") or "tool")
        status = str(receipt.get("status") or "").upper()
        state_class, mark, state = {
            "SUCCESS": ("ok", "✓", "已完成"),
            "RUNNING": ("running", "·", "正在执行" if live else "结果尚未确认"),
            "INTERCEPTED": ("blocked", "–", "已拦截"),
            "FAILED": ("error", "!", "执行失败"),
        }.get(status, ("blocked", "·", "状态待核对"))
        label = TOOL_LABELS.get(tool_name, "其他工具操作")
        steps.append(
            '<div class="activity-step">'
            f'<span class="activity-mark {state_class}">{mark}</span>'
            '<div><div class="activity-title">'
            f'{html.escape(label)}'
            '</div><div class="activity-tool">'
            f'{html.escape(state)}'
            '</div></div></div>'
        )
    hidden = f'<span class="activity-note">最近 20 步，共 {len(receipts)} 步</span>' if len(receipts) > 20 else ""
    return (
        '<section class="agent-activity">'
        '<div class="activity-heading">调用工具'
        f'{hidden}</div>'
        f'<div class="activity-list">{"".join(steps)}</div>'
        '</section>'
    )


def friendly_task_progress(message: str) -> str:
    match = re.fullmatch(r"(正在执行：|已完成工具步骤：|工具步骤失败：)([A-Za-z0-9_]+)", message)
    if not match:
        return message
    label = TOOL_LABELS.get(match.group(2), "工具操作")
    return {
        "正在执行：": f"我正在{label}，等待操作结果。",
        "已完成工具步骤：": f"我已完成{label}，正在决定下一步。",
        "工具步骤失败：": f"{label}遇到问题，我正在核对当前结果。",
    }[match.group(1)]


def render_task_process(job: dict, task: Optional[dict], receipts: list[dict], *, live: bool) -> None:
    """Present a short narrative from observable progress and separate tool cards."""
    status = str(job.get("status") or "")
    status_label = {
        "queued": "等待执行", "running": "执行中", "awaiting_human": "等待审批",
        "completed": "已完成", "failed": "遇到问题", "interrupted": "已中断",
        "cancelled": "已停止", "missed": "已错过",
    }.get(status, "处理中")
    source = str(job.get("source") or "manual")
    opening = {
        "manual": "我先理解你的请求，再判断是否需要调用工具。",
        "schedule": "我先查看这项日程的要求，再按安排执行。",
        "approval": "我先核对你的选择和原任务进度，再继续处理。",
        "recovery": "我先核对已完成的操作，再恢复能够继续的步骤。",
    }.get(source, "我先确认这项任务的要求。")
    with st.expander(f"思考与操作 · {status_label}", expanded=live):
        st.write("任务已收到，正在等待处理。" if status == "queued" else opening)
        if receipts:
            st.write("处理过程中，我调用了这些工具：")
            st.markdown(build_activity_panel(receipts, live=live), unsafe_allow_html=True)
        elif status == "completed":
            st.write("这次可以直接回复，没有调用工具。")
        if task and str(task.get("latest_job_id") or "") == str(job.get("job_id") or ""):
            progress = str(task.get("progress_message") or "").strip()
            if live and progress and status != "queued":
                st.write(friendly_task_progress(progress))
        if status == "awaiting_human":
            st.write("我已暂停后续操作，正在等你的决定。")
        elif status == "completed" and receipts:
            st.write("操作已结束，我已根据结果整理回复。")
        elif status == "failed":
            st.write("处理时遇到问题，结果会在下方回复中说明。")
        elif status == "interrupted":
            st.write("处理已中断，继续之前需要核对实际结果。")
        elif status == "cancelled":
            st.write("这项任务已停止。")
        elif status == "missed":
            st.write("这个日程已错过执行时间。")


def render_assistant_message(content: str, job: Optional[dict] = None,
                             task: Optional[dict] = None) -> None:
    from src.core.agent.chat_manager import strip_activity_markup
    text = str(content or "")
    text = re.sub(r"(?is)<details>\s*<summary>\s*系统执行凭证\s*</summary>.*?</details>\s*", "", text, count=1)
    text = strip_activity_markup(text)
    if job:
        try:
            render_task_process(job, task, job_receipts_for_display(str(job["job_id"])), live=False)
        except Exception as exc:
            st.caption(f"任务过程暂时不可用：{exc}")
    if text:
        st.markdown(text, unsafe_allow_html=False)


@st.dialog("添加日程", width="small")
def schedule_dialog(default_day: date) -> None:
    title = st.text_input("标题", placeholder="例如：下午四点面试")
    owner_label = st.selectbox("归属", ["我的日程", "Agent 日程"])
    kind_label = st.selectbox("类型", ["固定日程", "循环日程"])
    description = st.text_area("备注", placeholder="可选")
    attach_files = st.checkbox("关联用户文件", help="只保存文件 ID 引用，不会复制或移动文件。")
    resource_ids = []
    if attach_files:
        try:
            ensure_file_catalog_synced()
            file_rows = catalog_rows_for_ui()
        except (OSError, ValueError, RuntimeError) as exc:
            st.error(f"文件目录无法读取：{exc}")
            return
        if file_rows:
            labels = {row["file_id"]: f"{row['file_name']} · {row['file_id']}" for row in file_rows}
            resource_ids = st.multiselect(
                "选择任务需要的文件",
                options=list(labels),
                format_func=lambda file_id: labels.get(file_id, file_id),
            )
        else:
            st.caption("data/users 目前没有可关联的文件。")
    schedule_date = default_day
    schedule_time = st.time_input("时间", value=(datetime.now() + timedelta(minutes=5)).time().replace(second=0, microsecond=0))
    schedule_end = None
    if owner_label == "我的日程" and kind_label == "固定日程":
        schedule_end = st.time_input("结束时间", value=(datetime.combine(date.today(), schedule_time) + timedelta(hours=1)).time().replace(second=0, microsecond=0), key="new_schedule_end")
    recurrence, weekday = "每天", "星期一"
    if kind_label == "固定日程":
        schedule_date = st.date_input("日期", value=default_day, key="new_schedule_date")
    else:
        recurrence = st.selectbox("重复频率", ["每天", "每周"])
        if recurrence == "每周":
            weekday = st.selectbox("每周几", ["星期一","星期二","星期三","星期四","星期五","星期六","星期日"])
    if st.button("保存日程", type="primary", use_container_width=True):
        if not title.strip():
            st.error("请填写日程标题。")
            return
        if kind_label == "固定日程":
            schedule_type, time_or_rule = "fixed", f"{schedule_date.isoformat()} {schedule_time.strftime('%H:%M')}"
            end_time = f"{schedule_date.isoformat()} {schedule_end.strftime('%H:%M')}" if schedule_end else None
        else:
            day_rule = "day" if recurrence == "每天" else {
                "星期一":"monday","星期二":"tuesday","星期三":"wednesday","星期四":"thursday","星期五":"friday","星期六":"saturday","星期日":"sunday"
            }[weekday]
            schedule_type, time_or_rule = "recurring", f"every_{day_rule} {schedule_time.strftime('%H:%M')}"
            end_time = None
        try:
            result = add_schedule(
                schedule_type=schedule_type, title=title, time_or_rule=time_or_rule,
                description=description, owner="user" if owner_label == "我的日程" else "agent",
                resource_ids=resource_ids,
                end_time=end_time,
            )
        except (ValueError, OSError) as exc:
            st.error(f"日程保存失败：{exc}")
            return
        st.session_state["pending_schedule_day"] = schedule_date
        st.success(result)
        st.rerun()


@st.cache_resource
def get_chat_job_manager():
    return ChatJobManager(PathManager.CHAT_RUNTIME_DB)


@st.cache_resource
def get_schedule_runner():
    return ScheduleRunner(get_chat_job_manager())


jobs = get_chat_job_manager()
schedule_runner = get_schedule_runner()
if st.session_state.get("main_nav") == "日程与任务":
    st.session_state["main_nav"] = "日程日历"
with st.sidebar:
    st.markdown('<div class="brand-title">灰风</div><div class="brand-subtitle">Personal AI workspace</div>', unsafe_allow_html=True)
    st.markdown('<div class="nav-caption">工作台</div>', unsafe_allow_html=True)
    nav_choice = st.radio("工作台导航", ["首页终端","对话中心","消息决策中心","日程日历","我的文件","运行日志","设置"], label_visibility="collapsed", key="main_nav")
    st.markdown("---")
    worker = getattr(jobs, "_worker", None)
    st.caption("后台 Agent 在线" if worker and worker.is_alive() else "后台 Agent 离线")
    st.caption("招聘等专项能力可在对话中调用。")


if nav_choice == "首页终端":
    st.title("首页终端")
    st.markdown('<div class="page-kicker">灰风的运行状态、当前模型与 API 用量。</div>', unsafe_allow_html=True)
    active = jobs.get_active_job(DEFAULT_CONVERSATION_ID)
    stats = get_today_stats()
    worker = getattr(jobs, "_worker", None)
    waiting_task = next((item for item in jobs.get_tasks(limit=30) if item.get("status") == "awaiting_human"), None)
    if active:
        task = jobs.get_task(str(active.get("task_id") or "")) or {}
        state = "正在处理"
        state_note = f"日程任务：{task.get('title')}" if task.get("source") == "schedule" else "正在处理对话任务"
    elif waiting_task:
        state, state_note = "等待审批", str(waiting_task.get("title") or "消息决策中心有待处理事项")
    else:
        state = "在线" if worker and worker.is_alive() else "离线"
        state_note = "可接收新任务"
    inp, out = int(stats.get("input_tokens", 0) or 0), int(stats.get("output_tokens", 0) or 0)
    total, calls = int(stats.get("total_tokens", inp + out) or 0), int(stats.get("api_calls", 0) or 0)
    c1, c2, c3 = st.columns([1, 1.15, 1])
    c1.markdown(f'<div class="home-stat"><div class="home-stat-label">Agent 状态</div><div class="home-stat-value">{html.escape(state)}</div><div class="home-stat-note">{html.escape(str(state_note))}</div></div>', unsafe_allow_html=True)
    c2.markdown(f'<div class="home-stat"><div class="home-stat-label">当前模型</div><div class="home-stat-value">{html.escape(_saved_runtime["model_name"])}</div><div class="home-stat-note">{html.escape(_saved_runtime["api_base"])}</div></div>', unsafe_allow_html=True)
    note = f"输入 {inp:,} · 输出 {out:,} · {calls} 次 API 调用" if calls else "等待 API 返回首笔 usage 记录"
    c3.markdown(f'<div class="home-stat"><div class="home-stat-label">今日 Token 总量</div><div class="home-stat-value">{total:,}</div><div class="home-stat-note">{html.escape(note)}</div></div>', unsafe_allow_html=True)
    st.subheader("运行终端")
    st.markdown('<div class="page-kicker">运行事件逐条换行显示。</div>', unsafe_allow_html=True)

    @st.fragment(run_every=3)
    def render_terminal():
        try:
            events = load_log_events()[-80:]
        except RuntimeError as exc:
            st.error(str(exc))
            return
        if not events:
            body = '<div class="terminal-output">还没有运行日志。发送一条消息后，事件会显示在这里。</div>'
        else:
            rows = []
            for event in events:
                t = html.escape(str(event.get("timestamp", "--:--:--")))
                default_mod, default_message = humanize_event(
                    event.get("module", "SYSTEM"), event.get("message", ""), event.get("level", "INFO")
                )
                mod = html.escape(str(event.get("display_module") or default_mod))
                message = html.escape(str(event.get("display_message") or default_message))
                level = str(event.get("level", "INFO")).upper()
                color = {"SUCCESS":"log-success","WARN":"log-warn","ERROR":"log-warn","CMD":"log-cmd"}.get(level, "log-info")
                rows.append(f'<div><span class="log-time">[{t}]</span> <span class="{color}">[{mod}]</span> {message}</div>')
            body = f'<div class="terminal-output">{"".join(rows)}</div>'
        st.markdown('<div class="terminal-window"><div class="terminal-bar"><span class="terminal-dots">● ● ●</span>AGENT RUNTIME</div>' + body + '</div>', unsafe_allow_html=True)
    render_terminal()


elif nav_choice == "对话中心":
    st.title("对话中心")
    st.markdown('<div class="page-kicker">直接描述目标；需要确认的操作会送到消息决策中心。</div>', unsafe_allow_html=True)

    @st.fragment(run_every=3)
    def render_chat():
        manager = get_chat_job_manager()
        messages = manager.get_messages(DEFAULT_CONVERSATION_ID, limit=50)
        jobs_by_id = {
            str(job["job_id"]): job
            for job in manager.get_jobs(DEFAULT_CONVERSATION_ID, limit=50)
        }
        tasks_by_id = {
            str(task["task_id"]): task for task in manager.get_tasks(limit=100)
        }
        for msg in messages:
            with st.chat_message(msg["role"]):
                if msg["role"] == "assistant":
                    job = jobs_by_id.get(str(msg.get("job_id") or ""))
                    task = tasks_by_id.get(str(job.get("task_id") or "")) if job else None
                    render_assistant_message(msg["content"], job, task)
                else:
                    st.markdown(str(msg["content"] or ""), unsafe_allow_html=False)
        active = manager.get_active_job(DEFAULT_CONVERSATION_ID)
        if active:
            with st.chat_message("assistant"):
                try:
                    from src.core.system.ledger import ledger
                    task = tasks_by_id.get(str(active.get("task_id") or ""))
                    if task is None:
                        task = manager.get_task(str(active.get("task_id") or ""))
                    render_task_process(
                        active, task, ledger.get_job_receipts(str(active["job_id"])), live=True,
                    )
                except Exception as exc:
                    st.caption(f"正在处理任务；执行过程暂时不可用：{exc}")
        user_input = st.chat_input("告诉灰风你希望完成什么…")
        if user_input:
            try:
                manager.submit(DEFAULT_CONVERSATION_ID, user_input)
                st.rerun()
            except Exception as exc:
                st.error(f"消息提交失败：{exc}")
    render_chat()


elif nav_choice == "消息决策中心":
    st.title("消息决策中心")
    st.markdown('<div class="page-kicker">集中处理 Agent 需要你确认、选择或知晓的事项。</div>', unsafe_allow_html=True)
    try:
        cards = load_cards()
    except (OSError, ValueError, TypeError) as exc:
        st.error(f"卡片存储无法读取，原文件已保留：{exc}")
        st.stop()
    pending = [x for x in cards if x.get("status") == "pending"]
    history = [x for x in cards if x.get("status") != "pending"]
    col1, col2 = st.columns(2)
    col1.metric("待处理", len(pending))
    col2.metric("已处理", len(history))
    pending_tab, history_tab = st.tabs(["待处理", "历史"])
    with pending_tab:
        if not pending:
            st.info("目前没有待你处理的卡片。")
        for card in pending:
            card_id = str(card.get("card_id") or "unknown")
            with st.container(border=True):
                st.subheader(str(card.get("title") or "待确认事项"))
                st.markdown(str(card.get("content") or ""), unsafe_allow_html=False)
                if card.get("created_at"):
                    st.caption(f"创建于 {card['created_at']}")
                options = card.get("options")
                if not isinstance(options, list) or not options:
                    options = ["approve", "reject"]
                comment = st.text_area("补充说明", key=f"comment_{card_id}", height=80) if card.get("allow_comment") else ""
                if str(card.get("selection_mode", "single")).lower() in ("multiple", "multi"):
                    selected = st.multiselect("可多选", options, format_func=choice_label, key=f"choices_{card_id}")
                    if st.button("提交选择", key=f"submit_{card_id}", disabled=not selected):
                        try:
                            apply_card_choice(card, selected, comment)
                            st.rerun()
                        except (ValueError, OSError) as exc:
                            st.error(f"保存选择失败：{exc}")
                else:
                    for offset in range(0, len(options), 4):
                        group = options[offset:offset + 4]
                        columns = st.columns(len(group))
                        for column, option in zip(columns, group):
                            raw = str(option)
                            if column.button(choice_label(option), key=f"choice_{card_id}_{offset}_{raw}", use_container_width=True):
                                try:
                                    apply_card_choice(card, raw, comment)
                                    st.rerun()
                                except (ValueError, OSError) as exc:
                                    st.error(f"保存选择失败：{exc}")
    with history_tab:
        if not history:
            st.info("还没有历史卡片。")
        for card in history:
            status = str(card.get("status") or "processed")
            label = {"approved":"已同意","rejected":"已拒绝","acknowledged":"已知晓","responded":"已回复","cancelled":"任务已取消"}.get(status, status)
            with st.container(border=True):
                st.markdown(f"**{card.get('title') or '已处理事项'}**")
                st.caption(f"{label} · {card.get('created_at') or ''}")
                if card.get("user_choice") is not None:
                    st.write(f"你的选择：{card['user_choice']}")
                if card.get("user_comment"):
                    st.write(f"补充说明：{card['user_comment']}")
                if card.get("action_error"):
                    st.warning(f"卡片选择已保存，但关联记录更新失败：{card['action_error']}")
                workflow = card.get("workflow") or {}
                if workflow.get("response_job_id"):
                    st.caption("审批结果已交回原任务处理。")
                elif workflow.get("resume_error"):
                    st.warning(f"审批已保存，但任务未能恢复：{workflow['resume_error']}")
                    if st.button("重试恢复任务", key=f"retry_resume_{card.get('card_id')}"):
                        try:
                            resume_task_after_approval(str(workflow.get("task_id") or ""))
                            st.rerun()
                        except (ValueError, OSError) as exc:
                            st.error(f"恢复任务失败：{exc}")


elif nav_choice == "日程日历":
    st.title("日程日历")
    st.markdown('<div class="page-kicker">选择日期，查看当天的个人安排与 Agent 日程。</div>', unsafe_allow_html=True)
    if "pending_schedule_day" in st.session_state:
        st.session_state["schedule_day"] = st.session_state.pop("pending_schedule_day")
    if "schedule_day" not in st.session_state:
        st.session_state["schedule_day"] = date.today()
    date_col, add_col = st.columns([3, 1], vertical_alignment="bottom")
    chosen = date_col.date_input("选择日期", key="schedule_day")
    if add_col.button("添加日程", type="primary", use_container_width=True):
        schedule_dialog(chosen)
    day_names = ("星期一","星期二","星期三","星期四","星期五","星期六","星期日")
    caption = "今天" if chosen == date.today() else chosen.strftime("%Y年%m月%d日")
    st.subheader(f"{caption} · {day_names[chosen.weekday()]}")
    try:
        visible = [x for x in normalize_schedules(_load_schedules()) if schedule_matches_date(x, chosen) and str(x.get("status","active")).lower() != "cancelled"]
    except (OSError, ValueError, RuntimeError) as exc:
        st.error(f"日程无法读取，原文件已保留：{exc}")
        st.stop()
    mine = [x for x in visible if str(x.get("owner","user")).lower() != "agent"]
    agent = [x for x in visible if str(x.get("owner","user")).lower() == "agent"]
    mine_tab, agent_tab = st.tabs([f"我的日程 · {len(mine)}", f"Agent 日程 · {len(agent)}"])
    statuses = {"active":"待执行","running":"执行中","awaiting_human":"等待审批","completed":"已完成","failed":"失败","missed":"已错过","paused":"已暂停","cancelled":"已取消"}

    def render_schedule_group(items, prefix):
        if not items:
            st.info("这一天没有安排。")
            return
        for idx, item in enumerate(sorted(items, key=schedule_display_time)):
            sid = str(item.get("id") or f"{prefix}_{idx}")
            status = str(item.get("status","active")).lower()
            kind = item.get("type") or item.get("schedule_type") or ("recurring" if "rule" in item else "fixed")
            with st.container(border=True):
                main, actions = st.columns([5,1.5])
                end_label = ""
                if item.get("end_time"):
                    try:
                        end_label = "—" + datetime.fromisoformat(str(item["end_time"])).strftime("%H:%M")
                    except ValueError:
                        end_label = "—结束时间待修复"
                main.markdown(f"**{schedule_display_time(item)}{end_label}　{item.get('title') or '未命名日程'}**")
                main.caption(f"{'循环' if kind == 'recurring' else '固定'} · {statuses.get(status,status)} · {item.get('description') or '无备注'}")
                linked_files = get_task_resources(sid)
                if linked_files:
                    main.caption("关联文件：" + "、".join(f"{x['file_name']} [{x['file_id']}]" for x in linked_files))
                if item.get("error"):
                    main.caption(f"最近运行信息：{item['error']}")
                a, d = actions.columns(2)
                if status == "active" and a.button("暂停", key=f"pause_{sid}", use_container_width=True):
                    update_schedule(sid, new_status="paused")
                    st.rerun()
                elif status == "paused" and a.button("启用", key=f"resume_{sid}", use_container_width=True):
                    update_schedule(sid, new_status="active")
                    st.rerun()
                if d.button("删除", key=f"delete_{sid}", use_container_width=True,
                            disabled=status in ("running", "awaiting_human")):
                    try:
                        delete_schedule(sid)
                        st.rerun()
                    except (ValueError, OSError) as exc:
                        st.error(f"删除日程失败：{exc}")
    with mine_tab:
        render_schedule_group(mine, "user")
    with agent_tab:
        render_schedule_group(agent, "agent")


elif nav_choice == "我的文件":
    st.title("我的文件")
    st.markdown('<div class="page-kicker">用户文件统一放在 data/users。这里维护文件属性和稳定 ID，任务通过 ID 引用同一个文件。</div>', unsafe_allow_html=True)
    search_col, refresh_col = st.columns([4, 1.2], vertical_alignment="bottom")
    file_query = search_col.text_input("搜索文件", placeholder="文件名、路径或摘要")
    refresh_clicked = refresh_col.button("同步目录", type="primary", use_container_width=True)
    if refresh_clicked:
        try:
            st.success(sync_user_file_catalog())
            st.session_state["_file_catalog_synced"] = True
        except (OSError, ValueError, RuntimeError) as exc:
            st.error(f"文件目录同步失败：{exc}")
    try:
        ensure_file_catalog_synced()
        rows = catalog_rows_for_ui(file_query)
    except (OSError, ValueError, RuntimeError) as exc:
        st.error(f"文件目录无法读取：{exc}")
        st.stop()
    if rows:
        display = [{
            "file_id": row["file_id"],
            "文件名": row["file_name"],
            "路径": row["rel_path"],
            "类型": row["mime_type"],
            "大小": f"{int(row.get('size_bytes') or 0):,} B",
            "摘要": row.get("summary") or "尚无摘要",
            "标签": "、".join(row.get("tags") or []),
        } for row in rows]
        st.dataframe(display, use_container_width=True, hide_index=True)
        selected_file = st.selectbox(
            "为文件生成属性摘要",
            options=[row["file_id"] for row in rows],
            format_func=lambda file_id: next((f"{row['file_name']} · {file_id}" for row in rows if row["file_id"] == file_id), file_id),
        )
        if st.button("生成 / 更新摘要", key="summarize_catalog_file"):
            try:
                st.success(summarize_user_file(selected_file))
                st.rerun()
            except Exception as exc:
                st.error(f"摘要生成失败：{exc}")
        selected_row = next((row for row in rows if row["file_id"] == selected_file), {})
        current_tags = st.text_input(
            "文件标签（逗号分隔）",
            value=", ".join(selected_row.get("tags") or []),
            key=f"catalog_tags_{selected_file}",
        )
        if st.button("保存标签", key="save_catalog_tags"):
            try:
                result = set_user_file_tags(selected_file, [tag.strip() for tag in current_tags.split(",") if tag.strip()])
                st.success(result)
                st.rerun()
            except Exception as exc:
                st.error(f"标签保存失败：{exc}")
    else:
        st.info("data/users 中还没有文件。把简历、笔记或其他个人文件放进去后，点击“同步目录”。")
    try:
        links = list_task_resource_links()
    except (OSError, ValueError, RuntimeError) as exc:
        st.error(f"任务文件引用无法读取：{exc}")
        links = []
    if links:
        st.subheader("任务文件引用")
        st.dataframe([{
            "task_id": row["task_id"],
            "任务": row["task_name"],
            "类型": row["task_type"],
            "file_id": row["file_id"],
            "文件": row["file_name"],
            "路径": row["rel_path"],
            "用途": row["purpose"],
            "状态": row["status"],
        } for row in links], use_container_width=True, hide_index=True)
    st.caption("一个文件可以关联到多个任务或日程；删除任务只会删除引用，不会删除文件。")
    with st.expander("Agent 生成的文档", expanded=False):
        output_dir = PathManager.DOCUMENT_OUTPUT_DIR
        generated = sorted(
            (path for path in output_dir.iterdir() if path.is_file() and not path.is_symlink()
             and path.suffix.lower() in {".txt", ".md", ".docx", ".pdf", ".xlsx"}),
            key=lambda path: path.stat().st_mtime, reverse=True,
        )[:100] if output_dir.is_dir() else []
        if generated:
            st.caption("这里是 Agent 生成或修订的成品；data/users 中的原始文件不会被覆盖。")
            chosen_output = st.selectbox("选择成品", generated, format_func=lambda path: path.name)
            st.write(f"保存位置：{chosen_output}")
            if chosen_output.stat().st_size <= 20 * 1024 * 1024:
                metadata = chosen_output.stat()
                try:
                    data = generated_document_bytes(str(chosen_output), metadata.st_mtime_ns, metadata.st_size)
                    st.download_button("下载成品", data=data, file_name=chosen_output.name,
                                       key="download_generated_document")
                except (OSError, ValueError) as exc:
                    st.error(f"读取成品失败：{exc}")
            else:
                st.caption("文件超过 20 MiB，请到上方保存位置手动获取。")
        else:
            st.caption("尚无生成的文档。")


elif nav_choice == "运行日志":
    st.title("运行日志")
    st.markdown('<div class="page-kicker">查看 Agent、工具和日程的运行事件。执行凭证继续用于系统内部核验，不再占用主导航。</div>', unsafe_allow_html=True)
    try:
        events = list(reversed(load_log_events()))
    except RuntimeError as exc:
        st.error(str(exc))
        st.stop()
    c1, c2, c3 = st.columns([3,1.3,1.2])
    search = c1.text_input("搜索日志", placeholder="搜索模块或内容")
    level_filter = c2.selectbox("级别", ["全部","INFO","SUCCESS","WARN","ERROR","CMD"])
    limit = c3.selectbox("显示数量", [20,50,100])
    filtered = []
    for item in events:
        level = str(item.get("level","INFO")).upper()
        text = f"{item.get('module','')} {item.get('message','')}".lower()
        if level_filter != "全部" and level != level_filter:
            continue
        if search.strip() and search.strip().lower() not in text:
            continue
        filtered.append(item)
    filtered = filtered[:limit]
    st.caption(f"显示 {len(filtered)} 条 · 本地保留最近 100 条事件")
    if not filtered:
        st.info("没有符合条件的运行日志。")
    for item in filtered:
        with st.container(border=True):
            meta, level_col = st.columns([5,1])
            meta.markdown(f"**{item.get('module','SYSTEM')}**　<span class='event-meta'>{item.get('timestamp','')}</span>", unsafe_allow_html=True)
            level_col.caption(str(item.get("level","INFO")).upper())
            st.write(str(item.get("message","")))


elif nav_choice == "设置":
    st.title("设置")
    st.markdown('<div class="page-kicker">模型接口、工具权限、记忆和行为规则都在这里管理。</div>', unsafe_allow_html=True)
    model_tab, access_tab, memory_tab, recruit_tab, prompt_tab, browser_tab, task_tab = st.tabs(
        ["模型与 API", "工具权限", "记忆", "求职偏好", "行为规则", "浏览器连接", "任务管理"]
    )
    with model_tab:
        st.subheader("模型与 API")
        st.caption("模型名称和 API 地址会保存在本地 Agent 设置中。API Key 不写入设置文件；输入框中的新 Key 仅在当前运行进程生效，重启后仍从环境变量读取。")
        st.caption(f"当前 API Key：{'已配置' if settings.SILICONFLOW_API_KEY else '未配置'}（不会显示密钥）")
        busy = jobs.is_busy()
        if busy:
            st.info("有任务正在运行，完成后才能切换接口。")
        with st.form("llm_config_form"):
            model = st.text_input("模型名称", value=load_runtime_settings()["model_name"])
            api_base = st.text_input("API 地址", value=load_runtime_settings()["api_base"])
            api_key = st.text_input("API Key（留空沿用当前密钥）", type="password")
            applied = st.form_submit_button("应用接口设置", type="primary", use_container_width=True, disabled=busy)
        if applied:
            if jobs.is_busy():
                st.error("任务正在运行，请完成后再切换接口。")
            elif not model.strip() or not api_base.strip():
                st.error("模型名称和 API 地址不能为空。")
            else:
                try:
                    from src.core.agent.agent_engine import configure_llm_settings
                    configure_llm_settings(model.strip(), api_base.strip(), api_key or settings.SILICONFLOW_API_KEY)
                    st.success("模型和 API 地址已保存；设置已应用到当前运行进程。")
                    st.rerun()
                except Exception as exc:
                    st.error(f"接口设置未能应用：{exc}")
    with access_tab:
        st.subheader("工具启停与访问权限")
        st.caption("关闭的工具不会提供给之后的新任务。正在执行任务时暂不允许修改。")
        runtime = load_runtime_settings()
        permissions = runtime["tool_permissions"]
        with st.form("tool_permissions_form"):
            browser_enabled = st.checkbox("网页浏览器操作", value=permissions.get("browser", True))
            email_enabled = st.checkbox("QQ 邮箱收件与写信（发送需审批）", value=permissions.get("email", True))
            desktop_chat_enabled = st.checkbox(
                "QQ / 微信桌面聊天", value=permissions.get("desktop_chat", True),
                help="需要桌面软件已登录且窗口可见。发送只允许来自当前对话的明确指令，不能由日程自动发送。",
            )
            documents_enabled = st.checkbox(
                "本地文档制作与修改", value=permissions.get("documents", True),
                help="生成的文档保存在 Agent 成品目录；修改用户文件时另存新版本，不覆盖原件。",
            )
            desktop_enabled = st.checkbox(
                "通用电脑操作", value=permissions.get("desktop", True),
                help="查看窗口、切换应用、操作普通控件、剪贴板和截图。聊天与浏览器继续使用专用工具。",
            )
            files_enabled = st.checkbox("读取和管理我的文件", value=permissions.get("user_files", True))
            schedules_enabled = st.checkbox("创建和管理日程", value=permissions.get("schedules", True))
            cards_enabled = st.checkbox("创建和管理决策卡片", value=permissions.get("decision_cards", True))
            recruitment_enabled = st.checkbox("招聘岗位整理能力", value=permissions.get("recruitment", True))
            knowledge_enabled = st.checkbox("个人知识库检索", value=permissions.get("knowledge_search", True))
            memory_enabled = st.checkbox("读取和搜索结构化记忆", value=permissions.get("memory", True))
            save_permissions = st.form_submit_button("保存权限", type="primary", disabled=jobs.is_busy())
        if save_permissions:
            if jobs.is_busy():
                st.error("任务正在运行，请完成后再修改权限。")
            else:
                try:
                    save_runtime_settings({"tool_permissions": {
                        "browser": browser_enabled, "user_files": files_enabled,
                        "email": email_enabled,
                        "desktop_chat": desktop_chat_enabled,
                        "documents": documents_enabled, "desktop": desktop_enabled,
                        "schedules": schedules_enabled, "decision_cards": cards_enabled,
                        "recruitment": recruitment_enabled, "knowledge_search": knowledge_enabled,
                        "memory": memory_enabled,
                    }})
                    st.success("权限已保存，将应用于之后提交的任务。")
                    st.rerun()
                except (OSError, ValueError) as exc:
                    st.error(f"保存权限失败：{exc}")
    with memory_tab:
        st.subheader("结构化记忆")
        runtime = load_runtime_settings()
        with st.form("memory_capture_form"):
            capture_enabled = st.checkbox(
                "仅在我明确表达记忆意图时，自动整理稳定偏好或约定",
                value=runtime.get("memory_capture_enabled", True),
            )
            save_capture = st.form_submit_button("保存记忆设置")
        if save_capture:
            save_runtime_settings({"memory_capture_enabled": capture_enabled})
            st.success("记忆整理设置已保存。")
        st.caption("记忆可查看和删除。自动提取只分析用户明确表达的内容，不用 Agent 回复推断事实。")
        memory_manager = MemoryManager()
        with st.form("add_memory_form"):
            memory_type = st.selectbox("记忆类型", ["profile", "episodic"], format_func=lambda x: "稳定偏好 / 个人背景" if x == "profile" else "现实约定 / 重要事件")
            memory_content = st.text_area("添加一条记忆", placeholder="例如：我更喜欢简洁的中文回复。")
            add_memory_clicked = st.form_submit_button("保存记忆")
        if add_memory_clicked:
            try:
                memory_manager.add_memory(memory_content, memory_type)
                st.success("记忆已保存。")
                st.rerun()
            except Exception as exc:
                st.error(f"保存失败：{exc}")
        memories = memory_manager.list_memories()
        if not memories:
            st.info("目前没有结构化记忆。")
        for memory in memories:
            label = {"profile":"稳定偏好 / 背景", "episodic":"约定 / 事件", "active":"短期状态"}.get(memory["type"], memory["type"])
            left, right = st.columns([8, 1.2])
            left.markdown(f"**{label}**　{memory['content']}")
            left.caption(f"ID: {memory['id']} · {memory.get('created_at') or memory.get('updated_at') or ''}")
            if right.button("删除", key=f"delete_memory_{memory['id']}"):
                memory_manager.delete_memory(memory["id"])
                st.rerun()
    with recruit_tab:
        st.subheader("求职偏好与面试时间")
        st.caption("只有明确保存的取舍才可用于 HR 自动回复。未设置的取舍会先发决策卡；自动回复仍需单独批准日程范围。")
        runtime = load_runtime_settings()
        prefs = runtime["recruitment_preferences"]
        window = runtime["interview_window"]
        choice_labels = {"ask": "先问我", "yes": "可以接受", "no": "不接受"}
        with st.form("recruitment_preferences_form"):
            travel_choice = st.selectbox("出差 / 外派", list(choice_labels), index=list(choice_labels).index(prefs["travel"]), format_func=choice_labels.get)
            six_day_choice = st.selectbox("单休 / 做六休一", list(choice_labels), index=list(choice_labels).index(prefs["six_day_week"]), format_func=choice_labels.get)
            relocation_choice = st.selectbox("异地工作 / 搬迁", list(choice_labels), index=list(choice_labels).index(prefs["relocation"]), format_func=choice_labels.get)
            enable_window = st.checkbox("允许 Agent 在这段时间内向 HR 提议面试", value=bool(window["start"]))
            start_value = datetime.strptime(window["start"] or "09:00", "%H:%M").time()
            end_value = datetime.strptime(window["end"] or "18:00", "%H:%M").time()
            window_start = st.time_input("最早可约时间", value=start_value)
            window_end = st.time_input("最晚结束时间", value=end_value)
            save_recruitment = st.form_submit_button("保存求职偏好", type="primary")
        if save_recruitment:
            try:
                save_runtime_settings({
                    "recruitment_preferences": {"travel": travel_choice,
                                                "six_day_week": six_day_choice,
                                                "relocation": relocation_choice},
                    "interview_window": ({"start": window_start.strftime("%H:%M"),
                                          "end": window_end.strftime("%H:%M")} if enable_window
                                         else {"start": "", "end": ""}),
                })
                st.success("求职偏好已保存。")
                st.rerun()
            except (OSError, ValueError) as exc:
                st.error(f"保存失败：{exc}")
    with prompt_tab:
        st.subheader("Agent 提示词")
        st.caption("修改后影响之后提交的新任务。")
        if "system_prompt_draft" not in st.session_state:
            st.session_state.system_prompt_draft = load_system_prompt()
        with st.form("system_prompt_form"):
            prompt = st.text_area("行为规则", value=st.session_state.system_prompt_draft, height=260, label_visibility="collapsed")
            save_prompt = st.form_submit_button("保存提示词", use_container_width=True)
        if save_prompt:
            if save_system_prompt(prompt):
                st.session_state.system_prompt_draft = prompt
                st.success("提示词已保存。")
            else:
                st.error("保存失败，请检查本地数据目录权限。")
    with browser_tab:
        st.subheader("浏览器连接")
        if st.button("重新检查连接", key="refresh_webbridge_status"):
            webbridge_connection_status.clear()
            check_local_tcp_port.clear()
            st.rerun()
        bridge = webbridge_connection_status()
        expected_address = f"{settings.KIMI_WEBBRIDGE_HOST}:{settings.KIMI_WEBBRIDGE_PORT}"
        st.caption(f"Agent 使用的本地地址：{expected_address}")
        if bridge["source"] == "daemon":
            if not bridge["running"]:
                st.warning("Kimi 本地服务未启动。只打开 Edge 或扩展，Agent 仍无法连接网页。")
                if os.name == "nt":
                    st.code('& "$env:USERPROFILE\\.kimi-webbridge\\bin\\kimi-webbridge.exe" start', language="powershell")
                else:
                    st.code("~/.kimi-webbridge/bin/kimi-webbridge start", language="bash")
            elif bridge["port"] and int(bridge["port"]) != settings.KIMI_WEBBRIDGE_PORT:
                st.error(f"Kimi 本地服务运行在 {bridge['port']} 端口，但 Agent 正在连接 {expected_address}。请统一端口配置。")
            elif bridge["extension_connected"]:
                st.success("Kimi 本地服务已运行，Edge 扩展已连接。")
            else:
                st.warning("Kimi 本地服务已运行，但 Edge 扩展尚未连接。请打开 Edge 中的 Kimi 扩展并检查其本地连接设置。")
        elif bridge["running"]:
            st.info("本地服务端口可以连接；当前无法确认 Edge 扩展是否也已连接。")
        else:
            st.warning("无法连接 Kimi 本地服务。请确认服务正在运行，且地址与 Agent 设置一致。")
        st.caption("网页的登录状态由 Edge 保存；只有本地服务和扩展都连接时，Agent 才能操作网页。")
    with task_tab:
        st.subheader("任务管理")
        st.caption("这里保留尚在执行或需要处理的任务；普通对话记录仍在对话中心。")
        statuses = {"queued": "排队中", "running": "执行中", "failed": "失败", "interrupted": "已中断"}
        actionable = [item for item in jobs.get_tasks(limit=50) if item.get("status") in statuses]
        if not actionable:
            st.info("目前没有需要处理的任务。")
        for task in actionable[:15]:
            task_id = str(task.get("task_id") or "")
            status = str(task.get("status") or "")
            with st.expander(f"{task.get('title') or '未命名任务'} · {statuses[status]}"):
                st.write(str(task.get("request_text") or "")[:1500])
                if task.get("error"):
                    st.caption(f"问题：{task['error']}")
                if status in ("queued", "running") and not task.get("cancel_requested"):
                    if st.button("取消任务", key=f"cancel_task_{task_id}"):
                        try:
                            st.info(cancel_task_from_ui(task_id))
                            st.rerun()
                        except Exception as exc:
                            st.error(f"取消失败：{exc}")
                elif status in ("failed", "interrupted"):
                    if st.button("核对进度并恢复", key=f"recover_task_{task_id}"):
                        try:
                            resume_interrupted_task(task_id)
                            st.success("已排入恢复流程。")
                            st.rerun()
                        except Exception as exc:
                            st.error(f"恢复失败：{exc}")
