"""Tests for :mod:`llm.openai_provider`."""

import unittest
import sys
import os
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

from llm.openai_provider import OpenAILLMProvider
from llm.interface import MessageWrapper, LLMProvider


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

    @patch('llm.openai_provider.openai')
    def test_chat_completion_modern_api(self, mock_openai):
        """When ``openai.OpenAI`` exists, it should be used."""
        mock_openai.OpenAI = MagicMock()
        mock_client = MagicMock()
        mock_openai.OpenAI.return_value = mock_client

        mock_message = MagicMock()
        mock_message.role = 'assistant'
        mock_message.content = 'hi there'
        mock_message.tool_calls = []

        mock_choice = MagicMock()
        mock_choice.message = mock_message
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_client.chat.completions.create.return_value = mock_response

        provider = OpenAILLMProvider(api_key="key")
        result = provider.chat_completion(
            messages=[{'role': 'user', 'content': 'hello'}],
            model='gpt-4',
        )

        self.assertIsInstance(result, MessageWrapper)
        self.assertEqual(result.content, 'hi there')
        self.assertEqual(result.role, 'assistant')

    @patch('llm.openai_provider.openai')
    def test_chat_completion_error_returns_wrapper(self, mock_openai):
        """When the API call raises, an error MessageWrapper is returned."""
        mock_openai.OpenAI = MagicMock(side_effect=Exception("network error"))

        provider = OpenAILLMProvider(api_key="key")
        result = provider.chat_completion(
            messages=[{'role': 'user', 'content': 'hello'}],
        )

        self.assertIsInstance(result, MessageWrapper)
        self.assertIn("Error", result.content)
        self.assertEqual(result.tool_calls, [])


if __name__ == '__main__':
    unittest.main()
