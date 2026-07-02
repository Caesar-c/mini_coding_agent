"""Async tool implementations.

``bash`` uses ``asyncio.create_subprocess_shell`` for true async execution.
File operations use ``asyncio.to_thread`` to wrap the synchronous sandbox
methods, since file I/O is blocking.
"""

import asyncio
import re
from typing import Any

from agent.path_sandbox import PathSandbox
from agent.tools import (
    BASH_TOOL_DEFINITION,
    CREATE_DIRECTORY_TOOL_DEFINITION,
    FILE_EXISTS_TOOL_DEFINITION,
    LIST_DIRECTORY_TOOL_DEFINITION,
    READ_FILE_TOOL_DEFINITION,
    WRITE_FILE_TOOL_DEFINITION,
    run_create_directory,
    run_file_exists,
    run_list_directory,
    run_read_file,
    run_write_file,
)
from logger import get_logger

logger = get_logger(__name__)

# Shared sandbox instance
sandbox = PathSandbox(root_dir=".")

# Dangerous command patterns — reused from agent.tools
DANGEROUS_PATTERNS = [
    r"\brm\s+-rf\b",
    r"\bsudo\b",
    r"\bshutdown\b",
    r"\breboot\b",
    r">\s*/dev/",
    r"\bmknod\b",
    r"\bmkfs\b",
    r"\bdd\b",
    r"\bchattr\b",
]


async def async_run_bash(args: dict[str, Any]) -> str:
    """Execute a bash command asynchronously with security checks."""
    command = args.get("command", "")
    timeout = args.get("timeout", 30)

    # Security checks
    command_lower = command.lower()
    for pattern in DANGEROUS_PATTERNS:
        if re.search(pattern, command_lower):
            logger.warning("Dangerous command blocked: %s (matched %s)", command[:100], pattern)
            return "Error: Dangerous command blocked"

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=sandbox.get_working_dir(),
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            logger.warning(
                "Async command timed out after %ds, killing process: %s", timeout, command[:100]
            )
            proc.kill()
            return f"Error: Command timed out after {timeout} seconds"

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        output = stdout
        if stderr:
            output += f"\nSTDERR: {stderr}"
        if not output.strip():
            output = f"(Command completed with exit code {proc.returncode})"
        logger.info(
            "bash: command=%.500s, exit_code=%d, output_len=%d",
            command,
            proc.returncode,
            len(output),
        )
        return output
    except Exception as e:
        logger.error("Async command failed: %s — %s", command[:100], e, exc_info=True)
        return f"Error: {e}"


async def async_run_read_file(args: dict[str, Any]) -> str:
    """Read a file using a thread pool (blocking I/O offloaded)."""
    return await asyncio.to_thread(run_read_file, args)


async def async_run_write_file(args: dict[str, Any]) -> str:
    """Write a file using a thread pool."""
    return await asyncio.to_thread(run_write_file, args)


async def async_run_list_directory(args: dict[str, Any]) -> str:
    """List directory contents using a thread pool."""
    return await asyncio.to_thread(run_list_directory, args)


async def async_run_create_directory(args: dict[str, Any]) -> str:
    """Create a directory using a thread pool."""
    return await asyncio.to_thread(run_create_directory, args)


async def async_run_file_exists(args: dict[str, Any]) -> str:
    """Check file existence using a thread pool."""
    return await asyncio.to_thread(run_file_exists, args)


ASYNC_ALL_TOOLS = [
    (BASH_TOOL_DEFINITION, async_run_bash),
    (READ_FILE_TOOL_DEFINITION, async_run_read_file),
    (WRITE_FILE_TOOL_DEFINITION, async_run_write_file),
    (LIST_DIRECTORY_TOOL_DEFINITION, async_run_list_directory),
    (CREATE_DIRECTORY_TOOL_DEFINITION, async_run_create_directory),
    (FILE_EXISTS_TOOL_DEFINITION, async_run_file_exists),
]
