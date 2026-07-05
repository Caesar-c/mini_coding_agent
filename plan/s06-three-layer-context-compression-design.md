# 设计方案：三层上下文压缩 — 渐进式上下文管理

## Context

当前 `ContextCompactor`（`context_manager/context.py`）是一个单层规则引擎：当 `messages[]` 超过 `max_messages`（默认 40）条时，一次性丢弃中间段所有 tool 结果，并将 assistant 消息的 `tool_calls` 剥离。这种"要么不管，要么全丢"的策略存在严重缺陷：

**核心问题 1 — 信息丢失**：中间段的 tool 结果被整条删除，LLM 丢失已读文件内容、命令输出、错误信息。压缩后 Agent 可能重复读取同一文件，浪费 token。

**核心问题 2 — 截断粗糙**：当前 tool 输出截断是 `output[:max_output]`（`async_loop.py` L131-134, `loop.py` L109-112），纯前缀截断。文件末尾的 import 语句、bash 尾部的错误信息和 exit code 全部丢失。

**核心问题 3 — 无渐进压缩**：消息从 0 涨到 40 条期间不做任何压缩，然后一次性大幅裁剪。用户体验是"Agent 突然忘了之前做过什么"。

**核心问题 4 — 无数值感知**：阈值基于消息条数而非 token。40 条短消息可能只有 8K token，40 条含大文件读取的消息可能超过 100K token。

**解决方案**：用三层渐进压缩管线 `ContextPipeline` 替换单层 `ContextCompactor`。Layer 1 在 tool 结果产生时做智能截断（零成本）；Layer 2 将中间段连续 tool 调用组压缩为摘要段落（规则/LLM 双模式）；Layer 3 在极端情况下全量重建为结构化摘要（LLM）。三层级联，代价递增。

**前置依赖**：s03（工具系统 `ToolRegistry`/`AsyncToolRegistry`）、s03（进度追踪 `ProgressTracker`）、s04（Subagent）。

## 方案概览

| 层 | 组件 | 职责 | 复杂度 |
|---|---|---|---|
| 1 | `micro_compressor.py` — `MicroCompressor` | 单条 tool 结果智能截断（按工具类型 head-tail） | ~100 行新模块 |
| 2 | `meso_compressor.py` — `MesoCompressor` | 中间段 tool 调用组 → 结构化摘要 | ~180 行新模块 |
| 3 | `macro_compressor.py` — `MacroCompressor` | LLM 全量上下文重建 + ProgressTracker 集成 | ~130 行新模块 |
| 4 | `pipeline.py` — `ContextPipeline` | 编排三层压缩，Drop-in 替换 ContextCompactor | ~80 行新模块 |
| 5 | `config.py` — 7 个新配置项 | 各层阈值参数 | ~10 行改动 |
| 6 | `async_loop.py` + `loop.py` — 替换压缩器 | ContextCompactor → ContextPipeline | ~30 行改动 × 2 |

### 设计决策

| # | 决策 | 选择 | 理由 |
|---|---|---|---|
| D1 | Layer 2 默认模式 | 规则模式（无 LLM 调用） | 零 API 成本、确定性输出、无网络延迟。LLM 模式作为 opt-in（`CONTEXT_MESO_USE_LLM=true`） |
| D2 | Token 估算方法 | `len(text) // 4`（粗估） | 仅用于阈值判断而非精确计费，误差在可接受范围。中文文本会高估 token（实际 ~2 chars/token），但阈值留有余量 |
| D3 | Layer 1 集成点 | 替换 `_handle_tool_call` 中的前缀截断 | Layer 1 在 tool 结果产生时即刻生效，是最自然的集成位置。同时保留 `compact()` 中的防御性重扫 |
| D4 | Layer 3 与 ProgressTracker 关系 | Layer 3 读取 tracker 但不修改 | tracker 已维护结构化的任务状态（done/in_progress/pending），直接喂入摘要的 "Completed Work" 和 "Remaining Tasks" 部分，比让 LLM 从对话推断更准确 |
| D5 | ContextCompactor 保留策略 | 保留不删除 | 向后兼容——现有 12 个测试用例继续通过。`ContextPipeline` 是新代码路径，不修改 `context.py` |
| D6 | Layer 3 LLM 失败降级 | 保留 system + first_user + recent | Agent 仍可继续工作，只是丢失压缩的中间段上下文。比完全崩溃好得多 |
| D7 | `estimate_tokens` 函数位置 | 放在 `micro_compressor.py` 中 | Layer 2/3 从 Layer 1 模块导入，避免额外 utils 文件。函数仅 2 行，不值得单独提取 |
| D8 | `_handle_tool_call` 截断替换方式 | 完全替换，不保留旧逻辑 | 新的 `compress_tool_result(tool_name, output)` 是旧截断的超集——对于小于阈值的输出直接返回原值，等效于 no-op |
| D9 | Layer 2 摘要消息格式 | 单条 assistant 消息，content 以 `[SUMMARY]` 前缀 | 与 Layer 3 的 `[CONTEXT SUMMARY]` 前缀区分。`_inject_progress` 通过前缀过滤 `[TASK PROGRESS]` 消息，不会误伤 |
| D10 | Layer 3 需要 LLM provider | 无 provider 时 Layer 3 不触发 | `MacroCompressor.should_compress()` 在无 LLM 时返回 False。Layer 1 + Layer 2（规则模式）可独立工作，无需 LLM |

## 架构总览

```
                    ┌──────────────────────────┐
                    │     Agent Loop            │
                    │ (loop.py / async_loop.py) │
                    └─────┬──────────┬─────────┘
                          │          │
             _handle_tool_call    chat() loop
                          │          │
                          ▼          ▼
              ┌────────────┐   ┌───────────────────┐
              │ compress_  │   │ should_compact()  │
              │ tool_      │   │ compact()         │
              │ result()   │   │                   │
              └─────┬──────┘   └────────┬──────────┘
                    │                   │
                    ▼                   ▼
            ┌───────────────────────────────────────────┐
            │           ContextPipeline                 │
            │                                           │
            │  Layer 1 (Micro)    — 每次 tool 结果产生时  │
            │    ↓ 消息累积                              │
            │  Layer 2 (Meso)     — 中间段超阈值时        │
            │    ↓ Layer 2 不够                         │
            │  Layer 3 (Macro)    — 总 token 超阈值时    │
            │                                           │
            │  ┌──────────┐  ┌──────────┐  ┌──────────┐ │
            │  │ Micro    │  │ Meso     │  │ Macro    │ │
            │  │Compressor│  │Compressor│  │Compressor│ │
            │  │ 规则     │  │ 规则/LLM │  │ LLM      │ │
            │  │ ~0ms     │  │ ~0-2000ms│  │ ~2-5s    │ │
            │  └──────────┘  └──────────┘  └──────────┘ │
            └──────────────┬────────────────────────────┘
                           │ reads
                           ▼
                  ┌─────────────────┐
                  │ ProgressTracker │
                  │ (不修改)         │
                  └─────────────────┘
```

**级联原则**：三层按代价从低到高逐层执行。Layer 2 压缩后如果总 token 已降至阈值以下，Layer 3 不触发。这是最常见的情况。

## 文件变更详设

### 1. `src/config.py` — 新增 7 个配置项

在 `Settings` 类的 `# ---- Skill Loading ----` 区域之后、`# ---- Sandbox ----` 之前新增：

```python
# ---- Context Compression (Three-Layer) ----

# Layer 1: Micro — 单条 tool 结果截断
CONTEXT_MICRO_MAX_CHARS: int = int(os.getenv("CONTEXT_MICRO_MAX_CHARS", "4000"))
CONTEXT_MICRO_KEEP_HEAD_LINES: int = int(os.getenv("CONTEXT_MICRO_KEEP_HEAD_LINES", "10"))
CONTEXT_MICRO_KEEP_TAIL_LINES: int = int(os.getenv("CONTEXT_MICRO_KEEP_TAIL_LINES", "15"))

# Layer 2: Meso — 段落级摘要
CONTEXT_MESO_MESSAGE_THRESHOLD: int = int(os.getenv("CONTEXT_MESO_MESSAGE_THRESHOLD", "20"))
CONTEXT_MESO_TOKEN_THRESHOLD: int = int(os.getenv("CONTEXT_MESO_TOKEN_THRESHOLD", "8000"))
CONTEXT_MESO_USE_LLM: bool = os.getenv("CONTEXT_MESO_USE_LLM", "false").lower() == "true"

# Layer 3: Macro — 全量重建
CONTEXT_MACRO_TOKEN_THRESHOLD: int = int(os.getenv("CONTEXT_MACRO_TOKEN_THRESHOLD", "32000"))
```

| 配置项 | 环境变量 | 默认值 | 层 | 说明 |
|---|---|---|---|---|
| `CONTEXT_MICRO_MAX_CHARS` | `CONTEXT_MICRO_MAX_CHARS` | 4000 | L1 | 单条 tool 结果压缩后的最大字符数 |
| `CONTEXT_MICRO_KEEP_HEAD_LINES` | `CONTEXT_MICRO_KEEP_HEAD_LINES` | 10 | L1 | 文件/输出保留的头部行数 |
| `CONTEXT_MICRO_KEEP_TAIL_LINES` | `CONTEXT_MICRO_KEEP_TAIL_LINES` | 15 | L1 | 文件/输出保留的尾部行数 |
| `CONTEXT_MESO_MESSAGE_THRESHOLD` | `CONTEXT_MESO_MESSAGE_THRESHOLD` | 20 | L2 | 中间段消息数触发阈值 |
| `CONTEXT_MESO_TOKEN_THRESHOLD` | `CONTEXT_MESO_TOKEN_THRESHOLD` | 8000 | L2 | 中间段 token 估算触发阈值 |
| `CONTEXT_MESO_USE_LLM` | `CONTEXT_MESO_USE_LLM` | false | L2 | 是否使用 LLM 生成摘要（opt-in，有 API 成本） |
| `CONTEXT_MACRO_TOKEN_THRESHOLD` | `CONTEXT_MACRO_TOKEN_THRESHOLD` | 32000 | L3 | 总上下文 token 估算触发阈值 |

**已有的不变配置**：

| 配置项 | 值 | 说明 |
|---|---|---|
| `CONTEXT_KEEP_RECENT` | 12 | 最近消息保留数，三层共享 |
| `CONTEXT_MAX_MESSAGES` | 40 | Legacy，保留但不再被 Agent 循环使用 |
| `MAX_TOOL_OUTPUT` | 8000 | Legacy，被 `CONTEXT_MICRO_MAX_CHARS` 取代 |

**改动范围**：新增 1 个注释块 + 7 个属性行。

---

### 2. `src/context_manager/micro_compressor.py` — Layer 1: 单条消息智能截断

#### 2.1 模块结构

```python
"""Layer 1: Per-message smart truncation (rule-based, no LLM).

Replaces the current dumb prefix truncation (``output[:max_output]``)
with tool-specific strategies that preserve the most informative
parts of each result.
"""

from logger import get_logger

logger = get_logger(__name__)


def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token for English/code."""
    return len(text) // 4


class MicroCompressor:
    """Intelligently compress individual tool result messages."""

    def __init__(
        self,
        max_chars: int = 4000,
        keep_head_lines: int = 10,
        keep_tail_lines: int = 15,
        max_dir_entries: int = 50,
    ): ...

    def compress(self, tool_name: str, content: str) -> str: ...
    def compress_message(self, msg: dict) -> dict: ...
    # Internal methods
    def _head_tail(self, content: str, head: int = None, tail: int = None) -> str: ...
    def _compress_read_file(self, content: str) -> str: ...
    def _compress_bash(self, content: str) -> str: ...
    def _compress_list_directory(self, content: str) -> str: ...
    def _generic_compress(self, content: str) -> str: ...

    _STRATEGIES = {
        "read_file": _compress_read_file,
        "bash": _compress_bash,
        "list_directory": _compress_list_directory,
        "load_skill": _compress_read_file,
    }
```

#### 2.2 `MicroCompressor.compress()` — 入口方法

```python
def compress(self, tool_name: str, content: str) -> str:
    """Compress a tool result based on tool type.

    If content is already within max_chars, return unchanged.
    Otherwise dispatch to tool-specific strategy.
    """
    if len(content) <= self.max_chars:
        return content

    strategy = self._STRATEGIES.get(tool_name, self._generic_compress)
    compressed = strategy(self, content)
    if len(compressed) < len(content):
        logger.info(
            "MicroCompressor: %s %d -> %d chars (%.0f%% reduction)",
            tool_name, len(content), len(compressed),
            (1 - len(compressed) / len(content)) * 100,
        )
    return compressed
```

**关键设计点**：

| 点 | 说明 |
|---|---|
| 短路返回 | `len(content) <= self.max_chars` 时直接返回原值，零开销 |
| 策略分发 | `_STRATEGIES` 类变量是 dict，按 `tool_name` 查找。未知工具 fallback 到 `_generic_compress` |
| 绑定方式 | `_STRATEGIES` 中的值是未绑定方法（unbound method），调用时传 `self` 作为第一个参数（`strategy(self, content)`） |

#### 2.3 `_head_tail()` — 通用首尾保留

```python
def _head_tail(self, content: str, head: int = None, tail: int = None) -> str:
    """Keep first N and last M lines, elide middle."""
    head = head or self.keep_head_lines
    tail = tail or self.keep_tail_lines
    lines = content.split("\n")
    if len(lines) <= head + tail:
        return content
    omitted = len(lines) - head - tail
    return "\n".join(
        lines[:head]
        + [f"\n[... {omitted} lines omitted ...]\n"]
        + lines[-tail:]
    )
```

#### 2.4 `_compress_bash()` — Bash 输出策略

```python
def _compress_bash(self, content: str) -> str:
    """Bash output: preserve stderr, keep tail of stdout."""
    lines = content.split("\n")
    # Separate stderr from stdout
    stderr_lines = []
    stdout_lines = []
    in_stderr = False
    for line in lines:
        if line.startswith("STDERR:"):
            in_stderr = True
        if in_stderr:
            stderr_lines.append(line)
        else:
            stdout_lines.append(line)

    # Keep tail of stdout + all stderr
    if len(stdout_lines) > self.keep_tail_lines:
        omitted = len(stdout_lines) - self.keep_tail_lines
        stdout_part = (
            [f"[... {omitted} stdout lines omitted ...]"]
            + stdout_lines[-self.keep_tail_lines:]
        )
    else:
        stdout_part = stdout_lines

    result = "\n".join(stdout_part)
    if stderr_lines:
        result += "\n" + "\n".join(stderr_lines)
    return result
```

**设计理由**：stderr 全量保留（错误信息是 Agent 排查问题的关键线索），stdout 保留尾部（命令最终输出通常在末尾）。

#### 2.5 `_compress_list_directory()` — 目录列表策略

```python
def _compress_list_directory(self, content: str) -> str:
    """Directory listings: cap entries."""
    lines = content.split("\n")
    if len(lines) <= self.max_dir_entries:
        return content
    kept = lines[:self.max_dir_entries]
    omitted = len(lines) - self.max_dir_entries
    return "\n".join(kept + [f"[... {omitted} more entries omitted ...]"])
```

#### 2.6 `compress_message()` — 对已有消息的防御性压缩

```python
def compress_message(self, msg: dict) -> dict:
    """Compress a tool result message (returns new dict).

    Used by ContextPipeline's defensive re-pass to catch any
    uncompressed tool results in existing messages.
    """
    if msg.get("role") != "tool":
        return msg
    content = msg.get("content", "")
    if len(content) <= self.max_chars:
        return msg
    return {**msg, "content": self._generic_compress(content)}
```

**关键设计点**：使用 `{**msg, ...}` 创建新 dict 而非修改原 dict，保留 `tool_call_id` 等元数据。

---

### 3. `src/context_manager/meso_compressor.py` — Layer 2: 段落级工具摘要

#### 3.1 模块结构

```python
"""Layer 2: Section-level summarization of completed tool exchanges.

Groups consecutive tool-call + tool-result pairs in the middle section
and replaces them with structured summaries, preserving key outcomes
(file paths, command results, errors) instead of dropping everything.
"""

import json

from context_manager.micro_compressor import estimate_tokens
from logger import get_logger

logger = get_logger(__name__)


class MesoCompressor:
    """Summarize groups of completed tool exchanges into brief prose."""

    def __init__(
        self,
        meso_message_threshold: int = 20,
        meso_token_threshold: int = 8000,
        keep_recent: int = 12,
        use_llm: bool = False,
        llm_provider=None,
    ): ...

    def should_compress(self, messages: list[dict]) -> bool: ...
    def compress(self, messages: list[dict]) -> list[dict]: ...

    # Internal
    def _get_middle(self, messages: list[dict]) -> list[dict]: ...
    def _group_tool_exchanges(self, messages: list[dict]) -> list[dict]: ...
    def _summarize_group(self, messages: list[dict]) -> str: ...
    def _rule_based_summarize(self, messages: list[dict]) -> str: ...
    def _format_tool_fact(self, tool_name: str, args: dict) -> str: ...
    def _extract_outcome(self, content: str) -> str: ...
    def _llm_summarize(self, messages: list[dict]) -> str: ...
```

#### 3.2 `should_compress()` — 双条件触发

```python
def should_compress(self, messages: list[dict]) -> bool:
    """Check if the middle section needs compression.

    Fires when middle section exceeds message count OR token threshold.
    """
    middle = self._get_middle(messages)
    if not middle:
        return False
    if len(middle) >= self.meso_message_threshold:
        return True
    middle_tokens = sum(
        estimate_tokens(str(m.get("content", ""))) for m in middle
    )
    return middle_tokens >= self.meso_token_threshold
```

#### 3.3 `compress()` — 核心压缩逻辑

```python
def compress(self, messages: list[dict]) -> list[dict]:
    """Compress middle section's tool exchanges into summaries.

    Returns: head[0:2] + compressed_middle + tail[-keep_recent:]
    """
    if len(messages) <= 2 + self.keep_recent:
        return messages

    head = messages[:2]
    tail = messages[-self.keep_recent:]
    middle = messages[2:len(messages) - self.keep_recent]

    groups = self._group_tool_exchanges(middle)

    compressed_middle = []
    for group in groups:
        if group["type"] == "tool_exchange":
            summary = self._summarize_group(group["messages"])
            if summary:
                compressed_middle.append({
                    "role": "assistant",
                    "content": f"[SUMMARY] {summary}",
                })
        else:
            compressed_middle.extend(group["messages"])

    result = head + compressed_middle + tail

    if len(result) >= len(messages):
        logger.warning("Meso compression produced no reduction, skipping")
        return messages

    logger.info(
        "Meso compression: %d -> %d messages (middle: %d -> %d)",
        len(messages), len(result), len(middle), len(compressed_middle),
    )
    return result
```

#### 3.4 `_group_tool_exchanges()` — 消息分组

```python
def _group_tool_exchanges(self, messages: list[dict]) -> list[dict]:
    """Group consecutive tool-call + tool-result pairs.

    Returns list of:
    - {"type": "tool_exchange", "messages": [...]}
    - {"type": "other", "messages": [...]}
    """
    groups = []
    current_group = []
    current_type = None

    for msg in messages:
        role = msg.get("role", "")
        is_tool_exchange = (
            (role == "assistant" and "tool_calls" in msg)
            or role == "tool"
        )
        msg_type = "tool_exchange" if is_tool_exchange else "other"

        if msg_type == current_type:
            current_group.append(msg)
        else:
            if current_group:
                groups.append({"type": current_type, "messages": current_group})
            current_group = [msg]
            current_type = msg_type

    if current_group:
        groups.append({"type": current_type, "messages": current_group})

    return groups
```

**分组逻辑**：连续出现的 `assistant(tool_calls)` + `tool` 消息归为一组 "tool_exchange"，其他消息（user、纯文本 assistant、system）归为 "other"。

#### 3.5 `_rule_based_summarize()` — 规则摘要

```python
def _rule_based_summarize(self, messages: list[dict]) -> str:
    """Extract structured facts from tool exchanges without LLM."""
    facts = []
    for msg in messages:
        role = msg.get("role", "")
        if role == "assistant" and "tool_calls" in msg:
            for tc in msg["tool_calls"]:
                fn = tc.get("function", {})
                name = fn.get("name", "unknown")
                try:
                    args = json.loads(fn.get("arguments", "{}"))
                except (json.JSONDecodeError, TypeError):
                    args = {}
                facts.append(self._format_tool_fact(name, args))
        elif role == "tool":
            content = msg.get("content", "")
            outcome = self._extract_outcome(content)
            if outcome and facts:
                facts[-1] += f" → {outcome}"

    if not facts:
        return ""
    return "Completed: " + "; ".join(facts)
```

#### 3.6 `_format_tool_fact()` — 事实格式化

```python
def _format_tool_fact(self, tool_name: str, args: dict) -> str:
    """Format a tool call as a brief fact string."""
    if tool_name == "bash":
        cmd = args.get("command", "")
        if len(cmd) > 80:
            cmd = cmd[:77] + "..."
        return f"ran `{cmd}`"
    elif tool_name == "read_file":
        return f"read `{args.get('path', '?')}`"
    elif tool_name == "write_file":
        path = args.get("path", "?")
        content = args.get("content", "")
        return f"wrote {len(content)} chars to `{path}`"
    elif tool_name == "list_directory":
        return f"listed `{args.get('path', '.')}`"
    elif tool_name == "create_directory":
        return f"created dir `{args.get('path', '?')}`"
    elif tool_name == "file_exists":
        return f"checked `{args.get('path', '?')}`"
    elif tool_name == "update_plan":
        return "updated task plan"
    elif tool_name == "load_skill":
        return f"loaded skill `{args.get('name', '?')}`"
    else:
        return f"called {tool_name}"
```

**覆盖的工具**：全部 8 个工具（6 个基础工具 + `update_plan` + `load_skill`）+ fallback。

#### 3.7 `_extract_outcome()` — 结果提取

```python
def _extract_outcome(self, content: str) -> str:
    """Extract a brief outcome summary from tool result content."""
    if not content:
        return ""
    if content.startswith("Error"):
        return f"error: {content[:100]}"
    if "exit code" in content:
        return content.strip()[:80]
    lines = content.count("\n") + 1
    if lines > 5:
        return f"{lines} lines"
    if "Successfully wrote" in content:
        return content.strip()[:80]
    first_line = content.split("\n")[0]
    if len(first_line) > 80:
        return first_line[:77] + "..."
    return first_line
```

#### 3.8 `_llm_summarize()` — LLM 摘要（可选）

```python
def _llm_summarize(self, messages: list[dict]) -> str:
    """Use LLM to generate a natural summary of tool exchanges."""
    condensed = []
    for msg in messages:
        role = msg.get("role", "")
        if role == "assistant" and "tool_calls" in msg:
            for tc in msg["tool_calls"]:
                fn = tc.get("function", {})
                condensed.append(
                    f"Tool call: {fn.get('name', '?')}({fn.get('arguments', '{}')[:200]})"
                )
            if msg.get("content"):
                condensed.append(f"Assistant said: {msg['content'][:200]}")
        elif role == "tool":
            content = msg.get("content", "")
            if len(content) > 300:
                content = content[:150] + "..." + content[-150:]
            condensed.append(f"Tool result: {content}")

    prompt_messages = [
        {
            "role": "system",
            "content": (
                "You are a summarizer. Given a sequence of tool calls and results "
                "from a coding agent, write a concise 2-4 sentence summary of what "
                "was accomplished. Focus on: files read/written, commands run and "
                "their outcomes, any errors encountered. Do NOT include code snippets. "
                "Start with '[SUMMARY]'. Be factual and brief."
            ),
        },
        {"role": "user", "content": "\n".join(condensed)},
    ]

    response = self.llm_provider.chat_completion(
        messages=prompt_messages,
        tools=None,
        max_tokens=256,
        temperature=0.3,
    )
    summary = getattr(response, "content", "") or ""
    if not summary.startswith("[SUMMARY]"):
        summary = "[SUMMARY] " + summary
    return summary
```

**降级策略**：在 `_summarize_group` 中用 try-except 包装 LLM 调用，失败时 fallback 到规则模式：

```python
def _summarize_group(self, messages: list[dict]) -> str:
    if self.use_llm and self.llm_provider:
        try:
            return self._llm_summarize(messages)
        except Exception as e:
            logger.warning("LLM summarization failed, falling back to rule-based: %s", e)
    return self._rule_based_summarize(messages)
```

---

### 4. `src/context_manager/macro_compressor.py` — Layer 3: 全量上下文重建

#### 4.1 模块结构

```python
"""Layer 3: Full context rebuild via LLM summarization.

When total context is very large (even after Layer 1 and Layer 2),
rebuild the entire conversation into a structured summary + recent
window. This is the most aggressive compression, requiring an LLM call.
"""

from context_manager.micro_compressor import estimate_tokens
from logger import get_logger

logger = get_logger(__name__)

MACRO_SUMMARY_PROMPT = """\
You are a conversation compressor for a coding agent. Given the full conversation
history, produce a structured summary that preserves all critical context.

Output EXACTLY this format (no markdown fences, no preamble):

[CONTEXT SUMMARY]
## Completed Work
<Summarize what was accomplished: files read/written, commands run, tests passed/failed>

## Current State
<Describe the current state: what files exist, what was modified, any errors>

## Key Decisions
<Important design choices, error workarounds, user corrections — if any>

Rules:
- Be factual and specific (file paths, command names, outcomes)
- Do NOT include full code snippets — describe what code does
- Keep total output under 800 words
- If the conversation is short, be correspondingly brief
"""


class MacroCompressor:
    """Rebuild entire conversation into structured summary + recent window."""

    def __init__(
        self,
        token_threshold: int = 32000,
        keep_recent: int = 12,
        llm_provider=None,
        progress_tracker=None,
    ): ...

    def should_compress(self, messages: list[dict]) -> bool: ...
    def compress(self, messages: list[dict]) -> list[dict]: ...
    def _build_history_digest(self, messages: list[dict]) -> str: ...
    def _generate_summary(self, history_text: str, progress_context: str) -> str: ...
```

#### 4.2 `should_compress()` — 带 LLM 可用性检查

```python
def should_compress(self, messages: list[dict]) -> bool:
    """Check if total context exceeds the macro threshold.

    Returns False if no LLM provider is available (Layer 3 requires LLM).
    """
    if not self.llm_provider:
        return False
    total_tokens = sum(
        estimate_tokens(str(m.get("content", ""))) for m in messages
    )
    return total_tokens >= self.token_threshold
```

#### 4.3 `compress()` — 全量重建

```python
def compress(self, messages: list[dict]) -> list[dict]:
    """Rebuild conversation into structured summary + recent window.

    Returns:
        [system_prompt, context_summary, original_task, progress, ...recent]
    """
    if len(messages) <= 2 + self.keep_recent:
        return messages

    system_prompt = messages[0]
    recent = messages[-self.keep_recent:]

    # Build condensed history for LLM
    history_text = self._build_history_digest(
        messages[1:len(messages) - self.keep_recent]
    )

    # Add ProgressTracker context
    progress_context = ""
    if self.progress_tracker and self.progress_tracker.has_plan:
        progress_context = f"\n\nCurrent task plan:\n{self.progress_tracker.format_summary()}"

    # Call LLM
    try:
        summary = self._generate_summary(history_text, progress_context)
    except Exception as e:
        logger.error("Macro compression LLM call failed: %s", e)
        # Fallback: system + first_user + recent
        first_user = messages[1] if len(messages) > 1 else None
        result = [system_prompt]
        if first_user:
            result.append(first_user)
        result.extend(recent)
        return result

    # Build compressed message list
    result = [
        system_prompt,
        {"role": "system", "content": summary},
    ]

    # Preserve original task (first user message)
    if len(messages) > 1 and messages[1].get("role") == "user":
        result.append(messages[1])

    # Inject progress if tracker active
    if self.progress_tracker and self.progress_tracker.has_plan:
        result.append({
            "role": "system",
            "content": self.progress_tracker.format_summary(),
        })

    result.extend(recent)

    before_tokens = sum(estimate_tokens(str(m.get("content", ""))) for m in messages)
    after_tokens = sum(estimate_tokens(str(m.get("content", ""))) for m in result)
    logger.info(
        "Macro compression: %d -> %d messages, ~%d -> ~%d tokens",
        len(messages), len(result), before_tokens, after_tokens,
    )
    return result
```

#### 4.4 `_build_history_digest()` — 精简历史

```python
def _build_history_digest(self, messages: list[dict]) -> str:
    """Build a condensed text representation of the history for the LLM."""
    parts = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")

        if role == "system":
            if content and not content.startswith("[TASK PROGRESS]"):
                parts.append(f"[SYSTEM] {content[:200]}")
        elif role == "user":
            parts.append(f"[USER] {content[:500]}")
        elif role == "assistant":
            if content:
                parts.append(f"[ASSISTANT] {content[:300]}")
            if "tool_calls" in msg:
                for tc in msg["tool_calls"]:
                    fn = tc.get("function", {})
                    args_str = fn.get("arguments", "{}")
                    if len(args_str) > 200:
                        args_str = args_str[:200] + "..."
                    parts.append(f"[TOOL_CALL] {fn.get('name', '?')}({args_str})")
        elif role == "tool":
            if len(content) > 300:
                content = content[:150] + "..." + content[-150:]
            parts.append(f"[TOOL_RESULT] {content}")

    return "\n".join(parts)
```

**关键设计点**：每条消息独立截断（`[:200]`/`[:500]`/`[:300]`），确保 digest 总长度可控。`[TASK PROGRESS]` 系统消息被跳过（ProgressTracker 数据通过 `progress_context` 单独传入）。

#### 4.5 `_generate_summary()` — LLM 调用

```python
def _generate_summary(self, history_text: str, progress_context: str) -> str:
    """Call the LLM to generate a structured conversation summary."""
    prompt_messages = [
        {"role": "system", "content": MACRO_SUMMARY_PROMPT},
        {"role": "user", "content": history_text + progress_context},
    ]

    response = self.llm_provider.chat_completion(
        messages=prompt_messages,
        tools=None,
        max_tokens=1024,
        temperature=0.3,
    )

    summary = getattr(response, "content", "") or ""
    if not summary.startswith("[CONTEXT SUMMARY]"):
        summary = "[CONTEXT SUMMARY]\n" + summary
    return summary
```

---

### 5. `src/context_manager/pipeline.py` — 编排器

#### 5.1 模块结构

```python
"""Orchestration pipeline: composes Layer 1, 2, 3 compression.

Drop-in replacement for ContextCompactor. Provides the same
``should_compact`` + ``compact`` interface, plus a new
``compress_tool_result`` entry point for Layer 1.
"""

from context_manager.macro_compressor import MacroCompressor
from context_manager.meso_compressor import MesoCompressor
from context_manager.micro_compressor import MicroCompressor, estimate_tokens
from context_manager.tracker import ProgressTracker
from logger import get_logger

logger = get_logger(__name__)


class ContextPipeline:
    """Three-layer context compression pipeline."""

    def __init__(
        self,
        micro_max_chars: int = 4000,
        micro_keep_head_lines: int = 10,
        micro_keep_tail_lines: int = 15,
        meso_message_threshold: int = 20,
        meso_token_threshold: int = 8000,
        meso_use_llm: bool = False,
        macro_token_threshold: int = 32000,
        keep_recent: int = 12,
        llm_provider=None,
        progress_tracker: ProgressTracker = None,
    ):
        self.micro = MicroCompressor(
            max_chars=micro_max_chars,
            keep_head_lines=micro_keep_head_lines,
            keep_tail_lines=micro_keep_tail_lines,
        )
        self.meso = MesoCompressor(
            meso_message_threshold=meso_message_threshold,
            meso_token_threshold=meso_token_threshold,
            keep_recent=keep_recent,
            use_llm=meso_use_llm,
            llm_provider=llm_provider,
        )
        self.macro = MacroCompressor(
            token_threshold=macro_token_threshold,
            keep_recent=keep_recent,
            llm_provider=llm_provider,
            progress_tracker=progress_tracker,
        )
        self._stats = {
            "micro_compressions": 0,
            "meso_compressions": 0,
            "macro_compressions": 0,
        }

    @property
    def stats(self) -> dict:
        return dict(self._stats)
```

#### 5.2 `compress_tool_result()` — Layer 1 入口

```python
def compress_tool_result(self, tool_name: str, content: str) -> str:
    """Layer 1 entry point: compress a tool result at creation time.

    Called from _handle_tool_call() instead of the old dumb truncation.
    """
    result = self.micro.compress(tool_name, content)
    if len(result) < len(content):
        self._stats["micro_compressions"] += 1
    return result
```

#### 5.3 `should_compact()` — 兼容接口

```python
def should_compact(self, messages: list[dict]) -> bool:
    """Check if any layer needs to run.

    Returns True if Layer 2 OR Layer 3 would fire.
    (Layer 1 runs eagerly via compress_tool_result, not checked here.)
    """
    return self.meso.should_compress(messages) or self.macro.should_compress(messages)
```

#### 5.4 `compact()` — 级联执行

```python
def compact(self, messages: list[dict]) -> list[dict]:
    """Run the full compression pipeline.

    Cascade: Layer 1 (defensive re-pass) → Layer 2 → Layer 3.
    """
    original_count = len(messages)
    original_tokens = sum(estimate_tokens(str(m.get("content", ""))) for m in messages)

    # Layer 1: defensive re-pass on any uncompressed tool results
    result = self._apply_micro_pass(messages)

    # Layer 2: section-level compression
    if self.meso.should_compress(result):
        before = len(result)
        result = self.meso.compress(result)
        if len(result) < before:
            self._stats["meso_compressions"] += 1
            logger.info("Layer 2 (Meso): %d -> %d messages", before, len(result))

    # Layer 3: full context rebuild (only if still too large)
    if self.macro.should_compress(result):
        before = len(result)
        result = self.macro.compress(result)
        if len(result) < before:
            self._stats["macro_compressions"] += 1
            logger.info("Layer 3 (Macro): %d -> %d messages", before, len(result))

    final_tokens = sum(estimate_tokens(str(m.get("content", ""))) for m in result)
    logger.info(
        "Pipeline: %d msgs (~%d tokens) -> %d msgs (~%d tokens)",
        original_count, original_tokens, len(result), final_tokens,
    )
    return result
```

#### 5.5 `_apply_micro_pass()` — 防御性重扫

```python
def _apply_micro_pass(self, messages: list[dict]) -> list[dict]:
    """Re-apply Layer 1 to any uncompressed tool result messages."""
    result = []
    for msg in messages:
        if msg.get("role") == "tool":
            compressed = self.micro.compress_message(msg)
            result.append(compressed)
        else:
            result.append(msg)
    return result
```

**为什么需要防御性重扫**：Layer 1 在 `_handle_tool_call` 中实时生效，但可能存在漏网情况（如从外部注入的消息、subagent 返回的大结果等）。防御性重扫确保所有 tool 消息都经过 Layer 1 处理。

---

### 6. `src/context_manager/__init__.py` — 新增导出

```python
"""Context management — progress tracking and context compaction for the agent loop."""

from context_manager.context import ContextCompactor
from context_manager.macro_compressor import MacroCompressor
from context_manager.meso_compressor import MesoCompressor
from context_manager.micro_compressor import MicroCompressor, estimate_tokens
from context_manager.pipeline import ContextPipeline
from context_manager.tracker import ProgressTracker

__all__ = [
    "ContextCompactor",      # Legacy — kept for backward compatibility
    "ContextPipeline",       # New: three-layer pipeline (preferred)
    "MacroCompressor",
    "MesoCompressor",
    "MicroCompressor",
    "ProgressTracker",
    "estimate_tokens",
]
```

**改动范围**：新增 4 个 import + 5 个 `__all__` 条目。

---

### 7. `src/agent/async_loop.py` — 替换 ContextCompactor 为 ContextPipeline

#### 7.1 新增 import

```python
# 替换:
# from context_manager.context import ContextCompactor
# 为:
from context_manager.pipeline import ContextPipeline
```

#### 7.2 `__init__` 中替换实例化

```python
# BEFORE (L54-57):
# self.context_compactor = ContextCompactor(
#     max_messages=settings.CONTEXT_MAX_MESSAGES,
#     keep_recent=settings.CONTEXT_KEEP_RECENT,
# )

# AFTER:
self.context_pipeline = ContextPipeline(
    micro_max_chars=settings.CONTEXT_MICRO_MAX_CHARS,
    micro_keep_head_lines=settings.CONTEXT_MICRO_KEEP_HEAD_LINES,
    micro_keep_tail_lines=settings.CONTEXT_MICRO_KEEP_TAIL_LINES,
    meso_message_threshold=settings.CONTEXT_MESO_MESSAGE_THRESHOLD,
    meso_token_threshold=settings.CONTEXT_MESO_TOKEN_THRESHOLD,
    meso_use_llm=settings.CONTEXT_MESO_USE_LLM,
    macro_token_threshold=settings.CONTEXT_MACRO_TOKEN_THRESHOLD,
    keep_recent=settings.CONTEXT_KEEP_RECENT,
    llm_provider=self.llm_provider,
    progress_tracker=self.progress_tracker,
)
```

#### 7.3 `_handle_tool_call` 中替换截断逻辑

```python
# BEFORE (L130-134):
# # Truncation
# max_output = settings.MAX_TOOL_OUTPUT
# if len(output) > max_output:
#     logger.warning("Tool output truncated: %d chars (max %d)", len(output), max_output)
#     output = output[:max_output] + f"\n... [truncated, {len(output)} chars total]"

# AFTER:
output = self.context_pipeline.compress_tool_result(tool_name, output)
```

#### 7.4 `chat()` 循环中替换压缩调用

```python
# BEFORE (L166-169):
# if self.context_compactor.should_compact(self.messages):
#     before = len(self.messages)
#     self.messages = self.context_compactor.compact(self.messages)
#     logger.info("Context compacted: %d -> %d messages", before, len(self.messages))

# AFTER:
if self.context_pipeline.should_compact(self.messages):
    before = len(self.messages)
    self.messages = self.context_pipeline.compact(self.messages)
    logger.info("Context compacted: %d -> %d messages", before, len(self.messages))
```

**改动范围**：1 个 import 替换 + 3 处代码替换。`_inject_progress()`、`_call_llm()` 及其他方法不变。

---

### 8. `src/agent/loop.py` — 同步 Agent 同等改造

改造方式与 `async_loop.py` 完全一致：

#### 8.1 替换 import

```python
# 替换:
# from context_manager.context import ContextCompactor
# 为:
from context_manager.pipeline import ContextPipeline
```

#### 8.2 `__init__` 替换实例化（L53-56）

同 async_loop.py 的 7.2 节。

#### 8.3 `_handle_tool_call` 替换截断（L108-112）

同 async_loop.py 的 7.3 节。

#### 8.4 `chat()` 循环替换压缩调用（L145-148）

同 async_loop.py 的 7.4 节。

**注意**：同步 Agent 的 `compress_tool_result` 调用是同步的（`MicroCompressor.compress` 是纯规则操作，不涉及 async），无需 `await`。

---

### 9. `src/context_manager/context.py` — 不变

保留 `ContextCompactor` 类不做任何修改。理由：
- 现有 12 个测试用例继续通过
- 作为 legacy fallback，用户可通过环境变量选择旧行为
- 不增加维护成本（~80 行自包含代码）

---

## 测试策略

### 单元测试矩阵

| 文件 | 测试数 | 覆盖要点 |
|---|---|---|
| `test_micro_compressor.py` | 10 | 各工具策略（read_file/bash/list_directory/write_file/generic）、`estimate_tokens`、`compress_message` 元数据保留、边界情况（空内容/单行/恰好在阈值） |
| `test_meso_compressor.py` | 13 | `_group_tool_exchanges` 分组逻辑、规则摘要各工具事实提取、`_extract_outcome` 错误/成功检测、双条件阈值触发、head/tail 保留、幂等性、LLM mock + fallback |
| `test_macro_compressor.py` | 8 | `_build_history_digest` 格式化、token 阈值检查、无 LLM 时不触发、Mock LLM 输出结构、ProgressTracker 集成、LLM 失败降级 |
| `test_pipeline.py` | 6 | `compress_tool_result` 委托、`should_compact` OR 逻辑、级联顺序、Layer 2 阻止 Layer 3、stats 计数、防御性 micro pass |
| `test_pipeline_integration.py` | 2 | 60 条消息完整对话压缩、压缩后关键信息保留验证 |

### 测试实现模式

遵循现有项目模式：`unittest.TestCase` + `asyncio.run()` + `unittest.mock`：

```python
# 示例: Mock LLM provider
from unittest.mock import MagicMock

mock_provider = MagicMock()
mock_response = MagicMock()
mock_response.content = "[CONTEXT SUMMARY]\n## Completed Work\n..."
mock_response.tool_calls = []
mock_provider.chat_completion.return_value = mock_response
```

```python
# 示例: 构造合成消息
def make_messages(n: int) -> list[dict]:
    """Build a synthetic conversation with n messages."""
    msgs = [
        {"role": "system", "content": "You are a coding assistant."},
        {"role": "user", "content": "Refactor the auth module."},
    ]
    for i in range(n - 2):
        if i % 2 == 0:
            msgs.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": f"tc_{i}",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path": "src/auth.py"}',
                    },
                }],
            })
        else:
            msgs.append({
                "role": "tool",
                "tool_call_id": f"tc_{i-1}",
                "content": "x" * 5000,  # 大文件内容
            })
    return msgs
```

## 数据流：三层压缩完整生命周期

```
Agent 启动
  │
  ├─ __init__() 中:
  │   ├─ context_pipeline = ContextPipeline(llm_provider=..., progress_tracker=...)
  │   └─ pipeline.micro = MicroCompressor(max_chars=4000, ...)
  │      pipeline.meso = MesoCompressor(threshold=20, ...)
  │      pipeline.macro = MacroCompressor(threshold=32000, ...)
  │
第 1-10 轮对话 (无压缩触发)
  │
  ├─ 每次 _handle_tool_call:
  │   ├─ output = tool_registry.execute("read_file", args)  → 10KB 文件内容
  │   ├─ output = context_pipeline.compress_tool_result("read_file", output)
  │   │   └─ Layer 1: 10KB > 4KB → head 10 行 + tail 10 行 → ~3KB
  │   └─ messages.append({role: "tool", content: ~3KB})
  │
  ├─ 每次 chat() 循环:
  │   ├─ context_pipeline.should_compact(messages)
  │   │   ├─ meso.should_compress: 中间段 10 条 < 20 条阈值 → False
  │   │   └─ macro.should_compress: 总 ~5K tokens < 32K → False
  │   └─ → 不压缩，继续
  │
第 15-25 轮对话 (Layer 2 触发)
  │
  ├─ messages 增长到 50 条
  │
  ├─ context_pipeline.should_compact(messages):
  │   └─ meso.should_compress: 中间段 36 条 > 20 条阈值 → True
  │
  ├─ context_pipeline.compact(messages):
  │   ├─ Layer 1 defensive pass: 所有 tool 消息已被 Layer 1 压缩 → no-op
  │   ├─ Layer 2:
  │   │   ├─ _get_middle: 提取 messages[2:38] (36 条)
  │   │   ├─ _group_tool_exchanges: 分为 5 组 tool_exchange + 3 组 other
  │   │   ├─ 每组 tool_exchange → _rule_based_summarize:
  │   │   │   "[SUMMARY] Completed: read src/auth.py → 245 lines;
  │   │   │    ran 'pytest tests/' → 12 passed, 0 failed; ..."
  │   │   └─ 结果: 36 条 → 8 条 (5 条摘要 + 3 条 other)
  │   ├─ Layer 3 检查:
  │   │   └─ 总 ~12K tokens < 32K → 不触发
  │   └─ 最终: 50 条 → 22 条
  │
  └─ logger.info: "Pipeline: 50 msgs (~25000 tokens) -> 22 msgs (~12000 tokens)"
  │
第 50+ 轮对话 (极端情况: Layer 3 触发)
  │
  ├─ 经过 Layer 2 后，消息仍在增长（持续的长任务）
  ├─ 总 token 估算超过 32K
  │
  ├─ context_pipeline.compact(messages):
  │   ├─ Layer 1: defensive pass
  │   ├─ Layer 2: 已压缩过的中间段，压缩空间不大
  │   ├─ Layer 3:
  │   │   ├─ _build_history_digest: 精简历史 ~5000 chars
  │   │   ├─ progress_context: "[TASK PROGRESS] ✓ [1/4] ... → [2/4] ..."
  │   │   ├─ LLM 调用 → "[CONTEXT SUMMARY]\n## Completed Work\n..."
  │   │   └─ 输出: [system, summary, original_task, progress, ...recent 12 条]
  │   └─ 最终: 30 条 → 17 条, ~35K tokens → ~8K tokens
  │
  └─ Agent 继续工作，上下文干净
```

## 边界情况处理

| 场景 | 处理方式 |
|---|---|
| Layer 1 head-tail 截断丢失中间关键代码 | `keep_head_lines + keep_tail_lines = 25` 行保留，覆盖多数函数签名和末尾逻辑。Agent 可重新 `read_file` 获取完整内容 |
| Layer 2 规则摘要不够准确 | 提供 `CONTEXT_MESO_USE_LLM=true` opt-in 选项。规则模式保留文件路径和命令名，Agent 可按需重新获取 |
| Layer 3 LLM 调用失败（网络/API 异常） | try-except 捕获，fallback 为 system + first_user + recent。Agent 仍可继续工作 |
| 无 LLM provider（如测试环境） | `MacroCompressor.should_compress()` 返回 False，Layer 3 不触发。Layer 1 + Layer 2 独立工作 |
| Token 估算不准（中文高估、代码低估） | 估算仅用于阈值判断，不用于精确计费。阈值留有余量（32K 阈值 ≈ 128K chars，实际中文可能只有 ~40K token） |
| 消息列表过短（≤ 2 + keep_recent） | `should_compact` 各层均返回 False / `_get_middle` 返回空列表。pipeline 直接返回原消息列表 |
| 压缩后消息数反而增加 | `compact()` 中 `if len(result) >= len(messages): return messages` 安全守卫，各层均实现 |
| `[SUMMARY]` 消息被 `_inject_progress` 误删 | `_inject_progress` 仅过滤 `[TASK PROGRESS]` 前缀，不会误伤 `[SUMMARY]` |
| `[CONTEXT SUMMARY]` 消息被 `_inject_progress` 误删 | 同上，前缀不同 |
| 多次触发 Layer 2（已压缩的摘要被再次压缩） | 已压缩的 `[SUMMARY]` 消息是 `role: "assistant"` 且无 `tool_calls`，被 `_group_tool_exchanges` 归为 "other" 组，不参与 tool_exchange 分组。幂等安全 |
| 防御性 micro pass 重复压缩已压缩的消息 | `MicroCompressor.compress_message` 检查 `len(content) <= self.max_chars`，已压缩的消息通常小于阈值，直接返回 |
| `progress_tracker` 为 None（未注入） | `MacroCompressor` 中 `if self.progress_tracker and self.progress_tracker.has_plan` 双重检查，跳过 tracker 集成 |

## 风险与缓解

| 风险 | 概率 | 影响 | 缓解措施 |
|---|---|---|---|
| Layer 1 head-tail 截断丢失中间关键代码 | 中 | 中 | `keep_head + keep_tail = 25` 行保留；Agent 可重新 `read_file` 获取完整内容；日志记录压缩比例便于调优 |
| Layer 2 规则摘要遗漏关键细节 | 中 | 中 | 摘要中保留文件路径和命令名（Agent 可按需重新获取）；提供 LLM opt-in 选项 |
| Layer 3 LLM 调用失败 | 低 | 高 | 完善 fallback（system + first_user + recent）；try-except 覆盖所有异常 |
| Pipeline 引入复杂度，增加调试难度 | 低 | 中 | 每层独立日志；`pipeline.stats` 提供可观测性；可单独禁用 Layer 2/3（调高阈值） |
| `estimate_tokens` 对中文文本高估 | 中 | 低 | 阈值留有余量（32K token ≈ 128K chars）；高估意味着更早触发压缩，不会导致上下文溢出 |
| Layer 2 LLM 模式增加 API 开销 | 低 | 低 | 默认关闭（`false`）；仅在用户显式设置环境变量时启用 |
| 向后兼容性：替换 ContextCompactor 导致行为变化 | 低 | 高 | `ContextCompactor` 保留不删除；`ContextPipeline` 是超集（更优的压缩质量）；现有 12 个测试继续通过 |
| `_STRATEGIES` 未绑定方法调用方式可能不直观 | 低 | 低 | 添加注释说明 `strategy(self, content)` 的调用方式；或在 `__init__` 中用 `functools.partial` 绑定 |

## 实现顺序

按依赖关系分 5 个阶段：

**阶段 1：基础设施（零风险，无行为变化）**
1. `src/config.py` — 新增 7 个配置项
2. `src/context_manager/micro_compressor.py` — `estimate_tokens()` + `MicroCompressor`

**阶段 2：Layer 2 + Layer 3（无行为变化，新模块独立）**
3. `src/context_manager/meso_compressor.py` — `MesoCompressor`
4. `src/context_manager/macro_compressor.py` — `MacroCompressor`

**阶段 3：编排器 + 集成（行为变化）**
5. `src/context_manager/pipeline.py` — `ContextPipeline`
6. `src/context_manager/__init__.py` — 新增导出
7. `src/agent/async_loop.py` — 替换 import + `__init__` + `_handle_tool_call` + `chat()`
8. `src/agent/loop.py` — 同上（同步版）

**阶段 4：单元测试**
9. `tests/context_manager/test_micro_compressor.py`
10. `tests/context_manager/test_meso_compressor.py`
11. `tests/context_manager/test_macro_compressor.py`
12. `tests/context_manager/test_pipeline.py`

**阶段 5：集成测试**
13. `tests/context_manager/test_pipeline_integration.py`

## 验证方式

1. **单元测试**：
   ```bash
   PYTHONPATH=src .venv/bin/python -m unittest tests.context_manager.test_micro_compressor -v
   PYTHONPATH=src .venv/bin/python -m unittest tests.context_manager.test_meso_compressor -v
   PYTHONPATH=src .venv/bin/python -m unittest tests.context_manager.test_macro_compressor -v
   PYTHONPATH=src .venv/bin/python -m unittest tests.context_manager.test_pipeline -v
   ```

2. **全量回归**：
   ```bash
   PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
   ```
   确保现有 `test_context.py`（12 个用例）和全部其他测试不受影响。

3. **集成测试**：
   ```bash
   PYTHONPATH=src .venv/bin/python -m unittest tests.context_manager.test_pipeline_integration -v
   ```

4. **手动验证**：启动 CLI，执行一个多步骤任务（如"读取 10 个文件并总结项目结构"），观察日志：
   ```bash
   PYTHONPATH=src .venv/bin/python -m cli.main chat
   # 执行长任务后:
   grep -E "MicroCompressor|Layer|Pipeline" logs/mini_agent.log
   ```
   期望看到：
   - `MicroCompressor: read_file 10234 -> 3891 chars (62% reduction)` — Layer 1 工作
   - `Layer 2 (Meso): 28 -> 15 messages` — Layer 2 工作
   - `Pipeline: 40 msgs (~12000 tokens) -> 27 msgs (~6500 tokens)` — 整体统计

5. **Pre-commit 检查**：
   ```bash
   .venv/bin/pre-commit run --all-files
   ```
   确保 ruff lint（含 `TID252` ban-relative-imports）和 format 通过。

## 文件变更清单总览

| 文件 | 类型 | 行数估算 | 说明 |
|---|---|---|---|
| `src/config.py` | 修改 | +10 | 7 个新配置项 |
| `src/context_manager/micro_compressor.py` | **新建** | ~100 | `estimate_tokens` + `MicroCompressor` |
| `src/context_manager/meso_compressor.py` | **新建** | ~180 | `MesoCompressor`（规则 + 可选 LLM） |
| `src/context_manager/macro_compressor.py` | **新建** | ~130 | `MacroCompressor` + ProgressTracker 集成 |
| `src/context_manager/pipeline.py` | **新建** | ~80 | `ContextPipeline` 编排器 |
| `src/context_manager/__init__.py` | 修改 | +7 | 新增 4 个 import + 5 个 `__all__` 条目 |
| `src/agent/async_loop.py` | 修改 | ~20 | import 替换 + `__init__` + `_handle_tool_call` + `chat()` |
| `src/agent/loop.py` | 修改 | ~20 | 同上（同步版） |
| `src/context_manager/context.py` | **不变** | 0 | 保留 `ContextCompactor` |
| `src/context_manager/tracker.py` | **不变** | 0 | Layer 3 读取但不修改 |
| `tests/context_manager/test_micro_compressor.py` | **新建** | ~150 | Layer 1 单元测试 |
| `tests/context_manager/test_meso_compressor.py` | **新建** | ~200 | Layer 2 单元测试 |
| `tests/context_manager/test_macro_compressor.py` | **新建** | ~130 | Layer 3 单元测试 |
| `tests/context_manager/test_pipeline.py` | **新建** | ~100 | 编排器单元测试 |
| `tests/context_manager/test_pipeline_integration.py` | **新建** | ~100 | 端到端集成测试 |

**总计**：9 个新文件，4 个修改文件，~590 行新代码 + ~580 行测试。
