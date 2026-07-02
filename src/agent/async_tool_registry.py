"""Async tool registry — dispatches tool calls with sync/async handler support."""

import asyncio
from collections.abc import Callable

from agent.async_tools import ASYNC_ALL_TOOLS
from logger import get_logger

logger = get_logger(__name__)


class AsyncToolRegistry:
    """Registry that supports both async and sync tool handlers.

    When ``execute`` is called, it detects whether the handler returned
    a coroutine and awaits it if so. This allows sync handlers like
    ``run_update_plan`` to coexist with async handlers like ``async_run_bash``.
    """

    def __init__(self, exclude: list[str] | None = None):
        self._tools: dict[str, dict] = {}
        self._handlers: dict[str, Callable] = {}

        exclude_set = set(exclude or [])
        # Register all async tools, skipping excluded ones
        for definition, handler in ASYNC_ALL_TOOLS:
            name = definition["function"]["name"]
            if name not in exclude_set:
                self.register(definition, handler)

    def register(self, definition: dict, handler: Callable):
        """Register a tool with its handler function."""
        name = definition["function"]["name"]
        self._tools[name] = definition
        self._handlers[name] = handler

    @property
    def definitions(self) -> list[dict]:
        """Return all tool definitions for the LLM."""
        return list(self._tools.values())

    async def execute(self, tool_name: str, args: dict) -> str:
        """Execute a tool, awaiting async handlers transparently."""
        if tool_name not in self._handlers:
            logger.warning("Unknown tool: %s", tool_name)
            return f"Error: Unknown tool '{tool_name}'"
        try:
            handler = self._handlers[tool_name]
            result = handler(args)
            if asyncio.iscoroutine(result):
                result = await result
            logger.debug("Tool %s executed: result_len=%d", tool_name, len(result))
            return result
        except Exception as e:
            logger.error("Tool %s failed: %s", tool_name, e, exc_info=True)
            return f"Error executing tool {tool_name}: {e}"

    def get_tool_names(self) -> list[str]:
        """Get a list of all registered tool names."""
        return list(self._tools.keys())
