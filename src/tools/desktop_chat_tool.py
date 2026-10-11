"""Conservative Windows UI Automation tools for the *desktop* QQ and WeChat apps.

The tools inspect the running application's accessibility tree. They do not use
web pages, undocumented network protocols, coordinate scripts, or local chat
databases. When a client does not expose the needed controls, they stop rather
than guessing where to click or claiming that a message was received/sent.
"""

from __future__ import annotations

import ctypes
import json
import re
import sys
import threading
import time
from pathlib import PureWindowsPath
from typing import Any

from src.core.tasks.task_context import get_current_task


_LOCK = threading.RLock()
_APPS = {
    "qq": {"executables": {"qq.exe", "qqnt.exe"}, "label": "QQ"},
    "wechat": {"executables": {"wechat.exe", "weixin.exe"}, "label": "微信"},
}
_SEARCH_HINTS = ("搜索", "查找", "search")
_SEND_LABEL = re.compile(r"^(?:发送|send)(?:\s*\([^)]{1,3}\))?$", re.I)
_UNREAD_PATTERN = re.compile(r"(?:未读|unread|\d+\s*条新消息|\d+\s*new messages?)", re.I)


class DesktopChatError(RuntimeError):
    """An operation could not be confirmed from the visible desktop UI."""


def _platform(value: str) -> str:
    key = str(value or "").strip().lower()
    if key in {"微信", "weixin", "wechat"}:
        key = "wechat"
    if key not in _APPS:
        raise ValueError("platform 只能是 qq 或 wechat。")
    return key


def _desktop():
    if sys.platform != "win32":
        raise DesktopChatError("QQ/微信桌面工具只能在 Windows 的交互式桌面上运行。")
    try:
        from pywinauto import Desktop
    except ImportError as exc:
        raise DesktopChatError(
            "缺少 pywinauto。请在运行 app.py 的同一个 Python 环境安装 requirements.txt。"
        ) from exc
    return Desktop(backend="uia")


def _process_name(pid: int) -> str:
    """Resolve the executable behind a UIA window instead of trusting its title."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.QueryFullProcessImageNameW.argtypes = (
        ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_wchar),
        ctypes.POINTER(ctypes.c_uint32)
    )
    kernel.QueryFullProcessImageNameW.restype = ctypes.c_int
    kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
    handle = kernel.OpenProcess(0x1000, 0, int(pid))
    if not handle:
        return ""
    try:
        length = ctypes.c_uint32(32768)
        buffer = ctypes.create_unicode_buffer(length.value)
        if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
            return ""
        return PureWindowsPath(buffer.value).name.lower()
    finally:
        kernel.CloseHandle(handle)


def _windows(platform: str) -> list[Any]:
    allowed = _APPS[platform]["executables"]
    matches = []
    for window in _desktop().windows(visible_only=True):
        try:
            pid = window.element_info.process_id
            if _process_name(pid) in allowed and window.is_visible():
                matches.append(window)
        except (AttributeError, OSError, RuntimeError):
            continue
    return matches


def _window(platform: str, window_handle: int = 0):
    candidates = _windows(platform)
    if window_handle:
        candidates = [w for w in candidates if int(w.handle) == int(window_handle)]
    if len(candidates) != 1:
        raise DesktopChatError(
            f"需要唯一的{_APPS[platform]['label']}主窗口；当前匹配 {len(candidates)} 个。"
            "先打开并登录桌面软件，再用 desktop_chat_windows 检查窗口；可传 window_handle。"
        )
    return candidates[0]


def _name(control: Any) -> str:
    try:
        return str(control.element_info.name or "").strip()
    except (AttributeError, RuntimeError):
        return ""


def _kind(control: Any) -> str:
    try:
        return str(control.element_info.control_type or "")
    except (AttributeError, RuntimeError):
        return ""


def _visible(control: Any) -> bool:
    try:
        return bool(control.is_visible())
    except (AttributeError, RuntimeError):
        return False


def _rect(control: Any) -> tuple[int, int, int, int]:
    box = control.rectangle()
    return int(box.left), int(box.top), int(box.right), int(box.bottom)


def _controls(window: Any, limit: int = 450) -> list[Any]:
    """Bound tree traversal so a large chat history cannot hang an Agent turn."""
    found = []
    pending = [(window, 0)]
    while pending and len(found) < limit:
        control, depth = pending.pop(0)
        if control is not window and _visible(control):
            found.append(control)
        if depth < 8:
            try:
                pending.extend((child, depth + 1) for child in control.children())
            except (AttributeError, RuntimeError):
                continue
    return found


def _entry(control: Any) -> dict[str, Any]:
    info = control.element_info
    return {
        "type": _kind(control),
        "name": _name(control)[:180],
        "automation_id": str(getattr(info, "automation_id", "") or "")[:90],
        "rect": _rect(control),
    }


def desktop_chat_windows(platform: str) -> str:
    """List windows belonging to the native QQ or WeChat process; no login or launch."""
    platform = _platform(platform)
    with _LOCK:
        return json.dumps({
            "platform": platform,
            "windows": [{"handle": int(w.handle), "title": w.window_text()[:100]}
                        for w in _windows(platform)],
        }, ensure_ascii=False)


def desktop_chat_snapshot(platform: str, window_handle: int = 0) -> str:
    """Inspect accessible visible controls to diagnose a client-version mismatch."""
    platform = _platform(platform)
    with _LOCK:
        window = _window(platform, window_handle)
        controls = _controls(window)
        return json.dumps({
            "platform": platform, "window_handle": int(window.handle),
            "controls": [_entry(c) for c in controls if _name(c) or _kind(c) in {"Edit", "Button"}][:180],
            "truncated": len(controls) >= 450,
            "note": "这是可访问控件快照；没有出现的聊天内容不可据此推断。",
        }, ensure_ascii=False)


def _header_matches(window: Any, controls: list[Any], contact: str) -> bool:
    left, top, right, bottom = _rect(window)
    for control in controls:
        try:
            x, y, _, _ = _rect(control)
            if (_name(control) == contact and _kind(control) in {"Text", "Pane", "Window"} and
                    x >= left + (right - left) * 0.42 and
                    y <= top + (bottom - top) * 0.18):
                return True
        except (AttributeError, RuntimeError):
            continue
    return False


def _open_conversation(window: Any, contact: str) -> list[Any]:
    controls = _controls(window)
    if _header_matches(window, controls, contact):
        return controls
    edits = [c for c in controls if _kind(c) == "Edit" and
             any(hint in _name(c).lower() for hint in _SEARCH_HINTS)]
    if len(edits) != 1:
        raise DesktopChatError("未找到唯一的联系人搜索框；请在桌面软件手动打开目标会话后重试。")
    search = edits[0]
    try:
        search.set_edit_text(contact)
    except (AttributeError, RuntimeError) as exc:
        raise DesktopChatError("联系人搜索框不支持可靠的文本输入，未选择联系人。") from exc
    try:
        if search.get_value().strip() != contact:
            raise DesktopChatError("联系人搜索内容无法核对，未选择会话。")
    except AttributeError as exc:
        raise DesktopChatError("联系人搜索内容无法读取，未选择会话。") from exc
    left, top, right, bottom = _rect(window)
    candidates = []
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        controls = _controls(window)
        candidates = [c for c in controls if _name(c) == contact and
                      _rect(c)[0] < left + (right - left) * 0.55 and
                      _rect(c)[1] > top + (bottom - top) * 0.08]
        if len(candidates) == 1:
            break
        time.sleep(0.25)
    if len(candidates) != 1:
        raise DesktopChatError("搜索后未出现唯一的精确联系人；未打开会话。")
    candidates[0].click_input()
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        controls = _controls(window)
        if _header_matches(window, controls, contact):
            return controls
        time.sleep(0.25)
    raise DesktopChatError("已点击搜索结果，但无法核对会话标题；停止后续操作。")


def desktop_chat_open_conversation(platform: str, contact: str, window_handle: int = 0) -> str:
    """Open a unique exact contact, then verify the chat header before returning."""
    platform = _platform(platform)
    contact = str(contact or "").strip()
    if not contact or len(contact) > 100:
        raise ValueError("contact 必须是完整、精确的联系人或群名称（最多 100 字）。")
    with _LOCK:
        window = _window(platform, window_handle)
        _open_conversation(window, contact)
        return json.dumps({"status": "OPEN_VERIFIED", "platform": platform,
                           "contact": contact, "window_handle": int(window.handle)}, ensure_ascii=False)


def desktop_chat_read_visible(platform: str, contact: str, window_handle: int = 0) -> str:
    """Read accessible text in the currently selected conversation's message area."""
    platform = _platform(platform)
    contact = str(contact or "").strip()
    if not contact:
        raise ValueError("必须提供要核对的联系人或群名称。")
    with _LOCK:
        window = _window(platform, window_handle)
        controls = _open_conversation(window, contact)
        left, top, right, bottom = _rect(window)
        rows = []
        for control in controls:
            name = _name(control)
            x, y, _, _ = _rect(control)
            if (name and x >= left + (right - left) * 0.42 and
                    top + (bottom - top) * 0.20 < y < top + (bottom - top) * 0.82 and
                    _kind(control) in {"Text", "ListItem", "DataItem"}):
                rows.append({"type": _kind(control), "text": name[:500]})
        if not rows:
            return json.dumps({"status": "UNAVAILABLE", "platform": platform,
                               "contact": contact, "reason": "客户端未公开可读取的消息控件。"}, ensure_ascii=False)
        return json.dumps({"status": "VISIBLE_TEXT", "platform": platform,
                           "contact": contact, "items": rows[-40:],
                           "note": "仅为当前可见文本；未验证发送者、历史完整性或是否新消息。"}, ensure_ascii=False)


def desktop_chat_check_unread(platform: str, window_handle: int = 0) -> str:
    """Report only unread labels actually exposed by the native app's UIA tree."""
    platform = _platform(platform)
    with _LOCK:
        window = _window(platform, window_handle)
        left, top, right, bottom = _rect(window)
        labels = [
            _entry(c) for c in _controls(window) if
            _name(c) and _UNREAD_PATTERN.search(_name(c)) and
            _rect(c)[0] < left + (right - left) * 0.55
        ][:30]
        return json.dumps({
            "status": "UNREAD_INDICATORS_FOUND" if labels else "UNKNOWN",
            "platform": platform, "indicators": labels,
            "note": "没有可访问的未读标记不等于没有新消息；此工具不在后台监听通知。",
        }, ensure_ascii=False)


def desktop_chat_send_message(platform: str, contact: str, message: str,
                              window_handle: int = 0) -> str:
    """Send one explicit message to one verified conversation in a manual task.

    A missing control or ambiguous result is returned as UNKNOWN; callers must
    inspect the conversation and must never automatically retry that message.
    """
    platform = _platform(platform)
    contact = str(contact or "").strip()
    message = str(message or "").strip()
    if not contact or len(contact) > 100 or not message or len(message) > 2000:
        raise ValueError("需要精确联系人和 1–2000 字的非空消息。")
    context = get_current_task()
    if context.get("source") != "manual" or not context.get("task_id"):
        raise PermissionError("桌面聊天发送目前仅限用户当前对话直接提出的任务；日程和自动恢复不能代发。")
    request = str(context.get("user_request") or "")
    if contact not in request or not re.search(r"发送|发(?:个|条|一|给|消息|信)|回复|回(?:个|条|复|消息)|send|reply|message", request, re.I):
        raise PermissionError("当前用户指令未同时明确发送意图和准确联系人；未发送。")
    with _LOCK:
        window = _window(platform, window_handle)
        controls = _open_conversation(window, contact)
        left, top, right, bottom = _rect(window)
        editors = [c for c in controls if _kind(c) == "Edit" and
                   _rect(c)[0] >= left + (right - left) * 0.42 and
                   _rect(c)[1] >= top + (bottom - top) * 0.70]
        if len(editors) != 1:
            raise DesktopChatError("未找到唯一的消息输入框；未发送。")
        editor = editors[0]
        try:
            if editor.get_value().strip():
                raise DesktopChatError("消息输入框已有内容；未覆盖，未发送。")
            editor.set_edit_text(message)
            if editor.get_value() != message:
                raise DesktopChatError("填写内容无法核对；未点击发送。")
        except (AttributeError, RuntimeError) as exc:
            raise DesktopChatError("消息输入框不支持可靠填写或核对；请手动检查，未点击发送。") from exc
        controls = _controls(window)
        if not _header_matches(window, controls, contact):
            raise DesktopChatError("填写后会话标题已变化；未点击发送，请手动检查输入框。")
        def visible_message_count(items: list[Any]) -> int:
            return sum(
                1 for c in items if _name(c) == message and
                _kind(c) in {"Text", "ListItem", "DataItem"} and
                _rect(c)[0] >= left + (right - left) * 0.42 and
                _rect(c)[1] < top + (bottom - top) * 0.82
            )
        before_count = visible_message_count(controls)
        buttons = [c for c in controls if _kind(c) == "Button" and
                   _SEND_LABEL.fullmatch(_name(c)) and
                   _rect(c)[0] >= left + (right - left) * 0.42 and
                   _rect(c)[1] >= top + (bottom - top) * 0.70 and
                   c.is_enabled()]
        if len(buttons) != 1:
            raise DesktopChatError("未找到唯一、可用的发送按钮；消息留在输入框中，未点击发送。")
        try:
            buttons[0].invoke()  # Do not press Enter: clients may interpret it differently.
        except Exception as exc:
            raise DesktopChatError("发送动作结果不明，请检查会话，切勿自动重试。") from exc
        time.sleep(0.4)
        controls = _controls(window)
        visible = visible_message_count(controls) > before_count
        try:
            current_editors = [c for c in controls if _kind(c) == "Edit" and
                               _rect(c)[0] >= left + (right - left) * 0.42 and
                               _rect(c)[1] >= top + (bottom - top) * 0.70]
            emptied = len(current_editors) == 1 and not current_editors[0].get_value().strip()
        except (AttributeError, RuntimeError):
            emptied = False
        status = "SENT_VISIBLE" if visible and emptied and _header_matches(window, controls, contact) else "UNKNOWN"
        return json.dumps({
            "status": status, "platform": platform, "contact": contact,
            "message_length": len(message),
            "note": "仅确认客户端可见结果，不能保证对方已收到。" if status == "SENT_VISIBLE"
                    else "发送结果不明；请手动核对会话，绝不能自动重试。",
        }, ensure_ascii=False)
