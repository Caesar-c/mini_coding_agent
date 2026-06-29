# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A minimal Python coding agent that interacts with LLM APIs (via OpenAI-compatible protocol) and executes tool calls (bash, file ops) in a sandboxed environment. Built as a research codebase under `src/`.

## Common Commands

```bash
# Run the agent REPL (from project root)
python -m src.agent.loop

# Run all tests (stdlib unittest, no pytest required)
python -m unittest discover -s tests -v

# Run a single test module
python -m unittest tests.llm.test_factory

# Run a single test case
python -m unittest tests.llm.test_factory.TestCreateLLMProvider.test_create_openai_provider

# Environment setup
cp .env.example .env   # then fill in API keys
```

There is no build step. Linting and formatting are handled by pre-commit (see below). The project runs directly from source.

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

The code lives under `src/` and is split into two top-level Python packages:

### `src/llm/` — Pluggable LLM Provider System

Decouples the agent loop from any specific LLM vendor. Public API is re-exported via `src/llm/__init__.py`:

- `LLMProvider` (abstract base in `interface.py`) — the contract every provider must implement via `chat_completion(messages, tools, model, max_tokens, temperature, **kwargs)`.
- `MessageWrapper` — normalises heterogeneous provider responses into a uniform `{content, role, tool_calls}` shape so the agent loop never has to care which provider replied.
- `LLMProviderType` (enum in `factory.py`) + `create_llm_provider()` — the single point of instantiation.

**Adding a new provider** (the most common extension point):
1. Create `src/llm/<provider>_provider.py` with a class inheriting `LLMProvider`.
2. Add an enum value to `LLMProviderType`.
3. Add an `elif` branch in `create_llm_provider()`.
4. Optionally re-export the new class from `src/llm/__init__.py`.

Currently implemented: `OpenAILLMProvider` (also handles legacy `openai` v0.x), `ZhipuAILLMProvider` (lazy-imports `zhipuai` SDK — not a hard dependency).

### `src/agent/` — Agent Loop & Tool System

- `loop.py` — `Agent` class owns the ReAct-style loop: send messages → if response has `tool_calls`, execute each via `ToolRegistry`, feed results back, loop until the LLM returns text. The loop is provider-agnostic; it just calls `llm_provider.chat_completion(...)` and `tool_registry.execute(...)`.
- `tool_registry.py` — `ToolRegistry` loads all `(definition, handler)` pairs from `tools.ALL_TOOLS` at init. Dispatch is by tool name. Adding a tool means appending a new tuple to `ALL_TOOLS` in `tools.py` — no changes to the loop required.
- `tools.py` — Defines tool JSON schemas (OpenAI function-calling format) and their handler functions. Each handler takes an `args: dict` and returns a `str`.
- `path_sandbox.py` — `PathSandbox` enforces that all file operations stay inside a root directory via path resolution + `relative_to()` containment check. Used by `read_file`, `write_file`, `list_directory`, `create_directory`, `file_exists` tools and constrains bash `cwd`.

## Key Conventions

- **All imports must be absolute** — `from src.agent.tools import X`, `from src.llm import Y`. Relative imports (`from .foo`, `from ..bar`) are banned by ruff rule `TID252` (`ban-relative-imports = "all"` in `pyproject.toml`) and will fail `git commit`. When adding new modules, always use the `src.<package>.<module>` form.
- **Tests mirror source structure**: `tests/llm/` tests `src/llm/`, `tests/agent/` tests `src/agent/`. Test imports use the same `from src.X import ...` form as source code.
- **`.env` is gitignored**; `loop.py` loads it via `python-dotenv`. Provider API keys are looked up as `{PROVIDER_TYPE_VALUE_UPPER()}_API_KEY` (e.g. `OPENAI_API_KEY`, `ZHIPU_AI_API_KEY`) or via the provider-specific env vars.
- **No external test runner needed** — all tests use `unittest`. If pytest gets added later, existing tests are already compatible.
