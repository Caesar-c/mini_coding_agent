"""OpenAI LLM Provider Implementation."""

from typing import Any

import openai

from llm.interface import LLMProvider, MessageWrapper
from logger import get_logger

logger = get_logger(__name__)


class OpenAILLMProvider(LLMProvider):
    """LLM provider for OpenAI-compatible APIs.

    Uses the modern ``openai.AsyncOpenAI`` client (v1.x). The client is
    instantiated once and reused so the underlying httpx connection pool and
    retry/timeout configuration persist across calls.
    """

    def __init__(
        self,
        api_key: str = None,
        base_url: str | None = None,
        model: str = None,
        timeout: float = 60.0,
        max_retries: int = 2,
    ):
        if not api_key:
            raise ValueError("api_key must not be null.")
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        # Single shared async client — httpx connection pool is reused across
        # calls and the event loop binding stays consistent. base_url=None
        # makes the SDK use its default OpenAI endpoint.
        client_kwargs: dict[str, Any] = {
            "api_key": self.api_key,
            "timeout": timeout,
            "max_retries": max_retries,
        }
        if base_url:
            client_kwargs["base_url"] = base_url
        self._client = openai.AsyncOpenAI(**client_kwargs)

    async def chat_completion(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        stream: bool = True,
        **kwargs,
    ) -> MessageWrapper:
        """Generate a chat completion using the OpenAI API.

        Args:
            messages: List of message dictionaries with ``role`` and ``content``.
            tools: Optional list of tool definitions (OpenAI function-calling
                schema). When set, the model may reply with ``tool_calls``
                instead of plain text; the caller is responsible for executing
                the tools and feeding results back.
            max_tokens: Maximum number of tokens to generate.
            temperature: Sampling temperature.
            stream: If True (default), consume the response as a stream of
                chunks and silently accumulate ``content`` /
                ``reasoning_content`` / ``tool_calls`` into a single
                :class:`MessageWrapper`.
            **kwargs: Additional parameters forwarded to the API. To enable
                OpenAI o-series reasoning effort, pass
                ``reasoning_effort="low"|"medium"|"high"`` here explicitly —
                it is NOT auto-injected.

        Note:
            ``reasoning_content`` (the chain-of-thought field returned by some
            OpenAI-compatible providers such as DeepSeek) is accumulated
            unconditionally whenever chunks carry it; nothing needs to be
            enabled for that.

        Returns:
            :class:`MessageWrapper` containing the provider response. The
            wrapper's data always has keys ``role``, ``content``,
            ``tool_calls``; ``reasoning_content`` is also present.

        Raises:
            ``openai.APIError`` (or subclass) on failure. Errors are NOT
            swallowed into a fake assistant message — callers must handle or
            retry them.
        """
        api_params: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": stream,
        }

        if tools:
            api_params["tools"] = tools

        # `reasoning_effort` is a top-level OpenAI o-series param. It is only
        # forwarded when the caller passes it explicitly via kwargs — never
        # auto-injected, since it is model-specific and conflating it with
        # reasoning_content accumulation was a bug source.

        api_params.update(kwargs)
        api_params = {k: v for k, v in api_params.items() if v is not None}

        logger.info(
            "OpenAI chat_completion: model=%s, messages=%d, stream=%s, tools=%s",
            self.model,
            len(messages),
            stream,
            bool(tools),
        )

        if stream:
            # OpenAI v1.x streams usage only when explicitly requested; the
            # usage chunk arrives last, with an empty `choices` list.
            api_params["stream_options"] = api_params.get("stream_options", {"include_usage": True})
            return await self._stream_completion(api_params)
        return await self._non_stream_completion(api_params)

    async def _stream_completion(self, api_params: dict[str, Any]) -> MessageWrapper:
        content = ""
        reasoning_content = ""
        tool_calls: list[dict[str, Any]] = []
        usage = None

        response = await self._client.chat.completions.create(**api_params)
        async for chunk in response:
            # The final usage chunk has empty choices.
            chunk_usage = getattr(chunk, "usage", None)
            if chunk_usage is not None:
                usage = chunk_usage
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta

            # 累积正文
            c = getattr(delta, "content", None)
            if c:
                content += c

            # 累积深度思考内容（DeepSeek / 兼容协议），无条件收集
            r = getattr(delta, "reasoning_content", None)
            if r:
                reasoning_content += r

            # 收集 tool_calls（分块到达，按 index 合并）
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
                        # `name` is typically sent once on the first chunk for
                        # a given index — assign rather than append to avoid
                        # concatenating duplicates into e.g. "read_fileread_file".
                        name = getattr(fn, "name", None)
                        if name and not tool_calls[idx]["function"]["name"]:
                            tool_calls[idx]["function"]["name"] = name
                        args = getattr(fn, "arguments", None)
                        if args:
                            tool_calls[idx]["function"]["arguments"] += args

        message_data = {
            "role": "assistant",
            "content": content,
            "tool_calls": tool_calls,
            "reasoning_content": reasoning_content,
        }

        self._log_result(message_data, usage)
        return MessageWrapper(message_data)

    async def _non_stream_completion(self, api_params: dict[str, Any]) -> MessageWrapper:
        response = await self._client.chat.completions.create(**api_params)
        message = response.choices[0].message
        message_data = {
            "role": getattr(message, "role", "assistant"),
            "content": getattr(message, "content", None),
            "tool_calls": list(getattr(message, "tool_calls", None) or []),
            "reasoning_content": getattr(message, "reasoning_content", None) or "",
        }
        self._log_result(message_data, getattr(response, "usage", None))
        return MessageWrapper(message_data)

    @staticmethod
    def _log_result(message_data: dict[str, Any], usage: Any) -> None:
        logger.info(
            "OpenAI response: content_len=%d, tool_calls=%d",
            len(message_data.get("content") or ""),
            len(message_data.get("tool_calls") or []),
        )
        if usage is not None:
            logger.info(
                "OpenAI tokens: prompt=%d, completion=%d, total=%d",
                getattr(usage, "prompt_tokens", 0),
                getattr(usage, "completion_tokens", 0),
                getattr(usage, "total_tokens", 0),
            )


async def main():
    from config import settings

    provider = OpenAILLMProvider(
        settings.OPENAI_API_KEY, settings.OPENAI_BASE_URL, settings.OPENAI_MODEL
    )
    message = [
        {"role": "user", "content": "作为一名营销专家，请为我的产品创作一个吸引人的口号"},
        {
            "role": "assistant",
            "content": "当然，要创作一个吸引人的口号，请告诉我一些关于您产品的信息",
        },
    ]
    res = await provider.chat_completion(message, stream=True)
    print(res)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
