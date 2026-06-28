"""Bash tool: definition and execution."""

import subprocess

TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": "Execute a shell command in bash. Use for running scripts, installing packages, file operations, etc.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The bash command to execute.",
                }
            },
            "required": ["command"],
        },
    },
}


def run(command: str, timeout: int = 30) -> str:
    """Execute a bash command and return stdout+stderr."""
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += result.stderr
        if not output.strip():
            output = f"(exit code: {result.returncode})"
        return output.strip()
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {timeout}s"
    except Exception as e:
        return f"Error: {e}"
