"""Tests for :mod:`src.llm.openai_provider`."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

# NOTE: imports use the bare-package form (`llm...`, not `src.llm...`) to match
# the provider's own imports. Mixing the two prefixes creates two distinct
# module objects, which would break isinstance(provider, LLMProvider).
from llm.interface import LLMProvider, MessageWrapper
from llm.openai_provider import OpenAILLMProvider


def _make_chunk(content=None, tool_calls=None, reasoning=None, usage=None):
    """Build a mock stream chunk in the OpenAI v1.x shape."""
    delta = MagicMock()
    delta.content = content
    delta.tool_calls = tool_calls
    delta.reasoning_content = reasoning

    choice = MagicMock()
    choice.delta = delta
    chunk = MagicMock()
    chunk.choices = [choice] if (content or tool_calls or reasoning) else []
    chunk.usage = usage
    return chunk


class _AsyncStream:
    """Async-iterable wrapper over a list of chunks (mimics AsyncOpenAI stream)."""

    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for c in self._chunks:
            yield c


class TestOpenAILLMProvider(unittest.TestCase):
    """Unit tests for the OpenAI provider."""

    def test_is_llm_provider(self):
        provider = OpenAILLMProvider(api_key="test-key")
        self.assertIsInstance(provider, LLMProvider)

    def test_stores_api_key(self):
        provider = OpenAILLMProvider(api_key="my-key")
        self.assertEqual(provider.api_key, "my-key")

    def test_stores_base_url(self):
        provider = OpenAILLMProvider(api_key="k", base_url="https://example.com/v1")
        self.assertEqual(provider.base_url, "https://example.com/v1")

    def test_missing_api_key_raises(self):
        with self.assertRaises(ValueError):
            OpenAILLMProvider(api_key=None)

    @patch("llm.openai_provider.openai.AsyncOpenAI")
    def test_chat_completion_non_stream(self, mock_async_openai):
        """Non-stream path returns a MessageWrapper with content."""
        mock_message = MagicMock()
        mock_message.role = "assistant"
        mock_message.content = "hi there"
        mock_message.tool_calls = []
        mock_message.reasoning_content = None

        mock_choice = MagicMock()
        mock_choice.message = mock_message
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage = None

        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
        mock_async_openai.return_value = mock_client

        provider = OpenAILLMProvider(api_key="key")
        result = asyncio.run(
            provider.chat_completion(
                messages=[{"role": "user", "content": "hello"}],
                stream=False,
            )
        )

        self.assertIsInstance(result, MessageWrapper)
        self.assertEqual(result.content, "hi there")
        self.assertEqual(result.role, "assistant")
        self.assertEqual(result.tool_calls, [])

    @patch("llm.openai_provider.openai.AsyncOpenAI")
    def test_chat_completion_stream(self, mock_async_openai):
        """Stream path accumulates content across chunks."""
        usage = MagicMock(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        chunks = [
            _make_chunk(content="hel"),
            _make_chunk(content="lo"),
            _make_chunk(usage=usage),  # final usage chunk, empty choices
        ]
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=_AsyncStream(chunks))
        mock_async_openai.return_value = mock_client

        provider = OpenAILLMProvider(api_key="key")
        result = asyncio.run(provider.chat_completion(messages=[{"role": "user", "content": "hi"}]))

        self.assertIsInstance(result, MessageWrapper)
        self.assertEqual(result.content, "hello")
        self.assertEqual(result.tool_calls, [])
        # stream_options must request usage
        call_kwargs = mock_client.chat.completions.create.call_args.kwargs
        self.assertEqual(call_kwargs.get("stream_options"), {"include_usage": True})

    @patch("llm.openai_provider.openai.AsyncOpenAI")
    def test_chat_completion_stream_tool_calls_merge(self, mock_async_openai):
        """tool_calls arrive in fragments and are merged by index; name is set
        once (not concatenated into duplicates)."""
        # Two tool calls, each fragmented across chunks. Use SimpleNamespace for
        # the function payload to avoid the MagicMock `name=` constructor gotcha
        # (which returns a child mock, not the configured string).
        tc1a = SimpleNamespace(
            index=0, id="call_1", function=SimpleNamespace(name="read_file", arguments='{"pa')
        )
        tc1b = SimpleNamespace(
            index=0, id=None, function=SimpleNamespace(name="read_file", arguments='th": "a"}')
        )
        tc2a = SimpleNamespace(
            index=1, id="call_2", function=SimpleNamespace(name="bash", arguments='{"cmd":')
        )
        chunks = [
            _make_chunk(tool_calls=[tc1a]),
            _make_chunk(tool_calls=[tc1b, tc2a]),
        ]
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=_AsyncStream(chunks))
        mock_async_openai.return_value = mock_client

        provider = OpenAILLMProvider(api_key="key")
        result = asyncio.run(provider.chat_completion(messages=[{"role": "user", "content": "go"}]))

        self.assertEqual(len(result.tool_calls), 2)
        self.assertEqual(result.tool_calls[0]["function"]["name"], "read_file")
        self.assertEqual(result.tool_calls[0]["function"]["arguments"], '{"path": "a"}')
        self.assertEqual(result.tool_calls[1]["function"]["name"], "bash")

    @patch("llm.openai_provider.openai.AsyncOpenAI")
    def test_chat_completion_error_raises(self, mock_async_openai):
        """API errors propagate — they are NOT swallowed into a fake message."""
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(side_effect=RuntimeError("network error"))
        mock_async_openai.return_value = mock_client

        provider = OpenAILLMProvider(api_key="key")
        with self.assertRaises(RuntimeError):
            asyncio.run(provider.chat_completion(messages=[{"role": "user", "content": "hello"}]))

    @patch("llm.openai_provider.openai.AsyncOpenAI")
    def test_reasoning_effort_not_auto_injected(self, mock_async_openai):
        """reasoning_effort must be passed explicitly; not auto-added."""
        mock_message = MagicMock()
        mock_message.role = "assistant"
        mock_message.content = "ok"
        mock_message.tool_calls = []
        mock_message.reasoning_content = None
        mock_choice = MagicMock()
        mock_choice.message = mock_message
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage = None
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
        mock_async_openai.return_value = mock_client

        provider = OpenAILLMProvider(api_key="key")
        asyncio.run(
            provider.chat_completion(messages=[{"role": "user", "content": "x"}], stream=False)
        )
        call_kwargs = mock_client.chat.completions.create.call_args.kwargs
        self.assertNotIn("reasoning_effort", call_kwargs)


if __name__ == "__main__":
    unittest.main()
