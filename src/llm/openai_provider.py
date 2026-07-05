"""OpenAI LLM Provider Implementation."""

from typing import Any

import openai

from llm.interface import LLMProvider, MessageWrapper
from logger import get_logger

logger = get_logger(__name__)


class OpenAILLMProvider(LLMProvider):
    """LLM provider for OpenAI-compatible APIs.

    Supports both the modern ``openai.OpenAI`` client (v1.x) and the legacy
    ``openai.ChatCompletion`` module (v0.x) to maximise compatibility with
    different runtime environments.
    """

    def __init__(self, api_key: str = None, base_url: str = None, model: str = None):
        if not api_key or not base_url:
            raise ValueError("API_KEY and BASE_URL must not be NULL.")
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        # Configure legacy module-level attributes for v0.x clients
        openai.api_key = self.api_key
        if base_url and hasattr(openai, "api_base"):
            openai.api_base = base_url

    def chat_completion(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        stream: bool = True,
        reasoning: bool = False,
        reasoning_effort: str = "medium",
        **kwargs,
    ) -> MessageWrapper:
        """Generate a chat completion using the OpenAI API.

        Args:
            messages: List of message dictionaries with ``role`` and ``content``.
            tools: Optional list of tool definitions (OpenAI function-calling schema).
                When set, the model may reply with ``tool_calls`` instead of plain
                text; the caller is responsible for executing the tools and
                feeding results back.
            model: Model identifier. Defaults to ``gpt-4o-mini``.
            max_tokens: Maximum number of tokens to generate.
            temperature: Sampling temperature.
            stream: If True, consume the response as a stream of chunks and
                silently accumulate ``content`` / ``reasoning_content`` /
                ``tool_calls`` into a single :class:`MessageWrapper`.
            reasoning: If True, enable reasoning capabilities. For OpenAI
                o-series models (o1 / o3 / o4-mini) this sets the
                ``reasoning_effort`` parameter; for compatible providers
                (e.g. DeepSeek) that expose ``reasoning_content`` in chunks,
                the reasoning text is accumulated and returned under
                ``message_data["reasoning_content"]``.
            reasoning_effort: Effort level for reasoning models — ``"low"``,
                ``"medium"`` (default), or ``"high"``. Only used when
                ``reasoning=True``.
            **kwargs: Additional parameters forwarded to the API.

        Returns:
            :class:`MessageWrapper` containing the provider response. The
            wrapper's data always has keys ``role``, ``content``,
            ``tool_calls``; ``reasoning_content`` is also present when
            ``stream=True`` or when the upstream message exposes it.
        """
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
            # o-series reasoning models accept this top-level param.
            api_params["reasoning_effort"] = reasoning_effort

        api_params.update(kwargs)
        api_params = {k: v for k, v in api_params.items() if v is not None}

        try:
            logger.info(
                "OpenAI chat_completion: model=%s, messages=%d, stream=%s, tools=%s",
                self.model,
                len(messages),
                stream,
                bool(tools),
            )
            # v1.x client — streaming + tool_calls + reasoning all require it.
            client = openai.OpenAI(api_key=self.api_key, base_url=self.base_url)
            response = client.chat.completions.create(**api_params)

            if stream:
                # 流式：逐 chunk 累积 content / reasoning_content / tool_calls
                content = ""
                reasoning_content = ""
                tool_calls: list[dict[str, Any]] = []

                for chunk in response:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta

                    # 累积正文
                    c = getattr(delta, "content", None)
                    if c:
                        content += c

                    # 累积深度思考内容（DeepSeek / 兼容协议）
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
                # 非流式：一次性拿到完整 message
                message = response.choices[0].message
                message_data = {
                    "role": getattr(message, "role", "assistant"),
                    "content": getattr(message, "content", None),
                    "tool_calls": list(getattr(message, "tool_calls", None) or []),
                    "reasoning_content": getattr(message, "reasoning_content", None) or "",
                }

            logger.info(
                "OpenAI response: content_len=%d, tool_calls=%d",
                len(message_data.get("content") or ""),
                len(message_data.get("tool_calls") or []),
            )
            # Log token usage if available
            usage = getattr(response, "usage", None)
            if usage:
                logger.info(
                    "OpenAI tokens: prompt=%d, completion=%d, total=%d",
                    getattr(usage, "prompt_tokens", 0),
                    getattr(usage, "completion_tokens", 0),
                    getattr(usage, "total_tokens", 0),
                )
            return MessageWrapper(message_data)
        except Exception as e:
            logger.error("OpenAI API error: %s", e, exc_info=True)
            return MessageWrapper(
                {
                    "role": "assistant",
                    "content": f"Error calling OpenAI API: {e}",
                    "tool_calls": [],
                    "reasoning_content": "",
                }
            )
