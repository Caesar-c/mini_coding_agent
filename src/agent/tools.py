import json
import subprocess
import re
from typing import Dict, Any, Optional
from .path_sandbox import PathSandbox

# Create a global sandbox instance with current directory as root
sandbox = PathSandbox(root_dir=".")

def run_bash(args: Dict[str, Any]) -> str:
    """
    Execute a bash command with enhanced security checks.
    Args should contain 'command' key.
    """
    command = args.get('command', '')
    timeout = args.get('timeout', 30)

    # Enhanced security checks with regex patterns
    dangerous_patterns = [
        r"\brm\s+-rf\b",  # Case insensitive rm -rf
        r"\bsudo\b",      # sudo
        r"\bshutdown\b",  # shutdown
        r"\breboot\b",    # reboot
        r">\s*/dev/",     # Redirecting to device files
        r"\bmknod\b",     # Creating device nodes
        r"\bmkfs\b",      # Creating filesystems
        r"\bdd\b",        # Direct disk access
        r"\bchattr\b",    # Changing file attributes (could lock files)
    ]

    command_lower = command.lower()
    for pattern in dangerous_patterns:
        if re.search(pattern, command_lower):
            return "Error: Dangerous command blocked"

    # Execute command in the sandboxed directory
    try:
        # Use older subprocess API for compatibility with Python 3.6
        result = subprocess.run(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,  # This is the Python 3.6 equivalent of text=True
            timeout=timeout,
            cwd=sandbox.get_working_dir(),  # Constrain working directory
        )
        output = result.stdout
        if result.stderr:
            output += f"\nSTDERR: {result.stderr}"
        if not output.strip():
            output = f"(Command completed with exit code {result.returncode})"
        return output
    except subprocess.TimeoutExpired:
        return f"Error: Command timed out after {timeout} seconds"
    except Exception as e:
        return f"Error: {str(e)}"

# Tool definitions
BASH_TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": "Execute a shell command in bash. Use for running scripts, installing packages. Use other tools for file operations.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute"
                }
            },
            "required": ["command"]
        }
    }
}

READ_FILE_TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read the contents of a file from the sandboxed directory.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The relative path to the file to read (relative to sandbox root)"
                }
            },
            "required": ["path"]
        }
    }
}

WRITE_FILE_TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "Write content to a file in the sandboxed directory.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The relative path to the file to write (relative to sandbox root)"
                },
                "content": {
                    "type": "string",
                    "description": "The content to write to the file"
                }
            },
            "required": ["path", "content"]
        }
    }
}

LIST_DIRECTORY_TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "list_directory",
        "description": "List files in a directory from the sandboxed directory.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The relative path to the directory to list (relative to sandbox root), default is '.'"
                },
                "recursive": {
                    "type": "boolean",
                    "description": "Whether to list files recursively, default is false",
                    "default": False
                }
            }
        }
    }
}

CREATE_DIRECTORY_TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "create_directory",
        "description": "Create a directory in the sandboxed directory.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The relative path to the directory to create (relative to sandbox root)"
                },
                "parents": {
                    "type": "boolean",
                    "description": "Whether to create parent directories if they don't exist, default is true",
                    "default": True
                }
            }
        }
    }
}

FILE_EXISTS_TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "file_exists",
        "description": "Check if a file exists in the sandboxed directory.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The relative path to the file to check (relative to sandbox root)"
                }
            },
            "required": ["path"]
        }
    }
}

# Execution functions
def run_read_file(args: Dict[str, Any]) -> str:
    try:
        path = args["path"]
        content = sandbox.read_file(path)
        return content
    except Exception as e:
        return f"Error reading file: {str(e)}"

def run_write_file(args: Dict[str, Any]) -> str:
    try:
        path = args["path"]
        content = args["content"]
        sandbox.write_file(path, content)
        return f"Successfully wrote {len(content)} characters to {path}"
    except Exception as e:
        return f"Error writing file: {str(e)}"

def run_list_directory(args: Dict[str, Any]) -> str:
    try:
        path = args.get("path", ".")
        recursive = args.get("recursive", False)
        files = sandbox.list_directory(path, recursive)
        if not files:
            return f"No files found in directory: {path}"
        return "\n".join(files)
    except Exception as e:
        return f"Error listing directory: {str(e)}"

def run_create_directory(args: Dict[str, Any]) -> str:
    try:
        path = args["path"]
        parents = args.get("parents", True)
        sandbox.create_directory(path, parents)
        return f"Successfully created directory: {path}"
    except Exception as e:
        return f"Error creating directory: {str(e)}"

def run_file_exists(args: Dict[str, Any]) -> str:
    try:
        path = args["path"]
        exists = sandbox.file_exists(path)
        return f"File {path} {'exists' if exists else 'does not exist'}"
    except Exception as e:
        return f"Error checking file existence: {str(e)}"


# Export the main bash runner with its definition for backward compatibility
def bash_run(command: str, timeout: int = 30) -> str:
    """
    Legacy bash runner for backward compatibility.
    """
    return run_bash({'command': command, 'timeout': timeout})

TOOL_DEFINITION = BASH_TOOL_DEFINITION

# Export all tool definitions and runners for the registry
ALL_TOOLS = [
    (BASH_TOOL_DEFINITION, run_bash),
    (READ_FILE_TOOL_DEFINITION, run_read_file),
    (WRITE_FILE_TOOL_DEFINITION, run_write_file),
    (LIST_DIRECTORY_TOOL_DEFINITION, run_list_directory),
    (CREATE_DIRECTORY_TOOL_DEFINITION, run_create_directory),
    (FILE_EXISTS_TOOL_DEFINITION, run_file_exists),
]
