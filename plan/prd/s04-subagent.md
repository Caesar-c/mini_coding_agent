# PRD: Subagent — 上下文隔离的子任务执行

> **阶段**: s04 · **前置**: s03 (工具系统) · **后续**: s05 (上下文压缩)
>
> 核心理念: *"大任务拆小, 每个小任务干净的上下文"* — Subagent 用独立 messages[], 不污染主对话。

## 1. 背景与动机

### 问题

当前 Agent 采用单一 `messages[]` 数组管理全部对话历史。随着工作深入, 上下文会持续膨胀:

- 每次工具调用的输入和输出都**永久保留**在 messages 中
- 一个 "读 5 个文件总结项目结构" 的任务, 父 Agent 的 messages 里会塞满所有文件的完整内容
- 即使有 `ContextCompactor` 做规则裁剪, 也只是事后补救 — 大量无用信息已经消耗了 token 配额
- LLM 的注意力被无关上下文稀释, 导致回答质量下降

### 典型场景

```
用户: "这个项目用什么测试框架?"

当前行为 (无 subagent):
  → Agent 读 pyproject.toml    → 输出 2KB 留在 messages
  → Agent 读 tests/test_a.py   → 输出 5KB 留在 messages
  → Agent 读 tests/test_b.py   → 输出 3KB 留在 messages
  → Agent 读 conftest.py       → 输出 1KB 留在 messages
  → Agent 读 README.md         → 输出 4KB 留在 messages
  → Agent 回答: "pytest"
  → 15KB 的中间结果永久留在上下文里, 后续每一轮 LLM 调用都要付费处理

期望行为 (有 subagent):
  → 父 Agent 调用 task 工具, 派生 subagent
  → Subagent 独立上下文里读 5 个文件, 跑 30+ 次工具调用
  → Subagent 返回摘要: "项目使用 pytest, 测试在 tests/ 下..."
  → 父 Agent 收到一条 tool_result, 上下文保持干净
  → Subagent 的全部中间 messages 直接丢弃
```



## 2. 目标与非目标



### 目标


| #   | 目标       | 衡量标准                                     |
| --- | -------- | ---------------------------------------- |
| G1  | 子任务上下文隔离 | Subagent 的 messages 不影响父 Agent           |
| G2  | 仅返回摘要    | 父 Agent 收到的 tool_result 为文本摘要, ≤2000 字符  |
| G3  | 禁止递归生成   | Subagent 不可再派生 subagent (无 `task` 工具)    |
| G4  | 安全上限     | Subagent 最大迭代次数、最大输出长度可配置                |
| G5  | 透明可观测    | 通过日志记录 subagent 的生命周期 (spawn / 迭代数 / 返回) |




### 非目标

- **不做** subagent 间通信 (subagent 之间互相调用)
- **不做** 持久化 subagent 状态 (subagent 是短生命周期的)
- **不做** 流式输出 subagent 中间过程 (只返回最终结果)



## 3. 架构设计



### 3.1 整体结构

```
Parent Agent (AsyncAgent)                 Subagent
┌──────────────────────────┐              ┌──────────────────────────┐
│ messages = [sys, user…]  │              │ messages = [sys, user]   │ ← fresh
│                          │   dispatch   │                          │
│ tool: task               │ ──────────>  │ while tool_calls:        │
│   prompt="..."           │              │   call child tools       │
│                          │   summary    │   append results         │
│ result = "摘要文本"      │ <──────────  │ return last text         │
└──────────────────────────┘              └──────────────────────────┘
     上下文保持干净                             上下文用完即弃
```



### 3.2 工具集划分

```python
# 基础工具 — 父子共享
CHILD_TOOL_NAMES = ["bash", "read_file", "write_file",
                    "list_directory", "create_directory", "file_exists"]

# 父 Agent 独有 — 可派生 subagent
PARENT_ONLY_TOOLS = ["task"]

# 父 Agent 工具集 = CHILD + task
# Subagent 工具集 = CHILD only (禁止递归)
```



### 3.3 `task` 工具定义

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
                    "description": "The task description for the subagent to execute."
                }
            },
            "required": ["prompt"],
        },
    },
}
```



## 4. 详细设计



### 4.1 Subagent 执行流程

```python
async def run_subagent(
    prompt: str,
    llm_provider: LLMProvider,
    child_tools: list[dict],       # CHILD_TOOLS 定义 (不含 task)
    child_registry: AsyncToolRegistry,  # 仅注册 CHILD_TOOLS 的 registry
    system_prompt: str,
    max_iterations: int = 30,
    max_output_chars: int = 2000,
) -> str:
    """派生一个 subagent, 在独立上下文中执行任务, 返回摘要文本."""

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
        if hasattr(response, "model_dump"):
            messages.append(response.model_dump(exclude_unset=True))
        else:
            msg_dict = {"role": "assistant", "content": response.content}
            if response.tool_calls:
                msg_dict["tool_calls"] = response.tool_calls
            messages.append(msg_dict)

        # 提取 tool_calls
        tool_calls = getattr(response, "tool_calls", []) or []
        if not tool_calls:
            # 无工具调用 → subagent 完成, 提取最终文本
            last_text = getattr(response, "content", "") or ""
            logger.info("Subagent finished: iterations=%d, result_len=%d",
                        iteration, len(last_text))
            break

        # 执行工具调用, 结果写入子上下文
        for tc in tool_calls:
            tool_name = tc_attr(tc, "function.name", "")
            raw_args = tc_attr(tc, "function.arguments", "{}")
            args = json.loads(raw_args) if raw_args else {}
            tc_id = tc_attr(tc, "id", "")

            output = await child_registry.execute(tool_name, args)
            messages.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "content": output[:50000],  # 单子结果上限
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



### 4.2 Subagent System Prompt

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



### 4.3 task 工具 Handler

```python
# 闭包绑定: 在 AsyncAgent.__init__ 中注册
def make_task_handler(agent):
    """创建 task handler 闭包, 绑定到父 agent 的 provider 和配置."""

    async def run_task(args: dict) -> str:
        prompt = args.get("prompt", "")
        if not prompt:
            return "Error: 'prompt' is required."

        logger.info("Task tool invoked: prompt_len=%d", len(prompt))

        result = await run_subagent(
            prompt=prompt,
            llm_provider=agent.llm_provider,
            child_tools=agent._child_tool_definitions,
            child_registry=agent._child_registry,
            system_prompt=SUBAGENT_SYSTEM_PROMPT,
            max_iterations=settings.SUBAGENT_MAX_ITERATIONS,
            max_output_chars=settings.SUBAGENT_MAX_OUTPUT,
        )
        return result

    return run_task
```



### 4.4 配置项 (`config.py`)

```python
# ---- Subagent ----
SUBAGENT_MAX_ITERATIONS: int = int(os.getenv("SUBAGENT_MAX_ITERATIONS", "30"))
SUBAGENT_MAX_OUTPUT: int = int(os.getenv("SUBAGENT_MAX_OUTPUT", "2000"))
```



## 5. 集成方案



### 5.1 AsyncAgent 改造

在 `AsyncAgent.__init__` 中:

```python
def __init__(self, llm_provider_type=None, display=None):
    # ... 现有初始化 ...

    # 主 registry: 全部工具 (CHILD + task + update_plan)
    self.tool_registry = AsyncToolRegistry()
    self.tool_registry.register(UPDATE_PLAN_TOOL_DEFINITION, ...)
    self.tool_registry.register(TASK_TOOL_DEFINITION, make_task_handler(self))

    # 子 registry: 仅 CHILD 工具 (无 task, 无 update_plan)
    self._child_registry = AsyncToolRegistry()  # 已包含 CHILD_TOOLS
    self._child_tool_definitions = self._child_registry.definitions
```

**关键点**: `self.tool_registry` 包含 `task`, 传给 LLM 的 `tools` 参数来自它; `self._child_registry` 不含 `task`, 传给 subagent。LLM 在父端能看到并调用 `task`, 在子端看不到。

### 5.2 同步 Agent (可选)

同步 `Agent` 可采用相同模式, 但 `run_subagent` 改为同步版本:

```python
def run_subagent_sync(prompt, llm_provider, child_tools, child_registry, ...) -> str:
    # 同 run_subagent 但去掉 await asyncio.to_thread, 直接调用
```

鉴于 CLI 主要走 `AsyncAgent` 路径, 同步版本优先级较低, 可作为后续迭代。

## 6. 文件变更清单


| 文件                                 | 变更类型   | 说明                                                                      |
| ---------------------------------- | ------ | ----------------------------------------------------------------------- |
| `src/agent/subagent.py`            | **新建** | `run_subagent()` 函数 + `SUBAGENT_SYSTEM_PROMPT` + `TASK_TOOL_DEFINITION` |
| `src/agent/async_loop.py`          | 修改     | `__init__` 中创建 `_child_registry`; 注册 `task` 工具                          |
| `src/agent/async_tool_registry.py` | 修改     | 支持 "排除指定工具" 的初始化方式 (或新建一个不含 task 的 registry)                            |
| `src/config.py`                    | 修改     | 新增 `SUBAGENT_MAX_ITERATIONS`, `SUBAGENT_MAX_OUTPUT`                     |
| `src/agent/__init__.py`            | 修改     | 导出 `run_subagent`                                                       |
| `tests/agent/test_subagent.py`     | **新建** | 测试 subagent 生命周期、上下文隔离、迭代上限                                             |




## 7. 测试策略



### 7.1 单元测试


| 测试                                | 验证点                                         |
| --------------------------------- | ------------------------------------------- |
| `test_subagent_returns_summary`   | subagent 执行后返回文本摘要                          |
| `test_subagent_context_isolation` | subagent 的 messages 不泄漏到父 agent             |
| `test_subagent_no_task_tool`      | subagent 的 tools 列表中不含 `task`               |
| `test_subagent_iteration_limit`   | 达到 `max_iterations` 后停止并返回警告                |
| `test_subagent_output_truncation` | 超过 `max_output_chars` 的摘要被截断                |
| `test_task_tool_handler`          | `task` 工具 handler 正确调用 `run_subagent` 并返回结果 |
| `test_empty_prompt_rejected`      | 空 prompt 返回错误信息                             |




### 7.2 集成测试

```
用户输入: "Use a subtask to find what testing framework this project uses"

期望日志:
  INFO  agent.async_loop: Tool call: task, args={'prompt': '...'}
  INFO  agent.subagent: Subagent spawned: prompt_len=52, max_iter=30
  INFO  agent.subagent: Subagent finished: iterations=3, result_len=142
  INFO  agent.async_loop: Tool result: task, output_len=142

期望父 Agent messages 变化:
  +1 user message (用户输入)
  +1 assistant message (含 task tool_call)
  +1 tool message (subagent 摘要, ~142 chars)
  +1 assistant message (最终回答)
  总计 4 条, 不含任何中间文件内容
```



## 8. 风险与缓解


| 风险                      | 概率  | 影响  | 缓解措施                                 |
| ----------------------- | --- | --- | ------------------------------------ |
| Subagent 死循环 (工具调用不终止)  | 中   | 高   | `max_iterations=30` 硬上限 + 日志告警       |
| Subagent 输出过长撑爆父上下文     | 低   | 中   | `max_output_chars=2000` 截断           |
| Subagent 消耗大量 API token | 中   | 中   | 日志记录每次 subagent 的迭代数, 便于监控           |
| Subagent 调用危险命令         | 低   | 高   | 复用已有的 bash 安全检查 (dangerous_patterns) |
| 父 Agent 频繁派生 subagent   | 低   | 低   | LLM prompt 中说明 "仅对多步骤研究任务使用 task"    |
