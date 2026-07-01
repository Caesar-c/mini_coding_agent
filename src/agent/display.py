"""Display handler protocol — decouples the agent loop from terminal rendering."""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class DisplayHandler(Protocol):
    """Protocol for rendering agent events to the user.

    Implementations can render to a rich terminal, stay silent for tests,
    or output to any other target. The agent loop only depends on this
    protocol, not on any specific rendering library.
    """

    def on_llm_start(self) -> None:
        """Called before an LLM API call (e.g. show spinner)."""
        ...

    def on_llm_end(self) -> None:
        """Called after an LLM API call completes (e.g. hide spinner)."""
        ...

    def on_tool_call(self, name: str, args: dict[str, Any], output: str) -> None:
        """Called after a tool executes (e.g. show result panel)."""
        ...

    def on_response(self, content: str) -> None:
        """Called with the final text response (e.g. render markdown)."""
        ...

    def on_progress(self, summary: str) -> None:
        """Called with a formatted progress summary string."""
        ...


class SilentDisplayHandler:
    """No-op display handler for testing and non-interactive use."""

    def on_llm_start(self) -> None:
        pass

    def on_llm_end(self) -> None:
        pass

    def on_tool_call(self, name: str, args: dict[str, Any], output: str) -> None:
        pass

    def on_response(self, content: str) -> None:
        pass

    def on_progress(self, summary: str) -> None:
        pass
