"""Mini Coding Agent CLI — Typer entry point."""

import asyncio

import typer

app = typer.Typer(
    name="mini-agent",
    help="🤖 Mini Coding Agent — an AI coding assistant with pluggable LLM backends.",
    no_args_is_help=True,
)


@app.command()
def chat(
    provider: str | None = typer.Option(
        "zhipu_ai", "--provider", "-p", help="LLM provider (openai, zhipu_ai)"
    ),
    model: str | None = typer.Option("glm-4.7-flash", "--model", "-m", help="Model name override"),
    sandbox: str | None = typer.Option(None, "--sandbox", "-s", help="Sandbox root directory"),
    max_tokens: int | None = typer.Option(8192, "--max-tokens", help="Max output tokens"),
) -> None:
    """Start an interactive chat session with the coding agent."""
    from cli.commands.chat import async_chat

    asyncio.run(async_chat(provider=provider, model=model, sandbox=sandbox, max_tokens=max_tokens))


@app.command()
def run(
    task: str = typer.Argument(..., help="Task description to execute"),
    provider: str | None = typer.Option(
        "zhipu_ai", "--provider", "-p", help="LLM provider (openai, zhipu_ai)"
    ),
    model: str | None = typer.Option("glm-4.7-flash", "--model", "-m", help="Model name override"),
    sandbox: str | None = typer.Option(None, "--sandbox", "-s", help="Sandbox root directory"),
) -> None:
    """Execute a single task non-interactively."""
    from cli.commands.run import async_run

    asyncio.run(async_run(task=task, provider=provider, model=model, sandbox=sandbox))


@app.command()
def config() -> None:
    """Show current configuration."""
    from cli.commands.config_cmd import show_config

    show_config()


if __name__ == "__main__":
    app()
