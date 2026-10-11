import json
import os
import threading
from datetime import date

from src.core.system.path_manager import PathManager

STATS_DIR = PathManager.STATS_DIR
TOKEN_FILE = PathManager.TOKEN_FILE
_TOKEN_LOCK = threading.RLock()


def _default_stats() -> dict:
    return {"total_all_time": 0, "daily_records": {}}


def _write_stats(data: dict) -> None:
    PathManager._atomic_json(TOKEN_FILE, data)


def _validate_stats(data: dict) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get("daily_records", {}), dict):
        raise ValueError("Token 统计数据格式无效。")
    for record in [data, *data.get("daily_records", {}).values()]:
        if not isinstance(record, dict):
            raise ValueError("Token 日统计数据格式无效。")
        for metric in ("total_all_time", "prompt_tokens", "completion_tokens", "total_tokens", "api_calls"):
            if metric in record:
                if isinstance(record[metric], bool) or int(record[metric]) < 0:
                    raise ValueError("Token 统计数量无效。")
    return data


def _ensure_stats_file(*, strict: bool = False) -> dict:
    with _TOKEN_LOCK:
        if not TOKEN_FILE.exists():
            data = _default_stats()
            try:
                _write_stats(data)
            except OSError:
                pass
            return data
        try:
            data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
            return _validate_stats(data)
        except (OSError, ValueError, TypeError):
            if strict:
                raise ValueError("Token 统计文件无法读取，已保留原文件，未覆盖历史用量。")
            return _default_stats()


def record_tokens(prompt_tokens: int, completion_tokens: int) -> int:
    """Persist token usage reported by the configured LLM API."""
    prompt_tokens = max(0, int(prompt_tokens or 0))
    completion_tokens = max(0, int(completion_tokens or 0))
    with _TOKEN_LOCK:
        data = _ensure_stats_file(strict=True)
        today_str = date.today().isoformat()
        daily_records = data.setdefault("daily_records", {})
        daily = daily_records.setdefault(
            today_str,
            {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "api_calls": 0},
        )
        daily["prompt_tokens"] = int(daily.get("prompt_tokens", 0)) + prompt_tokens
        daily["completion_tokens"] = int(daily.get("completion_tokens", 0)) + completion_tokens
        daily["total_tokens"] = int(daily.get("total_tokens", 0)) + prompt_tokens + completion_tokens
        daily["api_calls"] = int(daily.get("api_calls", 0)) + 1
        data["total_all_time"] = int(data.get("total_all_time", 0)) + prompt_tokens + completion_tokens
        _write_stats(data)
        return daily["total_tokens"]


def get_today_stats() -> dict:
    data = _ensure_stats_file()
    daily = data.get("daily_records", {}).get(date.today().isoformat(), {})
    input_tokens = int(daily.get("prompt_tokens", 0) or 0)
    output_tokens = int(daily.get("completion_tokens", 0) or 0)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": int(daily.get("total_tokens", input_tokens + output_tokens) or 0),
        "api_calls": int(daily.get("api_calls", 0) or 0),
    }


def get_today_tokens() -> int:
    return get_today_stats()["total_tokens"]


def get_total_all_time_tokens() -> int:
    return int(_ensure_stats_file().get("total_all_time", 0) or 0)
