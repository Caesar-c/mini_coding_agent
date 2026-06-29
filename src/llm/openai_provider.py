"""OpenAI LLM Provider Implementation."""

from typing import Any

import openai

from llm.interface import LLMProvider, MessageWrapper


class OpenAILLMProvider(LLMProvider):
    """LLM provider for OpenAI-compatible APIs.

    Supports both the modern ``openai.OpenAI`` client (v1.x) and the legacy
    ``openai.ChatCompletion`` module (v0.x) to maximise compatibility with
    different runtime environments.
    """

    def __init__(self, api_key: str = None, base_url: str = None):
        self.api_key = api_key or ""
        self.base_url = base_url
        # Configure legacy module-level attributes for v0.x clients
        openai.api_key = self.api_key
        if base_url and hasattr(openai, "api_base"):
            openai.api_base = base_url

    def chat_completion(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None = None,
        model: str = "gpt-3.5-turbo",
        max_tokens: int = 4096,
        temperature: float = 0.7,
        **kwargs,
    ) -> MessageWrapper:
        """Generate a chat completion using the OpenAI API.

        Args:
            messages: List of message dictionaries with ``role`` and ``content``.
            tools: Optional list of tool definitions (OpenAI function-calling schema).
            model: Model identifier.
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
            # Modern OpenAI SDK uses ``tools``; legacy uses ``functions``.
            # We keep both shapes for maximum compatibility.
            api_params["tools"] = tools

        api_params.update(kwargs)
        api_params = {k: v for k, v in api_params.items() if v is not None}

        try:
            if hasattr(openai, "OpenAI"):
                # v1.x client
                client = openai.OpenAI(api_key=self.api_key)
                if self.base_url:
                    client.base_url = self.base_url
                response = client.chat.completions.create(**api_params)
                message = response.choices[0].message
                message_data = {
                    "role": getattr(message, "role", "assistant"),
                    "content": getattr(message, "content", None),
                    "tool_calls": getattr(message, "tool_calls", []) or [],
                }
            else:
                # v0.x legacy client
                response = openai.ChatCompletion.create(**api_params)
                message = response.choices[0].message
                message_data = {
                    "role": getattr(message, "role", "assistant"),
                    "content": getattr(message, "content", None),
                    "tool_calls": getattr(message, "tool_calls", []) or [],
                }

            return MessageWrapper(message_data)
        except Exception as e:
            return MessageWrapper(
                {
                    "role": "assistant",
                    "content": f"Error calling OpenAI API: {e}",
                    "tool_calls": [],
                }
            )
