"""Config command — show or modify configuration."""

from rich.console import Console
from rich.table import Table

from config import settings


def show_config() -> None:
    """Display the current configuration."""
    console = Console()
    table = Table(title="Mini Coding Agent Configuration", show_lines=True)
    table.add_column("Setting", style="bold cyan")
    table.add_column("Value", style="green")
    table.add_column("Source", style="dim")

    table.add_row("LLM_PROVIDER", settings.LLM_PROVIDER, "env: LLM_PROVIDER")
    table.add_row("OPENAI_MODEL", settings.OPENAI_MODEL, "env: OPENAI_MODEL")
    table.add_row("OPENAI_BASE_URL", settings.OPENAI_BASE_URL, "env: OPENAI_BASE_URL")
    table.add_row(
        "OPENAI_API_KEY",
        "***" + settings.OPENAI_API_KEY[-4:] if settings.OPENAI_API_KEY else "(not set)",
        "env: OPENAI_API_KEY",
    )
    table.add_row("ZHIPU_MODEL", settings.ZHIPU_MODEL or "(not set)", "env: ZHIPU_MODEL")
    table.add_row(
        "ZHIPU_API_KEY",
        "***" + settings.ZHIPU_API_KEY[-4:] if settings.ZHIPU_API_KEY else "(not set)",
        "env: ZHIPU_API_KEY",
    )
    table.add_row("MAX_TOKENS", str(settings.MAX_TOKENS), "env: MAX_TOKENS")
    table.add_row("MAX_TOOL_OUTPUT", str(settings.MAX_TOOL_OUTPUT), "env: MAX_TOOL_OUTPUT")
    table.add_row(
        "CONTEXT_MAX_MESSAGES",
        str(settings.CONTEXT_MAX_MESSAGES),
        "env: CONTEXT_MAX_MESSAGES",
    )
    table.add_row(
        "CONTEXT_KEEP_RECENT",
        str(settings.CONTEXT_KEEP_RECENT),
        "env: CONTEXT_KEEP_RECENT",
    )
    table.add_row("SANDBOX_ROOT", settings.SANDBOX_ROOT, "env: SANDBOX_ROOT")

    console.print(table)
    console.print(
        "\n[dim]To change settings, modify your .env file or set environment variables.[/dim]"
    )
