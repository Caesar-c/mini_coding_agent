"""Interactive REPL using prompt_toolkit with slash commands."""

import asyncio
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import FileHistory

from cli.display import RichDisplayHandler
from config import settings
from logger import get_logger
from session.manager import SessionManager

logger = get_logger(__name__)

_HISTORY_FILE = Path.home() / ".mini_agent_history"

SLASH_COMMANDS = [
    "/quit",
    "/clear",
    "/plan",
    "/reset",
    "/new",
    "/switch",
    "/sessions",
    "/config",
    "/compact",
    "/history",
    "/tools",
    "/skills",
    "/help",
]

HELP_TEXT = """\
[bold]Available commands:[/bold]
  /quit       — Exit the agent
  /clear      — Clear conversation history (current session)
  /plan       — Show the current task plan
  /reset      — Reset the current task plan
  /new [name] — Create a new session
  /switch <n> — Switch to session <n>
  /sessions   — List all sessions
  /config     — Show current configuration
  /compact    — Manually trigger context compaction
  /history    — Show message count statistics
  /tools      — List available tools
  /skills     — List loaded skills (domain knowledge)
  /help       — Show this help message
"""


async def run_repl(
    session_manager: SessionManager,
    display: RichDisplayHandler,
) -> None:
    """Run the interactive REPL loop.

    Args:
        session_manager: The session manager with an active session.
        display: The Rich display handler for rendering output.
    """
    prompt_session = PromptSession(
        history=FileHistory(str(_HISTORY_FILE)),
        completer=WordCompleter(SLASH_COMMANDS, sentence=True),
        multiline=False,
    )
    logger.info("REPL started")

    while True:
        # Get input in a thread to keep event loop free for spinners
        try:
            user_input = await asyncio.to_thread(prompt_session.prompt, "> ", multiline=False)
        except (EOFError, KeyboardInterrupt):
            logger.info("REPL exiting (interrupt)")
            display.show_info("\nGoodbye!")
            break

        user_input = user_input.strip()
        if not user_input:
            continue

        # Handle slash commands
        if user_input.startswith("/"):
            logger.info("Slash command: %s", user_input)
            should_quit = await _handle_slash_command(user_input, session_manager, display)
            if should_quit:
                logger.info("REPL exiting")
                break
            continue

        logger.debug("User input: len=%d", len(user_input))

        # Regular chat message
        agent = session_manager.active
        if agent is None:
            display.show_error("No active session. Use /new to create one.")
            continue

        try:
            await agent.chat(user_input)
        except Exception as e:
            display.show_error(f"{e}")


async def _handle_slash_command(
    command: str,
    session_manager: SessionManager,
    display: RichDisplayHandler,
) -> bool:
    """Handle a slash command. Returns True if the REPL should quit."""
    from rich.table import Table

    console = display.console
    parts = command.split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd == "/quit":
        display.show_info("Goodbye!")
        return True

    elif cmd == "/clear":
        agent = session_manager.active
        if agent:
            system_msg = agent.messages[0]
            agent.messages = [system_msg]
            display.show_info("Conversation history cleared.")
        else:
            display.show_error("No active session.")

    elif cmd == "/plan":
        agent = session_manager.active
        if agent and agent.task_graph.has_plan:
            summary = agent.task_graph.format_summary()
            display.on_progress(summary)
        else:
            display.show_info("No active plan.")

    elif cmd == "/reset":
        agent = session_manager.active
        if agent:
            agent.task_graph.reset()
            display.show_info("Plan reset.")
        else:
            display.show_error("No active session.")

    elif cmd == "/new":
        name = arg or f"session-{len(session_manager.list_sessions()) + 1}"
        try:
            session_manager.create(name)
            display.show_info(f"Created and switched to session '{name}'.")
        except ValueError as e:
            display.show_error(str(e))

    elif cmd == "/switch":
        if not arg:
            display.show_error("Usage: /switch <session-name>")
        else:
            try:
                session_manager.switch(arg)
                display.show_info(f"Switched to session '{arg}'.")
            except KeyError as e:
                display.show_error(str(e))

    elif cmd == "/sessions":
        sessions = session_manager.list_sessions()
        if not sessions:
            display.show_info("No active sessions.")
        else:
            table = Table(title="Sessions", show_lines=False)
            table.add_column("Name", style="bold")
            table.add_column("Messages", justify="right")
            table.add_column("Plan", justify="center")
            active = session_manager.active_name
            for name in sessions:
                agent = session_manager.get(name)
                msg_count = len(agent.messages) if agent else 0
                has_plan = "✓" if agent and agent.task_graph.has_plan else "—"
                marker = " ← active" if name == active else ""
                table.add_row(name + marker, str(msg_count), has_plan)
            console.print(table)

    elif cmd == "/config":
        table = Table(title="Configuration", show_lines=False)
        table.add_column("Key", style="bold cyan")
        table.add_column("Value")
        table.add_row("LLM_PROVIDER", settings.LLM_PROVIDER)
        table.add_row("MAX_TOKENS", str(settings.MAX_TOKENS))
        table.add_row("MAX_TOOL_OUTPUT", str(settings.MAX_TOOL_OUTPUT))
        table.add_row("CONTEXT_MAX_MESSAGES", str(settings.CONTEXT_MAX_MESSAGES))
        table.add_row("CONTEXT_KEEP_RECENT", str(settings.CONTEXT_KEEP_RECENT))
        table.add_row("SANDBOX_ROOT", settings.SANDBOX_ROOT)
        console.print(table)

    elif cmd == "/compact":
        agent = session_manager.active
        if agent:
            before = len(agent.messages)
            logger.info("Manual /compact triggered: %d messages", before)
            agent.messages = await asyncio.to_thread(agent.context_pipeline.compact, agent.messages)
            after = len(agent.messages)
            stats = agent.context_pipeline.stats
            logger.info(
                "Manual /compact done: %d -> %d messages, stats=%s",
                before,
                after,
                stats,
            )
            display.show_info(
                f"Compaction: {before} → {after} messages. "
                f"(L1:{stats['micro_compressions']} L2:{stats['meso_compressions']} L3:{stats['macro_compressions']})"
            )
        else:
            display.show_error("No active session.")

    elif cmd == "/history":
        agent = session_manager.active
        if agent:
            count = len(agent.messages)
            display.show_info(
                f"Current session: {count} messages "
                f"(compact threshold: {agent.context_pipeline.meso.meso_message_threshold} msgs)"
            )
        else:
            display.show_error("No active session.")

    elif cmd == "/tools":
        agent = session_manager.active
        if agent:
            names = agent.tool_registry.get_tool_names()
            display.show_info(f"Available tools: {', '.join(names)}")
        else:
            display.show_error("No active session.")

    elif cmd == "/skills":
        agent = session_manager.active
        if agent:
            skill_loader = agent.skill_loader
            if skill_loader.count == 0:
                display.show_info("No skills loaded.")
            else:
                table = Table(title="Loaded Skills", show_lines=False)
                table.add_column("Name", style="bold cyan")
                table.add_column("Description")
                table.add_column("Version", justify="center")
                table.add_column("Tags")
                for name in skill_loader.list_names():
                    entry = skill_loader.get_skill(name)
                    if entry is None:
                        continue
                    ver = entry.version or "—"
                    tags = ", ".join(entry.tags) if entry.tags else "—"
                    table.add_row(entry.name, entry.description, ver, tags)
                console.print(table)
        else:
            display.show_error("No active session.")

    elif cmd == "/help":
        console.print(HELP_TEXT)

    else:
        display.show_error(f"Unknown command: {cmd}. Type /help for available commands.")

    return False
