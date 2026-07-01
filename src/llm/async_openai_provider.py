"""Async OpenAI LLM Provider Implementation."""

from typing import Any

from openai import AsyncOpenAI

from llm.interface import AsyncLLMProvider, MessageWrapper


class AsyncOpenAILLMProvider(AsyncLLMProvider):
    """Async LLM provider for OpenAI-compatible APIs.

    Uses the ``AsyncOpenAI`` client for non-blocking API calls.
    Supports streaming and tool calls.
    """

    def __init__(self, api_key: str = None, base_url: str = None, model: str = None):
        if not api_key or not base_url:
            raise ValueError("API_KEY and BASE_URL must not be NULL.")
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.model = model

    async def chat_completion(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        stream: bool = False,
        reasoning: bool = False,
        reasoning_effort: str = "medium",
        **kwargs,
    ) -> MessageWrapper:
        """Generate an async chat completion using the OpenAI API."""
        api_params = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": stream,
        }

        if tools:
            api_params["tools"] = tools
        if reasoning:
            api_params["reasoning_effort"] = reasoning_effort

        api_params.update(kwargs)
        api_params = {k: v for k, v in api_params.items() if v is not None}

        try:
            response = await self.client.chat.completions.create(**api_params)

            if stream:
                content = ""
                reasoning_content = ""
                tool_calls: list[dict[str, Any]] = []

                async for chunk in response:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta

                    c = getattr(delta, "content", None)
                    if c:
                        content += c

                    r = getattr(delta, "reasoning_content", None)
                    if r:
                        reasoning_content += r

                    tcs = getattr(delta, "tool_calls", None)
                    if tcs:
                        for tc in tcs:
                            idx = getattr(tc, "index", len(tool_calls))
                            while len(tool_calls) <= idx:
                                tool_calls.append(
                                    {
                                        "id": None,
                                        "type": "function",
                                        "function": {"name": "", "arguments": ""},
                                    }
                                )
                            if getattr(tc, "id", None):
                                tool_calls[idx]["id"] = tc.id
                            fn = getattr(tc, "function", None)
                            if fn:
                                if getattr(fn, "name", None):
                                    tool_calls[idx]["function"]["name"] += fn.name
                                if getattr(fn, "arguments", None):
                                    tool_calls[idx]["function"]["arguments"] += fn.arguments

                message_data = {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": tool_calls,
                    "reasoning_content": reasoning_content,
                }
            else:
                message = response.choices[0].message
                message_data = {
                    "role": getattr(message, "role", "assistant"),
                    "content": getattr(message, "content", None),
                    "tool_calls": list(getattr(message, "tool_calls", None) or []),
                    "reasoning_content": getattr(message, "reasoning_content", None) or "",
                }

            return MessageWrapper(message_data)
        except Exception as e:
            return MessageWrapper(
                {
                    "role": "assistant",
                    "content": f"Error calling OpenAI API: {e}",
                    "tool_calls": [],
                    "reasoning_content": "",
                }
            )
