"""Async Zhipu AI (智谱) LLM Provider using httpx.

Uses ``httpx.AsyncClient`` to call the Zhipu AI API directly,
bypassing the synchronous SDK for true async support.
"""

import json
from typing import Any

import httpx

from llm.interface import AsyncLLMProvider, MessageWrapper

_ZHIPU_API_BASE = "https://open.bigmodel.cn/api/paas/v4"


class AsyncZhipuAILLMProvider(AsyncLLMProvider):
    """Async LLM provider for the Zhipu AI (智谱清言) API.

    Calls the REST API directly via ``httpx.AsyncClient`` instead of
    the synchronous ``zhipuai`` SDK.
    """

    def __init__(self, api_key: str = None, model: str = None):
        if not api_key:
            raise ValueError("ZHIPU API KEY must not be NULL.")
        self.api_key = api_key
        self.model = model
        self._client = httpx.AsyncClient(
            base_url=_ZHIPU_API_BASE,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=120.0,
        )

    async def chat_completion(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None = None,
        thinking: bool = True,
        max_tokens: int = 65536,
        temperature: float = 0.7,
        stream: bool = False,
        **kwargs,
    ) -> MessageWrapper:
        """Generate an async chat completion using the Zhipu AI API."""
        api_params = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": stream,
        }
        if thinking:
            api_params["thinking"] = {"type": "enabled"}
        if tools:
            api_params["tools"] = tools

        api_params.update(kwargs)
        api_params = {k: v for k, v in api_params.items() if v is not None}

        try:
            if stream:
                return await self._stream_completion(api_params)
            else:
                return await self._sync_completion(api_params)
        except Exception as e:
            return MessageWrapper(
                {
                    "role": "assistant",
                    "content": f"Error calling Zhipu AI API: {e}",
                    "tool_calls": [],
                }
            )

    async def _sync_completion(self, api_params: dict) -> MessageWrapper:
        """Non-streaming async call."""
        resp = await self._client.post("/chat/completions", json=api_params)
        resp.raise_for_status()
        data = resp.json()
        message = data["choices"][0]["message"]
        return MessageWrapper(
            {
                "role": message.get("role", "assistant"),
                "content": message.get("content", ""),
                "tool_calls": message.get("tool_calls") or [],
                "reasoning_content": message.get("reasoning_content", ""),
            }
        )

    async def _stream_completion(self, api_params: dict) -> MessageWrapper:
        """Streaming async call — accumulates chunks into a single response."""
        content = ""
        reasoning_content = ""
        tool_calls: list[dict[str, Any]] = []

        async with self._client.stream("POST", "/chat/completions", json=api_params) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                payload = line[6:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue

                if not chunk.get("choices"):
                    continue
                delta = chunk["choices"][0].get("delta", {})

                c = delta.get("content")
                if c:
                    content += c

                r = delta.get("reasoning_content")
                if r:
                    reasoning_content += r

                tcs = delta.get("tool_calls")
                if tcs:
                    for tc in tcs:
                        idx = tc.get("index", len(tool_calls))
                        while len(tool_calls) <= idx:
                            tool_calls.append(
                                {
                                    "id": None,
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""},
                                }
                            )
                        if tc.get("id"):
                            tool_calls[idx]["id"] = tc["id"]
                        fn = tc.get("function", {})
                        if fn.get("name"):
                            tool_calls[idx]["function"]["name"] += fn["name"]
                        if fn.get("arguments"):
                            tool_calls[idx]["function"]["arguments"] += fn["arguments"]

        return MessageWrapper(
            {
                "role": "assistant",
                "content": content,
                "tool_calls": tool_calls,
                "reasoning_content": reasoning_content,
            }
        )
