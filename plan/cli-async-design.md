# 设计方案：CLI 终端 + 异步支持

## Context

当前项目是一个纯同步的 Python coding agent，使用裸 `input()`/`print()` 做交互式 REPL，无 CLI 框架、无异步代码、无正式打包配置。需要将其改造为：
1. **专业 CLI 工具**：Typer 框架 + Rich 终端渲染 + prompt_toolkit REPL
2. **全量异步**：Agent 循环、LLM 调用、工具执行均异步化，支持并发 tool call 和多会话
3. **可安装**：pyproject.toml 完整配置，`pip install` 后可用 `mini-agent` 命令

**前置修复**：`src/config.py` 缺失 `MAX_TOOL_OUTPUT`、`CONTEXT_MAX_MESSAGES`、`CONTEXT_KEEP_RECENT` 三个属性（上次 pre-commit stash 回滚导致），`Agent.__init__` 会 `AttributeError`。

## 依赖

新增依赖（添加到 `pyproject.toml [project.dependencies]`）：

| 包 | 用途 |
|---|---|
| `typer>=0.12` | CLI 框架 |
| `rich>=13.0` | 终端渲染（Markdown、Panel、Spinner、Table） |
| `prompt-toolkit>=3.0` | REPL 输入（历史记录、多行、自动补全） |
| `openai[async]` 已含 | `AsyncOpenAI` 客户端（openai 包自带） |

不需要额外安装 `httpx`（openai 包已依赖）。

## 架构总览

```
src/
├── config.py                     # 修复 + 新增 ASYNC_* 配置
├── cli/                          # 新建 — CLI 层
│   ├── __init__.py
│   ├── main.py                   # Typer app 入口 (mini-agent)
│   ├── commands/
│   │   ├── __init__.py
│   │   ├── chat.py               # mini-agent chat 命令
│   │   ├── run.py                # mini-agent run "task" 命令
│   │   └── config_cmd.py         # mini-agent config 命令
│   ├── display.py                # RichDisplayHandler (Rich 渲染)
│   └── repl.py                   # prompt_toolkit REPL
├── agent/
│   ├── loop.py                   # 保留同步 Agent（向后兼容）
│   ├── async_loop.py             # 新建 — AsyncAgent
│   ├── async_tools.py            # 新建 — 异步工具实现
│   ├── async_tool_registry.py    # 新建 — 异步 ToolRegistry
│   ├── tools.py                  # 保留
│   ├── tool_registry.py          # 保留
│   └── path_sandbox.py           # 保留
├── llm/
│   ├── interface.py              # 新增 AsyncLLMProvider ABC
│   ├── async_openai_provider.py  # 新建 — AsyncOpenAI
│   ├── async_zhipu_provider.py   # 新建 — httpx 异步调用
│   ├── async_factory.py          # 新建 — create_async_llm_provider()
│   ├── factory.py                # 保留
│   ├── openai_provider.py        # 保留
│   └── zhipu_provider.py         # 保留
├── context_manager/              # 不变
└── session/
    ├── __init__.py
    └── manager.py                # 新建 — SessionManager (多会话)
```

## 文件变更详设

### 1. `pyproject.toml` — 新增完整打包配置

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "mini-coding-agent"
version = "0.1.0"
description = "A minimal coding agent with pluggable LLM backends"
requires-python = ">=3.11"
dependencies = [
    "openai>=1.0",
    "python-dotenv>=1.0",
    "typer>=0.12",
    "rich>=13.0",
    "prompt-toolkit>=3.0",
]

[project.optional-dependencies]
zhipu = ["zhipuai>=2.0"]
dev = ["ruff", "pre-commit"]

[project.scripts]
mini-agent = "cli.main:app"

[tool.hatch.build.targets.wheel]
packages = ["src/cli", "src/agent", "src/llm", "src/context_manager", "src/session", "src/config.py"]
```

运行方式：`uv pip install -e .` 后可用 `mini-agent` 命令。

### 2. `src/config.py` — 修复缺失属性

在 `Settings` 类的 `# ---- Agent behaviour ----` 区域确认/新增：

```python
MAX_TOKENS: int = int(os.getenv("MAX_TOKENS", "4096"))
LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "openai").lower()
MAX_TOOL_OUTPUT: int = int(os.getenv("MAX_TOOL_OUTPUT", "8000"))
CONTEXT_MAX_MESSAGES: int = int(os.getenv("CONTEXT_MAX_MESSAGES", "40"))
CONTEXT_KEEP_RECENT: int = int(os.getenv("CONTEXT_KEEP_RECENT", "12"))
```

### 3. `src/llm/interface.py` — 新增 AsyncLLMProvider

```python
class AsyncLLMProvider(ABC):
    @abstractmethod
    async def chat_completion(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        **kwargs,
    ) -> Any:
        pass
```

与同步 `LLMProvider` 接口一致，仅方法变为 `async`。

### 4. `src/llm/async_openai_provider.py` — 新建

```python
class AsyncOpenAILLMProvider(AsyncLLMProvider):
    def __init__(self, api_key, base_url, model):
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.model = model

    async def chat_completion(self, messages, tools=None, ...):
        # 使用 self.client.chat.completions.create(...) — 天然异步
        # 支持 streaming（async for chunk in stream）
        # 返回 MessageWrapper
```

复用现有 `MessageWrapper` 和流式 tool_calls 合并逻辑（从 `openai_provider.py` 提取公共方法）。

### 5. `src/llm/async_zhipu_provider.py` — 新建

使用 `httpx.AsyncClient` 直接调用 Zhipu AI API（绕过同步 SDK）。与同步版本逻辑相同，HTTP 调用改为 async。

### 6. `src/llm/async_factory.py` — 新建

```python
def create_async_llm_provider(provider_type: LLMProviderType) -> AsyncLLMProvider:
    if provider_type == LLMProviderType.OPENAI:
        return AsyncOpenAILLMProvider(...)
    elif provider_type == LLMProviderType.ZHIPU_AI:
        return AsyncZhipuAILLMProvider(...)
```

### 7. `src/agent/async_tools.py` — 新建异步工具

```python
async def async_run_bash(args: dict) -> str:
    command = args.get("command", "")
    timeout = args.get("timeout", 30)
    # 安全检查（复用现有 dangerous_patterns）
    proc = await asyncio.create_subprocess_shell(
        command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=sandbox.get_working_dir(),
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return f"Error: Command timed out after {timeout} seconds"
    # 返回输出...

async def async_run_read_file(args: dict) -> str:
    return await asyncio.to_thread(run_read_file, args)  # 文件 I/O 用线程池

# ... 其他文件操作同理，用 asyncio.to_thread 包装同步版本

ASYNC_ALL_TOOLS = [
    (BASH_TOOL_DEFINITION, async_run_bash),
    (READ_FILE_TOOL_DEFINITION, async_run_read_file),
    # ...
]
```

`bash` 用 `create_subprocess_shell`（真正异步），文件操作用 `asyncio.to_thread`（线程池，因为底层是同步文件 I/O）。

### 8. `src/agent/async_tool_registry.py` — 新建

```python
class AsyncToolRegistry:
    async def execute(self, tool_name: str, args: dict) -> str:
        handler = self._handlers[tool_name]
        result = handler(args)
        if asyncio.iscoroutine(result):
            return await result
        return result  # 兼容同步 handler（如 update_plan）
```

关键设计：`execute` 自动检测 handler 是同步还是异步，都支持。这样 `update_plan`（同步 handler）可以直接注册到 AsyncToolRegistry 中。

### 9. `src/agent/async_loop.py` — 新建核心 AsyncAgent

```python
class AsyncAgent:
    def __init__(self, provider_type=None, display=None):
        self.llm_provider = create_async_llm_provider(...)
        self.tool_registry = AsyncToolRegistry()
        self.progress_tracker = ProgressTracker()
        self.context_compactor = ContextCompactor(...)
        self.display = display or SilentDisplayHandler()
        # 注册所有异步工具 + 同步的 update_plan
        ...

    async def _call_llm(self):
        self.display.on_llm_start()  # 显示 Spinner
        response = await self.llm_provider.chat_completion(...)
        self.display.on_llm_end()    # 隐藏 Spinner
        return response

    async def _handle_tool_call(self, tool_call):
        output = await self.tool_registry.execute(tool_name, args)
        # 截断 + 显示
        self.display.on_tool_call(tool_name, args, output)
        return {"role": "tool", "tool_call_id": ..., "content": output}

    async def chat(self, user_input: str) -> str:
        self.messages.append({"role": "user", "content": user_input})
        while True:
            self._inject_progress()
            if self.context_compactor.should_compact(self.messages):
                self.messages = self.context_compactor.compact(self.messages)
            message = await self._call_llm()
            # ... 解析 assistant message
            if not tool_calls:
                self.display.on_response(content)
                return content
            # ★ 并发执行所有 tool calls
            results = await asyncio.gather(
                *[self._handle_tool_call(tc) for tc in tool_calls]
            )
            self.messages.extend(results)
```

**并发 tool 执行**：当 LLM 返回多个 tool_calls 时，用 `asyncio.gather` 并发执行，而非串行。

### 10. `src/agent/display.py` — DisplayHandler 协议

```python
from typing import Protocol

class DisplayHandler(Protocol):
    def on_llm_start(self) -> None: ...       # Spinner 开始
    def on_llm_end(self) -> None: ...         # Spinner 结束
    def on_tool_call(self, name, args, output) -> None: ...  # 工具执行
    def on_response(self, content: str) -> None: ...  # 最终响应（Markdown 渲染）
    def on_progress(self, summary: str) -> None: ...  # 进度更新

class SilentDisplayHandler:
    """No-op handler for testing and non-interactive use."""
    def on_llm_start(self): pass
    def on_llm_end(self): pass
    # ...
```

`AsyncAgent` 依赖 `DisplayHandler` 协议，与 Rich 解耦。CLI 层传入 `RichDisplayHandler`，测试用 `SilentDisplayHandler`。

### 11. `src/cli/display.py` — RichDisplayHandler

```python
class RichDisplayHandler:
    def __init__(self, console=None):
        self.console = console or Console()
        self._spinner = None

    def on_llm_start(self):
        self._spinner = self.console.status("[bold cyan]Thinking...")
        self._spinner.start()

    def on_llm_end(self):
        if self._spinner:
            self._spinner.stop()

    def on_tool_call(self, name, args, output):
        self.console.print(Panel(
            output[:500],
            title=f"🔧 {name}: {str(args)[:80]}",
            border_style="yellow",
        ))

    def on_response(self, content):
        self.console.print(Markdown(content))

    def on_progress(self, summary):
        # 渲染 [TASK PROGRESS] 为 Table
        ...
```

### 12. `src/cli/repl.py` — prompt_toolkit REPL

```python
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.completion import WordCompleter

SLASH_COMMANDS = ["/quit", "/clear", "/plan", "/reset", "/new", "/switch",
                  "/sessions", "/config", "/help", "/compact", "/history", "/tools"]

async def run_repl(agent: AsyncAgent, display: RichDisplayHandler):
    session = PromptSession(
        history=FileHistory(".mini_agent_history"),
        completer=WordCompleter(SLASH_COMMANDS),
        multiline=False,
    )
    while True:
        user_input = await asyncio.to_thread(session.prompt, "You> ")
        if user_input.startswith("/"):
            handle_slash_command(user_input, agent, display)
        else:
            response = await agent.chat(user_input)
```

`prompt_toolkit` 的 `prompt()` 是阻塞的，用 `asyncio.to_thread` 包装以保持事件循环畅通（Spinner 能继续转动）。

斜杠命令：
- `/quit` — 退出
- `/clear` — 清空对话历史
- `/plan` — 显示当前计划
- `/reset` — 重置计划
- `/new [name]` — 创建新会话
- `/switch <name>` — 切换会话
- `/sessions` — 列出所有会话
- `/config` — 显示当前配置
- `/compact` — 手动触发上下文压缩
- `/history` — 显示消息数量统计
- `/tools` — 列出可用工具
- `/help` — 帮助

### 13. `src/cli/main.py` — Typer 入口

```python
import typer
app = typer.Typer(name="mini-agent", help="Mini Coding Agent CLI")

@app.command()
def chat(
    provider: str = typer.Option(None, "--provider", "-p", help="LLM provider"),
    model: str = typer.Option(None, "--model", "-m", help="Model name"),
    sandbox: str = typer.Option(".", "--sandbox", "-s", help="Sandbox root directory"),
    max_tokens: int = typer.Option(None, "--max-tokens", help="Max output tokens"),
):
    """Start an interactive chat session."""
    asyncio.run(_async_chat(provider, model, sandbox, max_tokens))

@app.command()
def run(
    task: str = typer.Argument(..., help="Task description"),
    provider: str = typer.Option(None, "--provider", "-p"),
    model: str = typer.Option(None, "--model", "-m"),
    sandbox: str = typer.Option(".", "--sandbox", "-s"),
):
    """Execute a single task non-interactively."""
    asyncio.run(_async_run(task, provider, model, sandbox))

@app.command()
def config(
    show: bool = typer.Option(True, "--show", help="Show current configuration"),
    set_key: str = typer.Option(None, "--set", help="Set a config value (KEY=VALUE)"),
):
    """Show or modify configuration."""
    ...
```

安装后通过 `mini-agent chat`、`mini-agent run "refactor auth module"` 调用。

### 14. `src/cli/commands/chat.py` — chat 命令实现

```python
async def _async_chat(provider, model, sandbox, max_tokens):
    display = RichDisplayHandler()
    agent = AsyncAgent(provider_type=..., display=display)
    # 显示启动 Panel
    display.console.print(Panel(
        f"Provider: {provider}\nModel: {model}\nSandbox: {sandbox}",
        title="🤖 Mini Coding Agent",
        border_style="blue",
    ))
    await run_repl(agent, display)
```

### 15. `src/cli/commands/run.py` — run 命令实现

```python
async def _async_run(task, provider, model, sandbox):
    display = RichDisplayHandler()
    agent = AsyncAgent(provider_type=..., display=display)
    response = await agent.chat(task)
    # response 已通过 display.on_response 渲染
```

### 16. `src/session/manager.py` — 多会话管理

```python
class SessionManager:
    def __init__(self):
        self._sessions: dict[str, AsyncAgent] = {}
        self._active: str | None = None

    def create(self, name: str, **kwargs) -> AsyncAgent:
        agent = AsyncAgent(**kwargs)
        self._sessions[name] = agent
        self._active = name
        return agent

    def switch(self, name: str) -> AsyncAgent:
        self._active = name
        return self._sessions[name]

    def list_sessions(self) -> list[str]:
        return list(self._sessions.keys())

    @property
    def active(self) -> AsyncAgent:
        return self._sessions[self._active]
```

### 17. 测试

| 测试文件 | 测试内容 |
|---|---|
| `tests/agent/test_async_loop.py` | AsyncAgent.chat() 基础流程、并发 tool 执行 |
| `tests/agent/test_async_tools.py` | async_run_bash 超时/输出、async_run_read_file |
| `tests/agent/test_async_tool_registry.py` | 同步/异步 handler 混合执行 |
| `tests/llm/test_async_openai.py` | AsyncOpenAILLMProvider mock 测试 |
| `tests/llm/test_async_factory.py` | create_async_llm_provider 工厂 |
| `tests/session/test_manager.py` | SessionManager CRUD |
| `tests/cli/test_commands.py` | Typer CliRunner 测试 chat/run/config |

异步测试使用 `unittest` + `asyncio.run()`：
```python
class TestAsyncAgent(unittest.TestCase):
    def test_chat(self):
        asyncio.run(self._test_chat())
    async def _test_chat(self):
        agent = AsyncAgent(display=SilentDisplayHandler())
        ...
```

## 实现顺序

按依赖图分 6 个阶段：

**阶段 1：基础设施**（无外部依赖）
1. 修复 `src/config.py` — 补回 3 个缺失属性
2. 更新 `pyproject.toml` — 添加 [project]、[build-system]、dependencies
3. 创建 `src/cli/__init__.py`、`src/cli/commands/__init__.py`、`src/session/__init__.py` — 包结构

**阶段 2：异步 LLM 层**
4. `src/llm/interface.py` — 新增 AsyncLLMProvider ABC
5. `src/llm/async_openai_provider.py` — AsyncOpenAI 实现
6. `src/llm/async_zhipu_provider.py` — httpx 异步实现
7. `src/llm/async_factory.py` — 异步工厂

**阶段 3：异步 Agent 层**
8. `src/agent/display.py` — DisplayHandler 协议 + SilentDisplayHandler
9. `src/agent/async_tools.py` — 异步工具（bash 用 subprocess、文件 I/O 用 to_thread）
10. `src/agent/async_tool_registry.py` — 异步注册表
11. `src/agent/async_loop.py` — AsyncAgent（核心）

**阶段 4：多会话**
12. `src/session/manager.py` — SessionManager

**阶段 5：CLI 层**
13. `src/cli/display.py` — RichDisplayHandler
14. `src/cli/repl.py` — prompt_toolkit REPL
15. `src/cli/commands/chat.py` — chat 命令
16. `src/cli/commands/run.py` — run 命令
17. `src/cli/commands/config_cmd.py` — config 命令
18. `src/cli/main.py` — Typer 入口

**阶段 6：测试 + 验证**
19. 全部测试文件

## 验证方式

1. **安装验证**：`uv pip install -e .` → `mini-agent --help` 显示命令列表
2. **单元测试**：`PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -t . -v`
3. **chat 命令**：`mini-agent chat --provider openai` → 进入 REPL，发送消息，看到 Rich 渲染
4. **run 命令**：`mini-agent run "列出当前目录文件"` → 单次执行，输出结果
5. **多会话**：REPL 中 `/new task2` → `/sessions` 列出 → `/switch task2` 切换
6. **并发 tool**：让 LLM 同时调用多个 `read_file`，观察是否并行执行
7. **pre-commit**：`.venv/bin/pre-commit run --all-files` 全部通过
