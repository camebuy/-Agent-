"""Context attached to a task while its Agent tools are being executed."""
from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any


_CURRENT_TASK: ContextVar[dict[str, Any] | None] = ContextVar("aacwork_current_task", default=None)


def set_current_task(context: dict[str, Any]) -> Token:
    # Async tasks and FunctionTool's executor copy contextvars to their workers.
    # A process-wide fallback incorrectly attaches scheduler/UI work to whichever
    # chat happens to be running in another thread.
    return _CURRENT_TASK.set(dict(context))


def reset_current_task(token: Token) -> None:
    _CURRENT_TASK.reset(token)


def get_current_task() -> dict[str, Any]:
    return dict(_CURRENT_TASK.get() or {})
