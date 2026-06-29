"""Abstract interface for LLM providers to enable pluggable LLM backends."""

from abc import ABC, abstractmethod
from typing import Any


class LLMProvider(ABC):
    """Abstract base class for LLM providers.

    All LLM provider implementations must subclass this and implement
    :meth:`chat_completion`. This guarantees a uniform contract so the
    :class:`agent.loop.Agent` can drive any provider interchangeably.
    """

    @abstractmethod
    def chat_completion(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None = None,
        model: str = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        **kwargs,
    ) -> Any:
        """Generate a chat completion.

        Args:
            messages: List of message dictionaries with ``role`` and ``content``.
            tools: Optional list of tool definitions (OpenAI function-calling
                schema). Providers that do not support tools may ignore this.
            model: Model identifier.
            max_tokens: Maximum number of tokens to generate.
            temperature: Sampling temperature.
            **kwargs: Additional provider-specific parameters.

        Returns:
            A response object (or :class:`MessageWrapper`) exposing at least
            ``content``, ``role`` and ``tool_calls`` attributes.
        """
        pass


class MessageWrapper:
    """Wrapper that provides a consistent message interface across providers.

    Different LLM providers return messages in different shapes. This wrapper
    normalises them so downstream code can access ``content``, ``role``,
    ``tool_calls`` and (optionally) ``reasoning_content`` uniformly.
    """

    def __init__(self, message_data: dict[str, Any]):
        self.data = message_data

    @property
    def content(self) -> str | None:
        return self.data.get("content", None)

    @property
    def role(self) -> str:
        return self.data.get("role", "assistant")

    @property
    def tool_calls(self) -> list:
        return self.data.get("tool_calls", [])

    @property
    def reasoning_content(self) -> str:
        """Reasoning / chain-of-thought content from reasoning-capable models.

        Empty string when the provider did not produce any reasoning output
        (e.g. non-reasoning models, or ``reasoning=False``).
        """
        return self.data.get("reasoning_content", "") or ""

    def model_dump(self, **kwargs):
        """Return the underlying data dict (OpenAI-compatible interface)."""
        return self.data
