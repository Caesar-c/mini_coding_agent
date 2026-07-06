"""Factory for creating LLM provider instances.

This module centralises provider instantiation so that consumers (e.g.
:class:`agent.async_loop.AsyncAgent`) only need to know about the high-level
:class:`LLMProviderType` enum. New providers can be added by:

1. Implementing :class:`llm.interface.LLMProvider` in a new module
   under ``src/llm/``.
2. Adding a new :class:`LLMProviderType` enum value.
3. Extending :func:`create_llm_provider` with the corresponding branch.
"""

from enum import Enum

from config import settings
from llm.interface import LLMProvider
from llm.openai_provider import OpenAILLMProvider
from llm.zhipu_provider import ZhipuAILLMProvider
from logger import get_logger

logger = get_logger(__name__)


class LLMProviderType(Enum):
    """Enumeration of supported LLM provider backends."""

    OPENAI = "openai"
    ZHIPU_AI = "zhipu_ai"


def create_llm_provider(provider_type: LLMProviderType) -> LLMProvider:
    """Instantiate an LLM provider of the requested type.

    API keys and base URLs are resolved via the process-wide :data:`settings`
    singleton (reads from environment / ``.env``).

    Args:
        provider_type: The kind of provider to create.

    Returns:
        An :class:`LLMProvider` instance ready for use.

    Raises:
        ValueError: If ``provider_type`` is not recognised.
    """
    if provider_type == LLMProviderType.OPENAI:
        logger.info(
            "Creating OpenAI provider: model=%s, base_url=%s",
            settings.OPENAI_MODEL,
            settings.OPENAI_BASE_URL,
        )
        return OpenAILLMProvider(
            api_key=settings.OPENAI_API_KEY,
            base_url=settings.OPENAI_BASE_URL,
            model=settings.OPENAI_MODEL,
        )
    elif provider_type == LLMProviderType.ZHIPU_AI:
        logger.info("Creating ZhipuAI provider: model=%s", settings.ZHIPU_MODEL)
        return ZhipuAILLMProvider(api_key=settings.ZHIPU_API_KEY, model=settings.ZHIPU_MODEL)
    else:
        raise ValueError(f"Unsupported provider type: {provider_type}")


def get_available_providers() -> dict[str, LLMProviderType]:
    """Return a mapping of provider name -> :class:`LLMProviderType`."""
    return {provider.value: provider for provider in LLMProviderType}
