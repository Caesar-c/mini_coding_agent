"""Run command — single-shot non-interactive task execution."""

import sys

from agent.async_loop import AsyncAgent
from cli.display import RichDisplayHandler
from config import settings
from llm.factory import LLMProviderType, MissingAPIKeyError, validate_provider_config
from logger import get_logger

logger = get_logger(__name__)


async def async_run(
    task: str,
    provider: str | None = None,
    model: str | None = None,
    sandbox: str | None = None,
) -> None:
    """Execute a single task non-interactively.

    Args:
        task: The task description to execute.
        provider: LLM provider name (overrides env).
        model: Model name (overrides env).
        sandbox: Sandbox root directory.
    """
    provider_name = provider or settings.LLM_PROVIDER
    try:
        provider_type = LLMProviderType(provider_name)
    except ValueError:
        provider_type = LLMProviderType(settings.LLM_PROVIDER)

    # Pre-flight: check API key before creating agent
    try:
        validate_provider_config(provider_type)
    except MissingAPIKeyError as e:
        from rich.console import Console

        Console(stderr=True).print(f"[bold red]配置错误[/bold red]\n{e}")
        sys.exit(1)

    display = RichDisplayHandler()
    agent = AsyncAgent(llm_provider_type=provider_type, display=display)

    display.show_info(f"Executing: {task}")
    await agent.chat(task)
    logger.info("Run completed: task='%s'", task[:80])
