"""Zhipu AI (智谱) LLM Provider Implementation.

Uses the official ``zhipuai`` SDK when available, falling back to a graceful
error if the SDK is not installed.
"""

from typing import Any

from src.llm.interface import LLMProvider, MessageWrapper


class ZhipuAILLMProvider(LLMProvider):
    """LLM provider for the Zhipu AI (智谱清言) API.

    The provider lazily imports the ``zhipuai`` SDK so that it is only
    required at runtime when this provider is actually used.
    """

    def __init__(self, api_key: str = None):
        self.api_key = api_key or ""
        self._client = None

    def _get_client(self):
        """Lazy-load the Zhipu AI client."""
        if self._client is None:
            try:
                import zhipuai  # type: ignore
            except ImportError as exc:
                raise ImportError(
                    "The `zhipuai` package is required for ZhipuAILLMProvider. "
                    "Install it with: pip install zhipuai"
                ) from exc
            self._client = zhipuai.ZhipuAI(api_key=self.api_key)
        return self._client

    def chat_completion(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None = None,
        model: str = "glm-4",
        max_tokens: int = 8192,
        temperature: float = 0.95,
        **kwargs,
    ) -> MessageWrapper:
        """Generate a chat completion using the Zhipu AI API.

        Args:
            messages: List of message dictionaries with ``role`` and ``content``.
            tools: Optional list of tool definitions.
            model: Model identifier (default: ``glm-4``).
            max_tokens: Maximum number of tokens to generate.
            temperature: Sampling temperature.
            **kwargs: Additional parameters forwarded to the API.

        Returns:
            :class:`MessageWrapper` containing the provider response.
        """
        api_params = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        if tools:
            api_params["tools"] = tools

        api_params.update(kwargs)
        api_params = {k: v for k, v in api_params.items() if v is not None}

        try:
            client = self._get_client()
            response = client.chat.completions.create(**api_params)

            message = response.choices[0].message
            message_data = {
                "role": getattr(message, "role", "assistant"),
                "content": getattr(message, "content", ""),
                "tool_calls": list(getattr(message, "tool_calls", None) or []),
            }
            return MessageWrapper(message_data)
        except Exception as e:
            return MessageWrapper(
                {
                    "role": "assistant",
                    "content": f"Error calling Zhipu AI API: {e}",
                    "tool_calls": [],
                }
            )
