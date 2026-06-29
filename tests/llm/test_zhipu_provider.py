"""Tests for :mod:`src.llm.zhipu_provider`."""

import sys
import unittest
from unittest.mock import MagicMock, patch

from src.llm.interface import LLMProvider, MessageWrapper
from src.llm.zhipu_provider import ZhipuAILLMProvider


class TestZhipuAILLMProvider(unittest.TestCase):
    """Unit tests for the Zhipu AI provider."""

    def test_is_llm_provider(self):
        provider = ZhipuAILLMProvider(api_key="test-key")
        self.assertIsInstance(provider, LLMProvider)

    def test_stores_api_key(self):
        provider = ZhipuAILLMProvider(api_key="zhipu-key")
        self.assertEqual(provider.api_key, "zhipu-key")

    def test_lazy_import_missing_sdk_raises(self):
        """If zhipuai is not installed, a clear ImportError is raised."""
        provider = ZhipuAILLMProvider(api_key="key")
        with patch.dict(sys.modules, {"zhipuai": None}):
            # Force re-import attempt
            with self.assertRaises(ImportError):
                provider._get_client()

    @patch("src.llm.zhipu_provider.ZhipuAILLMProvider._get_client")
    def test_chat_completion_success(self, mock_get_client):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        mock_message = MagicMock()
        mock_message.role = "assistant"
        mock_message.content = "你好"
        mock_message.tool_calls = []

        mock_choice = MagicMock()
        mock_choice.message = mock_message
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_client.chat.completions.create.return_value = mock_response

        provider = ZhipuAILLMProvider(api_key="key")
        result = provider.chat_completion(
            messages=[{"role": "user", "content": "你好"}],
            model="glm-4",
        )

        self.assertIsInstance(result, MessageWrapper)
        self.assertEqual(result.content, "你好")
        self.assertEqual(result.role, "assistant")

    @patch("src.llm.zhipu_provider.ZhipuAILLMProvider._get_client")
    def test_chat_completion_error_returns_wrapper(self, mock_get_client):
        mock_get_client.side_effect = Exception("auth failed")

        provider = ZhipuAILLMProvider(api_key="key")
        result = provider.chat_completion(
            messages=[{"role": "user", "content": "hello"}],
        )

        self.assertIsInstance(result, MessageWrapper)
        self.assertIn("Error", result.content)
        self.assertEqual(result.tool_calls, [])


if __name__ == "__main__":
    unittest.main()
