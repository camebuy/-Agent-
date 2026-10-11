"""Persistent, non-secret runtime configuration for the personal Agent."""
from __future__ import annotations

import json
import os
import threading
import re
from urllib.parse import urlsplit
from typing import Any

from config.settings import settings
from src.core.system.path_manager import PathManager


_LOCK = threading.RLock()
DEFAULT_TOOL_PERMISSIONS = {
    "browser": True,
    "email": True,
    "desktop_chat": True,
    "desktop": True,
    "documents": True,
    "user_files": True,
    "schedules": True,
    "decision_cards": True,
    "recruitment": True,
    "knowledge_search": True,
    "memory": True,
}

RECRUITMENT_PREFERENCE_KEYS = ("travel", "six_day_week", "relocation")


def _recruitment_preferences(value: Any) -> dict[str, str]:
    result = {key: "ask" for key in RECRUITMENT_PREFERENCE_KEYS}
    if isinstance(value, dict):
        for key in RECRUITMENT_PREFERENCE_KEYS:
            choice = str(value.get(key) or "ask").strip().lower()
            if choice in {"yes", "no", "ask"}:
                result[key] = choice
    return result


def _interview_window(value: Any) -> dict[str, str]:
    blank = {"start": "", "end": ""}
    if not isinstance(value, dict):
        return blank
    start, end = str(value.get("start") or "").strip(), str(value.get("end") or "").strip()
    if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", start) and re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", end) and start < end:
        return {"start": start, "end": end}
    return blank


def _defaults() -> dict[str, Any]:
    return {
        "model_name": settings.MODEL_NAME,
        "api_base": settings.SILICONFLOW_BASE_URL,
        "tool_permissions": dict(DEFAULT_TOOL_PERMISSIONS),
        "memory_capture_enabled": True,
        "recruitment_preferences": _recruitment_preferences({}),
        "interview_window": _interview_window({}),
    }


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    return default


def load_runtime_settings(*, strict: bool = False) -> dict[str, Any]:
    with _LOCK:
        data = _defaults()
        path = PathManager.RUNTIME_SETTINGS_FILE
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                data.update({key: stored[key] for key in ("model_name", "api_base", "memory_capture_enabled") if key in stored})
                permissions = stored.get("tool_permissions")
                data["recruitment_preferences"] = _recruitment_preferences(stored.get("recruitment_preferences"))
                data["interview_window"] = _interview_window(stored.get("interview_window"))
                if isinstance(permissions, dict):
                    data["tool_permissions"].update(
                        {key: _as_bool(permissions.get(key, value)) for key, value in DEFAULT_TOOL_PERMISSIONS.items()}
                    )
            elif strict:
                raise ValueError("设置文件格式异常，已保留原文件，请修复后重试。")
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError):
            if strict:
                raise
        data["model_name"] = str(data.get("model_name") or settings.MODEL_NAME).strip()
        data["api_base"] = str(data.get("api_base") or settings.SILICONFLOW_BASE_URL).strip()
        data["memory_capture_enabled"] = _as_bool(data.get("memory_capture_enabled", True))
        data["recruitment_preferences"] = _recruitment_preferences(data.get("recruitment_preferences"))
        data["interview_window"] = _interview_window(data.get("interview_window"))
        return data


def save_runtime_settings(updates: dict[str, Any]) -> dict[str, Any]:
    """Persist allow-listed settings; API keys are deliberately never written here."""
    with _LOCK:
        return _save_runtime_settings_locked(updates)


def _save_runtime_settings_locked(updates: dict[str, Any]) -> dict[str, Any]:
    current = load_runtime_settings(strict=True)
    if "model_name" in updates:
        model = str(updates["model_name"] or "").strip()
        if not model:
            raise ValueError("模型名称不能为空。")
        current["model_name"] = model
    if "api_base" in updates:
        api_base = str(updates["api_base"] or "").strip()
        parsed = urlsplit(api_base)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("API 地址必须是完整的 http:// 或 https:// 地址，且不能含有账号或密码。")
        current["api_base"] = api_base.rstrip("/")
    if "memory_capture_enabled" in updates:
        if not isinstance(updates["memory_capture_enabled"], bool):
            raise ValueError("记忆设置必须是开关值。")
        current["memory_capture_enabled"] = updates["memory_capture_enabled"]
    if "tool_permissions" in updates:
        supplied = updates["tool_permissions"]
        if not isinstance(supplied, dict):
            raise ValueError("工具权限设置格式无效。")
        if any(not isinstance(value, bool) for key, value in supplied.items() if key in DEFAULT_TOOL_PERMISSIONS):
            raise ValueError("工具权限必须是开关值。")
        current["tool_permissions"] = {
            key: bool(supplied.get(key, current["tool_permissions"].get(key, default)))
            for key, default in DEFAULT_TOOL_PERMISSIONS.items()
        }
    if "recruitment_preferences" in updates:
        supplied = updates["recruitment_preferences"]
        if not isinstance(supplied, dict) or any(key not in RECRUITMENT_PREFERENCE_KEYS for key in supplied):
            raise ValueError("求职偏好字段无效。")
        if any(value not in {"yes", "no", "ask"} for value in supplied.values()):
            raise ValueError("求职偏好只能设置为接受、不接受或询问我。")
        current["recruitment_preferences"] = {
            **current["recruitment_preferences"], **supplied,
        }
    if "interview_window" in updates:
        supplied = updates["interview_window"]
        if not isinstance(supplied, dict):
            raise ValueError("面试可约时段格式无效。")
        normalized = _interview_window(supplied)
        if normalized == {"start": "", "end": ""} and any(str(v or "").strip() for v in supplied.values()):
            raise ValueError("面试可约时段必须是同一天内的 HH:MM 起止时间，且结束晚于开始。")
        current["interview_window"] = normalized

    path = PathManager.RUNTIME_SETTINGS_FILE
    PathManager._atomic_json(path, current)
    return current
