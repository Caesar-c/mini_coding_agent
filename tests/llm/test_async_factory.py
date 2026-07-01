"""Tests for async LLM provider factory."""

import unittest
from unittest.mock import patch


class TestCreateAsyncLLMProvider(unittest.TestCase):
    """Tests for create_async_llm_provider."""

    @patch("llm.async_factory.settings")
    @patch("llm.async_openai_provider.AsyncOpenAI")
    def test_create_openai_provider(self, mock_async_openai, mock_settings):
        mock_settings.OPENAI_API_KEY = "test-key"
        mock_settings.OPENAI_BASE_URL = "https://api.test.com/v1"
        mock_settings.OPENAI_MODEL = "gpt-4o"

        from llm.async_factory import create_async_llm_provider
        from llm.async_openai_provider import AsyncOpenAILLMProvider
        from llm.factory import LLMProviderType

        provider = create_async_llm_provider(LLMProviderType.OPENAI)
        self.assertIsInstance(provider, AsyncOpenAILLMProvider)

    @patch("llm.async_factory.settings")
    def test_create_zhipu_provider(self, mock_settings):
        mock_settings.ZHIPU_API_KEY = "test-zhipu-key"
        mock_settings.ZHIPU_MODEL = "glm-4"

        from llm.async_factory import create_async_llm_provider
        from llm.async_zhipu_provider import AsyncZhipuAILLMProvider
        from llm.factory import LLMProviderType

        provider = create_async_llm_provider(LLMProviderType.ZHIPU_AI)
        self.assertIsInstance(provider, AsyncZhipuAILLMProvider)

    def test_unsupported_provider_raises(self):
        from llm.async_factory import create_async_llm_provider

        with self.assertRaises(ValueError):
            create_async_llm_provider("invalid_type")


if __name__ == "__main__":
    unittest.main()
