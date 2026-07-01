"""Run command — single-shot non-interactive task execution."""

from agent.async_loop import AsyncAgent
from cli.display import RichDisplayHandler
from config import settings
from llm.factory import LLMProviderType


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

    display = RichDisplayHandler()
    agent = AsyncAgent(llm_provider_type=provider_type, display=display)

    display.show_info(f"Executing: {task}")
    await agent.chat(task)
