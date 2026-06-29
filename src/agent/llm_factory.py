"""Factory for creating LLM provider instances."""

from enum import Enum
from typing import Dict, Any, Optional
from .llm_openai import OpenAILLMProvider
from .llm_zhipu import ZhipuAILLMProvider
from .llm_interface import LLMProvider


class LLMProviderType(Enum):
    OPENAI = "openai"
    ZHIPU_AI = "zhipu_ai"


def create_llm_provider(provider_type: LLMProviderType, **config) -> LLMProvider:
    """
    Factory function to create LLM provider instances.

    Args:
        provider_type: Type of LLM provider to create
        **config: Configuration parameters for the provider

    Returns:
        Instance of the requested LLMProvider
    """
    if provider_type == LLMProviderType.OPENAI:
        return OpenAILLMProvider(
            api_key=config.get('api_key'),
            base_url=config.get('base_url')
        )
    elif provider_type == LLMProviderType.ZHIPU_AI:
        return ZhipuAILLMProvider(
            api_key=config.get('api_key')
        )
    else:
        raise ValueError(f"Unsupported provider type: {provider_type}")


def get_available_providers() -> Dict[str, LLMProviderType]:
    """
    Get a dictionary of available LLM providers.

    Returns:
        Dictionary mapping provider names to their enum values
    """
    return {provider.value: provider for provider in LLMProviderType}