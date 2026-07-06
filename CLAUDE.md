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

The core execution engine:

- **`async_loop.py`** — `AsyncAgent`: Agent loop with concurrent tool execution via `asyncio.gather` and `DisplayHandler` for Rich terminal output. This is the primary production path used by the CLI.
- **`subagent.py`** — Context-isolated subtask execution. The `task` tool spawns a subagent with a fresh `messages[]` list; only the final text summary is returned to the parent. Subagents cannot recursively spawn (no `task` tool in child registry).
- **`message_utils.py`** — Shared helpers (`response_to_dict`, `extract_tool_calls`, `parse_tool_call`, `tc_attr`) used by `async_loop` and `subagent`. Eliminates duplication of LLM response handling.
- **`tools.py`** / **`async_tools.py`** — Tool definitions (OpenAI function-calling JSON schemas) and handlers. `tools.py` has sync handlers; `async_tools.py` wraps them with `asyncio.to_thread` (file ops) or `create_subprocess_shell` (bash).
- **`async_tool_registry.py`** — Maps tool names to handlers. `AsyncToolRegistry` detects coroutine returns and awaits them transparently, allowing sync and async handlers to coexist. Accepts an `exclude` parameter to filter auto-registered tools.
- **`path_sandbox.py`** — `PathSandbox` enforces that all file operations stay inside a root directory via path resolution + `relative_to()` containment check.
- **`display.py`** — `DisplayHandler` protocol for terminal output rendering (Rich). `SilentDisplayHandler` is the no-op default.

**Adding a new tool:**
1. Add a `TOOL_DEFINITION` dict (OpenAI function-calling schema) and handler function in `tools.py`.
2. Append `(definition, handler)` to `ALL_TOOLS` in `tools.py`.
3. If the tool needs async execution, add an async wrapper in `async_tools.py` and append to `ASYNC_ALL_TOOLS`.
4. No changes to the agent loop or registry are needed — they auto-load from these lists.

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

### `src/context_manager/` — Task Graph & Context Compression

- `task_graph.py` — `TaskGraphManager` + 4 tools (`create_plan`, `update_task`, `add_task`, `get_plan`). DAG-based task planning with dependency tracking, cycle detection, and JSON persistence. Tasks auto-transition to `ready` when all dependencies are satisfied (`done` or `skipped`). Persistence is session-scoped via `session_id` and anchored to `PROJECT_ROOT`. `_inject_progress()` in the agent loop inserts formatted `[TASK PROGRESS]` summaries as system messages.
- `pipeline.py` — `ContextPipeline`: three-layer compression orchestrator. Drop-in replacement for `ContextCompactor`. Layers cascade cheapest-first: Layer 1 → Layer 2 → Layer 3.
- `micro_compressor.py` — Layer 1: per-message smart truncation with tool-specific strategies (`read_file` → head+tail lines, `bash` → stderr + stdout tail, `list_directory` → entry cap). Runs eagerly via `compress_tool_result()` in `_handle_tool_call`. Includes hard cap fallback for single-line large content.
- `meso_compressor.py` — Layer 2: groups consecutive tool-call + tool-result pairs in the middle section into `[SUMMARY]` prose. Rule-based by default (zero API cost); optional LLM mode via `CONTEXT_MESO_USE_LLM=true`. Parallel tool results matched by `tool_call_id`.
- `macro_compressor.py` — Layer 3: full context rebuild via LLM into `[CONTEXT SUMMARY]` (Completed Work / Current State / Key Decisions). Integrates with `TaskGraphManager` for task state. Falls back to system + first_user + recent on LLM failure. Only fires when Layer 2 wasn't enough.
- `context.py` — Legacy `ContextCompactor` (kept for backward compatibility, no longer used by agent loops).

### `src/skills/` — On-Demand Domain Knowledge Injection

Two-layer injection model: Layer 1 places skill names + one-line descriptions in the system prompt (~100 tokens/skill, always present). Layer 2 returns full skill content via `load_skill` tool result (~2000 tokens, only when LLM calls it).

- `loader.py` — `SkillLoader` scans `SKILL.md` files (YAML frontmatter + markdown body) from: custom dirs (`SKILL_DIRS` env, highest priority) → project `skills/` → user `~/.config/mini-agent/skills/`. Name collisions: last loaded wins, logged as warning.
- `skill_tool.py` — `LOAD_SKILL_TOOL_DEFINITION` + `make_load_skill_handler()` closure. Both parent agent and subagents can call `load_skill` (registered in both main and child registries).

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
- **`.env` is gitignored**; loaded via `python-dotenv` in `config.py`. Provider API keys follow `{PROVIDER_TYPE_VALUE_UPPER()}_API_KEY` convention. Relative paths in `.env` (e.g. `LOG_FILE`) are resolved against `PROJECT_ROOT` in `config.py`.
- **No external test runner needed** — all tests use `unittest`. Async tests use `asyncio.run()`.
- **Logging uses `get_logger(__name__)`** — never `print()` for diagnostic output. Log content values with `%.2000s` format to cap length.

## Cross-Cutting Patterns

- **LLM responses with `content=None`**: OpenAI returns `content=None` alongside `tool_calls`. Assistant messages with `tool_calls` **must** be appended to `messages[]` even when content is falsy — otherwise tool results become orphaned and the API rejects the next call. Guard all `msg.get("content")` access with `or ""` (not `get("content", "")` which fails when the key exists with `None` value).
- **Async LLM calls in agent loop**: `compact()` may invoke the LLM (Layer 3 macro compression). In `async_loop.py`, it's wrapped in `await asyncio.to_thread()` to avoid blocking the event loop.
- **Tool output compression chain**: `_handle_tool_call()` calls `context_pipeline.compress_tool_result(tool_name, output)` instead of the old prefix truncation. The pipeline's `compact()` also runs a defensive Layer 1 re-pass on any uncompressed tool messages. The old `MAX_TOOL_OUTPUT` setting is no longer read by the agent loop (kept for backward compat).
- **Progress injection cycle**: Each agent loop iteration calls `_inject_progress()` which removes old `[TASK PROGRESS]` system messages and inserts a fresh one at position 1. The `[SUMMARY]` prefix (Layer 2) and `[CONTEXT SUMMARY]` prefix (Layer 3) are **not** filtered by this — only `[TASK PROGRESS]` is.
- **Design docs live in `plan/`**: PRDs in `plan/prd/`, detailed designs in `plan/`. Each stage (s04 subagent, s05 skill loading, s06 context compression, s07 persistent task graph) has both a PRD and a design doc.
