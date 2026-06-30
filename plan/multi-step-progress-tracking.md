# 解决方案：多步任务中 LLM 进度丢失

## Context

当前 `Agent.chat()` 的消息列表 `self.messages` 无限增长——每条工具结果都完整保留，没有任何裁剪、压缩或进度追踪机制。在多步任务中，这导致：
1. **上下文膨胀**：工具输出（如 `read_file` 读取大文件）一次性占满上下文窗口
2. **无结构化记忆**：LLM 只能靠对话历史回忆"做了什么、还剩什么"，对话越长越不可靠
3. **系统提示稀释**：system prompt 是第 0 条消息，40 条消息后其注意力权重被大幅稀释
4. **无边界控制**：消息数量无上限，最终会超出模型上下文限制

## 方案：四层防护

| 层 | 组件 | 解决什么 | 复杂度 |
|---|---|---|---|
| 1 | 工具输出截断 | 单次输出过大 | ~5 行 |
| 2 | 任务进度追踪器 | 无结构化记忆 | ~120 行新模块 |
| 3 | 进度强化注入 | 系统提示稀释 | ~20 行方法 |
| 4 | 上下文压缩 | 旧消息累积 | ~80 行新模块 |

### 设计决策

- **通过工具管理计划，而非 NLP 解析**：给 LLM 一个 `update_plan` 工具来创建/更新计划。比从自由文本中提取更可靠，且与现有 tool-calling 模式一致。
- **计划是可选的**：系统提示建议 LLM 对多步任务使用 `update_plan`，简单任务跳过。无计划时 agent 行为与原来完全一致。
- **计划跨轮次保留，支持用户纠正**：计划不在每次 `chat()` 时自动重置。LLM 通过 `update_plan` 工具管理计划的完整生命周期——创建新计划、修改步骤、标记完成。当用户说"步骤3应该改成X"时，LLM 看到注入的进度摘要后自然会调用 `update_plan` 修正计划。当 LLM 开始全新任务时，它会用新的步骤列表替换旧计划。
- **基于规则的压缩，非 LLM 摘要**：压缩是基础设施层的操作，不应调用 LLM（增加延迟和成本）。用简单字符串截断代替。

## 文件变更

### 1. `src/config.py` — 新增 3 个配置项

在 `Settings` 类的 `# ---- Agent behaviour ----` 区域新增：

```python
MAX_TOOL_OUTPUT: int = int(os.getenv("MAX_TOOL_OUTPUT", "8000"))
CONTEXT_MAX_MESSAGES: int = int(os.getenv("CONTEXT_MAX_MESSAGES", "40"))
CONTEXT_KEEP_RECENT: int = int(os.getenv("CONTEXT_KEEP_RECENT", "12"))
```

- `MAX_TOOL_OUTPUT`：单条工具结果最大字符数（8000）
- `CONTEXT_MAX_MESSAGES`：触发压缩的消息数阈值（40）
- `CONTEXT_KEEP_RECENT`：压缩时保留最近消息数（12）

### 2. `src/context_manager/` — 新建独立包

新包结构：
```
src/context_manager/
  __init__.py       # 导出 ProgressTracker, ContextCompactor
  tracker.py        # 任务进度追踪器 + update_plan 工具定义
  context.py        # 上下文压缩器
```

#### `src/context_manager/tracker.py` — 任务进度追踪器

核心类：

```python
@dataclass
class Step:
    description: str
    status: str = "pending"    # pending | in_progress | done | skipped
    notes: str = ""

class ProgressTracker:
    def update_plan(self, steps: list[dict]) -> str   # 替换计划
    def format_summary(self) -> str                     # 格式化为 [TASK PROGRESS] 摘要
    def reset(self)                                      # 清空计划
    @property
    def has_plan(self) -> bool
    @property
    def steps(self) -> list[Step]
```

`format_summary()` 输出示例：
```
[TASK PROGRESS]
✓ [1/5] Set up project structure
→ [2/5] Implement core parser (in progress)
○ [3/5] Write unit tests
○ [4/5] Add error handling
○ [5/5] Update documentation
Progress: 1/5 steps complete
Next: Implement core parser
```

同文件提供工具定义和 handler：

```python
UPDATE_PLAN_TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "update_plan",
        "description": (
            "Create or update the task plan. Call this when you decompose a "
            "multi-step task into steps, when you complete a step and want "
            "to mark it done, or when the user corrects the plan. "
            "Keeps you on track for long tasks."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "description": {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "done", "skipped"],
                            },
                            "notes": {"type": "string"},
                        },
                        "required": ["description"],
                    },
                }
            },
            "required": ["steps"],
        },
    },
}

def run_update_plan(args: dict, tracker: ProgressTracker) -> str:
    ...
```

### 3. `src/context_manager/context.py` — 上下文压缩器

```python
class ContextCompactor:
    def __init__(self, max_messages: int = 40, keep_recent: int = 12)
    def should_compact(self, messages: list[dict]) -> bool
    def compact(self, messages: list[dict]) -> list[dict]
```

压缩策略：
- **保留**：`messages[0]`（system prompt）、`messages[1]`（原始任务）、`messages[-keep_recent:]`（近期上下文）
- **压缩**：中间区域的 tool 结果 → `[compacted] {前150字符}...`
- **幂等**：已压缩的消息不重复处理
- **安全**：压缩后消息数不减则返回原始列表

### 4. `src/agent/loop.py` — 修改 — 集成点

**新增 imports：**
```python
from src.context_manager.context import ContextCompactor
from src.context_manager.tracker import UPDATE_PLAN_TOOL_DEFINITION, ProgressTracker, run_update_plan
```

**更新 SYSTEM_PROMPT：** 追加一段引导 LLM 使用 `update_plan`：
```
For multi-step tasks, use the update_plan tool to create a plan with numbered steps.
Update the plan as you complete each step by marking it "done" and moving the next
step to "in_progress". If the user corrects your plan or asks you to change steps,
call update_plan with the revised step list. This keeps you on track for long tasks.
```

**`__init__` 中新增：**
```python
self.progress_tracker = ProgressTracker()
self.context_compactor = ContextCompactor(
    max_messages=settings.CONTEXT_MAX_MESSAGES,
    keep_recent=settings.CONTEXT_KEEP_RECENT,
)
self.tool_registry.register(
    UPDATE_PLAN_TOOL_DEFINITION,
    lambda args: run_update_plan(args, self.progress_tracker),
)
```

**新增 `_inject_progress()` 方法：**
- 清除旧的 `[TASK PROGRESS]` 系统消息
- 如有计划，在 `messages[1]` 位置插入最新进度摘要
- 这样进度信息始终在系统提示旁边，处于注意力高权重区

**`_handle_tool_call` 中新增截断：**
```python
max_output = settings.MAX_TOOL_OUTPUT
if len(output) > max_output:
    output = output[:max_output] + f"\n... [truncated, {len(output)} chars total]"
```

**`chat()` 中新增：**
- **不在开头调用 `tracker.reset()`**——计划跨轮次保留，LLM 通过 `update_plan` 自行管理生命周期
- while 循环内 `_call_llm()` 前：调用 `_inject_progress()` + 条件触发 `compact()`
- 这样用户可以在新一轮对话中说"跳过步骤4"或"步骤2改成X"，LLM 会更新计划

### 5. `src/agent/__init__.py` — 修改

从 `src.context_manager` 重导出 `ProgressTracker`：
```python
from src.context_manager.tracker import ProgressTracker
```

### 6. `tests/context_manager/test_tracker.py` — 新建

测试：Step 默认值、update_plan 创建/替换计划、format_summary 格式正确、reset 清空、steps 属性返回副本。

### 7. `tests/context_manager/test_context.py` — 新建

测试：should_compact 阈值判断、compact 保留头尾、中间 tool 结果被压缩、幂等性、压缩不增加消息数。

## 数据流：10 步任务示例（含用户纠正）

```
用户: "重构 auth 模块：提取验证器、添加测试、更新文档"
  │
  ├─ chat() — 不重置 tracker
  ├─ LLM 调用 update_plan 创建 10 步计划
  │
  ├─ 迭代 1:
  │   ├─ _inject_progress() → messages[1] 插入进度
  │   ├─ LLM 看到完整进度 + 原始任务 → 执行步骤 1
  │   └─ 工具结果截断（如超限）
  │
  ├─ 迭代 2:
  │   ├─ _inject_progress() → 更新进度（步骤1→done, 步骤2→in_progress）
  │   └─ LLM 执行步骤 2...
  │
  ├─ ... 迭代 3-8，消息增长 ...
  │
  ├─ 迭代 9:
  │   ├─ 消息数 > 40 → compact() 触发
  │   ├─ 迭代 1-5 的旧 tool 结果 → "[compacted]" 摘要
  │   ├─ 迭代 6-9 的近期上下文 → 原样保留
  │   ├─ _inject_progress() → 显示 8/10 完成，下一步: 9
  │   └─ LLM 保持正轨 ✓
  │
  └─ 迭代 10: 完成所有步骤，返回总结

--- 下一轮对话（计划仍保留）---

用户: "等一下，步骤5的方案不对，应该用策略模式而不是继承"
  │
  ├─ chat() — tracker 中仍有上次的 10 步计划
  ├─ _inject_progress() → LLM 看到当前进度摘要
  ├─ LLM 看到用户纠正 → 调用 update_plan 修改步骤5的描述和状态
  └─ LLM 继续执行修正后的步骤5
```

## 验证方式

1. **单元测试**：`python -m unittest tests.context_manager.test_tracker tests.context_manager.test_context -v`
2. **集成测试**：手动运行 agent，给出多步任务（如"创建项目结构，写 3 个模块，每个模块加测试"），观察 LLM 是否使用 `update_plan` 并按计划执行
3. **压力测试**：构造大量工具调用（读取多个大文件），验证压缩和截断是否正常触发

## 实现顺序

1. `src/config.py` — 添加配置（零风险）
2. `src/context_manager/__init__.py` — 创建包结构
3. `src/context_manager/tracker.py` — 核心新模块，可独立测试
4. `src/context_manager/context.py` — 上下文管理，可独立测试
5. `src/agent/loop.py` — 集成点（依赖 1-4）
6. `src/agent/__init__.py` — 导出更新
7. `tests/context_manager/test_tracker.py` + `tests/context_manager/test_context.py` — 测试
