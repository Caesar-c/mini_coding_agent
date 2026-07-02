"""Chat command — interactive REPL session."""

from cli.display import RichDisplayHandler
from cli.repl import run_repl
from config import settings
from llm.factory import LLMProviderType
from logger import get_logger
from session.manager import SessionManager

logger = get_logger(__name__)


async def async_chat(
    provider: str | None = None,
    model: str | None = None,
    sandbox: str | None = None,
    max_tokens: int | None = None,
) -> None:
    """Start an interactive chat session.

    Args:
        provider: LLM provider name (overrides env).
        model: Model name (overrides env).
        sandbox: Sandbox root directory.
        max_tokens: Max output tokens.
    """
    # Resolve provider type
    provider_name = provider or settings.LLM_PROVIDER
    try:
        provider_type = LLMProviderType(provider_name)
    except ValueError:
        provider_type = LLMProviderType(settings.LLM_PROVIDER)
        provider_name = settings.LLM_PROVIDER

    display = RichDisplayHandler()
    resolved_model = model

    # Show welcome banner
    display.show_welcome(provider_name, resolved_model, sandbox or settings.SANDBOX_ROOT)

    # Create session manager with default session
    manager = SessionManager()
    manager.create("default", llm_provider_type=provider_type, display=display)
    logger.info("Chat session ready: provider=%s, model=%s", provider_name, resolved_model)

    # Run the REPL
    await run_repl(manager, display)
