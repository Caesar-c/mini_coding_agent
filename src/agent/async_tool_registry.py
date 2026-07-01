"""Async tool registry — dispatches tool calls with sync/async handler support."""

import asyncio
from collections.abc import Callable

from agent.async_tools import ASYNC_ALL_TOOLS


class AsyncToolRegistry:
    """Registry that supports both async and sync tool handlers.

    When ``execute`` is called, it detects whether the handler returned
    a coroutine and awaits it if so. This allows sync handlers like
    ``run_update_plan`` to coexist with async handlers like ``async_run_bash``.
    """

    def __init__(self):
        self._tools: dict[str, dict] = {}
        self._handlers: dict[str, Callable] = {}

        # Register all async tools
        for definition, handler in ASYNC_ALL_TOOLS:
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
            return f"Error: Unknown tool '{tool_name}'"
        try:
            handler = self._handlers[tool_name]
            result = handler(args)
            if asyncio.iscoroutine(result):
                return await result
            return result
        except Exception as e:
            return f"Error executing tool {tool_name}: {e}"

    def get_tool_names(self) -> list[str]:
        """Get a list of all registered tool names."""
        return list(self._tools.keys())
