"""Chat command — interactive REPL session."""

import sys

from cli.display import RichDisplayHandler
from cli.repl import run_repl
from config import settings
from llm.factory import LLMProviderType, MissingAPIKeyError, validate_provider_config
from logger import get_logger
from session.manager import SessionManager

logger = get_logger(__name__)


async def async_chat(
    provider: str | None = None,
    model: str | None = None,
    sandbox: str | None = None,
) -> None:
    """Start an interactive chat session.

    Args:
        provider: LLM provider name (overrides env).
        model: Model name (overrides env).
        sandbox: Sandbox root directory.
    """
    # Resolve provider type
    provider_name = provider or settings.LLM_PROVIDER
    try:
        provider_type = LLMProviderType(provider_name)
    except ValueError:
        provider_type = LLMProviderType(settings.LLM_PROVIDER)
        provider_name = settings.LLM_PROVIDER

    # Pre-flight: check API key before entering REPL
    try:
        validate_provider_config(provider_type)
    except MissingAPIKeyError as e:
        from rich.console import Console

        Console(stderr=True).print(f"[bold red]配置错误[/bold red]\n{e}")
        sys.exit(1)

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
