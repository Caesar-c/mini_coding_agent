"""Factory for creating async LLM provider instances."""

from config import settings
from llm.async_openai_provider import AsyncOpenAILLMProvider
from llm.async_zhipu_provider import AsyncZhipuAILLMProvider
from llm.factory import LLMProviderType
from llm.interface import AsyncLLMProvider


def create_async_llm_provider(provider_type: LLMProviderType) -> AsyncLLMProvider:
    """Instantiate an async LLM provider of the requested type.

    API keys and base URLs are resolved via the process-wide :data:`settings`
    singleton (reads from environment / ``.env``).

    Args:
        provider_type: The kind of provider to create.

    Returns:
        An :class:`AsyncLLMProvider` instance ready for use.

    Raises:
        ValueError: If ``provider_type`` is not recognised.
    """
    if provider_type == LLMProviderType.OPENAI:
        return AsyncOpenAILLMProvider(
            api_key=settings.OPENAI_API_KEY,
            base_url=settings.OPENAI_BASE_URL,
            model=settings.OPENAI_MODEL,
        )
    elif provider_type == LLMProviderType.ZHIPU_AI:
        return AsyncZhipuAILLMProvider(
            api_key=settings.ZHIPU_API_KEY,
            model=settings.ZHIPU_MODEL,
        )
    else:
        raise ValueError(f"Unsupported async provider type: {provider_type}")
