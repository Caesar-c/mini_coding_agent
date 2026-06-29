"""Tests for :mod:`src.llm.factory`."""

import unittest

from src.llm.factory import LLMProviderType, create_llm_provider, get_available_providers
from src.llm.interface import LLMProvider
from src.llm.openai_provider import OpenAILLMProvider


class TestLLMProviderType(unittest.TestCase):
    """Verify the :class:`LLMProviderType` enum values."""

    def test_openai_value(self):
        self.assertEqual(LLMProviderType.OPENAI.value, "openai")

    def test_zhipu_ai_value(self):
        self.assertEqual(LLMProviderType.ZHIPU_AI.value, "zhipu_ai")

    def test_can_be_constructed_from_string(self):
        self.assertEqual(LLMProviderType("openai"), LLMProviderType.OPENAI)
        self.assertEqual(LLMProviderType("zhipu_ai"), LLMProviderType.ZHIPU_AI)


class TestCreateLLMProvider(unittest.TestCase):
    """Verify :func:`create_llm_provider` routes to the right class."""

    def test_create_openai_provider(self):
        provider = create_llm_provider(LLMProviderType.OPENAI, api_key="k")
        self.assertIsInstance(provider, LLMProvider)
        self.assertIsInstance(provider, OpenAILLMProvider)

    def test_create_zhipu_provider(self):
        # Zhipu provider lazy-imports the SDK, so instantiation succeeds
        # even without zhipuai installed.
        from src.llm.zhipu_provider import ZhipuAILLMProvider

        provider = create_llm_provider(LLMProviderType.ZHIPU_AI, api_key="k")
        self.assertIsInstance(provider, LLMProvider)
        self.assertIsInstance(provider, ZhipuAILLMProvider)

    def test_unknown_provider_raises(self):
        with self.assertRaises(ValueError):
            create_llm_provider("not_a_provider")


class TestGetAvailableProviders(unittest.TestCase):
    """Verify :func:`get_available_providers` returns the expected mapping."""

    def test_contains_openai(self):
        providers = get_available_providers()
        self.assertIn("openai", providers)
        self.assertEqual(providers["openai"], LLMProviderType.OPENAI)

    def test_contains_zhipu_ai(self):
        providers = get_available_providers()
        self.assertIn("zhipu_ai", providers)
        self.assertEqual(providers["zhipu_ai"], LLMProviderType.ZHIPU_AI)


if __name__ == "__main__":
    unittest.main()
