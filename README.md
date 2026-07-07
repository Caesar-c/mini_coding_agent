# Mini Coding Agent

A minimal Python coding agent that interacts with LLM APIs and executes tool calls (bash, file operations) in a sandboxed environment.

## Features

- 🤖 **Interactive & One-shot modes** — REPL chat or single-task execution
- 🔌 **Pluggable LLM backends** — OpenAI, Zhipu AI (extensible to any OpenAI-compatible API)
- 🛡️ **Sandboxed execution** — file operations constrained to project root
- 📋 **Task planning** — DAG-based task graph with dependency tracking
- 🗜️ **Context compression** — three-layer pipeline to stay within token limits
- 🧩 **Skill system** — on-demand domain knowledge injection

## Installation

### From PyPI

```bash
pip install mini-coding-agent
```

### From source

```bash
git clone https://github.com/yourname/mini-coding-agent.git
cd mini-coding-agent
pip install .
```

### Standalone binary (no Python required)

Download the pre-built binary for your platform from the [Releases page](https://github.com/yourname/mini-coding-agent/releases).

## Configuration

Create a `.env` file in your project directory:

```bash
OPENAI_API_KEY=sk-your-key-here
# Or for Zhipu AI:
# PROVIDER_TYPE=zhipuai
# ZHIPUAI_API_KEY=your-key
```

## Usage

```bash
# Interactive REPL
mini-agent chat

# One-shot execution
mini-agent run "创建一个 Python HTTP 服务器"

# Show current config
mini-agent config
```

## Requirements

- Python ≥ 3.11
- An API key for a supported LLM provider

## License

MIT
