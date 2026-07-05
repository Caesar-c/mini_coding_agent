# PRD: 三层上下文压缩 — 无限会话的渐进式上下文管理

> **阶段**: s06 · **前置**: s05 (Skill Loading) · **后续**: s07 (待定)
>
> 核心理念: *"上下文总会满, 要有办法腾地方"* — 三层压缩策略, 换来无限会话。从单条消息的智能截断, 到段落级的工具摘要, 再到全量重建, 层层递进、代价递增。

## 1. 背景与动机

### 问题

当前 `ContextCompactor` 是一个单层规则引擎: 当 `messages[]` 超过 40 条时, 一次性丢弃中间段所有 tool 结果。这种"要么不管, 要么全丢"的策略存在严重缺陷:

- **信息丢失严重**: tool 结果被整条删除, LLM 丢失已读文件内容、命令输出、错误信息。压缩后 Agent 可能重复读取同一文件, 浪费 token
- **截断策略粗糙**: 单条 tool 输出的截断是 `output[:8000]` 前缀截断。文件末尾的 import 语句、bash 输出尾部的错误信息和 exit code 全部丢失
- **无渐进压缩**: 消息从 0 涨到 40 条期间不做任何压缩, 然后一次性大幅裁剪。用户体验是"Agent 突然忘了之前做过什么"
- **无数值感知**: 阈值基于消息条数而非 token。40 条短消息可能只有 8K token, 40 条含大文件读取的消息可能超过 100K token

### 典型场景

```
场景: 用户让 Agent 重构一个模块 (约 10 个文件, 30+ 次工具调用)

当前行为 (单层压缩):
  → 前 40 条消息: 不做任何压缩, 上下文涨到 80K token
  → 第 41 条消息触发压缩: 中间段 20 条 tool 结果全部删除
  → Agent 丢失了 "src/auth.py 有 3 个函数需要改" 的关键上下文
  → 第 45 条消息又读了一遍 src/auth.py, 重复消耗 4K token
  → 第 50 条再次触发压缩, 又丢失一轮上下文
  → 最终 Agent 的回答质量明显下降, 遗漏了之前发现的问题

期望行为 (三层压缩):
  → Layer 1 (实时): 每次 read_file 返回 10KB, 智能截断为 4KB (保留首尾)
  → Layer 2 (第 25 条): 中间段 15 条 tool 调用压缩为摘要段落:
      "[SUMMARY] 已完成: 读取 src/auth.py (3 个函数), 读取 src/db.py (连接池),
       运行 pytest — 12 passed, 修改 src/utils.py (1.2KB)"
  → Layer 3 (仅在极端情况): 全量重建为结构化摘要 + 最近 12 条
  → Agent 始终知道已读文件、已跑命令、当前进度
```

### 与 s04/s05 的关系

- **s04 Subagent** 解决了"大任务拆小, 子上下文隔离"。但父 Agent 的上下文仍然会膨胀
- **s05 Skill Loading** 让知识按需加载, 减少 system prompt 浪费。但 tool 结果仍然是上下文膨胀的主因
- **s06 三层压缩** 解决正交问题: "已产生的上下文如何高效压缩, 让 Agent 在长任务中保持记忆力"

三者组合: Subagent 处理子任务的上下文隔离 → Skill 减少 system prompt 浪费 → 三层压缩管理父 Agent 的长期记忆。

## 2. 目标与非目标

### 目标

| #   | 目标           | 衡量标准                                                              |
| --- | ------------ | ----------------------------------------------------------------- |
| G1  | 渐进式压缩        | 压缩在三个粒度层层递进, 不做"全有或全无"的裁剪。每次压缩保留可追溯的摘要                    |
| G2  | 零成本优先        | Layer 1 (规则) 和 Layer 2 (规则模式) 不消耗 API token。仅在 Layer 3 或 Layer 2 LLM 模式时调用 LLM |
| G3  | 智能截断         | 单条 tool 结果按工具类型做差异化截断: 文件保留首尾行, bash 保留 stderr, 目录限制条目数      |
| G4  | 可观测性         | 每层压缩独立记录日志 (触发次数、压缩前后消息数/token 数), 便于调优阈值                    |
| G5  | Drop-in 替换    | `ContextPipeline` 兼容 `ContextCompactor` 接口 (`should_compact` + `compact`), Agent 循环改动最小化 |
| G6  | 向后兼容         | 保留 `ContextCompactor` 不删除, 现有测试继续通过                                |

### 非目标

- **不做** token 精确计数 (使用 `len(text) // 4` 粗估, 足够驱动阈值判断)
- **不做** 向量检索/RAG 式记忆 (超出本阶段范围, 可作为 s07 考虑)
- **不做** 跨会话记忆持久化 (本 PRD 仅解决单次会话内的上下文管理)
- **不做** 用户可控的"记忆固定" (pin messages) 机制
- **不做** 压缩内容的二次解压/恢复 (压缩是单向操作)

## 3. 架构设计

### 3.1 三层模型

```
┌─────────────────────────────────────────────────────────────────┐
│ Layer 3 — Macro (宏观): 全量上下文重建                             │
│                                                                 │
│ 触发: 总 token 估算 > 32K (~128K chars)                         │
│ 方法: LLM 生成 [CONTEXT SUMMARY], 含 Completed/Current/Remaining│
│ 成本: ~2-5s, ~1K tokens API 消耗                                │
│ 频率: 极少触发 (Layer 2 通常已阻止总 token 涨到这个级别)            │
├─────────────────────────────────────────────────────────────────┤
│ Layer 2 — Meso (中观): 段落级工具摘要                              │
│                                                                 │
│ 触发: 中间段 > 20 条消息 或 > 8K token                           │
│ 方法: 连续 tool 调用组 → 结构化摘要 (规则或 LLM)                   │
│ 成本: ~0ms (规则) 或 ~2s (LLM, opt-in)                          │
│ 频率: 中等 (长任务中每 15-20 轮触发一次)                           │
├─────────────────────────────────────────────────────────────────┤
│ Layer 1 — Micro (微观): 单条消息智能截断                           │
│                                                                 │
│ 触发: 单条 tool 结果 > 4K chars                                  │
│ 方法: 按工具类型做差异化截断 (head+tail, stderr 保留, 条目数限制)    │
│ 成本: ~0ms, 纯规则                                               │
│ 频率: 每次工具调用都可能触发                                       │
└─────────────────────────────────────────────────────────────────┘
```

### 3.2 级联原则

```
代价从低到高, 逐层升级:

  Layer 1 (Micro)
    ↓ 已在 tool 返回时压缩, 但消息仍在累积
  Layer 2 (Meso)
    ↓ 中间段压缩为摘要, 通常足以控制总量
  Layer 3 (Macro)
    ↓ 仅当 Layer 2 不够时, 全量重建

每层输出作为下一层输入。Layer 2 压缩后如果总 token 已降至阈值以下,
Layer 3 不触发。这是最常见的情况。
```

### 3.3 组件交互

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
            ┌──────────────────────────────────────┐
            │         ContextPipeline              │
            │  (orchestrator — 编排三层压缩)         │
            │                                      │
            │  ┌──────────┐  ┌──────────┐  ┌──────┐│
            │  │  Layer 1 │  │  Layer 2 │  │Layer ││
            │  │  Micro   │  │  Meso    │  │  3   ││
            │  │Compressor│  │Compressor│  │Macro ││
            │  └──────────┘  └──────────┘  └──────┘│
            └──────────────┬───────────────────────┘
                           │ feeds Layer 3
                           ▼
                  ┌─────────────────┐
                  │ ProgressTracker │
                  │ (已有, 不修改)    │
                  └─────────────────┘
```

## 4. 详细设计

### 4.1 Layer 1: MicroCompressor

#### 设计原则

替代当前的"前缀截断" (`output[:max_output]`), 按工具类型做差异化智能截断:

| 工具             | 策略                                                                                          |
| -------------- | ------------------------------------------------------------------------------------------- |
| `read_file`    | 保留首 `keep_head_lines` (10) 行 + 尾 `keep_tail_lines` (10) 行, 中间插入 `[... N lines omitted ...]` |
| `bash`         | stderr 行全量保留 (错误信息不可丢), stdout 保留尾 `keep_tail_lines` (15) 行。Exit code ≠ 0 时保留全量 (失败输出通常短且重要) |
| `list_directory` | 最多保留 `max_dir_entries` (50) 条, 超出显示 `[... N more entries omitted ...]`                     |
| `write_file`   | No-op (确认消息本身就短)                                                                          |
| `file_exists`  | No-op                                                                                       |
| `load_skill`   | 同 `read_file` 策略                                                                           |
| 其他工具          | 通用 head-tail 压缩 (fallback)                                                                  |

#### 接口设计

```python
class MicroCompressor:
    """单条 tool 结果的智能截断. 纯规则, 零 API 开销."""

    def __init__(
        self,
        max_chars: int = 4000,
        keep_head_lines: int = 10,
        keep_tail_lines: int = 15,
        max_dir_entries: int = 50,
    ): ...

    def compress(self, tool_name: str, content: str) -> str:
        """按工具类型压缩 tool 结果. 小于 max_chars 直接返回."""
        ...

    def compress_message(self, msg: dict) -> dict:
        """对已有 tool 消息做防御性压缩 (保留 tool_call_id 等元数据)."""
        ...
```

#### Token 估算工具函数

```python
def estimate_tokens(text: str) -> int:
    """粗估 token 数: 英文/代码约 4 chars/token."""
    return len(text) // 4
```

此函数放在 `micro_compressor.py` 中, 供 Layer 2/3 复用。

### 4.2 Layer 2: MesoCompressor

#### 设计原则

将中间段连续的 tool 调用组 (assistant+tool_calls → tool → assistant+tool_calls → tool → ...) 压缩为结构化摘要, 替代当前"直接删除 tool 结果"的做法。

#### 两种模式

| 模式              | 触发条件                       | 成本   | 效果        |
| --------------- | -------------------------- | ---- | --------- |
| **规则模式** (默认) | 始终可用                       | ~0ms | 提取结构化事实  |
| **LLM 模式** (可选) | `CONTEXT_MESO_USE_LLM=true` | ~2s  | 更自然的上下文感知 |

#### 规则模式摘要示例

输入 (中间段 8 条消息):
```
assistant: tool_calls=[read_file(src/auth.py)]
tool: "class Auth:\n    def login(self)...\n    def logout(self)...\n..."  (3KB)
assistant: tool_calls=[read_file(src/db.py)]
tool: "import sqlite3\nclass Database:\n..."  (2KB)
assistant: tool_calls=[bash("pytest tests/")]
tool: "12 passed, 0 failed\n..."  (800 chars)
assistant: tool_calls=[write_file(src/utils.py)]
tool: "Successfully wrote 1234 chars to src/utils.py"
```

输出 (1 条摘要消息):
```
[SUMMARY] Completed: read src/auth.py (Auth class, 245 lines);
read src/db.py (Database class, 89 lines);
ran 'pytest tests/' → 12 passed, 0 failed;
wrote 1234 chars to src/utils.py
```

#### 接口设计

```python
class MesoCompressor:
    """中间段 tool 调用组的段落级摘要."""

    def __init__(
        self,
        meso_message_threshold: int = 20,
        meso_token_threshold: int = 8000,
        keep_recent: int = 12,
        use_llm: bool = False,
        llm_provider=None,
    ): ...

    def should_compress(self, messages: list[dict]) -> bool:
        """中间段消息数或 token 超阈值."""
        ...

    def compress(self, messages: list[dict]) -> list[dict]:
        """保留 head[0:2] + tail[-keep_recent:], 压缩中间."""
        ...

    def _group_tool_exchanges(self, messages: list[dict]) -> list[dict]:
        """将连续的 tool_call + tool_result 分组."""
        ...

    def _rule_based_summarize(self, messages: list[dict]) -> str:
        """从 tool 调用中提取结构化事实 (无 LLM)."""
        ...

    def _llm_summarize(self, messages: list[dict]) -> str:
        """调用 LLM 生成自然语言摘要 (可选, opt-in)."""
        ...
```

#### 事实提取规则

| 工具调用              | 提取的事实                              |
| ----------------- | ---------------------------------- |
| `bash(cmd)`       | `ran 'cmd'` + exit code (如有)      |
| `read_file(path)` | `read path (N lines)`              |
| `write_file(path)` | `wrote N chars to path`            |
| `list_directory`  | `listed dir_path`                  |
| `create_directory` | `created dir dir_path`             |
| `update_plan`     | `updated task plan`                |
| 其他                | `called tool_name`                 |

Tool 结果追加简短 outcome: 错误信息、exit code、行数等。

### 4.3 Layer 3: MacroCompressor

#### 设计原则

当总上下文即使经过 Layer 2 仍然很大时, 全量重建为结构化摘要。这是最昂贵的操作, 必须调用 LLM, 但能提供最彻底的压缩。

#### 输出格式

Layer 3 将整个对话 (除 system prompt 和最近 `keep_recent` 条) 重建为:

```
[CONTEXT SUMMARY]
## Completed Work
- 读取并分析了 src/auth.py, src/db.py, src/config.py
- 修改了 src/utils.py: 添加了 sanitize_input() 函数
- 运行 pytest: 12 passed, 0 failed

## Current State
- 正在重构 src/auth.py 的 login() 方法
- 已创建 backup 文件 src/auth.py.bak

## Key Decisions
- 选择 bcrypt 而非 argon2 (用户明确要求)
- 输入验证使用 whitelist 模式

## Remaining Tasks
→ [2/4] 重构 login() 方法 (in progress)
○ [3/4] 添加集成测试
○ [4/4] 更新 API 文档
```

#### 与 ProgressTracker 的集成

Layer 3 直接利用已有的 `ProgressTracker` 状态:

- `status == "done"` 的步骤 → 喂入 "Completed Work"
- `status == "in_progress"` 的步骤 → 喂入 "Remaining Tasks"
- `status == "pending"` 的步骤 → 喂入 "Remaining Tasks"
- `notes` 字段 → 丰富 "Key Decisions" 部分

这避免了让 LLM 重新从对话历史中推断任务状态 — 结构化数据比非结构化对话更准确。

#### 接口设计

```python
class MacroCompressor:
    """全量上下文重建. 需要 LLM, 是最昂贵但最彻底的压缩."""

    def __init__(
        self,
        token_threshold: int = 32000,
        keep_recent: int = 12,
        llm_provider=None,
        progress_tracker: ProgressTracker = None,
    ): ...

    def should_compress(self, messages: list[dict]) -> bool:
        """总 token 估算超阈值, 且 LLM provider 可用."""
        ...

    def compress(self, messages: list[dict]) -> list[dict]:
        """重建为: [system, summary, original_task, progress, ...recent]."""
        ...

    def _build_history_digest(self, messages: list[dict]) -> str:
        """构建精简历史 (每条消息截断后喂给 LLM)."""
        ...

    def _generate_summary(self, history_text: str, progress_context: str) -> str:
        """调用 LLM 生成结构化摘要."""
        ...
```

#### LLM 失败降级

当 LLM 调用失败时 (网络错误、超时、API 异常), 降级为保留基本结构:

```python
# Fallback: system_prompt + first_user_message + recent_window
# 不使用 LLM 摘要, 但保证 Agent 仍可继续工作
```

### 4.4 ContextPipeline — 编排器

#### 接口兼容

`ContextPipeline` 提供与 `ContextCompactor` 相同的接口, 实现 Drop-in 替换:

```python
class ContextPipeline:
    """三层压缩编排器. Drop-in 替换 ContextCompactor."""

    def should_compact(self, messages: list[dict]) -> bool:
        """Layer 2 或 Layer 3 需要触发时返回 True."""
        ...

    def compact(self, messages: list[dict]) -> list[dict]:
        """级联执行: Layer 1 (防御性) → Layer 2 → Layer 3."""
        ...

    def compress_tool_result(self, tool_name: str, content: str) -> str:
        """Layer 1 入口: 在 _handle_tool_call 中调用."""
        ...
```

#### 级联执行逻辑

```python
def compact(self, messages):
    # Step 1: Layer 1 防御性重扫 — 压缩任何未被压缩的 tool 结果
    result = self._apply_micro_pass(messages)

    # Step 2: Layer 2 — 中间段 tool 调用组摘要
    if self.meso.should_compress(result):
        result = self.meso.compress(result)

    # Step 3: Layer 3 — 全量重建 (仅当 Layer 2 不够时)
    if self.macro.should_compress(result):
        result = self.macro.compress(result)

    return result
```

#### 可观测性

```python
# 压缩统计 (通过 pipeline.stats 访问)
{
    "micro_compressions": 47,   # Layer 1 累计压缩次数
    "meso_compressions": 3,     # Layer 2 累计压缩次数
    "macro_compressions": 0,    # Layer 3 累计压缩次数
}
```

每次压缩记录日志:
```
INFO context_manager.pipeline: Layer 2 (Meso): 28 -> 15 messages
INFO context_manager.pipeline: Pipeline: 40 msgs (~12000 tokens) -> 27 msgs (~6500 tokens)
```

## 5. 集成方案

### 5.1 Agent 循环改造

**修改 `src/agent/async_loop.py`** (和同步 `loop.py`):

```python
# === __init__ 中 ===

# BEFORE:
# self.context_compactor = ContextCompactor(
#     max_messages=settings.CONTEXT_MAX_MESSAGES,
#     keep_recent=settings.CONTEXT_KEEP_RECENT,
# )

# AFTER:
from context_manager.pipeline import ContextPipeline

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

```python
# === _handle_tool_call 中 ===

# BEFORE:
# max_output = settings.MAX_TOOL_OUTPUT
# if len(output) > max_output:
#     output = output[:max_output] + f"\n... [truncated, {len(output)} chars total]"

# AFTER:
output = self.context_pipeline.compress_tool_result(tool_name, output)
```

```python
# === chat() 循环中 ===

# BEFORE:
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

### 5.2 配置项 (`config.py`)

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

# Shared — 最近消息保留数 (已有, 不变)
CONTEXT_KEEP_RECENT: int = int(os.getenv("CONTEXT_KEEP_RECENT", "12"))

# Legacy — 保留以兼容旧代码 (不再被 Agent 循环使用)
CONTEXT_MAX_MESSAGES: int = int(os.getenv("CONTEXT_MAX_MESSAGES", "40"))
```

### 5.3 配置参数调优指南

| 场景              | 建议调整                                                                                   |
| --------------- | -------------------------------------------------------------------------------------- |
| 频繁读取大文件 (代码审查) | 降低 `CONTEXT_MICRO_MAX_CHARS` 到 2000-3000, 减少单条占用                                        |
| 长任务 (50+ 步骤)   | 降低 `CONTEXT_MESO_MESSAGE_THRESHOLD` 到 15, 更频繁触发段落摘要, 防止 Layer 3 触发                    |
| 有限 API 预算       | 保持 `CONTEXT_MESO_USE_LLM=false` (默认), Layer 3 仅在极端情况触发一次 LLM 调用                  |
| 高质量要求 (不能丢上下文) | 开启 `CONTEXT_MESO_USE_LLM=true`, LLM 生成的摘要保留更多语义。同时提高 `CONTEXT_MACRO_TOKEN_THRESHOLD` 到 48000 |
| 短对话为主           | 默认配置即可, Layer 2/3 几乎不会触发                                                          |

## 6. 文件变更清单

| 文件                                                | 变更类型   | 说明                                                               |
| ------------------------------------------------- | ------ | ---------------------------------------------------------------- |
| `src/context_manager/micro_compressor.py`         | **新建** | `MicroCompressor` 类 + `estimate_tokens()` 工具函数                 |
| `src/context_manager/meso_compressor.py`          | **新建** | `MesoCompressor` 类: 规则摘要 + 可选 LLM 摘要                           |
| `src/context_manager/macro_compressor.py`         | **新建** | `MacroCompressor` 类: LLM 全量重建 + ProgressTracker 集成            |
| `src/context_manager/pipeline.py`                 | **新建** | `ContextPipeline` 编排器, 组合三层压缩                                  |
| `src/context_manager/__init__.py`                 | 修改     | 新增导出: `ContextPipeline`, `MicroCompressor`, `MesoCompressor`, `MacroCompressor`, `estimate_tokens` |
| `src/agent/async_loop.py`                         | 修改     | `ContextCompactor` → `ContextPipeline`; `_handle_tool_call` 使用 `compress_tool_result` |
| `src/agent/loop.py`                               | 修改     | 同上 (同步版)                                                        |
| `src/config.py`                                   | 修改     | 新增 7 个配置项 (见 5.2)                                              |
| `src/context_manager/context.py`                  | **不变** | 保留 `ContextCompactor`, 向后兼容                                       |
| `src/context_manager/tracker.py`                  | **不变** | Layer 3 读取但不修改                                                    |
| `tests/context_manager/test_micro_compressor.py`  | **新建** | Layer 1 单元测试                                                      |
| `tests/context_manager/test_meso_compressor.py`   | **新建** | Layer 2 单元测试                                                      |
| `tests/context_manager/test_macro_compressor.py`  | **新建** | Layer 3 单元测试 (含 Mock LLM)                                        |
| `tests/context_manager/test_pipeline.py`          | **新建** | 编排器单元测试                                                          |
| `tests/context_manager/test_pipeline_integration.py` | **新建** | 端到端集成测试: 60 条消息的完整对话压缩                                         |

## 7. 测试策略

### 7.1 Layer 1 (MicroCompressor) 测试

| 测试                                     | 验证点                                                             |
| -------------------------------------- | --------------------------------------------------------------- |
| `test_read_file_head_tail`             | 大文件保留 head 10 + tail 10 行, 中间显示 `[... N lines omitted ...]` |
| `test_read_file_small_noop`            | 小于 max_chars 的文件不做任何压缩                                          |
| `test_bash_stderr_preserved`           | stderr 行全量保留, 不被截断                                              |
| `test_bash_stdout_tail`                | stdout 保留 tail 15 行, 中间显示 `[... N stdout lines omitted ...]`  |
| `test_bash_failure_full_output`        | exit code ≠ 0 时保留全量输出 (失败输出短且重要)                                |
| `test_list_directory_cap`              | 超过 50 条时截断, 显示 `[... N more entries omitted ...]`                |
| `test_write_file_noop`                 | write_file 确认消息不压缩                                                |
| `test_generic_fallback`                | 未知工具名使用 head-tail 通用策略                                             |
| `test_estimate_tokens`                 | `estimate_tokens` 对英文/代码的粗估精度在 ±30% 内                             |
| `test_compress_message_preserves_meta` | `compress_message` 保留 `tool_call_id` 等元数据字段                        |

### 7.2 Layer 2 (MesoCompressor) 测试

| 测试                                     | 验证点                                              |
| -------------------------------------- | ------------------------------------------------ |
| `test_group_tool_exchanges`            | 连续 tool_call + tool_result 正确分组                  |
| `test_group_mixed_messages`            | tool 组与非 tool 消息交替时分组正确                            |
| `test_rule_based_bash_fact`            | bash 调用提取为 `ran 'cmd'`                         |
| `test_rule_based_read_fact`            | read_file 提取为 `read path (N lines)`              |
| `test_rule_based_write_fact`           | write_file 提取为 `wrote N chars to path`           |
| `test_extract_outcome_error`           | 错误结果提取 `error: ...`                             |
| `test_extract_outcome_success`         | 成功结果提取行数/状态                                      |
| `test_should_compress_by_count`        | 中间段消息数 ≥ 阈值时返回 True                            |
| `test_should_compress_by_tokens`       | 中间段 token 估算 ≥ 阈值时返回 True                       |
| `test_compress_preserves_head_tail`    | 压缩后 head[0:2] 和 tail[-12:] 不变                    |
| `test_compress_idempotent`             | 对已压缩结果再次压缩不增加消息数                                |
| `test_llm_summarize_with_mock`         | Mock LLM 返回正确格式摘要                                |
| `test_llm_failure_fallback`            | LLM 调用失败时降级为规则模式                                  |

### 7.3 Layer 3 (MacroCompressor) 测试

| 测试                                 | 验证点                                          |
| ---------------------------------- | -------------------------------------------- |
| `test_build_history_digest_format` | 各角色消息正确格式化为 [SYSTEM]/[USER]/[ASSISTANT]/[TOOL_CALL]/[TOOL_RESULT] |
| `test_should_compress_threshold`   | 总 token 估算超阈值且 LLM 可用时返回 True                 |
| `test_should_compress_no_llm`      | 无 LLM provider 时始终返回 False                    |
| `test_compress_with_mock_llm`      | Mock LLM 生成 `[CONTEXT SUMMARY]` 格式输出         |
| `test_compress_output_structure`   | 输出为 [system, summary, original_task, ...recent]  |
| `test_progress_tracker_integration` | ProgressTracker 的步骤信息出现在摘要中                  |
| `test_llm_failure_fallback`        | LLM 失败时降级为 system + first_user + recent       |
| `test_short_conversation_noop`     | 短对话 (≤ 2 + keep_recent) 不做压缩                 |

### 7.4 Pipeline 编排测试

| 测试                                | 验证点                                          |
| --------------------------------- | -------------------------------------------- |
| `test_compress_tool_result_layer1` | `compress_tool_result` 委托给 MicroCompressor  |
| `test_should_compact_layer2_or_3` | Layer 2 或 Layer 3 需要时返回 True                |
| `test_cascade_order`              | 执行顺序: Micro → Meso → Macro                 |
| `test_layer2_prevents_layer3`     | Layer 2 压缩后总 token 降至阈值以下, Layer 3 不触发     |
| `test_stats_tracking`             | 各层压缩计数器正确递增                                 |
| `test_defensive_micro_pass`        | 未被压缩的旧 tool 消息在 compact 时被 Layer 1 捕获       |

### 7.5 集成测试

```
测试场景: 60 条消息的完整对话压缩

构造:
  - messages[0]: system prompt
  - messages[1]: user task "重构 auth 模块"
  - messages[2-45]: 22 次 tool 调用 (read_file × 8, bash × 6, write_file × 5, update_plan × 3)
  - messages[46-59]: 最近 14 条消息 (含 user 追问 + assistant 回答)
  - ProgressTracker: 4 个步骤, 2 done, 1 in_progress, 1 pending

期望行为:
  1. should_compact() 返回 True (中间段 44 条 > 20 条阈值)
  2. compact() 执行:
     - Layer 1: 8 条大文件读取结果被 head-tail 压缩
     - Layer 2: 中间段 44 条 → ~3 条摘要
     - Layer 3: 不触发 (Layer 2 已足够)
  3. 输出消息数 < 20
  4. 验证保留:
     - messages[0] == 原 system prompt
     - messages[1] == 原 user task
     - 摘要中包含 "read src/auth.py", "ran pytest"
     - 最近 12 条消息完整保留
     - ProgressTracker 的 in_progress 步骤出现在日志中
```

### 7.6 回归测试

```
现有 tests/context_manager/test_context.py 不做修改。
验证 ContextCompactor 仍然独立工作, 不受新代码影响。
```

## 8. 风险与缓解

| 风险                                    | 概率 | 影响 | 缓解措施                                                                                             |
| ------------------------------------- | -- | -- | ------------------------------------------------------------------------------------------------ |
| **Layer 1 head-tail 截断丢失中间关键代码**: 函数的核心逻辑恰好在文件中间 | 中  | 中   | `keep_head_lines + keep_tail_lines` 保留 20+ 行, 覆盖多数函数签名和末尾逻辑; 真正需要完整内容时 Agent 可重新 `read_file` |
| **Layer 2 规则摘要不够准确**: 提取的事实遗漏关键细节         | 中  | 中   | 提供 `CONTEXT_MESO_USE_LLM=true` opt-in 选项; 规则模式持续调优事实提取规则; 摘要中保留文件路径和命令名, Agent 可按需重新获取          |
| **Layer 3 LLM 调用失败**: 网络错误、API 超时、余额不足     | 低  | 高   | 完善的 fallback: 保留 system + first_user + recent_window; Agent 仍可继续工作, 只是丢失了压缩的中间段上下文            |
| **Token 估算不准**: `len/4` 对中文文本高估, 对代码低估    | 中  | 低   | 估算仅用于阈值判断而非精确计费; 阈值留有充足余量 (32K token 阈值 ≈ 128K chars, 实际 128K 中文可能只有 ~40K token)          |
| **Pipeline 引入复杂度**: 三层组合增加调试难度            | 低  | 中   | 每层独立日志; `pipeline.stats` 提供可观测性; 可单独禁用 Layer 2/3 (调高阈值到极大值即等效禁用)                              |
| **Layer 2 LLM 模式增加 API 开销**     | 低  | 低   | 默认关闭 (`false`); 仅在用户显式设置环境变量时启用; 文档说明成本影响                                                  |
| **MesoCompressor 的 tool 分组逻辑出错**: 误将非 tool 消息分组 | 低  | 中   | `_group_tool_exchanges` 逻辑简单明确 (仅匹配 assistant+tool_calls 和 tool role); 充分单元测试覆盖边界情况             |
| **向后兼容性破坏**: 替换 ContextCompactor 导致现有行为变化  | 低  | 高   | `ContextCompactor` 保留不删除; Agent 循环中的变量名从 `context_compactor` 改为 `context_pipeline`, 但行为是超集 (更优)  |

## 附录 A: 与现有组件的关系

```
┌─────────────┐     ┌──────────────┐     ┌───────────────────┐
│ Subagent    │     │ Skill Loader │     │ ProgressTracker   │
│ (s04)       │     │ (s05)        │     │ (已有)             │
└──────┬──────┘     └──────┬───────┘     └────────┬──────────┘
       │                   │                      │
       │ 子任务隔离          │ 知识按需加载           │ 任务进度追踪
       │                   │                      │
       ▼                   ▼                      ▼
┌────────────────────────────────────────────────────────────┐
│                    Agent Loop                               │
│                                                             │
│  messages[] ←──────── ContextPipeline (s06)                 │
│                   ┌──── Layer 1: 单条截断                    │
│                   ├──── Layer 2: 段落摘要                    │
│                   └──── Layer 3: 全量重建 ←─ 读取 Tracker    │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

- **Subagent**: 子任务的中间 messages 不进入父 Agent, 从源头减少父上下文膨胀。三层压缩管理父 Agent 自身的长期记忆
- **Skill Loading**: `load_skill` 的 tool_result 作为普通消息参与压缩流程。被压缩后可重新 `load_skill` 获取 (按需加载的设计意图)
- **ProgressTracker**: Layer 3 直接读取 tracker 的步骤状态, 生成更准确的 "Completed Work" 和 "Remaining Tasks"

## 附录 B: 压缩效果预估

| 场景                     | 压缩前 (消息数 / ~token) | 压缩后 (消息数 / ~token) | 压缩率    | 主要触发层 |
| ---------------------- | ------------- | ------------- | ------ | ----- |
| 读 5 个文件 + 跑 3 个命令     | 20 / 15K      | 20 / 8K       | ~47%   | L1    |
| 重构模块 (30 次工具调用)      | 65 / 40K      | 25 / 10K      | ~75%   | L1+L2 |
| 大型任务 (100+ 次工具调用)    | 120 / 80K     | 18 / 8K       | ~90%   | L1+L2+L3 |
| 短对话 (10 轮问答)           | 22 / 5K       | 22 / 5K       | 0%     | 不触发   |
