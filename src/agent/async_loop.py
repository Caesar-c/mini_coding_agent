"""Async agent loop with concurrent tool execution and display handler support."""

import asyncio
import json

from agent.async_tool_registry import AsyncToolRegistry
from agent.display import DisplayHandler, SilentDisplayHandler
from agent.loop import SYSTEM_PROMPT
from config import settings
from context_manager.context import ContextCompactor
from context_manager.tracker import (
    UPDATE_PLAN_TOOL_DEFINITION,
    ProgressTracker,
    run_update_plan,
)
from llm import LLMProviderType, create_async_llm_provider


class AsyncAgent:
    """Async version of the agent loop with concurrent tool execution.

    Key differences from the sync :class:`agent.loop.Agent`:
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
    ):
        self.llm_provider = create_async_llm_provider(
            llm_provider_type or LLMProviderType(settings.LLM_PROVIDER)
        )

        self.messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
        ]

        self.tool_registry = AsyncToolRegistry()
        self.progress_tracker = ProgressTracker()
        self.context_compactor = ContextCompactor(
            max_messages=settings.CONTEXT_MAX_MESSAGES,
            keep_recent=settings.CONTEXT_KEEP_RECENT,
        )
        self.display = display or SilentDisplayHandler()

        # Register the sync update_plan tool (AsyncToolRegistry handles both)
        self.tool_registry.register(
            UPDATE_PLAN_TOOL_DEFINITION,
            lambda args: run_update_plan(args, self.progress_tracker),
        )

    async def _call_llm(self):
        """Call LLM provider API asynchronously and return the assistant message."""
        self.display.on_llm_start()
        try:
            response = await self.llm_provider.chat_completion(
                messages=self.messages,
                tools=self.tool_registry.definitions,
            )
        finally:
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
        if self.progress_tracker.has_plan:
            summary = self.progress_tracker.format_summary()
            self.messages.insert(1, {"role": "system", "content": summary})
            self.display.on_progress(summary)

    async def _handle_tool_call(self, tool_call):
        """Execute a tool asynchronously and return a tool result message."""
        if hasattr(tool_call, "function"):
            args = json.loads(tool_call.function.arguments)
            tool_name = tool_call.function.name
        else:
            args = json.loads(tool_call.arguments)
            tool_name = tool_call.name

        output = await self.tool_registry.execute(tool_name, args)

        # Truncation
        max_output = settings.MAX_TOOL_OUTPUT
        if len(output) > max_output:
            output = output[:max_output] + f"\n... [truncated, {len(output)} chars total]"

        self.display.on_tool_call(tool_name, args, output)
        return {
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": output,
        }

    async def chat(self, user_input: str) -> str:
        """Run one turn of the async agent loop, returns the final text response."""
        self.messages.append({"role": "user", "content": user_input})

        while True:
            # Progress reinforcement
            self._inject_progress()

            # Context compaction
            if self.context_compactor.should_compact(self.messages):
                self.messages = self.context_compactor.compact(self.messages)

            message = await self._call_llm()

            # Append assistant message to history
            if hasattr(message, "content") and message.content:
                if hasattr(message, "model_dump"):
                    self.messages.append(message.model_dump(exclude_unset=True))
                else:
                    msg_dict = {
                        "role": getattr(message, "role", "assistant"),
                        "content": message.content,
                    }
                    if hasattr(message, "tool_calls") and message.tool_calls:
                        msg_dict["tool_calls"] = message.tool_calls
                    self.messages.append(msg_dict)

            # Extract tool calls
            if hasattr(message, "tool_calls"):
                tool_calls = message.tool_calls
            else:
                tool_calls = getattr(message, "data", {}).get("tool_calls", [])

            if not tool_calls:
                content = getattr(message, "content", "")
                if content:
                    self.display.on_response(content)
                    return content
                return ""

            # ★ Concurrent tool execution
            results = await asyncio.gather(*[self._handle_tool_call(tc) for tc in tool_calls])
            self.messages.extend(results)
