"""Tests for :mod:`src.llm.zhipu_provider`."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# Bare-package imports to match the provider's own imports (see
# test_openai_provider for the dual-module rationale).
from llm.interface import LLMProvider, MessageWrapper
from llm.zhipu_provider import ZhipuAILLMProvider


def _make_chunk(content=None, tool_calls=None, reasoning=None, usage=None):
    """Build a mock stream chunk in the Zhipu streaming shape."""
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


class TestZhipuAILLMProvider(unittest.TestCase):
    """Unit tests for the Zhipu AI provider."""

    def test_is_llm_provider(self):
        provider = ZhipuAILLMProvider(api_key="test-key")
        self.assertIsInstance(provider, LLMProvider)

    def test_stores_api_key(self):
        provider = ZhipuAILLMProvider(api_key="zhipu-key")
        self.assertEqual(provider.api_key, "zhipu-key")

    def test_missing_api_key_raises(self):
        with self.assertRaises(ValueError):
            ZhipuAILLMProvider(api_key=None)

    @patch("llm.zhipu_provider.ZhipuAILLMProvider._get_client")
    def test_chat_completion_success(self, mock_get_client):
        """Stream path accumulates content into a MessageWrapper."""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        # Zhipu provider iterates `for chunk in response` synchronously
        # (offloaded via to_thread); a plain list of chunks works.
        mock_client.chat.completions.create.return_value = [
            _make_chunk(content="你"),
            _make_chunk(content="好"),
        ]

        provider = ZhipuAILLMProvider(api_key="key")
        result = asyncio.run(
            provider.chat_completion(messages=[{"role": "user", "content": "你好"}])
        )

        self.assertIsInstance(result, MessageWrapper)
        self.assertEqual(result.content, "你好")
        self.assertEqual(result.role, "assistant")

    @patch("llm.zhipu_provider.ZhipuAILLMProvider._get_client")
    def test_chat_completion_stream_tool_calls(self, mock_get_client):
        """tool_calls fragments merge by index."""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        tc1a = SimpleNamespace(
            index=0, id="c1", function=SimpleNamespace(name="bash", arguments='{"a":')
        )
        tc1b = SimpleNamespace(
            index=0, id=None, function=SimpleNamespace(name="bash", arguments="1}")
        )
        mock_client.chat.completions.create.return_value = [_make_chunk(tool_calls=[tc1a, tc1b])]

        provider = ZhipuAILLMProvider(api_key="key")
        result = asyncio.run(provider.chat_completion(messages=[{"role": "user", "content": "go"}]))
        self.assertEqual(len(result.tool_calls), 1)
        self.assertEqual(result.tool_calls[0]["function"]["name"], "bash")
        self.assertEqual(result.tool_calls[0]["function"]["arguments"], '{"a":1}')

    @patch("llm.zhipu_provider.ZhipuAILLMProvider._get_client")
    def test_chat_completion_error_raises(self, mock_get_client):
        """API errors propagate — not swallowed into a fake message."""
        mock_get_client.side_effect = RuntimeError("auth failed")

        provider = ZhipuAILLMProvider(api_key="key")
        with self.assertRaises(RuntimeError):
            asyncio.run(provider.chat_completion(messages=[{"role": "user", "content": "hello"}]))


if __name__ == "__main__":
    unittest.main()
