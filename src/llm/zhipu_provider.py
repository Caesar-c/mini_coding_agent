"""Zhipu AI (智谱) LLM Provider Implementation.

Uses the official ``zhipuai`` SDK when available, falling back to a graceful
error if the SDK is not installed.
"""

from typing import Any

from zai import ZhipuAiClient

from llm.interface import LLMProvider, MessageWrapper


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
            self._client = ZhipuAiClient(api_key=self.api_key)
        return self._client

    def chat_completion(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None = None,
        model: str = "glm-4.7-flash",
        thinking: bool = True,
        max_tokens: int = 65536,
        temperature: float = 0.7,
        stream: bool = False,
        **kwargs,
    ) -> MessageWrapper:
        """Generate a chat completion using the Zhipu AI API.

        Args:
            messages: List of message dictionaries with ``role`` and ``content``.
            tools: Optional list of tool definitions.
            model: Model identifier (default: ``glm-4.7-flash``).
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
            "stream": stream,
        }
        if thinking:
            # 启用深度思考模式
            api_params["thinking"] = {"type": "enabled"}
        if tools:
            api_params["tools"] = tools

        api_params.update(kwargs)
        api_params = {k: v for k, v in api_params.items() if v is not None}

        try:
            client = self._get_client()
            response = client.chat.completions.create(**api_params)

            if stream:
                # 流式获取回复：逐 chunk 累积 reasoning_content / content / tool_calls
                reasoning_content = ""
                content = ""
                tool_calls = []

                for chunk in response:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta

                    # 累积深度思考内容
                    reasoning = getattr(delta, "reasoning_content", None)
                    if reasoning:
                        reasoning_content += reasoning

                    # 累积正文内容
                    c = getattr(delta, "content", None)
                    if c:
                        content += c

                    # 收集 tool_calls（分块到达，需要按 index 合并）
                    tcs = getattr(delta, "tool_calls", None)
                    if tcs:
                        for tc in tcs:
                            idx = getattr(tc, "index", len(tool_calls))
                            # 扩展列表到足够容纳当前 index
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


if __name__ == "__main__":
    import os

    zp = ZhipuAILLMProvider(os.getenv("ZHIPU_API_KEY"))
    message = [
        {"role": "user", "content": "作为一名营销专家，请为我的产品创作一个吸引人的口号"},
        {
            "role": "assistant",
            "content": "当然，要创作一个吸引人的口号，请告诉我一些关于您产品的信息",
        },
        {"role": "user", "content": "智谱开放平台"},
    ]
    res = zp.chat_completion(message, stream=True)
    print(res)
