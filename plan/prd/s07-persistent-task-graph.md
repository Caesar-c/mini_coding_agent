# PRD: 持久化任务图 — 有依赖关系的 DAG 任务管理

> **阶段**: s07 · **前置**: s06 (三层上下文压缩) · **后续**: s08 (待定)
>
> 核心理念: *"大目标要拆成小任务, 排好序, 记在磁盘上"* — 文件持久化的任务图, 为多 agent 协作打基础。

## 1. 背景与动机

### 问题

当前 `ProgressTracker`（s03）是一个纯内存的扁平清单:

- **没有依赖关系**: 所有步骤是线性列表, Agent 无法知道"任务 B 必须等任务 A 完成"。真实目标是有结构的 — C 和 D 可以并行, E 要等 C 和 D 都完成
- **没有就绪计算**: 所有未完成的步骤都是 `pending`, Agent 分不清什么能做 (ready)、什么被卡住 (blocked)、什么能同时跑
- **状态过于简单**: 只有 `pending`/`in_progress`/`done`/`skipped` 四种状态, 没有失败 (`failed`) 的概念
- **整体替换模式**: `update_plan` 工具每次调用时 LLM 必须传完整的步骤列表。随着任务推进, LLM 需要不断重复整个计划, 既浪费 token 又容易丢失步骤
- **纯内存, 无持久化**: 上下文压缩 (s06) 或 Agent 重启后, 计划全部丢失。对于跨越多个会话的长期任务, 这意味着 Agent "忘了自己做到哪了"

### 典型场景

```
场景: 用户让 Agent 重构一个模块 (涉及 5 个文件, 需要设计→实现→测试→集成)

当前行为 (ProgressTracker 扁平清单):
  → Agent 调用 update_plan 创建 5 个步骤:
    [1] 设计接口, [2] 实现模块 A, [3] 实现模块 B, [4] 写测试, [5] 集成测试
  → Agent 做完 [1], 调用 update_plan 标记 done + 开始 [2]
  → 上下文压缩触发, Layer 3 重建摘要
  → Agent 在第 30 轮时已经忘了 [3] 和 [4] 可以并行, 串行执行了两个独立模块
  → 第 45 轮上下文再次压缩, Agent 丢失了 [5] 需要等 [2]+[3]+[4] 的依赖信息
  → Agent 在 [4] 还没做完时就开始 [5], 集成测试缺少模块 B

期望行为 (TaskGraph 持久化任务图):
  → Agent 调用 create_plan 创建 DAG:
    T1: 设计接口 (无依赖)
    T2: 实现模块 A (依赖 T1)
    T3: 实现模块 B (依赖 T1)        ← T2 和 T3 可并行
    T4: 写单元测试 (依赖 T2, T3)
    T5: 集成测试 (依赖 T4)
  → T1 自动标记 ready, Agent 开始执行
  → T1 完成 → T2, T3 同时变为 ready
  → Agent 看到 "Ready: T2, T3", 知道可以并行
  → 上下文压缩触发, Layer 3 读取 TaskGraph: 结构化状态完整保留
  → 即使 Agent 重启, plan.json 恢复后一切依赖关系完好
  → T5 始终被 T4 卡住, 不会被提前执行
```

### 与 s03-s06 的关系

- **s03 ProgressTracker** 建立了"工具管理计划"的模式。s07 保留这个模式但升级数据结构: 从扁平列表到有向无环图
- **s04 Subagent** 提供了上下文隔离的子任务执行。当前 subagent 不感知父 Agent 的任务计划。s07 的持久化任务图为未来"多个 subagent 并行领取 ready 任务"打下数据基础
- **s05 Skill Loading** 与任务管理正交, 不受影响
- **s06 三层压缩** 的 Layer 3 读取 tracker 状态生成摘要。s07 将 `progress_tracker` 替换为 `task_graph`, 提供更丰富的结构化信息 (依赖关系、ready/blocked 状态)

## 2. 目标与非目标

### 目标

| #   | 目标              | 衡量标准                                                                                         |
| --- | --------------- | -------------------------------------------------------------------------------------------- |
| G1  | DAG 依赖关系        | 任务支持 `depends_on` 字段, 系统自动验证无环。任务 B 在依赖 A 未完成前保持 blocked                        |
| G2  | 自动就绪计算          | 所有依赖完成后, 任务自动从 `pending` 转为 `ready`。Agent 一眼看出"现在能做什么"                           |
| G3  | 文件持久化           | 计划状态写入 `.mini_agent/plan.json`, Agent 重启后自动恢复。原子写入防止损坏                             |
| G4  | 细粒度工具操作         | 4 个工具 (`create_plan`, `update_task`, `add_task`, `get_plan`) 替代整体替换的 `update_plan`。每次操作只需传增量变更 |
| G5  | 完全替换 ProgressTracker | 新模块 `TaskGraphManager` 完全取代 `ProgressTracker`。旧代码删除, 不保留两套系统                          |
| G6  | 并行调度建议          | `[TASK PROGRESS]` 输出明确标注 ready 任务列表, Agent 自行决定是否并行执行 (建议但不强制)                     |

### 非目标

- **不做** 真正的并行执行引擎 (本阶段只做数据模型和就绪计算, 实际并行由 Agent 通过多次调用 `task` 工具实现)
- **不做** 跨 Agent 的任务图共享协议 (持久化文件为单 Agent 使用设计, 多 Agent 协作留给后续阶段)
- **不做** 任务回退/重开 (completed/failed 的任务不可重新打开, 简化状态机)
- **不做** 任务优先级/权重 (所有 ready 任务平等, 由 Agent 自行排序)
- **不做** 子任务嵌套 (任务是扁平的 DAG, 不支持 task 内嵌 subtask)
- **不做** SQLite 或复杂存储 (JSON 文件足够, 人类可读可调试)

## 3. 架构设计

### 3.1 数据模型

```
Task (任务节点):
  id: str              # "T1", "T2", ... 自动递增
  description: str     # 任务描述
  status: str          # pending → ready → in_progress → done / failed
  depends_on: list[str]  # 依赖的任务 ID
  notes: str           # 备注/结果摘要
  created_at: str      # ISO 8601 时间戳
  updated_at: str      # ISO 8601 时间戳
```

### 3.2 状态生命周期

```
         ┌─────────┐
         │ pending  │  (依赖未满足)
         └────┬─────┘
              │ 所有依赖完成 (自动)
              ▼
         ┌─────────┐
         │  ready   │  (可以开始, Agent 可领取)
         └────┬─────┘
              │ Agent 调用 update_task(status="in_progress")
              ▼
         ┌─────────────┐
         │ in_progress  │  (正在执行)
         └────┬─────────┘
              │ Agent 调用 update_task(status="done" 或 "failed")
              ▼
      ┌───────┴───────┐
      │ done    failed │  (终态, 不可变更)
      └───────────────┘
```

**关键**: `ready` 是系统自动计算的状态, 不由 Agent 手动设置。Agent 只能设置 `in_progress` / `done` / `failed`。

### 3.3 组件交互

```
                    ┌──────────────────────────┐
                    │     Agent Loop            │
                    │ (loop.py / async_loop.py) │
                    └─────┬──────────┬─────────┘
                          │          │
            create/update/add     _inject_progress()
                          │          │
                          ▼          ▼
              ┌───────────────────────────────────┐
              │        TaskGraphManager            │
              │                                    │
              │  create_plan()  — 初始化 DAG        │
              │  update_task()  — 更新单个任务状态     │
              │  add_task()     — 追加新任务          │
              │  format_summary() — [TASK PROGRESS] │
              │  _compute_ready() — 自动就绪计算      │
              │  save()/load()   — JSON 持久化       │
              │                                    │
              │  .mini_agent/plan.json              │
              └──────────────┬─────────────────────┘
                             │ feeds
                             ▼
                    ┌─────────────────┐
                    │ ContextPipeline │
                    │ Layer 3 reads   │
                    │ task_graph state│
                    └─────────────────┘
```

### 3.4 工具集

| 工具             | 用途                              | 调用频率   |
| -------------- | ------------------------------- | ------ |
| `create_plan`  | 初始化计划, 定义任务和依赖关系。替换已有计划       | 每个任务 1 次 |
| `update_task`  | 更新单个任务的状态和备注                    | 高频, 每个任务 2-3 次 |
| `add_task`     | 执行过程中发现新工作时追加任务                | 偶尔     |
| `get_plan`     | 读取当前计划的格式化状态 (上下文压缩后重新获取)     | 偶尔     |

**注册范围**: 4 个工具仅注册到父 Agent 的 registry。Subagent (child registry) 不拥有这些工具, 避免子任务干扰父级计划。

## 4. 详细设计

### 4.1 `[TASK PROGRESS]` 格式

注入到 `messages[1]` 的进度摘要, 扩展为包含依赖关系和就绪状态:

```
[TASK PROGRESS]
✓ T1: 设计数据模型 — Task dataclass + DAG 验证
✓ T2: 实现 TaskGraphManager — 核心逻辑完成
→ T3: 写单元测试 (in_progress)
◉ T4: 写持久化层 [ready]
○ T5: 集成到 agent loop [waiting: T3, T4]
Ready: T4
Progress: 2/5 done, 1 ready, 1 in progress, 1 pending
Next: T4 — 写持久化层
```

状态图标:

| 图标  | 状态            | 含义      |
| --- | ------------- | ------- |
| `✓` | done          | 已完成     |
| `✗` | failed        | 失败      |
| `→` | in_progress   | 正在执行    |
| `◉` | ready         | 可开始     |
| `○` | pending       | 等待依赖完成  |

### 4.2 持久化格式

文件: `.mini_agent/plan.json` (相对于 sandbox root, gitignored)

```json
{
  "version": 1,
  "tasks": [
    {
      "id": "T1",
      "description": "设计数据模型",
      "status": "done",
      "depends_on": [],
      "notes": "Task dataclass + DAG 验证",
      "created_at": "2026-07-05T10:30:00",
      "updated_at": "2026-07-05T10:31:15"
    },
    {
      "id": "T2",
      "description": "实现 TaskGraphManager",
      "status": "done",
      "depends_on": ["T1"],
      "notes": "核心逻辑完成",
      "created_at": "2026-07-05T10:30:00",
      "updated_at": "2026-07-05T10:35:00"
    },
    {
      "id": "T3",
      "description": "写单元测试",
      "status": "in_progress",
      "depends_on": ["T2"],
      "notes": "",
      "created_at": "2026-07-05T10:30:00",
      "updated_at": "2026-07-05T10:36:00"
    }
  ]
}
```

写入策略: 先写临时文件再 `os.replace()`, 保证原子性。

### 4.3 DAG 操作

**就绪计算** (`_compute_ready`): 每次状态变更后扫描所有任务。当 pending 任务的所有依赖都是 `done` 时, 自动转为 `ready`。反向也处理: 如果 ready 任务的某个依赖不再是 `done`, 回退为 `pending` (防御性)。

**环检测** (`_detect_cycle`): DFS 三色算法。`create_plan` 和 `add_task` 时调用, 检测到环则拒绝并返回错误。

**ID 生成**: 自动递增 T1, T2, T3...。基于当前最大 ID + 1。LLM 不指定 ID, 只在 `depends_on` 中引用已有的 ID。

## 5. 集成方案

### 5.1 Agent 循环改造

在 `AsyncAgent.__init__` (和同步 `Agent`) 中:

```python
# BEFORE:
self.progress_tracker = ProgressTracker()
self.context_pipeline = ContextPipeline(..., progress_tracker=self.progress_tracker)
self.tool_registry.register(
    UPDATE_PLAN_TOOL_DEFINITION,
    lambda args: run_update_plan(args, self.progress_tracker),
)

# AFTER:
self.task_graph = TaskGraphManager(sandbox_root=settings.SANDBOX_ROOT)
self.task_graph.load()  # 从磁盘恢复
self.context_pipeline = ContextPipeline(..., task_graph=self.task_graph)
for definition, handler in ALL_TASK_GRAPH_TOOLS:
    self.tool_registry.register(
        definition,
        lambda args, h=handler: h(args, self.task_graph),
    )
```

子 registry 排除列表:
```python
# BEFORE: exclude=["task", "update_plan"]
# AFTER:  exclude=["task", "create_plan", "update_task", "add_task", "get_plan"]
```

### 5.2 Context Pipeline 集成

`ContextPipeline` 和 `MacroCompressor` 的参数从 `progress_tracker` 改为 `task_graph`。Layer 3 读取 `task_graph.format_summary()` 为摘要提供结构化任务状态 — 比旧的 ProgressTracker 提供更丰富的信息 (依赖关系、ready/blocked 标注)。

### 5.3 CLI 集成

`/plan` 命令: 显示 `agent.task_graph.format_summary()`
`/reset` 命令: 调用 `agent.task_graph.reset()`, 同时删除 plan.json

### 5.4 配置项

```python
# config.py 新增:
TASK_GRAPH_DIR: str = os.getenv("TASK_GRAPH_DIR", ".mini_agent")
```

## 6. 文件变更清单

| 文件                                       | 变更类型   | 说明                                                    |
| ---------------------------------------- | ------ | ----------------------------------------------------- |
| `src/context_manager/task_graph.py`      | **新建** | `Task` dataclass + `TaskGraphManager` + 4 工具定义 + 4 handler |
| `src/context_manager/tracker.py`         | **删除** | 被 `task_graph.py` 完全替代                                |
| `src/context_manager/pipeline.py`        | 修改     | `progress_tracker` → `task_graph` (参数名和类型)            |
| `src/context_manager/macro_compressor.py` | 修改     | `progress_tracker` → `task_graph` (5 处引用)             |
| `src/context_manager/meso_compressor.py`  | 修改     | `"update_plan"` → 新工具名映射                              |
| `src/context_manager/__init__.py`        | 修改     | 导出 `TaskGraphManager` 替代 `ProgressTracker`           |
| `src/agent/async_loop.py`                | 修改     | 替换 tracker 为 graph, 注册 4 工具, 更新 child exclude        |
| `src/agent/loop.py`                      | 修改     | 同上 + SYSTEM_PROMPT 更新                                 |
| `src/agent/__init__.py`                  | 修改     | 导出更新                                                  |
| `src/cli/repl.py`                        | 修改     | `progress_tracker` → `task_graph` 引用                   |
| `src/cli/display.py`                     | 修改     | 新增 ◉/✗ 图标解析                                         |
| `src/config.py`                          | 修改     | 新增 `TASK_GRAPH_DIR` 配置项                              |
| `.gitignore`                             | 修改     | 新增 `.mini_agent/`                                      |
| `tests/context_manager/test_task_graph.py` | **新建** | TaskGraphManager 全面测试                                 |
| `tests/context_manager/test_tracker.py`  | **删除** | 被 `test_task_graph.py` 替代                             |
| `tests/context_manager/test_macro_compressor.py` | 修改 | ProgressTracker → TaskGraphManager                    |
| `tests/context_manager/test_pipeline_integration.py` | 修改 | ProgressTracker → TaskGraphManager              |
| `tests/agent/test_subagent.py`           | 修改     | child registry exclude 断言更新                           |
| `tests/agent/test_skill_integration.py`  | 修改     | child registry exclude 断言更新                           |

## 7. 测试策略

### 7.1 TaskGraphManager 单元测试

| 测试                                         | 验证点                                                         |
| ------------------------------------------ | ----------------------------------------------------------- |
| `test_initial_state`                       | 空状态: `has_plan` 为 False, `format_summary()` 返回空字符串          |
| `test_create_plan_basic`                   | 创建 3 个无依赖任务, 验证 ID 自动分配 T1/T2/T3                            |
| `test_create_plan_with_dependencies`       | 任务带 `depends_on`, 验证 ready 计算正确                              |
| `test_create_plan_replaces_existing`       | 第二次 `create_plan` 清除旧任务                                     |
| `test_create_plan_cycle_detection`         | A→B→A 依赖环被拒绝, 抛出 ValueError                                 |
| `test_create_plan_invalid_dep_reference`   | `depends_on` 引用不存在的 ID 时报错                                  |
| `test_update_task_status_transition`       | pending → in_progress → done 状态流转正确                        |
| `test_update_task_ready_computation`       | T1 done 后 T2 (depends_on=[T1]) 自动变为 ready                   |
| `test_update_task_invalid_id`              | 更新不存在的任务 ID 返回错误                                             |
| `test_update_task_invalid_status`          | 拒绝无效状态值 (如直接设 "ready")                                       |
| `test_add_task`                            | 追加新任务, ID 自动递增                                               |
| `test_add_task_with_deps`                  | 追加带依赖的任务, ready 重新计算                                        |
| `test_reset`                               | 清空任务 + 删除 plan.json                                          |
| `test_chain_dependency`                    | T1→T2→T3 链式依赖, T1 done 后 T2 ready, T3 仍 pending            |
| `test_diamond_dependency`                  | T1→T3, T2→T3 菱形依赖, T1 done 但 T2 未完成时 T3 不 ready           |
| `test_cycle_detection_transitive`          | A→B→C→A 传递环被检测                                             |
| `test_no_false_cycle`                      | A→B, A→C, B→C (合法 DAG) 不被误报为环                              |

### 7.2 持久化测试

| 测试                                     | 验证点                                               |
| -------------------------------------- | ------------------------------------------------- |
| `test_save_creates_file`               | plan.json 文件被创建, JSON 格式合法                        |
| `test_load_restores_tasks`             | save 后新实例 load 恢复所有任务, 状态一致                        |
| `test_load_no_file_returns_false`      | 无文件时 load 返回 False, 不报错                            |
| `test_load_corrupted_file_returns_false` | 损坏的 JSON 文件不导致崩溃, 返回 False                      |
| `test_reset_deletes_file`              | reset 后 plan.json 被删除                             |
| `test_atomic_write`                    | 临时文件在写入后被清理                                         |

### 7.3 格式输出测试

| 测试                                       | 验证点                                         |
| ---------------------------------------- | ------------------------------------------- |
| `test_status_icons`                      | ✓/✗/→/◉/○ 对应正确状态                         |
| `test_waiting_annotation`                | pending 任务显示 `[waiting: T1]`               |
| `test_ready_annotation`                  | ready 任务显示 `[ready]`                        |
| `test_progress_line`                     | `Progress:` 行数字正确                            |
| `test_next_line_prefers_ready`           | `Next:` 优先显示 ready 任务                       |
| `test_notes_shown`                       | notes 显示在 `—` 之后                           |

### 7.4 集成测试

```
测试场景: 多步骤任务的完整生命周期

构造:
  1. Agent 收到 "重构 auth 模块" 任务
  2. Agent 调用 create_plan:
     T1: 读取现有代码 (无依赖)
     T2: 设计新接口 (依赖 T1)
     T3: 实现 login 重构 (依赖 T2)
     T4: 实现 logout 重构 (依赖 T2)
     T5: 运行测试 (依赖 T3, T4)
  3. Agent 依次 update_task 推进状态

期望行为:
  - create_plan 后: T1 ready, T2-T5 pending
  - T1 done 后: T2 ready, T3-T5 pending (T3/T4 waiting: T2)
  - T2 done 后: T3 ready, T4 ready, T5 pending (waiting: T3, T4)
  - [TASK PROGRESS] 显示 Ready: T3, T4
  - 验证 .mini_agent/plan.json 内容正确
  - 新 Agent 实例 load 后状态一致
```

## 8. 风险与缓解

| 风险                                    | 概率  | 影响 | 缓解措施                                                                                        |
| ------------------------------------- | --- | -- | ------------------------------------------------------------------------------------------- |
| **LLM 错误指定依赖 ID**: 引用不存在的 T99       | 中   | 低  | `create_plan` 和 `add_task` 验证所有 `depends_on` 引用存在, 不存在则返回错误信息让 LLM 修正                  |
| **LLM 创建环依赖**: T1→T2→T1                | 低   | 低  | `_detect_cycle()` 在 `create_plan` 和 `add_task` 时检测, 返回错误                              |
| **JSON 文件损坏**: 进程崩溃导致写入中断             | 低   | 中  | 原子写入 (tempfile + os.replace)。最坏情况: load 返回 False, Agent 从头开始                          |
| **LLM 忘记更新任务状态**: 做完工作但没调 update_task | 中   | 中  | `_inject_progress` 每轮注入 `[TASK PROGRESS]`, 提醒 LLM 当前状态。`get_plan` 工具可按需查看         |
| **任务图过大**: 50+ 任务的 DAG 占用上下文         | 低   | 低  | `format_summary()` 输出紧凑 (每任务一行), 50 个任务约 3KB。远小于 tool 结果。如有需要可后续加摘要模式         |
| **向后兼容测试断裂**: 删除 ProgressTracker 影响现有测试 | 中   | 高  | 7 步迁移顺序确保: 先加新代码 (Step 1-2), 再改集成点 (Step 3-5), 最后删旧代码 (Step 6)。每步验证测试通过       |
| **频繁磁盘写入**: 每次 update_task 都写盘       | 低   | 低  | JSON 文件通常 < 10KB, 原子 rename 是微秒级操作。即使每分钟 10 次写入, 对 SSD 寿命影响可忽略             |
