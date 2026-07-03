# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A minimal Python coding agent that interacts with LLM APIs (via OpenAI-compatible protocol) and executes tool calls (bash, file ops) in a sandboxed environment. Built as a research codebase under `src/`.

## Common Commands

```bash
# Run the CLI (Typer-based, the primary entry point)
PYTHONPATH=src .venv/bin/python -m cli.main chat          # interactive REPL
PYTHONPATH=src .venv/bin/python -m cli.main run "task"    # one-shot execution
# Or if installed: mini-agent chat / mini-agent run "task"

# Run the minimal sync REPL (no CLI framework)
PYTHONPATH=src .venv/bin/python -m agent.loop

# Run all tests (stdlib unittest, no pytest required)
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -t . -v

# Run a single test module
PYTHONPATH=src .venv/bin/python -m unittest tests.agent.test_subagent -v

# Run a single test case
PYTHONPATH=src .venv/bin/python -m unittest tests.agent.test_subagent.TestSubagentReturnsSummary.test_returns_summary

# Environment setup
cp .env.example .env   # then fill in API keys
```

There is no build step. Linting and formatting are handled by pre-commit (see below). The project runs directly from source with `PYTHONPATH=src`.

## Pre-commit Hooks

Pre-commit runs on every `git commit` and enforces code style. Hooks are defined in `.pre-commit-config.yaml`:

- **ruff** (lint + auto-fix): runs `ruff check --fix`. Catches import violations, bug patterns, etc.
- **ruff-format**: deterministic code formatter (replaces black + isort).
- **pre-commit-hooks**: trailing whitespace, end-of-file newlines, YAML validity, large-file guard, merge-conflict markers.

Setup (one-time):
```bash
uv pip install pre-commit ruff
.venv/bin/pre-commit install
```

Run manually on all files: `.venv/bin/pre-commit run --all-files`.

## High-Level Architecture

The code lives under `src/` and is organized into these packages:

### `src/agent/` — Agent Loop, Tool System & Subagent

The core execution engine. Two agent variants share the same tool infrastructure:

- **`loop.py`** — `Agent` (sync): ReAct-style loop with sequential tool execution. Used for the minimal REPL.
- **`async_loop.py`** — `AsyncAgent` (async): Same loop with concurrent tool execution via `asyncio.gather` and `DisplayHandler` for Rich terminal output. **This is the primary production path** used by the CLI.
- **`subagent.py`** — Context-isolated subtask execution. The `task` tool spawns a subagent with a fresh `messages[]` list; only the final text summary is returned to the parent. Subagents cannot recursively spawn (no `task` tool in child registry).
- **`message_utils.py`** — Shared helpers (`response_to_dict`, `extract_tool_calls`, `parse_tool_call`, `tc_attr`) used by all three agent variants. Eliminates 3-way duplication of LLM response handling.
- **`tools.py`** / **`async_tools.py`** — Tool definitions (OpenAI function-calling JSON schemas) and handlers. `tools.py` has sync handlers; `async_tools.py` wraps them with `asyncio.to_thread` (file ops) or `create_subprocess_shell` (bash).
- **`tool_registry.py`** / **`async_tool_registry.py`** — Maps tool names to handlers. `AsyncToolRegistry` detects coroutine returns and awaits them transparently, allowing sync and async handlers to coexist. Accepts an `exclude` parameter to filter auto-registered tools.
- **`path_sandbox.py`** — `PathSandbox` enforces that all file operations stay inside a root directory via path resolution + `relative_to()` containment check.
- **`display.py`** — `DisplayHandler` protocol for terminal output rendering (Rich). `SilentDisplayHandler` is the no-op default.

**Adding a new tool:**
1. Add a `TOOL_DEFINITION` dict (OpenAI function-calling schema) and handler function in `tools.py`.
2. Append `(definition, handler)` to `ALL_TOOLS` in `tools.py`.
3. If the tool needs async execution, add an async wrapper in `async_tools.py` and append to `ASYNC_ALL_TOOLS`.
4. No changes to the agent loops or registries are needed — they auto-load from these lists.

**Adding a new provider:**
1. Create `src/llm/<provider>_provider.py` with a class inheriting `LLMProvider`.
2. Add an enum value to `LLMProviderType` in `factory.py`.
3. Add an `elif` branch in `create_llm_provider()`.
4. Optionally re-export from `src/llm/__init__.py`.

### `src/llm/` — Pluggable LLM Provider System

Decouples the agent loop from any specific LLM vendor:

- `LLMProvider` (abstract base in `interface.py`) — contract via `chat_completion(messages, tools, model, max_tokens, temperature, **kwargs)`.
- `MessageWrapper` — normalises provider responses into `{content, role, tool_calls, reasoning_content}`.
- `LLMProviderType` (enum) + `create_llm_provider()` — single instantiation point.

Currently implemented: `OpenAILLMProvider`, `ZhipuAILLMProvider` (lazy-imports `zhipuai` SDK).

### `src/context_manager/` — Progress Tracking & Context Compression

- `tracker.py` — `ProgressTracker` + `update_plan` tool. Tracks multi-step task plans with step statuses. `_inject_progress()` in the agent loops inserts formatted summaries as system messages.
- `context.py` — `ContextCompactor` performs rule-based message pruning when `messages[]` exceeds a threshold. Preserves head (system + first user) and tail (recent messages), compacts middle tool results.

### `src/session/` — Multi-Session Management

`SessionManager` manages multiple named `AsyncAgent` instances, each with its own conversation history and state. Used by the CLI for session switching.

### `src/logger/` — Centralized Logging

`get_logger(name)` returns a standard Python logger. `setup_logging()` configures a `RotatingFileHandler` (auto-called on first `get_logger()` use). Logs go to `logs/mini_agent.log` by default — no console output (reserved for Rich display).

### `src/cli/` — Typer CLI Application

Entry point: `cli.main:app`. Commands: `chat` (interactive), `run` (one-shot), `config` (show settings). Uses `prompt-toolkit` for input and Rich for rendering.

### `src/config.py` — Settings Singleton

`Settings` class loads from environment / `.env` file. Singleton `settings` is imported directly. All config values follow the pattern: `ATTR: type = type(os.getenv("ATTR", "default"))`.

## Key Conventions

- **All imports must be absolute** — `from agent.tools import X`, `from llm import Y`. Relative imports (`from .foo`, `from ..bar`) are banned by ruff rule `TID252` (`ban-relative-imports = "all"`) and will fail `git commit`. The `src/` directory is a ruff source root, so imports use the package name directly (no `src.` prefix).
- **Tests mirror source structure**: `tests/llm/` tests `src/llm/`, `tests/agent/` tests `src/agent/`. All tests require `PYTHONPATH=src`.
- **`.env` is gitignored**; loaded via `python-dotenv` in `config.py`. Provider API keys follow `{PROVIDER_TYPE_VALUE_UPPER()}_API_KEY` convention.
- **No external test runner needed** — all tests use `unittest`. Async tests use `asyncio.run()`.
- **Logging uses `get_logger(__name__)`** — never `print()` for diagnostic output. Log content values with `%.2000s` format to cap length.
