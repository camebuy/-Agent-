"""Bounded Windows desktop primitives for ordinary visible applications.

Chat clients and browsers have dedicated tools. Generic controls cannot be used
to bypass their send, upload, or application approval paths.
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path, PureWindowsPath
from typing import Any

from src.core.system.path_manager import PathManager
from src.core.tasks.task_context import get_current_task
from src.tools.database_tool import get_file_path


_LOCK = threading.RLock()
_SNAPSHOTS: dict[str, dict[str, Any]] = {}
_PROTECTED_EXES = {
    "qq.exe", "qqnt.exe", "wechat.exe", "weixin.exe",
    "msedge.exe", "chrome.exe", "firefox.exe", "brave.exe", "opera.exe",
    "cmd.exe", "powershell.exe", "pwsh.exe", "windowsterminal.exe",
    "regedit.exe", "mmc.exe", "msiexec.exe",
}
_BLOCKED_ACTION = re.compile(
    r"发送|投递|申请|支付|付款|转账|购买|删除|卸载|提交|发布|上传|"
    r"安装|执行|运行|确认|确定|允许|授权|退出|关闭|"
    r"send|submit|apply|pay|purchase|delete|uninstall|publish|upload|"
    r"install|execute|run|confirm|accept|allow|authorize|close|exit",
    re.I,
)
_FILE_SUFFIXES = {".txt", ".md", ".pdf", ".docx", ".xlsx", ".csv", ".png", ".jpg", ".jpeg"}
_APPS = {"notepad": "notepad.exe", "calculator": "calc.exe", "explorer": "explorer.exe"}


class DesktopToolError(RuntimeError):
    """A UI action cannot be verified from the current interactive desktop."""


def _desktop():
    if sys.platform != "win32":
        raise DesktopToolError("通用桌面工具只能在 Windows 交互式会话使用。")
    try:
        from pywinauto import Desktop
    except ImportError as exc:
        raise DesktopToolError("缺少 pywinauto，请在当前 Python 环境安装 requirements.txt。") from exc
    return Desktop(backend="uia")


def _process_name(pid: int) -> str:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.QueryFullProcessImageNameW.argtypes = (
        ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_wchar), ctypes.POINTER(ctypes.c_uint32)
    )
    kernel.QueryFullProcessImageNameW.restype = ctypes.c_int
    kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
    handle = kernel.OpenProcess(0x1000, 0, int(pid))
    if not handle:
        return ""
    try:
        size = ctypes.c_uint32(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return PureWindowsPath(buffer.value).name.lower()
        return ""
    finally:
        kernel.CloseHandle(handle)


def _window(handle: int):
    try:
        wanted = int(handle)
    except (TypeError, ValueError) as exc:
        raise ValueError("window_handle 必须来自 desktop_list_windows。") from exc
    for window in _desktop().windows(visible_only=True):
        try:
            if int(window.handle) == wanted and window.is_visible():
                return window
        except (AttributeError, RuntimeError):
            continue
    raise DesktopToolError("窗口已关闭或不可见；先重新列出窗口。")


def _window_process(window) -> str:
    try:
        return _process_name(int(window.element_info.process_id))
    except (AttributeError, RuntimeError):
        return ""


def _ordinary_window(window) -> str:
    executable = _window_process(window)
    if not executable or executable in _PROTECTED_EXES:
        raise PermissionError("此窗口须使用专用网页或聊天工具，或无法核实所属进程。")
    return executable


def _manual_action() -> None:
    context = get_current_task()
    if context.get("source") != "manual" or not context.get("task_id"):
        raise PermissionError("通用桌面控件操作只支持当前对话的手动任务，不由日程或恢复任务自动点击。")


def _kind(control) -> str:
    try:
        return str(control.element_info.control_type or "")
    except (AttributeError, RuntimeError):
        return ""


def _name(control) -> str:
    try:
        return str(control.element_info.name or "").strip()
    except (AttributeError, RuntimeError):
        return ""


def _rect(control) -> list[int]:
    rect = control.rectangle()
    return [int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)]


def _visible(control) -> bool:
    try:
        return bool(control.is_visible())
    except (AttributeError, RuntimeError):
        return False


def _controls(window) -> list:
    found = []
    pending = deque([(window, 0)])
    while pending and len(found) < 240:
        control, depth = pending.popleft()
        if control is not window and _visible(control):
            found.append(control)
        if depth < 6:
            try:
                pending.extend((child, depth + 1) for child in control.children())
            except (AttributeError, RuntimeError):
                pass
    return found


def desktop_list_windows() -> str:
    """List currently visible top-level windows and their process names."""
    with _LOCK:
        rows = []
        for window in _desktop().windows(visible_only=True)[:100]:
            try:
                rows.append({"handle": int(window.handle), "title": window.window_text()[:120],
                             "process": _window_process(window)})
            except (AttributeError, RuntimeError):
                continue
        return json.dumps({"windows": rows}, ensure_ascii=False)


def desktop_inspect_window(window_handle: int) -> str:
    """Return bounded accessible controls and short-lived references for one window."""
    with _LOCK:
        window = _window(window_handle)
        executable = _ordinary_window(window)
        now = time.monotonic()
        for old_id in list(_SNAPSHOTS):
            if now - _SNAPSHOTS[old_id]["created"] > 120:
                del _SNAPSHOTS[old_id]
        snapshot_id = uuid.uuid4().hex[:16]
        controls = {}
        rows = []
        for control in _controls(window):
            kind, name = _kind(control), _name(control)
            if not name and kind not in {"Edit", "Button", "ComboBox"}:
                continue
            ref = f"c{len(rows) + 1}"
            try:
                bounds = _rect(control)
                rows.append({"ref": ref, "type": kind, "name": name[:160], "rect": bounds})
                controls[ref] = {"control": control, "type": kind, "name": name, "rect": bounds}
            except (AttributeError, RuntimeError):
                continue
            if len(rows) >= 150:
                break
        _SNAPSHOTS[snapshot_id] = {"created": now, "handle": int(window.handle),
                                   "process": executable, "controls": controls}
        while len(_SNAPSHOTS) > 8:
            del _SNAPSHOTS[next(iter(_SNAPSHOTS))]
        return json.dumps({"snapshot_id": snapshot_id, "window_handle": int(window.handle),
                           "process": executable, "controls": rows,
                           "note": "引用两分钟内有效；控件不可访问或已变化时重新检查窗口。"},
                          ensure_ascii=False)


def _control(snapshot_id: str, control_ref: str):
    snapshot = _SNAPSHOTS.get(str(snapshot_id))
    if not snapshot or time.monotonic() - snapshot["created"] > 120:
        raise DesktopToolError("控件快照已过期，请重新检查窗口。")
    window = _window(snapshot["handle"])
    if _ordinary_window(window) != snapshot["process"]:
        raise DesktopToolError("窗口所属进程已变化，请重新检查。")
    entry = snapshot["controls"].get(str(control_ref))
    if not entry:
        raise DesktopToolError("控件引用不在当前快照中。")
    control = entry["control"]
    try:
        if (not control.is_visible() or not control.is_enabled()
                or _kind(control) != entry["type"] or _name(control) != entry["name"]
                or _rect(control) != entry["rect"]):
            raise DesktopToolError("控件位置或状态已变化，请重新检查窗口。")
    except (AttributeError, RuntimeError) as exc:
        raise DesktopToolError("控件已失效，请重新检查窗口。") from exc
    return control, entry


def desktop_focus_window(window_handle: int) -> str:
    """Bring a verified ordinary window to the foreground."""
    _manual_action()
    with _LOCK:
        window = _window(window_handle)
        _ordinary_window(window)
        try:
            window.set_focus()
        except Exception as exc:
            raise DesktopToolError("无法将该窗口切到前台。") from exc
        return json.dumps({"status": "FOCUSED", "window_handle": int(window.handle)}, ensure_ascii=False)


def desktop_invoke_control(snapshot_id: str, control_ref: str) -> str:
    """Invoke one observed low-risk button/menu/tab; never click guessed coordinates."""
    _manual_action()
    with _LOCK:
        control, entry = _control(snapshot_id, control_ref)
        if entry["type"] not in {"Button", "MenuItem", "TabItem", "ListItem"} or not entry["name"]:
            raise DesktopToolError("只支持快照中具名的按钮、菜单、标签或列表项。")
        if _BLOCKED_ACTION.search(entry["name"]):
            raise PermissionError("此控件可能发送、提交、支付或删除内容；通用桌面工具不会执行。")
        try:
            if entry["type"] in {"TabItem", "ListItem"}:
                control.select()
            else:
                control.invoke()
        except Exception as exc:
            raise DesktopToolError("控件未提供可核验的 Invoke 动作；未尝试坐标点击。") from exc
        return json.dumps({"status": "INVOKED", "control": entry["name"][:100]}, ensure_ascii=False)


def desktop_fill_control(snapshot_id: str, control_ref: str, text: str,
                         replace_existing: bool = False) -> str:
    """Fill one observed Edit field, verifying the resulting text."""
    _manual_action()
    text = str(text or "")
    if not text or len(text) > 5_000:
        raise ValueError("填写文字须为 1–5000 字。")
    with _LOCK:
        control, entry = _control(snapshot_id, control_ref)
        if entry["type"] != "Edit" or _BLOCKED_ACTION.search(entry["name"]):
            raise DesktopToolError("只能填写普通的可访问文本输入框。")
        try:
            try:
                is_password = bool(control.element_info.element.CurrentIsPassword)
            except Exception as exc:
                raise DesktopToolError("无法核实输入框是否为密码字段，未填写。") from exc
            if is_password:
                raise PermissionError("不读取或填写密码输入框。")
            previous = str(control.get_value() or "")
            if previous and not replace_existing:
                raise DesktopToolError("输入框已有内容；要替换时显式设置 replace_existing=true。")
            control.set_edit_text(text)
            if control.get_value() != text:
                raise DesktopToolError("填写结果无法核对，请查看当前窗口。")
        except (AttributeError, RuntimeError) as exc:
            raise DesktopToolError("此输入框不支持可靠的读取与填写。") from exc
        return json.dumps({"status": "FILLED_VISIBLE", "characters": len(text)}, ensure_ascii=False)


def desktop_launch_application(app: str) -> str:
    """Launch one of a small set of ordinary Windows utilities without a shell."""
    _manual_action()
    key = str(app or "").strip().lower()
    if key not in _APPS:
        raise ValueError("目前可启动的应用：notepad、calculator、explorer。其他已打开软件可用窗口工具操作。")
    windows = Path(os.environ.get("WINDIR", r"C:\Windows"))
    executable = windows / "System32" / _APPS[key]
    if not executable.is_file():
        raise FileNotFoundError(f"找不到 {key} 的 Windows 程序文件。")
    subprocess.Popen([str(executable)], close_fds=True)
    return json.dumps({"status": "LAUNCH_REQUESTED", "app": key,
                       "note": "已请求启动；请用 desktop_list_windows 确认窗口出现。"}, ensure_ascii=False)


def desktop_open_user_file(file_id: str) -> str:
    """Open one catalogued ordinary user document with its default Windows app."""
    _manual_action()
    _desktop()
    path = get_file_path(file_id)
    if path.suffix.lower() not in _FILE_SUFFIXES or path.is_symlink():
        raise PermissionError("只允许打开已登记的普通文档或图片，不执行脚本和程序。")
    os.startfile(str(path))
    return json.dumps({"status": "OPEN_REQUESTED", "file_id": file_id, "name": path.name,
                       "note": "已请求默认应用打开；不代表该应用已成功加载文件。"}, ensure_ascii=False)


def desktop_read_clipboard() -> str:
    """Read bounded Unicode text from the current Windows clipboard."""
    _manual_action()
    if not re.search(r"剪贴板|复制|粘贴|clipboard|copied|paste", str(get_current_task().get("user_request") or ""), re.I):
        raise PermissionError("当前指令没有要求读取剪贴板。")
    _desktop()
    import win32clipboard
    import win32con
    with _LOCK:
        win32clipboard.OpenClipboard()
        try:
            if not win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                return json.dumps({"status": "NO_TEXT"}, ensure_ascii=False)
            value = str(win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT) or "")
        finally:
            win32clipboard.CloseClipboard()
    return json.dumps({"status": "TEXT", "text": value[:10_000],
                       "truncated": len(value) > 10_000}, ensure_ascii=False)


def desktop_write_clipboard(text: str) -> str:
    """Put user-requested Unicode text on the clipboard; does not paste it."""
    _manual_action()
    if not re.search(r"剪贴板|复制|粘贴|clipboard|copy|paste", str(get_current_task().get("user_request") or ""), re.I):
        raise PermissionError("当前指令没有要求写入剪贴板。")
    _desktop()
    text = str(text or "")
    if not text or len(text) > 10_000:
        raise ValueError("剪贴板文字须为 1–10000 字。")
    import win32clipboard
    import win32con
    with _LOCK:
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
        finally:
            win32clipboard.CloseClipboard()
    return json.dumps({"status": "WRITTEN", "characters": len(text)}, ensure_ascii=False)


def desktop_capture_window(window_handle: int) -> str:
    """Save visible pixels of one window; overlapping windows may appear in the image."""
    _manual_action()
    if not re.search(r"截图|截屏|画面|screenshot|capture|screen", str(get_current_task().get("user_request") or ""), re.I):
        raise PermissionError("当前指令没有要求保存窗口截图。")
    with _LOCK:
        window = _window(window_handle)
        _ordinary_window(window)
        left, top, right, bottom = _rect(window)
        if right <= left or bottom <= top or (right - left) * (bottom - top) > 20_000_000:
            raise DesktopToolError("窗口截图范围无效或过大。")
        try:
            from PIL import ImageGrab
        except ImportError as exc:
            raise DesktopToolError("缺少 Pillow，无法截图。") from exc
        PathManager.DESKTOP_CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
        path = PathManager.DESKTOP_CAPTURE_DIR / f"window_{uuid.uuid4().hex[:12]}.png"
        ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True).save(path, format="PNG")
        return json.dumps({"status": "CAPTURED", "path": str(path),
                           "note": "保存的是屏幕可见像素；窗口被遮挡时截图也可能被遮挡。"},
                          ensure_ascii=False)
