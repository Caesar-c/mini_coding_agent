"""Tests for AsyncOpenAILLMProvider."""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch


class TestAsyncOpenAILLMProvider(unittest.TestCase):
    """Tests for AsyncOpenAILLMProvider."""

    def test_is_async_llm_provider(self):
        from llm.async_openai_provider import AsyncOpenAILLMProvider
        from llm.interface import AsyncLLMProvider

        with patch("llm.async_openai_provider.AsyncOpenAI"):
            provider = AsyncOpenAILLMProvider(
                api_key="test", base_url="https://api.test.com", model="gpt-4o"
            )
            self.assertIsInstance(provider, AsyncLLMProvider)

    def test_constructor_requires_api_key(self):
        from llm.async_openai_provider import AsyncOpenAILLMProvider

        with self.assertRaises(ValueError):
            AsyncOpenAILLMProvider(api_key=None, base_url="https://test.com")

    def test_constructor_requires_base_url(self):
        from llm.async_openai_provider import AsyncOpenAILLMProvider

        with self.assertRaises(ValueError):
            AsyncOpenAILLMProvider(api_key="test", base_url=None)

    @patch("llm.async_openai_provider.AsyncOpenAI")
    def test_chat_completion_non_streaming(self, mock_client_cls):
        """Non-streaming call returns a MessageWrapper."""
        from llm.async_openai_provider import AsyncOpenAILLMProvider
        from llm.interface import MessageWrapper

        # Set up mock
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        mock_message = MagicMock()
        mock_message.role = "assistant"
        mock_message.content = "Hello!"
        mock_message.tool_calls = None
        mock_message.reasoning_content = None

        mock_choice = MagicMock()
        mock_choice.message = mock_message

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]

        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

        provider = AsyncOpenAILLMProvider(
            api_key="test", base_url="https://api.test.com", model="gpt-4o"
        )

        result = asyncio.run(provider.chat_completion([{"role": "user", "content": "Hi"}]))
        self.assertIsInstance(result, MessageWrapper)
        self.assertEqual(result.content, "Hello!")
        self.assertEqual(result.role, "assistant")

    @patch("llm.async_openai_provider.AsyncOpenAI")
    def test_chat_completion_error_returns_wrapper(self, mock_client_cls):
        """API errors are caught and returned as MessageWrapper with error content."""
        from llm.async_openai_provider import AsyncOpenAILLMProvider
        from llm.interface import MessageWrapper

        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.chat.completions.create = AsyncMock(side_effect=Exception("API error"))

        provider = AsyncOpenAILLMProvider(
            api_key="test", base_url="https://api.test.com", model="gpt-4o"
        )

        result = asyncio.run(provider.chat_completion([{"role": "user", "content": "Hi"}]))
        self.assertIsInstance(result, MessageWrapper)
        self.assertIn("Error", result.content)


if __name__ == "__main__":
    unittest.main()
