"""Factory for creating LLM provider instances.

This module centralises provider instantiation so that consumers (e.g.
:class:`agent.loop.Agent`) only need to know about the high-level
:class:`LLMProviderType` enum. New providers can be added by:

1. Implementing :class:`llm.interface.LLMProvider` in a new module
   under ``src/llm/``.
2. Adding a new :class:`LLMProviderType` enum value.
3. Extending :func:`create_llm_provider` with the corresponding branch.
"""

from enum import Enum

from src.llm.interface import LLMProvider
from src.llm.openai_provider import OpenAILLMProvider
from src.llm.zhipu_provider import ZhipuAILLMProvider


class LLMProviderType(Enum):
    """Enumeration of supported LLM provider backends."""

    OPENAI = "openai"
    ZHIPU_AI = "zhipu_ai"


def create_llm_provider(provider_type: LLMProviderType, **config) -> LLMProvider:
    """Instantiate an LLM provider of the requested type.

    Args:
        provider_type: The kind of provider to create.
        **config: Keyword arguments forwarded to the provider constructor
            (e.g. ``api_key``, ``base_url``).

    Returns:
        An :class:`LLMProvider` instance ready for use.

    Raises:
        ValueError: If ``provider_type`` is not recognised.
    """
    if provider_type == LLMProviderType.OPENAI:
        return OpenAILLMProvider(
            api_key=config.get("api_key"),
            base_url=config.get("base_url"),
        )
    elif provider_type == LLMProviderType.ZHIPU_AI:
        return ZhipuAILLMProvider(
            api_key=config.get("api_key"),
        )
    else:
        raise ValueError(f"Unsupported provider type: {provider_type}")


def get_available_providers() -> dict[str, LLMProviderType]:
    """Return a mapping of provider name -> :class:`LLMProviderType`."""
    return {provider.value: provider for provider in LLMProviderType}
