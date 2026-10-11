"""Structured, inspectable user memory for the personal Agent."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from llama_index.core import Settings

from src.core.system.path_manager import PathManager

logger = logging.getLogger(__name__)
MEMORY_DIR = PathManager.MEMORY_DIR
PROFILE_FILE = PathManager.USER_PROFILE_FILE
EPISODIC_FILE = PathManager.EPISODIC_MEMORY_FILE
ACTIVE_FILE = PathManager.ACTIVE_CONTEXT_FILE
_MEMORY_LOCK = threading.RLock()
_MEMORY_CUES = (
    "记住", "记下来", "记一下", "别忘了", "请记得", "以后请", "以后都",
    "我通常", "我习惯", "我喜欢", "我偏好", "我的长期",
)
_SECRET_PATTERN = re.compile(r"(?i)(api[_ -]?key|password|密码|密钥|token)\s*[:=：]\s*\S+")


class MemoryManager:
    """Profile, episodic and short-lived context storage with user controls."""

    def __init__(self):
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        self._init_storage()

    @staticmethod
    def _now() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    def _init_storage(self) -> None:
        with _MEMORY_LOCK:
            if not PROFILE_FILE.exists():
                self._save_json(PROFILE_FILE, {"user_name": "", "permanent_traits": [], "updated_at": self._now()})
            if not EPISODIC_FILE.exists():
                self._save_json(EPISODIC_FILE, [])
            if not ACTIVE_FILE.exists():
                self._save_json(ACTIVE_FILE, {"active_tasks": [], "recent_mood": "", "last_interaction": self._now()})

    def _load_json(self, file_path: Path, default: Any = None) -> Any:
        if default is None:
            default = [] if file_path == EPISODIC_FILE else {}
        try:
            return json.loads(file_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            logger.warning("Could not load memory file %s: %s", file_path.name, exc)
            return default

    def _save_json(self, file_path: Path, data: Any) -> None:
        with _MEMORY_LOCK:
            # A fallback displayed after a read error must never replace history.
            if file_path.exists():
                try:
                    previous = json.loads(file_path.read_text(encoding="utf-8"))
                    expected = list if file_path == EPISODIC_FILE else dict
                    if not isinstance(previous, expected):
                        raise ValueError("unexpected record format")
                    if file_path == PROFILE_FILE and not isinstance(previous.get("permanent_traits", []), list):
                        raise ValueError("invalid permanent_traits")
                except (ValueError, TypeError) as exc:
                    raise ValueError(f"记忆文件 {file_path.name} 格式异常，已保留原文件，请修复后重试。") from exc
            PathManager._atomic_json(file_path, data)

    @staticmethod
    def _profile_id(content: str) -> str:
        digest = hashlib.sha256(content.casefold().encode("utf-8")).hexdigest()[:12]
        return f"profile_{digest}"

    @staticmethod
    def _decay_stage(item: dict[str, Any]) -> int:
        try:
            return max(0, int(item.get("decay_stage", 0) or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _has_memory_cue(user_msg: str) -> bool:
        text = str(user_msg or "").casefold()
        return any(cue.casefold() in text for cue in _MEMORY_CUES)

    async def analyze_and_extract_memory(
        self, user_msg: str, assistant_reply: str = "", source: str = "manual"
    ) -> None:
        """Extract only when the user signals durable memory intent.

        Assistant text is intentionally excluded as a source of facts. Scheduled and
        approval-generated messages are excluded so operational prompts do not become
        personal memories.
        """
        if source != "manual" or not self._has_memory_cue(user_msg):
            return
        if _SECRET_PATTERN.search(str(user_msg or "")):
            logger.info("Skipped memory capture because the message appears to contain a secret.")
            return

        prompt = f"""从用户本条消息中提取用户明确希望长期记住的信息。不要从助手回复推断事实。
只保存用户明确表达的个人背景、稳定偏好、习惯，或用户要求记住的现实约定。
忽略临时测试、一次性操作指令、模型/API 密钥、密码、支付信息和其他秘密。
若没有合适内容，返回 {{"should_remember": false}}。
有内容时只返回 JSON：{{"should_remember":true,"memory_type":"profile 或 episodic","content":"简洁事实","importance":"low/medium/high"}}

用户消息：
{user_msg}
"""
        try:
            response = await Settings.llm.acomplete(prompt)
            text = str(response).strip()
            start, end = text.find("{"), text.rfind("}")
            if start < 0 or end < start:
                return
            result = json.loads(text[start:end + 1])
            if result.get("should_remember"):
                self._save_extracted_memory(result)
        except Exception:
            logger.exception("Memory extraction failed; the conversation itself is unaffected.")

    def _save_extracted_memory(self, result: dict[str, Any]) -> str | None:
        memory_type = str(result.get("memory_type") or "").strip().lower()
        content = " ".join(str(result.get("content") or "").split())
        if memory_type not in {"profile", "episodic", "active"} or not content or len(content) > 500:
            return None
        if _SECRET_PATTERN.search(content):
            return None

        with _MEMORY_LOCK:
            now = self._now()
            if memory_type == "profile":
                profile = self._load_json(PROFILE_FILE, {})
                if not isinstance(profile, dict):
                    profile = {}
                facts = profile.get("permanent_traits", [])
                if not isinstance(facts, list):
                    facts = []
                existing = next((str(x) for x in facts if str(x).casefold() == content.casefold()), None)
                if existing:
                    return self._profile_id(existing)
                facts.append(content)
                profile["permanent_traits"] = facts
                profile["updated_at"] = now
                self._save_json(PROFILE_FILE, profile)
                return self._profile_id(content)

            if memory_type == "episodic":
                episodic = self._load_json(EPISODIC_FILE, [])
                if not isinstance(episodic, list):
                    episodic = []
                duplicate = next(
                    (item for item in episodic if isinstance(item, dict)
                     and str(item.get("content", "")).casefold() == content.casefold()),
                    None,
                )
                if duplicate:
                    try:
                        duplicate["mention_count"] = max(1, int(duplicate.get("mention_count", 1))) + 1
                    except (TypeError, ValueError):
                        duplicate["mention_count"] = 2
                    duplicate["last_mentioned"] = now
                    duplicate["decay_stage"] = 0
                    duplicate["user_confirmed"] = True
                    self._save_json(EPISODIC_FILE, episodic)
                    return str(duplicate.get("id"))
                memory_id = f"mem_{uuid.uuid4().hex[:10]}"
                episodic.append({
                    "id": memory_id, "content": content, "created_at": now,
                    "last_mentioned": now, "mention_count": 1,
                    "importance": str(result.get("importance") or "medium"),
                    "decay_stage": 0, "is_permanent": False, "user_confirmed": True,
                })
                self._save_json(EPISODIC_FILE, episodic)
                return memory_id

            active = self._load_json(ACTIVE_FILE, {})
            if not isinstance(active, dict):
                active = {}
            active["recent_mood"] = content
            active["last_interaction"] = now
            active["user_confirmed"] = True
            self._save_json(ACTIVE_FILE, active)
            return "active_context"

    def add_memory(self, content: str, memory_type: str = "profile") -> str:
        content = " ".join(str(content or "").split())
        if not content:
            raise ValueError("记忆内容不能为空。")
        memory_id = self._save_extracted_memory({
            "memory_type": memory_type, "content": content, "importance": "high",
        })
        if not memory_id:
            raise ValueError("记忆类型无效，或内容包含不适合保存的信息。")
        return memory_id

    def list_memories(self) -> list[dict[str, Any]]:
        with _MEMORY_LOCK:
            profile = self._load_json(PROFILE_FILE, {})
            episodic = self._load_json(EPISODIC_FILE, [])
            active = self._load_json(ACTIVE_FILE, {})
        memories = []
        traits = profile.get("permanent_traits", []) if isinstance(profile, dict) else []
        for content in traits if isinstance(traits, list) else []:
            text = str(content)
            memories.append({"id": self._profile_id(text), "type": "profile", "content": text})
        for item in episodic if isinstance(episodic, list) else []:
            if isinstance(item, dict) and item.get("content"):
                memories.append({
                    "id": str(item.get("id") or ""), "type": "episodic",
                    "content": str(item["content"]), "created_at": item.get("created_at"),
                    "importance": item.get("importance", "medium"),
                })
        if isinstance(active, dict) and active.get("recent_mood"):
            memories.append({
                "id": "active_context", "type": "active",
                "content": str(active["recent_mood"]), "updated_at": active.get("last_interaction"),
            })
        return memories

    def delete_memory(self, memory_id: str) -> bool:
        memory_id = str(memory_id or "").strip()
        with _MEMORY_LOCK:
            profile = self._load_json(PROFILE_FILE, {})
            traits = profile.get("permanent_traits", []) if isinstance(profile, dict) else []
            if isinstance(traits, list):
                kept = [str(item) for item in traits if self._profile_id(str(item)) != memory_id]
                if len(kept) != len(traits):
                    profile["permanent_traits"] = kept
                    profile["updated_at"] = self._now()
                    self._save_json(PROFILE_FILE, profile)
                    return True

            episodic = self._load_json(EPISODIC_FILE, [])
            if isinstance(episodic, list):
                kept = [item for item in episodic if not isinstance(item, dict)
                        or str(item.get("id") or "") != memory_id]
                if len(kept) != len(episodic):
                    self._save_json(EPISODIC_FILE, kept)
                    return True

            if memory_id == "active_context":
                active = self._load_json(ACTIVE_FILE, {})
                if isinstance(active, dict) and active.get("recent_mood"):
                    active["recent_mood"] = ""
                    active["last_interaction"] = self._now()
                    self._save_json(ACTIVE_FILE, active)
                    return True
        return False

    def search_memories(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        terms = [term.casefold() for term in str(query or "").split() if term]
        memories = self.list_memories()
        if terms:
            memories = [item for item in memories if all(term in item["content"].casefold() for term in terms)]
        return memories[:max(1, min(int(limit), 20))]

    def get_memory_context_for_prompt(self) -> str:
        profile = self._load_json(PROFILE_FILE, {})
        episodic = self._load_json(EPISODIC_FILE, [])
        active = self._load_json(ACTIVE_FILE, {})
        traits = profile.get("permanent_traits", []) if isinstance(profile, dict) else []
        episodic = episodic if isinstance(episodic, list) else []
        traits = [str(item) for item in traits if isinstance(item, (str, int, float))] if isinstance(traits, list) else []
        valid = [
            item for item in episodic
            if isinstance(item, dict) and item.get("content")
            and (item.get("user_confirmed") or item.get("is_permanent"))
            and (self._decay_stage(item) < 2 or item.get("is_permanent"))
        ]
        valid.sort(key=lambda item: str(item.get("last_mentioned", "")), reverse=True)
        lines = ["【灰风结构化记忆】"]
        lines.append("• 用户画像与稳定偏好：" + ("；".join(map(str, traits[:20])) if traits else "暂无记录"))
        lines.append("• 近期约定与重要事件：" + ("；".join(str(item["content"]) for item in valid[:5]) if valid else "暂无记录"))
        if isinstance(active, dict) and active.get("recent_mood") and active.get("user_confirmed"):
            lines.append("• 用户近期状态：" + str(active["recent_mood"]))
        return "\n".join(lines)[:5000]


def search_user_memory(query: str) -> str:
    rows = MemoryManager().search_memories(query)
    if not rows:
        return "没有找到匹配的用户记忆。"
    return "\n".join(f"- {row['id']} [{row['type']}] {row['content']}" for row in rows)


def remember_user_memory(content: str, memory_type: str = "profile") -> str:
    memory_id = MemoryManager().add_memory(content, memory_type)
    return f"已保存一条用户记忆（{memory_type}，ID: {memory_id}）。"
