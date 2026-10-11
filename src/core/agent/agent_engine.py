"""Model setup, usage tracking and Agent tool assembly."""
import logging
import threading
from functools import wraps
from typing import Any, Callable, Dict, Iterable, List, Optional

from llama_index.core import Settings
from llama_index.core.callbacks import CallbackManager, CBEventType, EventPayload
from llama_index.core.callbacks.base_handler import BaseCallbackHandler
from llama_index.core.tools import BaseTool, FunctionTool
from llama_index.llms.openai_like import OpenAILike

from config.settings import settings
from config.prompts import load_system_prompt
from src.core.system.runtime_settings import load_runtime_settings, save_runtime_settings
from src.tools.executor import Executor, tool_has_side_effect
from src.tools.token_tracker import record_tokens

logger = logging.getLogger(__name__)
_llm_initialized = False
_callback_manager = None
_llm_lock = threading.RLock()


def _mapping(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    for method_name in ("model_dump", "dict", "to_dict"):
        method = getattr(value, method_name, None)
        if callable(method):
            try:
                result = method()
                if isinstance(result, dict):
                    return result
            except Exception:
                pass
    try:
        return dict(value)
    except (TypeError, ValueError):
        return {}


def _usage_count(usage: Dict[str, Any], keys: tuple) -> Optional[int]:
    for key in keys:
        if key in usage and usage[key] is not None:
            try:
                return max(0, int(usage[key]))
            except (TypeError, ValueError):
                return None
    return None


class ApiUsageCallbackHandler(BaseCallbackHandler):
    """Persist exact input/output counts included by the API provider."""

    def __init__(self) -> None:
        super().__init__(event_starts_to_ignore=[], event_ends_to_ignore=[])

    def on_event_start(self, event_type, payload=None, event_id="", parent_id="", **kwargs):
        return event_id

    def on_event_end(self, event_type, payload=None, event_id="", **kwargs):
        if event_type != CBEventType.LLM or not payload:
            return
        response = payload.get(EventPayload.RESPONSE) or payload.get(EventPayload.COMPLETION)
        if response is None:
            return

        additional = _mapping(getattr(response, "additional_kwargs", None))
        raw = _mapping(getattr(response, "raw", None))
        raw_usage = _mapping(raw.get("usage") or raw.get("usage_metadata"))
        usage = {**raw_usage, **additional}
        prompt = _usage_count(usage, ("prompt_tokens", "input_tokens", "prompt_token_count"))
        completion = _usage_count(usage, ("completion_tokens", "output_tokens", "completion_token_count"))
        if prompt is not None and completion is not None:
            try:
                record_tokens(prompt, completion)
            except Exception:
                logger.exception("Could not persist provider-reported token usage")

    def start_trace(self, trace_id=None):
        return None

    def end_trace(self, trace_id=None, trace_map=None):
        return None


def _ensure_callback_manager():
    global _callback_manager
    if _callback_manager is None:
        _callback_manager = CallbackManager([ApiUsageCallbackHandler()])
        Settings.callback_manager = _callback_manager
    return _callback_manager


def _build_llm(model=None, api_base=None, api_key=None):
    return OpenAILike(
        model=model if model is not None else settings.MODEL_NAME,
        api_base=api_base if api_base is not None else settings.SILICONFLOW_BASE_URL,
        api_key=api_key if api_key is not None else settings.SILICONFLOW_API_KEY,
        temperature=settings.LLM_TEMPERATURE,
        context_window=settings.CONTEXT_WINDOW,
        is_chat_model=True,
        is_function_calling_model=settings.FUNCTION_CALLING,
        timeout=settings.LLM_TIMEOUT,
        callback_manager=_ensure_callback_manager(),
    )


def init_llm_settings():
    """Initialize the shared LLM and attach API-reported usage tracking."""
    global _llm_initialized
    with _llm_lock:
        if _llm_initialized:
            return
        saved = load_runtime_settings()
        candidate = _build_llm(model=saved["model_name"], api_base=saved["api_base"])
        Settings.llm = candidate
        settings.MODEL_NAME = saved["model_name"]
        settings.SILICONFLOW_BASE_URL = saved["api_base"]
        _llm_initialized = True


def get_llm():
    """Capture one model instance for the entire task, including configuration changes."""
    with _llm_lock:
        init_llm_settings()
        return Settings.llm


def configure_llm_settings(model: str, api_base: str, api_key: str):
    """Apply an OpenAI-compatible endpoint to future calls in this process."""
    global _llm_initialized
    with _llm_lock:
        model, api_base = str(model).strip(), str(api_base).strip().rstrip("/")
        api_key = str(api_key or "").strip()
        # Build first: a constructor failure must not replace working settings.
        candidate = _build_llm(model=model, api_base=api_base, api_key=api_key)
        saved = save_runtime_settings({"model_name": model, "api_base": api_base})
        Settings.llm = candidate
        settings.MODEL_NAME = saved["model_name"]
        settings.SILICONFLOW_BASE_URL = saved["api_base"]
        settings.SILICONFLOW_API_KEY = api_key
        _llm_initialized = True
        return candidate


def _wrap_tool_with_executor(
    tool_name: str,
    fn: Callable,
    description: str,
    fn_schema: Any = None,
) -> FunctionTool:
    """
    将实际工具包装进 Executor 控制平面。

    Agent 只能调用这个包装后的 FunctionTool；真正的业务函数
    必须经过 Executor.execute()，从而生成 Receipt。
    """

    @wraps(fn)
    def executor_wrapped_fn(**kwargs):
        tool_result, _ = Executor.execute(
            tool_name=tool_name,
            func=fn,
            params=kwargs,
            has_side_effect=tool_has_side_effect(tool_name),
        )

        if not tool_result.success:
            raise RuntimeError(f"工具物理执行失败: {tool_result.error}")

        return tool_result.data

    executor_wrapped_fn._aacwork_executor_wrapped = True

    common_kwargs = {
        "fn": executor_wrapped_fn,
        "name": tool_name,
        "description": description or f"工具：{tool_name}",
    }

    if fn_schema is not None:
        return FunctionTool.from_defaults(**common_kwargs, fn_schema=fn_schema)

    return FunctionTool.from_defaults(**common_kwargs)


def _native_callable(tool: BaseTool) -> Callable:
    """Adapt a QueryEngineTool without capturing a later loop iteration."""
    def native_call(**kwargs):
        result = tool(**kwargs)
        if getattr(result, "is_error", False):
            raise RuntimeError(str(result))
        return str(result)
    return native_call


def _normalize_and_wrap_tools(tools: Iterable[Any]) -> List[BaseTool]:
    """只接收项目实际导出的 LlamaIndex 工具，并确保经过 Executor。"""
    if not tools:
        return []

    wrapped_tools: List[BaseTool] = []

    for tool in tools:
        if not isinstance(tool, BaseTool):
            raise TypeError(f"Agent 工具必须是 LlamaIndex BaseTool：{type(tool)!r}")
        # Project tools from exporter already have an Executor wrapper. Avoid duplicate side effects.
        if getattr(getattr(tool, "fn", None), "_aacwork_executor_wrapped", False):
            wrapped_tools.append(tool)
            continue
        metadata = getattr(tool, "metadata", None)
        name = getattr(metadata, "name", None) or getattr(tool, "name", None) or "custom_tool"
        description = getattr(metadata, "description", None) or getattr(tool, "description", None) or ""
        fn = getattr(tool, "fn", None) or getattr(tool, "_fn", None) or getattr(tool, "callable", None)
        if fn is None:
            # QueryEngineTool exposes __call__ rather than a FunctionTool.fn.
            fn = _native_callable(tool)
        wrapped_tools.append(
            _wrap_tool_with_executor(name, fn, description, getattr(metadata, "fn_schema", None))
        )

    return wrapped_tools


def create_native_tool_agent(tools, system_prompt: str = None, llm=None):
    """
    构建 LlamaIndex Workflow Agent。

    优先使用当前官方 Workflow API：
        - FunctionAgent：LLM 支持原生 function/tool calling 时
        - ReActAgent：否则使用 ReAct

    不再使用：
        ReActAgent.from_tools(...)
        FunctionCallingAgent
    因为你的当前 LlamaIndex 版本已经采用 Workflow Agent API。
    """
    prompt = system_prompt if system_prompt else load_system_prompt()
    llm = llm if llm is not None else get_llm()
    wrapped_tools = _normalize_and_wrap_tools(tools)

    if not wrapped_tools:
        raise ValueError("没有可供 Agent 使用的工具，拒绝创建空工具 Agent。")

    # 当前 LlamaIndex 官方 API：
    # from llama_index.core.agent.workflow import FunctionAgent / ReActAgent
    # 并直接实例化。FunctionAgent 要求底层 LLM 支持 function calling。
    try:
        from llama_index.core.agent.workflow import FunctionAgent, ReActAgent
    except ImportError:
        # 极老版本兼容：只有确认老式 from_tools 存在时才调用。
        try:
            from llama_index.core.agent import ReActAgent

            if hasattr(ReActAgent, "from_tools"):
                return ReActAgent.from_tools(
                    tools=wrapped_tools,
                    llm=llm,
                    verbose=True,
                    system_prompt=prompt,
                    max_iterations=settings.MAX_ITERATIONS,
                )
        except ImportError:
            pass

        raise RuntimeError(
            "当前环境中的 LlamaIndex Agent API 无法识别。"
            "请检查 llama-index-core 版本。"
        )
    if bool(getattr(llm.metadata, "is_function_calling_model", False)):
        return FunctionAgent(tools=wrapped_tools, llm=llm, system_prompt=prompt,
                             allow_parallel_tool_calls=False)
    return ReActAgent(tools=wrapped_tools, llm=llm, system_prompt=prompt, verbose=False)
