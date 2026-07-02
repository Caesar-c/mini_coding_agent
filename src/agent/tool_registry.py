from collections.abc import Callable

from agent.tools import ALL_TOOLS
from logger import get_logger

logger = get_logger(__name__)


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, dict] = {}
        self._handlers: dict[str, Callable] = {}

        # Register all existing tools
        for definition, handler in ALL_TOOLS:
            self.register(definition, handler)

    def register(self, definition: dict, handler: Callable):
        """Register a new tool with its handler function."""
        name = definition["function"]["name"]
        self._tools[name] = definition
        self._handlers[name] = handler

    @property
    def definitions(self) -> list[dict]:
        """Return all tool definitions for the LLM."""
        return list(self._tools.values())

    def execute(self, tool_name: str, args: dict) -> str:
        """Execute a tool with the given arguments."""
        if tool_name not in self._handlers:
            logger.warning("Unknown tool: %s", tool_name)
            return f"Error: Unknown tool '{tool_name}'"
        try:
            result = self._handlers[tool_name](args)
            logger.debug("Tool %s executed: result_len=%d", tool_name, len(result))
            return result
        except Exception as e:
            logger.error("Tool %s failed: %s", tool_name, e, exc_info=True)
            return f"Error executing tool {tool_name}: {str(e)}"

    def get_tool_names(self) -> list[str]:
        """Get a list of all registered tool names."""
        return list(self._tools.keys())
