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


# 每个 provider 需要的环境变量（用于预检）
_REQUIRED_ENV: dict[LLMProviderType, list[tuple[str, str]]] = {
    LLMProviderType.OPENAI: [
        ("OPENAI_API_KEY", "OpenAI / 兼容 API 的密钥"),
    ],
    LLMProviderType.ZHIPU_AI: [
        ("ZHIPU_API_KEY", "智谱 AI 的 API 密钥"),
    ],
}


class MissingAPIKeyError(Exception):
    """Raised when required API key environment variables are not set."""


def validate_provider_config(provider_type: LLMProviderType) -> None:
    """Pre-flight check: verify required env vars are set for the given provider.

    Raises:
        MissingAPIKeyError: with a user-friendly message listing what's missing
            and how to fix it.
    """
    required = _REQUIRED_ENV.get(provider_type, [])
    missing = [(var, desc) for var, desc in required if not getattr(settings, var, "")]

    if missing:
        var_list = "\n  ".join(f"• {var} — {desc}" for var, desc in missing)
        raise MissingAPIKeyError(
            f"缺少 {provider_type.value} 所需的 API 配置。\n\n"
            f"  以下环境变量未设置或为空:\n  {var_list}\n\n"
            f"  请选择以下任一方式配置:\n\n"
            f"  方式 1 — 在当前目录创建 .env 文件:\n"
            f"    echo '{missing[0][0]}=your-api-key-here' > .env\n\n"
            f"  方式 2 — 创建全局配置文件:\n"
            f"    mkdir -p ~/.config/mini-agent\n"
            f"    echo '{missing[0][0]}=your-api-key-here' > ~/.config/mini-agent/.env\n\n"
            f"  方式 3 — 设置环境变量:\n"
            f"    export {missing[0][0]}=your-api-key-here"
        )


def create_llm_provider(provider_type: LLMProviderType) -> LLMProvider:
    """Instantiate an LLM provider of the requested type.

    API keys and base URLs are resolved via the process-wide :data:`settings`
    singleton (reads from environment / ``.env``).

    Args:
        provider_type: The kind of provider to create.

    Returns:
        An :class:`LLMProvider` instance ready for use.

    Raises:
        MissingAPIKeyError: If required API key env vars are not set.
        ValueError: If ``provider_type`` is not recognised.
    """
    validate_provider_config(provider_type)

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
