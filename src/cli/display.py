"""Rich-based display handler for the CLI terminal interface."""

from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table


class RichDisplayHandler:
    """Renders agent events to the terminal using Rich.

    Provides spinners during LLM calls, syntax-highlighted markdown
    for responses, coloured panels for tool output, and a progress
    table for task tracking.
    """

    def __init__(self, console: Console | None = None):
        self.console = console or Console()
        self._spinner = None

    def on_llm_start(self) -> None:
        """Show a spinner while the LLM is thinking."""
        self._spinner = self.console.status("[bold cyan]⠋ Thinking...")
        self._spinner.start()

    def on_llm_end(self) -> None:
        """Hide the spinner."""
        if self._spinner:
            self._spinner.stop()
            self._spinner = None

    def on_tool_call(self, name: str, args: dict[str, Any], output: str) -> None:
        """Display tool execution result in a panel."""
        args_str = str(args)[:100]
        if len(str(args)) > 100:
            args_str += "..."

        # Truncate long outputs for display
        display_output = output[:800]
        if len(output) > 800:
            display_output += f"\n... ({len(output)} chars total)"

        self.console.print(
            Panel(
                display_output,
                title=f"🔧 [bold]{name}[/bold]  [dim]{args_str}[/dim]",
                border_style="yellow",
                expand=False,
            )
        )

    def on_response(self, content: str) -> None:
        """Render the final response as markdown."""
        self.console.print()
        self.console.print(Markdown(content))
        self.console.print()

    def on_progress(self, summary: str) -> None:
        """Render the task progress summary as a table."""
        if not summary:
            return

        lines = summary.split("\n")
        table = Table(
            show_header=False,
            box=None,
            padding=(0, 1),
            title="[bold]📋 Task Progress[/bold]",
            title_style="blue",
        )
        table.add_column("status", style="bold", width=3)
        table.add_column("step")

        for line in lines:
            if line.startswith("[TASK PROGRESS]"):
                continue
            if line.startswith("Progress:"):
                self.console.print(f"[dim]{line}[/dim]")
                continue
            if line.startswith("Next:"):
                self.console.print(f"[cyan]{line}[/cyan]")
                continue
            if line.startswith("Ready:"):
                self.console.print(f"[yellow]{line}[/yellow]")
                continue
            # Parse step lines like "✓ T1: Description" or "✓ [1/5] Description"
            if len(line) >= 2 and line[0] in ("✓", "✗", "→", "◉", "○", "⊘"):
                icon = line[0]
                rest = line[2:]
                style_map = {
                    "✓": "green",
                    "✗": "red",
                    "→": "cyan",
                    "◉": "bold yellow",
                    "○": "dim",
                    "⊘": "dim magenta",
                }
                color = style_map.get(icon, "white")
                table.add_row(f"[{color}]{icon}[/{color}]", rest)
            else:
                self.console.print(line)

        if table.row_count > 0:
            self.console.print(table)

    def show_welcome(self, provider: str, model: str, sandbox: str) -> None:
        """Display the welcome banner."""
        self.console.print(
            Panel(
                f"[bold]Provider:[/bold] {provider}\n"
                f"[bold]Model:[/bold] {model}\n"
                f"[bold]Sandbox:[/bold] {sandbox}\n\n"
                f"Type [bold]/help[/bold] for commands, [bold]/quit[/bold] to exit.",
                title="🤖 Mini Coding Agent",
                border_style="blue",
            )
        )
        self.console.print()

    def show_error(self, message: str) -> None:
        """Display an error message."""
        self.console.print(f"[bold red]Error:[/bold red] {message}")

    def show_info(self, message: str) -> None:
        """Display an info message."""
        self.console.print(f"[dim]{message}[/dim]")
