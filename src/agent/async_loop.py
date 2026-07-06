"""Async agent loop with concurrent tool execution and display handler support."""

import asyncio
import time

from agent.async_tool_registry import AsyncToolRegistry
from agent.display import DisplayHandler, SilentDisplayHandler
from agent.message_utils import extract_tool_calls, parse_tool_call, response_to_dict
from agent.subagent import TASK_TOOL_DEFINITION, make_task_handler
from config import settings
from context_manager.pipeline import ContextPipeline
from context_manager.task_graph import ALL_TASK_GRAPH_TOOLS, TaskGraphManager
from llm import LLMProviderType, create_llm_provider
from logger import get_logger
from skills import LOAD_SKILL_TOOL_DEFINITION, build_system_prompt, make_load_skill_handler

logger = get_logger(__name__)

SYSTEM_PROMPT = """\
You are a helpful coding assistant. You can execute bash commands and use various \
file operation tools to accomplish tasks. Use the appropriate tools for file operations. \
Think step by step, and explain what you're doing before and after each command.

For multi-step tasks, use create_plan to define tasks and their dependencies. \
Mark tasks 'in_progress' when you start them and 'done' when finished using \
update_task. Tasks with all dependencies completed will automatically become \
'ready' — prioritize these. Use get_plan to check overall progress. You can \
add_task if you discover new work mid-execution. This keeps you on track for \
long tasks."""


class AsyncAgent:
    """Async agent loop with concurrent tool execution.

    Features:
    - ``chat()`` is ``async`` — uses ``await`` for LLM calls and tool execution
    - Multiple tool calls from a single LLM response are executed concurrently
      via ``asyncio.gather``
    - A :class:`DisplayHandler` is used for all output rendering (spinner,
      markdown, tool panels) instead of raw ``print()``
    """

    def __init__(
        self,
        llm_provider_type: LLMProviderType = None,
        display: DisplayHandler | None = None,
        session_id: str | None = None,
    ):
        self.llm_provider = create_llm_provider(
            llm_provider_type or LLMProviderType(settings.LLM_PROVIDER)
        )

        # --- Skill loading ---
        enhanced_prompt, self.skill_loader = build_system_prompt(SYSTEM_PROMPT)

        self.messages = [
            {"role": "system", "content": enhanced_prompt},
        ]

        self.tool_registry = AsyncToolRegistry()
        self.task_graph = TaskGraphManager(
            sandbox_root=settings.SANDBOX_ROOT, session_id=session_id
        )
        self.task_graph.load()  # Restore from disk if available
        self.context_pipeline = ContextPipeline(
            micro_max_chars=settings.CONTEXT_MICRO_MAX_CHARS,
            micro_keep_head_lines=settings.CONTEXT_MICRO_KEEP_HEAD_LINES,
            micro_keep_tail_lines=settings.CONTEXT_MICRO_KEEP_TAIL_LINES,
            meso_message_threshold=settings.CONTEXT_MESO_MESSAGE_THRESHOLD,
            meso_token_threshold=settings.CONTEXT_MESO_TOKEN_THRESHOLD,
            meso_use_llm=settings.CONTEXT_MESO_USE_LLM,
            macro_token_threshold=settings.CONTEXT_MACRO_TOKEN_THRESHOLD,
            keep_recent=settings.CONTEXT_KEEP_RECENT,
            llm_provider=self.llm_provider,
            task_graph=self.task_graph,
        )
        self.display = display or SilentDisplayHandler()

        # Register task graph tools — wrap sync handlers in asyncio.to_thread
        # to avoid blocking the event loop during disk I/O (save()).
        for definition, handler in ALL_TASK_GRAPH_TOOLS:
            self.tool_registry.register(
                definition,
                self._make_async_task_graph_handler(handler),
            )

        # --- Skill tool ---
        skill_handler = make_load_skill_handler(
            self.skill_loader, max_chars=settings.SKILL_MAX_CONTENT_CHARS
        )
        self.tool_registry.register(LOAD_SKILL_TOOL_DEFINITION, skill_handler)

        # --- Subagent support ---
        # Child registry: only base tools (no task, no task graph tools).
        # AsyncToolRegistry() auto-registers ASYNC_ALL_TOOLS (the 6 base tools).
        # task and task graph tools are manually registered to the main registry
        # only, so they are not in ASYNC_ALL_TOOLS. The exclude parameter is
        # used defensively to guard against future additions.
        self._child_registry = AsyncToolRegistry(
            exclude=["task", "create_plan", "update_task", "add_task", "get_plan"]
        )
        # Register load_skill to child registry (subagents can load skills too)
        self._child_registry.register(LOAD_SKILL_TOOL_DEFINITION, skill_handler)

        # Register task tool to main registry (parent Agent only)
        self.tool_registry.register(
            TASK_TOOL_DEFINITION,
            make_task_handler(self),
        )

    def _make_async_task_graph_handler(self, handler):
        """Wrap a sync task-graph handler so it runs in a worker thread.

        Prevents blocking the event loop during ``save()`` disk I/O.
        Returns an async closure compatible with :class:`AsyncToolRegistry`.
        """
        task_graph = self.task_graph

        async def _wrapper(args):
            return await asyncio.to_thread(handler, args, task_graph)

        return _wrapper

    async def _call_llm(self):
        """Call LLM provider API in a worker thread and return the assistant message."""
        logger.info("LLM call start: messages=%d", len(self.messages))
        self.display.on_llm_start()
        t0 = time.monotonic()
        try:
            response = await asyncio.to_thread(
                self.llm_provider.chat_completion,
                messages=self.messages,
                tools=self.tool_registry.definitions,
            )
        finally:
            elapsed = time.monotonic() - t0
            logger.info("LLM call end: %.2fs", elapsed)
            self.display.on_llm_end()
        return response

    def _inject_progress(self):
        """Remove old progress summary and inject current one as a system message."""
        self.messages = [
            m
            for m in self.messages
            if not (
                m.get("role") == "system"
                and isinstance(m.get("content"), str)
                and m["content"].startswith("[TASK PROGRESS]")
            )
        ]
        if self.task_graph.has_plan:
            summary = self.task_graph.format_summary()
            self.messages.insert(1, {"role": "system", "content": summary})
            self.display.on_progress(summary)

    async def _handle_tool_call(self, tool_call):
        """Execute a tool asynchronously and return a tool result message."""
        tool_name, args, tc_id = parse_tool_call(tool_call)

        logger.info("Tool call: %s, args=%s", tool_name, str(args)[:2000])

        output = await self.tool_registry.execute(tool_name, args)

        # Smart compression (replaces old dumb truncation)
        output = self.context_pipeline.compress_tool_result(tool_name, output)

        logger.info(
            "Tool result: %s, output_len=%d, output=%.2000s",
            tool_name,
            len(output),
            output,
        )
        self.display.on_tool_call(tool_name, args, output)
        return {
            "role": "tool",
            "tool_call_id": tc_id,
            "content": output,
        }

    async def chat(self, user_input: str) -> str:
        """Run one turn of the async agent loop, returns the final text response."""
        self.messages.append({"role": "user", "content": user_input})
        logger.info(
            "Chat turn start: user_input=%.2000s, messages=%d",
            user_input,
            len(self.messages),
        )

        iteration = 0
        while True:
            iteration += 1

            # Progress reinforcement
            self._inject_progress()

            # Context compaction
            if self.context_pipeline.should_compact(self.messages):
                before = len(self.messages)
                # compact() may call LLM (Layer 3), so offload to worker thread
                self.messages = await asyncio.to_thread(
                    self.context_pipeline.compact, self.messages
                )
                logger.info("Context compacted: %d -> %d messages", before, len(self.messages))

            message = await self._call_llm()

            # Extract tool_calls first — needed to decide whether to append.
            # OpenAI returns content=None alongside tool_calls; the message MUST
            # be appended even when content is falsy to avoid orphaned tool results.
            tool_calls = extract_tool_calls(message)
            content = getattr(message, "content", None)

            # Append assistant message when it carries content or tool_calls
            if content or tool_calls:
                self.messages.append(response_to_dict(message))

            if not tool_calls:
                logger.info(
                    "Chat turn end: iterations=%d, response=%.2000s",
                    iteration,
                    content or "",
                )
                if content:
                    self.display.on_response(content)
                    return content
                return ""

            # ★ Concurrent tool execution
            logger.info(
                "Executing %d tool call(s) concurrently in iteration %d", len(tool_calls), iteration
            )
            results = await asyncio.gather(*[self._handle_tool_call(tc) for tc in tool_calls])
            self.messages.extend(results)
