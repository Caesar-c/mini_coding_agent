# 设计方案：Subagent — 上下文隔离的子任务执行

## Context

当前 `AsyncAgent` 采用单一 `messages[]` 管理全部对话历史。每次工具调用的输入和输出都永久保留在 messages 中，导致上下文持续膨胀。即使有 `ContextCompactor` 做规则裁剪，也只是事后补救——大量无用信息已经消耗了 token 配额，LLM 的注意力被无关上下文稀释。

**核心问题**：一个 "读 5 个文件总结项目结构" 的任务，父 Agent 的 messages 里会塞满所有文件的完整内容（15KB+），后续每一轮 LLM 调用都要付费处理这些中间结果。

**解决方案**：引入 `task` 工具，让父 Agent 可以派生 Subagent。Subagent 拥有独立的 `messages[]`，执行完毕后将所有中间 messages 丢弃，仅向父 Agent 返回一条文本摘要（≤2000 字符）。

**前置依赖**：s03（工具系统 `AsyncToolRegistry`）、s03（进度追踪 `ProgressTracker` + 上下文压缩 `ContextCompactor`）。

## 方案概览

| 层 | 组件 | 职责 | 复杂度 |
|---|---|---|---|
| 1 | `subagent.py` — `run_subagent()` | Subagent 执行循环（独立 messages、独立 LLM 调用） | ~100 行新模块 |
| 2 | `subagent.py` — `TASK_TOOL_DEFINITION` | `task` 工具定义 + handler 闭包 | ~30 行 |
| 3 | `async_tool_registry.py` — `exclude` 参数 | 支持创建不含指定工具的 registry | ~5 行改动 |
| 4 | `async_loop.py` — `_child_registry` | 父 Agent 中构建子 registry + 注册 task 工具 | ~15 行改动 |
| 5 | `config.py` — 2 个新配置项 | `SUBAGENT_MAX_ITERATIONS`、`SUBAGENT_MAX_OUTPUT` | ~3 行改动 |

### 设计决策

| # | 决策 | 选择 | 理由 |
|---|---|---|---|
| D1 | 子 registry 构建方式 | `AsyncToolRegistry` 新增 `exclude` 参数 | 最简洁，复用已有自动注册逻辑，无需手动构建第二个 registry |
| D2 | `tc_attr` 的位置 | 保留在 `loop.py`，`subagent.py` 从 `loop.py` 导入 | `async_loop.py` 已从此处导入，10 行工具函数不值得单独提取 utils 模块 |
| D3 | PathSandbox | 父子共享同一个 sandbox | Subagent 操作的是同一项目目录，不需要隔离文件系统 |
| D4 | System Prompt | 在 `subagent.py` 中独立定义 `SUBAGENT_SYSTEM_PROMPT` | Subagent 的 system prompt 与父 Agent 差异较大（不含 update_plan 引导、强调自主执行和返回摘要） |
| D5 | 同步 Agent 支持 | 本迭代仅实现 AsyncAgent 版本 | CLI 主要走 `AsyncAgent` 路径；同步版本可作为后续迭代 |
| D6 | Subagent 内工具并发执行 | 不并发，顺序执行 | Subagent 本身是轻量级子任务，顺序执行更简单、日志更清晰；并发收益有限 |
| D7 | Subagent 的 ProgressTracker | 不注入 | Subagent 是短生命周期的子任务（≤30 轮），不需要进度追踪 |
| D8 | Subagent 的 ContextCompactor | 不注入 | 同上，30 轮上限足够，不需要压缩 |

## 架构总览

```
Parent AsyncAgent                              Subagent (run_subagent)
┌─────────────────────────────┐                ┌─────────────────────────────┐
│ messages = [sys, user, …]   │                │ messages = [sys, user]      │ ← fresh
│                             │    dispatch    │                             │
│ tool_registry:              │ ────────────> │ child_registry:             │
│   bash, read_file, ...      │   (task call)  │   bash, read_file, ...     │
│   update_plan               │                │   (无 task, 无 update_plan) │
│   task ← 仅父端可见         │                │                             │
│                             │    summary     │ while tool_calls:           │
│ result = "摘要文本"         │ <──────────── │   execute tool → append     │
│                             │                │ return last_text            │
└─────────────────────────────┘                └─────────────────────────────┘
     上下文保持干净                                  上下文用完即弃
```

**工具集划分**：

```python
# 基础工具 — 父子共享（CHILD_TOOLS）
CHILD_TOOL_NAMES = ["bash", "read_file", "write_file",
                    "list_directory", "create_directory", "file_exists"]

# 父 Agent 独有
PARENT_ONLY_TOOLS = ["task", "update_plan"]

# 父 Agent 工具集 = CHILD + task + update_plan
# Subagent 工具集 = CHILD only（禁止递归派生，无进度管理）
```

## 文件变更详设

### 1. `src/config.py` — 新增 2 个配置项

在 `Settings` 类的 `# ---- Agent behaviour ----` 区域末尾新增：

```python
# ---- Subagent ----
SUBAGENT_MAX_ITERATIONS: int = int(os.getenv("SUBAGENT_MAX_ITERATIONS", "30"))
SUBAGENT_MAX_OUTPUT: int = int(os.getenv("SUBAGENT_MAX_OUTPUT", "2000"))
```

| 配置项 | 环境变量 | 默认值 | 说明 |
|---|---|---|---|
| `SUBAGENT_MAX_ITERATIONS` | `SUBAGENT_MAX_ITERATIONS` | 30 | Subagent 最大迭代次数（LLM 调用轮数） |
| `SUBAGENT_MAX_OUTPUT` | `SUBAGENT_MAX_OUTPUT` | 2000 | Subagent 返回给父 Agent 的摘要最大字符数 |

### 2. `src/agent/async_tool_registry.py` — 支持排除工具

在 `__init__` 中新增 `exclude` 参数，控制哪些工具不被自动注册：

```python
class AsyncToolRegistry:
    def __init__(self, exclude: list[str] | None = None):
        self._tools: dict[str, dict] = {}
        self._handlers: dict[str, Callable] = {}

        exclude_set = set(exclude or [])
        # Register all async tools, skipping excluded ones
        for definition, handler in ASYNC_ALL_TOOLS:
            name = definition["function"]["name"]
            if name not in exclude_set:
                self.register(definition, handler)
```

**改动范围**：仅 `__init__` 方法，新增 3 行。`register`、`execute`、`definitions`、`get_tool_names` 均不变。

**向后兼容**：`exclude` 默认为 `None`，现有调用 `AsyncToolRegistry()` 行为完全不变。

**本迭代的实际用法**：`_child_registry = AsyncToolRegistry()` 不传 `exclude`，因为 `ASYNC_ALL_TOOLS` 中的 6 个基础工具恰好就是子 agent 需要的全集——`update_plan` 和 `task` 是手动注册到主 registry 的，不在自动注册范围内。`exclude` 参数作为 API 预留，支持未来需要创建部分工具子集的场景。

### 3. `src/agent/subagent.py` — 新建核心模块

#### 3.1 模块结构

```python
"""Subagent — isolated sub-task execution with fresh context.

The parent agent dispatches a subagent via the ``task`` tool. The subagent
runs in its own messages list, executes child tools (bash, file ops), and
returns only a text summary. All intermediate messages are discarded.
"""

import asyncio
import json

from agent.async_tool_registry import AsyncToolRegistry
from agent.loop import tc_attr
from config import settings
from logger import get_logger

logger = get_logger(__name__)

# --- Constants ---
SUBAGENT_SYSTEM_PROMPT = "..."
TASK_TOOL_DEFINITION = { ... }

# --- Core function ---
async def run_subagent(...) -> str: ...

# --- Handler factory ---
def make_task_handler(agent) -> Callable: ...
```

#### 3.2 `SUBAGENT_SYSTEM_PROMPT`

```python
SUBAGENT_SYSTEM_PROMPT = """\
You are a subagent executing a specific task. You have access to bash \
commands and file operation tools. Complete the task thoroughly but \
concisely. Your final text response will be returned to the parent agent \
as a summary — make it clear and self-contained.

Rules:
- Do NOT ask the user questions — work autonomously.
- Return a concise summary of your findings or actions.
- If you encounter errors, report them clearly.
"""
```

**与父 Agent SYSTEM_PROMPT 的差异**：
- 不包含 `update_plan` 引导（Subagent 无此工具）
- 强调 "自主执行"（不向用户提问）
- 强调 "返回摘要"（最终文本会被截断并返回）

#### 3.3 `TASK_TOOL_DEFINITION`

```python
TASK_TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "task",
        "description": (
            "Spawn a subagent with fresh context to perform a subtask. "
            "The subagent has access to bash, file read/write, and directory "
            "tools but NOT the task tool (no recursive spawning). "
            "Only the subagent's final text summary is returned — all "
            "intermediate tool calls and outputs are discarded. "
            "Use this for multi-step research, file exploration, or any "
            "task that would clutter the main conversation context."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "The task description for the subagent to execute.",
                }
            },
            "required": ["prompt"],
        },
    },
}
```

#### 3.4 `run_subagent()` — 核心执行函数

```python
async def run_subagent(
    prompt: str,
    llm_provider,                    # LLMProvider 实例（复用父 Agent 的）
    child_tools: list[dict],         # 子工具定义列表（不含 task）
    child_registry: AsyncToolRegistry,  # 仅注册 CHILD_TOOLS 的 registry
    system_prompt: str,
    max_iterations: int = 30,
    max_output_chars: int = 2000,
) -> str:
    """派生一个 subagent，在独立上下文中执行任务，返回摘要文本。"""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt},
    ]

    logger.info("Subagent spawned: prompt_len=%d, max_iter=%d",
                len(prompt), max_iterations)

    last_text = ""
    for iteration in range(1, max_iterations + 1):
        response = await asyncio.to_thread(
            llm_provider.chat_completion,
            messages=messages,
            tools=child_tools,
        )

        # 记录 assistant 消息到子上下文
        if hasattr(response, "content") and response.content:
            if hasattr(response, "model_dump"):
                messages.append(response.model_dump(exclude_unset=True))
            else:
                msg_dict = {
                    "role": getattr(response, "role", "assistant"),
                    "content": response.content,
                }
                if hasattr(response, "tool_calls") and response.tool_calls:
                    msg_dict["tool_calls"] = response.tool_calls
                messages.append(msg_dict)

        # 提取 tool_calls
        if hasattr(response, "tool_calls"):
            tool_calls = response.tool_calls
        else:
            tool_calls = getattr(response, "data", {}).get("tool_calls", [])

        if not tool_calls:
            # 无工具调用 → subagent 完成，提取最终文本
            last_text = getattr(response, "content", "") or ""
            logger.info("Subagent finished: iterations=%d, result_len=%d",
                        iteration, len(last_text))
            break

        # 顺序执行工具调用（非并发，保持日志清晰）
        for tc in tool_calls:
            tool_name = tc_attr(tc, "function.name", "")
            raw_args = tc_attr(tc, "function.arguments", "{}")
            args = json.loads(raw_args) if raw_args else {}
            tc_id = tc_attr(tc, "id", "")

            logger.info("Subagent tool call: %s, args=%s",
                        tool_name, str(args)[:200])

            output = await child_registry.execute(tool_name, args)

            # 单条工具结果上限 50000 字符（防止单文件内容撑爆子上下文）
            output = output[:50000]

            messages.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "content": output,
            })

    else:
        # 达到最大迭代次数
        logger.warning("Subagent hit iteration limit: %d", max_iterations)
        last_text = last_text or "(subagent reached iteration limit without completing)"

    # 截断最终输出
    if len(last_text) > max_output_chars:
        last_text = last_text[:max_output_chars] + "\n... [truncated]"

    return last_text
```

**关键设计点**：

| 点 | 说明 |
|---|---|
| 独立 messages | `messages` 是局部变量，函数结束后被 GC 回收，不污染父 Agent |
| 复用 llm_provider | 不创建新的 LLM provider 实例，复用父 Agent 的（同一个 API key / model） |
| 顺序执行工具 | Subagent 工具调用按顺序执行（非 `asyncio.gather`），简化日志和错误处理 |
| `asyncio.to_thread` | LLM 调用是同步 API，用 `to_thread` 包装避免阻塞事件循环 |
| assistant 消息格式 | 兼容 `model_dump`（OpenAI v1.x）和 `MessageWrapper`（自定义包装）两种格式 |
| 工具结果上限 | 单条工具结果截断到 50000 字符（不同于父 Agent 的 `MAX_TOOL_OUTPUT=8000`，因为 subagent 需要读完整文件） |
| `for...else` | Python 的 `for-else` 语法：循环未被 `break` 终止时执行 `else` 分支（即耗尽所有迭代、达到上限） |

#### 3.5 `make_task_handler()` — Handler 闭包工厂

```python
def make_task_handler(agent):
    """创建 task handler 闭包，绑定到父 agent 的 provider 和配置。"""

    async def run_task(args: dict) -> str:
        prompt = args.get("prompt", "")
        if not prompt:
            return "Error: 'prompt' is required."

        logger.info("Task tool invoked: prompt_len=%d", len(prompt))

        result = await run_subagent(
            prompt=prompt,
            llm_provider=agent.llm_provider,
            child_tools=agent._child_registry.definitions,
            child_registry=agent._child_registry,
            system_prompt=SUBAGENT_SYSTEM_PROMPT,
            max_iterations=settings.SUBAGENT_MAX_ITERATIONS,
            max_output_chars=settings.SUBAGENT_MAX_OUTPUT,
        )
        return result

    return run_task
```

**为什么用闭包而不是直接绑定**：闭包可以延迟访问 `agent._child_registry`，避免在 `__init__` 阶段 registry 尚未就绪时产生引用错误。同时也方便测试时 mock。

### 4. `src/agent/async_loop.py` — AsyncAgent 改造

#### 4.1 新增 imports

```python
from agent.subagent import TASK_TOOL_DEFINITION, make_task_handler
```

#### 4.2 `__init__` 中新增子 registry 和 task 注册

在现有 `__init__` 方法的末尾（`update_plan` 注册之后）新增：

```python
# --- Subagent support ---
# Child registry: 仅 CHILD 工具（无 task、无 update_plan）
# AsyncToolRegistry() 自动注册的是 ASYNC_ALL_TOOLS（即 6 个基础工具），
# update_plan 和 task 是手动注册到主 registry 的，不会出现在子 registry 中。
# 因此 AsyncToolRegistry() 默认构造恰好就是子 agent 需要的工具集。
self._child_registry = AsyncToolRegistry()
self._child_tool_definitions = self._child_registry.definitions

# 主 registry: 注册 task 工具（仅父 Agent 可见）
self.tool_registry.register(
    TASK_TOOL_DEFINITION,
    make_task_handler(self),
)
```

**完整的 `__init__` 改造后**：

```python
def __init__(
    self,
    llm_provider_type: LLMProviderType = None,
    display: DisplayHandler | None = None,
):
    self.llm_provider = create_llm_provider(
        llm_provider_type or LLMProviderType(settings.LLM_PROVIDER)
    )

    self.messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
    ]

    # 主 registry: CHILD + update_plan + task
    self.tool_registry = AsyncToolRegistry()
    self.progress_tracker = ProgressTracker()
    self.context_compactor = ContextCompactor(
        max_messages=settings.CONTEXT_MAX_MESSAGES,
        keep_recent=settings.CONTEXT_KEEP_RECENT,
    )
    self.display = display or SilentDisplayHandler()

    # Register update_plan (sync handler, AsyncToolRegistry handles both)
    self.tool_registry.register(
        UPDATE_PLAN_TOOL_DEFINITION,
        lambda args: run_update_plan(args, self.progress_tracker),
    )

    # --- Subagent support ---
    self._child_registry = AsyncToolRegistry()  # CHILD tools only
    self._child_tool_definitions = self._child_registry.definitions
    self.tool_registry.register(
        TASK_TOOL_DEFINITION,
        make_task_handler(self),
    )
```

**父 Agent 的 tool_registry 最终包含**：
- `bash`, `read_file`, `write_file`, `list_directory`, `create_directory`, `file_exists`（来自 `AsyncToolRegistry()` 自动注册）
- `update_plan`（手动注册）
- `task`（手动注册）

**子 Agent 的 `_child_registry` 包含**：
- `bash`, `read_file`, `write_file`, `list_directory`, `create_directory`, `file_exists`（来自 `AsyncToolRegistry()` 自动注册）

#### 4.3 `chat()` 和 `_handle_tool_call()` — 无改动

`chat()` 和 `_handle_tool_call()` **不需要任何改动**。当 LLM 返回 `task` 工具调用时，`_handle_tool_call` 会通过 `self.tool_registry.execute("task", args)` 分发到 `make_task_handler` 返回的闭包，闭包内部调用 `run_subagent()`。结果作为普通的 tool result 追加到父 Agent 的 messages 中。

### 5. `src/agent/__init__.py` — 导出 `run_subagent`

```python
from agent.async_loop import AsyncAgent
from agent.display import DisplayHandler, SilentDisplayHandler
from agent.loop import Agent
from agent.subagent import run_subagent          # ★ 新增
from context_manager.tracker import ProgressTracker

__all__ = [
    "Agent",
    "AsyncAgent",
    "DisplayHandler",
    "ProgressTracker",
    "SilentDisplayHandler",
    "run_subagent",       # ★ 新增
]
```

### 6. `tests/agent/test_subagent.py` — 新建测试文件

```
tests/agent/test_subagent.py
```

## 测试策略

### 单元测试

| 测试 | 验证点 | Mock 策略 |
|---|---|---|
| `test_subagent_returns_summary` | `run_subagent` 返回字符串摘要 | Mock `llm_provider.chat_completion` 返回无 tool_calls 的响应 |
| `test_subagent_context_isolation` | Subagent 的 messages 不影响父 Agent | 构造父 Agent，调用 task 工具，验证父 messages 仅增加 3 条（assistant + tool + assistant） |
| `test_subagent_no_task_tool` | Subagent 的 tools 列表中不含 `task` | 验证 `_child_registry.definitions` 中无 `task` 工具名 |
| `test_subagent_iteration_limit` | 达到 `max_iterations` 后停止并返回警告 | Mock LLM 始终返回带 tool_calls 的响应，设 `max_iterations=3` |
| `test_subagent_output_truncation` | 超过 `max_output_chars` 的摘要被截断 | Mock LLM 返回 5000 字符文本，设 `max_output_chars=100` |
| `test_task_tool_handler` | `task` 工具 handler 正确调用 `run_subagent` | Mock `run_subagent`，验证 handler 传入正确参数 |
| `test_empty_prompt_rejected` | 空 prompt 返回错误信息 | 调用 handler 传入 `{"prompt": ""}` |
| `test_child_registry_has_no_task` | `_child_registry` 不含 task 和 update_plan | 构建 AsyncAgent，检查 `_child_registry.get_tool_names()` |
| `test_child_registry_has_child_tools` | `_child_registry` 包含全部 6 个基础工具 | 验证 bash, read_file 等 6 个工具名 |
| `test_subagent_executes_tools` | Subagent 能正确执行工具调用并将结果追加到子上下文 | Mock LLM：第一次返回 tool_call，第二次返回纯文本 |

### 测试实现模式

遵循现有测试模式：`unittest.TestCase` + `asyncio.run()` + `unittest.mock`：

```python
class TestSubagent(unittest.TestCase):
    def test_subagent_returns_summary(self):
        asyncio.run(self._test_subagent_returns_summary())

    async def _test_subagent_returns_summary(self):
        from agent.subagent import run_subagent, SUBAGENT_SYSTEM_PROMPT
        from unittest.mock import MagicMock

        # Mock LLM provider — 直接返回无 tool_calls 的响应
        mock_provider = MagicMock()
        mock_response = MagicMock()
        mock_response.content = "Project uses pytest."
        mock_response.tool_calls = []
        mock_response.model_dump = None  # 不走 model_dump 路径
        mock_provider.chat_completion.return_value = mock_response

        registry = AsyncToolRegistry()
        result = await run_subagent(
            prompt="What test framework?",
            llm_provider=mock_provider,
            child_tools=registry.definitions,
            child_registry=registry,
            system_prompt=SUBAGENT_SYSTEM_PROMPT,
            max_iterations=5,
            max_output_chars=2000,
        )
        self.assertEqual(result, "Project uses pytest.")
```

### 集成测试（手动验证）

```
用户输入: "Use a subtask to find what testing framework this project uses"

期望日志:
  INFO  agent.async_loop: Tool call: task, args={'prompt': '...'}
  INFO  agent.subagent: Subagent spawned: prompt_len=52, max_iter=30
  INFO  agent.subagent: Subagent tool call: read_file, args={'path': 'pyproject.toml'}
  INFO  agent.subagent: Subagent tool call: list_directory, args={'path': 'tests'}
  INFO  agent.subagent: Subagent finished: iterations=3, result_len=142
  INFO  agent.async_loop: Tool result: task, output_len=142

期望父 Agent messages 变化:
  +1 user message（用户输入）
  +1 assistant message（含 task tool_call）
  +1 tool message（subagent 摘要，~142 chars）
  +1 assistant message（最终回答）
  总计 4 条，不含任何中间文件内容
```

## 数据流：Subagent 完整生命周期

```
用户: "分析一下这个项目的测试框架"
  │
  ├─ AsyncAgent.chat("分析一下这个项目的测试框架")
  │   ├─ _inject_progress()
  │   ├─ _call_llm() → LLM 返回:
  │   │   assistant: "我来用 subagent 分析..."
  │   │   tool_calls: [{name: "task", args: {prompt: "分析项目测试框架..."}}]
  │   │
  │   ├─ _handle_tool_call(task_call)
  │   │   ├─ tool_registry.execute("task", {prompt: "..."})
  │   │   │   └─ make_task_handler(self) → run_task(args)
  │   │   │       └─ run_subagent(prompt=..., llm_provider=..., child_registry=...)
  │   │   │           │
  │   │   │           │  messages = [system, user:prompt]   ← 独立上下文
  │   │   │           │
  │   │   │           ├─ 迭代 1:
  │   │   │           │   ├─ LLM → tool_call: read_file(pyproject.toml)
  │   │   │           │   ├─ child_registry.execute("read_file", ...) → 文件内容
  │   │   │           │   └─ messages.append(tool_result)
  │   │   │           │
  │   │   │           ├─ 迭代 2:
  │   │   │           │   ├─ LLM → tool_call: list_directory(tests)
  │   │   │           │   ├─ child_registry.execute("list_directory", ...) → 文件列表
  │   │   │           │   └─ messages.append(tool_result)
  │   │   │           │
  │   │   │           ├─ 迭代 3:
  │   │   │           │   ├─ LLM → 无 tool_calls
  │   │   │           │   ├─ last_text = "项目使用 pytest 框架..."
  │   │   │           │   └─ break
  │   │   │           │
  │   │   │           │  messages 被 GC 回收 ← 中间结果全部丢弃
  │   │   │           │
  │   │   │           └─ return "项目使用 pytest 框架..."（≤2000 chars）
  │   │   │
  │   │   └─ return tool_result: {role: "tool", content: "项目使用 pytest..."}
  │   │
  │   ├─ messages.append(tool_result)  ← 父 Agent 只看到一条摘要
  │   │
  │   ├─ _call_llm() → LLM 基于摘要生成最终回答
  │   └─ return "这个项目使用 pytest 作为测试框架..."
  │
  └─ 父 Agent messages 中无任何文件内容，只有 task 摘要
```

## 边界情况处理

| 场景 | 处理方式 |
|---|---|
| Subagent LLM 调用失败（网络错误等） | provider 返回错误 `MessageWrapper`（content 含错误信息，tool_calls 为空），Subagent 正常退出并返回错误文本。若 provider 直接抛异常，`asyncio.to_thread` 将异常传播到父 Agent 的 `_handle_tool_call`，被 registry 的 try-except 捕获 |
| Subagent 工具执行异常 | `AsyncToolRegistry.execute` 已有 try-except，返回 `"Error executing tool: ..."` |
| 空 prompt | `make_task_handler` 中提前检查，返回 `"Error: 'prompt' is required."` |
| Subagent 达到迭代上限 | `for-else` 分支返回 `"(subagent reached iteration limit without completing)"` |
| Subagent 输出超长 | 截断到 `max_output_chars`，追加 `"\n... [truncated]"` |
| 单条工具结果过大 | 截断到 50000 字符（subagent 内部上限，高于父 Agent 的 `MAX_TOOL_OUTPUT=8000`） |
| Subagent 返回空字符串 | `last_text` 初始化为 `""`，若 LLM 始终无内容输出则返回空字符串 |
| 父 Agent 并发调用多个 task | `asyncio.gather` 在 `_handle_tool_call` 层并发，每个 `run_subagent` 独立运行（各自有独立的 messages） |
| LLM 返回格式异常（无 content 也无 tool_calls） | `getattr(response, "content", "") or ""` 安全降级为空字符串 |

## 风险与缓解

| 风险 | 概率 | 影响 | 缓解措施 |
|---|---|---|---|
| Subagent 死循环（工具调用不终止） | 中 | 高 | `max_iterations=30` 硬上限 + 日志告警 |
| Subagent 输出过长撑爆父上下文 | 低 | 中 | `max_output_chars=2000` 截断 |
| Subagent 消耗大量 API token | 中 | 中 | 日志记录每次 subagent 的迭代数，便于监控和计费分析 |
| Subagent 调用危险命令 | 低 | 高 | 复用 `async_run_bash` 已有的 `DANGEROUS_PATTERNS` 安全检查 |
| 父 Agent 频繁派生 subagent | 低 | 低 | task 工具的 description 中说明 "Use this for multi-step research..."，引导 LLM 仅在需要时使用 |
| Subagent 递归派生（task 中再调 task） | 极低 | 高 | `_child_registry` 不含 `task` 工具，LLM 无法调用 |

## 实现顺序

按依赖关系分 4 个阶段：

**阶段 1：基础设施（零风险）**
1. `src/config.py` — 新增 `SUBAGENT_MAX_ITERATIONS`、`SUBAGENT_MAX_OUTPUT`

**阶段 2：核心模块**
2. `src/agent/async_tool_registry.py` — `__init__` 新增 `exclude` 参数（API 预留，本迭代 `_child_registry` 实际不传此参数）
3. `src/agent/subagent.py` — `SUBAGENT_SYSTEM_PROMPT` + `TASK_TOOL_DEFINITION` + `run_subagent()` + `make_task_handler()`

**阶段 3：集成**
4. `src/agent/async_loop.py` — `__init__` 中创建 `_child_registry` + 注册 `task` 工具
5. `src/agent/__init__.py` — 导出 `run_subagent`

**阶段 4：测试**
6. `tests/agent/test_subagent.py` — 全部单元测试

## 验证方式

1. **单元测试**：`python -m unittest tests.agent.test_subagent -v`
2. **全量回归**：`python -m unittest discover -s tests -v`（确保 `exclude` 改动和 `task` 注册不破坏现有测试）
3. **集成测试**：启动 CLI（`python -m src.agent.loop`），输入 "Use a subtask to find what testing framework this project uses"，观察日志和 messages 数量
4. **日志验证**：`grep -E "Subagent|Task tool" mini_agent.log` 检查生命周期日志完整性
