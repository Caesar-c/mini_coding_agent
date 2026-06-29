from typing import Dict, Callable, List, Tuple, Any
from .tools import ALL_TOOLS

class ToolRegistry:
    def __init__(self):
        self._tools: Dict[str, dict] = {}
        self._handlers: Dict[str, Callable] = {}

        # Register all existing tools
        for definition, handler in ALL_TOOLS:
            self.register(definition, handler)

    def register(self, definition: dict, handler: Callable):
        """Register a new tool with its handler function."""
        name = definition["function"]["name"]
        self._tools[name] = definition
        self._handlers[name] = handler

    @property
    def definitions(self) -> List[dict]:
        """Return all tool definitions for the LLM."""
        return list(self._tools.values())

    def execute(self, tool_name: str, args: dict) -> str:
        """Execute a tool with the given arguments."""
        if tool_name not in self._handlers:
            return f"Error: Unknown tool '{tool_name}'"
        try:
            return self._handlers[tool_name](args)
        except Exception as e:
            return f"Error executing tool {tool_name}: {str(e)}"

    def get_tool_names(self) -> List[str]:
        """Get a list of all registered tool names."""
        return list(self._tools.keys())