# 设计方案：持久化任务图 — DAG 依赖追踪与文件持久化

## Context

当前 `ProgressTracker`（`context_manager/tracker.py`）是一个扁平的 `Step` 列表，通过 `update_plan` 工具整体替换。不支持依赖关系、就绪计算或持久化。本方案将其替换为 `TaskGraphManager`——一个持久化的有向无环图，支持依赖声明、自动 ready 计算和 JSON 文件持久化。

**核心问题 1 — 无依赖关系**：所有步骤是线性列表，Agent 无法表达"任务 B 必须等任务 A 完成"。真实目标有 DAG 结构。

**核心问题 2 — 无就绪计算**：Agent 无法区分 ready（可开始）和 blocked（等待依赖）任务，导致串行执行本可并行的工作。

**核心问题 3 — 整体替换模式**：`update_plan` 每次调用 LLM 传完整步骤列表，随任务推进浪费 token 且易丢失步骤。

**核心问题 4 — 纯内存无持久化**：上下文压缩或 Agent 重启后计划丢失。

**解决方案**：用 `TaskGraphManager` 替换 `ProgressTracker`，引入 DAG 依赖、自动 ready 计算、4 个细粒度工具、JSON 文件持久化。

**前置依赖**：s03（ProgressTracker + update_plan 工具）、s04（Subagent，child registry exclude 列表）、s06（ContextPipeline + MacroCompressor 读取 tracker）。

## 方案概览

| 模块 | 组件 | 职责 | 复杂度 |
|---|---|---|---|
| 1 | `task_graph.py` — `Task` + `TaskGraphManager` | DAG 数据模型、就绪计算、环检测、JSON 持久化 | ~250 行新模块 |
| 2 | `task_graph.py` — 4 工具定义 + 4 handler | OpenAI function-calling schemas + handler 函数 | ~100 行（同文件） |
| 3 | `config.py` — `TASK_GRAPH_DIR` 配置 | 持久化目录 | ~1 行改动 |
| 4 | `pipeline.py` + `macro_compressor.py` — 参数重命名 | `progress_tracker` → `task_graph` | ~10 行改动 |
| 5 | `meso_compressor.py` — 工具名映射更新 | `"update_plan"` → 新工具名 | ~10 行改动 |
| 6 | `async_loop.py` + `loop.py` — 替换 tracker | 构造、注册、注入、pipeline 传参 | ~30 行改动 × 2 |
| 7 | `cli/repl.py` + `cli/display.py` — CLI 集成 | 引用替换 + 新图标 | ~15 行改动 |

### 设计决策

| # | 决策 | 选择 | 理由 |
|---|---|---|---|
| D1 | 数据模型 | `@dataclass Task` 含 `depends_on: list[str]` | 最小化数据结构，用 list 而非 set 保持序列化确定性。ID 字符串 "T1" 比纯数字更易被 LLM 读写 |
| D2 | `ready` 状态计算 | 系统自动计算，Agent 不可手动设置 | 就绪是确定性图算法（所有 deps done），不需要 LLM 推理。消除 LLM 计算错误的风险 |
| D3 | 工具粒度 | 4 个细粒度工具替代 1 个整体替换 | `update_task` 只传 task_id + status，比每次传完整列表省 token。`add_task` 支持执行中发现新工作 |
| D4 | 持久化格式 | JSON 文件 `.mini_agent/plan.json` | 人类可读可调试，Python 标准库原生支持，无需额外依赖。SQLite 对这个数据量（通常 <100 任务）是过度工程 |
| D5 | 写入策略 | tempfile + `os.replace()` 原子写入 | 防止进程崩溃导致半写的 JSON 文件。`os.replace()` 在同一文件系统上是原子操作 |
| D6 | ProgressTracker 处理 | 完全替换，删除旧代码 | 避免两套系统共存的心智负担和维护成本。旧测试直接删除并用新测试替代 |
| D7 | 并行调度策略 | 建议不强制——显示 ready 列表，Agent 自行决定 | Agent 通过多次调用 `task` 工具实现并行。强制并行会限制 Agent 的灵活性（有时串行更安全） |
| D8 | 环检测算法 | DFS 三色法（WHITE/GRAY/BLACK） | 标准算法，O(V+E) 复杂度。对于 <100 任务的 DAG 远小于 1ms |
| D9 | `get_plan` 作为工具 | 提供显式的计划读取工具 | 上下文压缩后 LLM 可能丢失进度。`get_plan` 让 LLM 按需重新获取，不依赖 `_inject_progress` 的被动注入 |
| D10 | Subagent 不可修改计划 | child registry exclude 所有 4 个 plan 工具 | Subagent 是短生命周期的执行者，不应修改父级的长期计划。保持 s04 的设计原则 |
| D11 | 任务 ID 格式 | "T" + 递增数字 (T1, T2, ...) | 短、唯一、LLM 友好。基于当前最大 ID + 1 生成，支持中间删除任务后继续递增 |
| D12 | `failed` 状态 | 新增终态 `failed`，与 `done` 并列 | 真实任务会失败。`done` 和 `failed` 都是终态，都解除对下游任务的阻塞（但语义不同——下游任务应该知道上游失败了）。当前简化处理：`failed` 也视为完成，解除阻塞，由 Agent 在 notes 中记录失败原因 |

## 架构总览

```
                    ┌──────────────────────────┐
                    │     Agent Loop            │
                    │ (loop.py / async_loop.py) │
                    └─────┬──────────┬─────────┘
                          │          │
           create/update/add/get  _inject_progress()
                          │          │
                          ▼          ▼
              ┌───────────────────────────────────────┐
              │        TaskGraphManager                │
              │                                        │
              │  ┌──────────────────────────────────┐  │
              │  │ _tasks: list[Task]               │  │
              │  │   T1 (done)                      │  │
              │  │   T2 (done, deps=[T1])           │  │
              │  │   T3 (in_progress, deps=[T2])    │  │
              │  │   T4 (ready, deps=[T2])          │  │
              │  │   T5 (pending, deps=[T3,T4])     │  │
              │  └──────────────────────────────────┘  │
              │                                        │
              │  create_plan()  → 初始化 + _compute    │
              │  update_task()  → 单任务更新 + _compute │
              │  add_task()     → 追加 + _compute      │
              │  format_summary() → [TASK PROGRESS]    │
              │  save()/load()  → .mini_agent/plan.json│
              │  _compute_ready() → pending ↔ ready   │
              │  _detect_cycle()  → DFS 三色环检测      │
              └──────────────┬─────────────────────────┘
                             │ feeds Layer 3
                             ▼
                    ┌─────────────────┐
                    │ ContextPipeline │
                    │ (macro_compressor│
                    │  reads state)   │
                    └─────────────────┘
```

## 文件变更详设

### 1. `src/config.py` — 新增 1 个配置项

在 `# ---- Skill Loading ----` 区域之后新增：

```python
# ---- Task Graph ----
TASK_GRAPH_DIR: str = os.getenv("TASK_GRAPH_DIR", ".mini_agent")
```

此目录相对于 sandbox root，用于存放 `plan.json`。

### 2. `.gitignore` — 新增 1 行

```
# Mini agent persistence
.mini_agent/
```

### 3. `src/context_manager/task_graph.py` — 核心新模块

#### 3.1 Task dataclass

```python
from dataclasses import dataclass, field

@dataclass
class Task:
    """DAG 中的单个任务节点."""

    id: str                                    # "T1", "T2", ...
    description: str
    status: str = "pending"                    # pending/ready/in_progress/done/failed
    depends_on: list[str] = field(default_factory=list)
    notes: str = ""
    created_at: str = ""                       # ISO 8601
    updated_at: str = ""                       # ISO 8601

    VALID_STATUSES = frozenset({"pending", "ready", "in_progress", "done", "failed"})
    TERMINAL_STATUSES = frozenset({"done", "failed"})
    AGENT_SETTABLE = frozenset({"in_progress", "done", "failed"})  # Agent 可设的状态
```

**字段说明**：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | `str` | 自动生成 "T1", "T2", ...。LLM 不指定 ID |
| `description` | `str` | 任务描述，创建时必填 |
| `status` | `str` | 生命周期见 PRD §3.2。`ready` 仅由 `_compute_ready()` 设置 |
| `depends_on` | `list[str]` | 依赖的任务 ID 列表。空列表表示无依赖 |
| `notes` | `str` | 执行备注，通常在 `update_task` 时附带 |
| `created_at` | `str` | 创建时间，ISO 8601 格式 |
| `updated_at` | `str` | 最后更新时间 |

#### 3.2 TaskGraphManager 类

```python
class TaskGraphManager:
    """管理带依赖关系的任务 DAG, 支持 JSON 持久化.

    替换 ProgressTracker. LLM 通过 4 个细粒度工具操作:
    - create_plan: 初始化计划
    - update_task: 更新单个任务
    - add_task: 追加任务
    - get_plan: 读取当前状态

    依赖全部完成的任务自动变为 'ready'. Agent 看到 ready 列表后
    自行决定执行顺序和并行策略.
    """

    def __init__(self, sandbox_root: str = ".") -> None:
        """初始化. 构造时自动尝试从磁盘加载."""
        self._tasks: list[Task] = []
        self._sandbox_root = sandbox_root
        self._persist_dir = Path(sandbox_root) / settings.TASK_GRAPH_DIR
```

##### 3.2.1 Properties

```python
@property
def tasks(self) -> list[Task]:
    """返回任务列表的浅拷贝."""
    return list(self._tasks)

@property
def has_plan(self) -> bool:
    """是否至少有一个任务."""
    return len(self._tasks) > 0
```

##### 3.2.2 create_plan

```python
def create_plan(self, tasks: list[dict[str, Any]]) -> str:
    """初始化计划, 替换已有计划.

    Args:
        tasks: 任务字典列表. 每个字典包含:
            - description (str, required)
            - depends_on (list[str], optional — 引用 "T1" 等位置 ID)
            - notes (str, optional)

    Returns:
        格式化的计划摘要字符串.

    Raises:
        ValueError: 检测到环依赖或无效引用.
    """
    self._tasks = []
    now = _now_iso()

    for i, td in enumerate(tasks, 1):
        desc = td.get("description", "").strip()
        if not desc:
            continue
        task_id = f"T{i}"
        self._tasks.append(Task(
            id=task_id,
            description=desc,
            status="pending",
            depends_on=td.get("depends_on", []),
            notes=td.get("notes", ""),
            created_at=now,
            updated_at=now,
        ))

    # 验证所有依赖引用存在
    valid_ids = {t.id for t in self._tasks}
    for t in self._tasks:
        for dep in t.depends_on:
            if dep not in valid_ids:
                self._tasks = []  # 回滚
                raise ValueError(
                    f"Task {t.id} depends on unknown task '{dep}'"
                )

    # 环检测
    if self._detect_cycle():
        self._tasks = []  # 回滚
        raise ValueError("Cycle detected in task dependencies")

    self._compute_ready()
    self.save()
    logger.info("Plan created: %d tasks", len(self._tasks))
    return self.format_summary()
```

**设计要点**：
- ID 按输入顺序自动分配 T1, T2, T3...
- 依赖引用使用位置 ID（即 LLM 传入列表时，第 N 个任务的 ID 是 "TN"）
- 验证失败时回滚到空状态
- 创建后立即计算 ready 并持久化

##### 3.2.3 update_task

```python
def update_task(self, task_id: str, status: str | None = None,
                notes: str | None = None) -> str:
    """更新单个任务的状态和/或备注.

    状态变更后自动重算 ready. 变更后自动持久化.

    Args:
        task_id: 任务 ID (如 "T1")
        status: 新状态 (in_progress/done/failed). None 表示不更新
        notes: 新备注. None 表示不更新

    Returns:
        确认字符串.

    Raises:
        ValueError: 无效 task_id 或 status.
    """
    task = self._find_task(task_id)
    if task is None:
        raise ValueError(f"Unknown task: '{task_id}'")

    if status is not None:
        if status not in Task.AGENT_SETTABLE:
            raise ValueError(
                f"Invalid status '{status}'. "
                f"Allowed: {', '.join(sorted(Task.AGENT_SETTABLE))}"
            )
        if task.status in Task.TERMINAL_STATUSES:
            raise ValueError(
                f"Task {task_id} is already '{task.status}' (terminal state)"
            )
        task.status = status

    if notes is not None:
        task.notes = notes

    task.updated_at = _now_iso()
    self._compute_ready()
    self.save()

    icon = {"in_progress": "→", "done": "✓", "failed": "✗"}.get(task.status, "○")
    result = f"{icon} {task.id}: {task.description} [{task.status}]"
    if task.notes:
        result += f" — {task.notes}"
    return result
```

**设计要点**：
- Agent 只能设 `in_progress` / `done` / `failed`（`AGENT_SETTABLE`），不能直接设 `ready`
- 终态任务 (`done`/`failed`) 不可再修改
- 每次更新后自动 `_compute_ready()` + `save()`

##### 3.2.4 add_task

```python
def add_task(self, description: str, depends_on: list[str] | None = None,
             notes: str = "") -> str:
    """向已有计划追加新任务.

    Returns:
        确认字符串, 含新任务 ID.

    Raises:
        ValueError: 无计划、无效引用或环依赖.
    """
    if not self._tasks:
        raise ValueError("No active plan. Call create_plan first.")

    now = _now_iso()
    new_id = self._next_id()
    deps = depends_on or []

    # 验证依赖引用
    valid_ids = {t.id for t in self._tasks}
    for dep in deps:
        if dep not in valid_ids:
            raise ValueError(f"Unknown dependency: '{dep}'")

    new_task = Task(
        id=new_id, description=description, status="pending",
        depends_on=deps, notes=notes, created_at=now, updated_at=now,
    )
    self._tasks.append(new_task)

    # 环检测 (含新任务)
    if self._detect_cycle():
        self._tasks.pop()  # 回滚
        raise ValueError(f"Adding {new_id} would create a cycle")

    self._compute_ready()
    self.save()
    logger.info("Task added: %s — %s", new_id, description)
    return f"Added {new_id}: {description}"
```

##### 3.2.5 reset

```python
def reset(self) -> None:
    """清空所有任务并删除持久化文件."""
    self._tasks = []
    plan_path = self._persist_dir / "plan.json"
    try:
        plan_path.unlink()
        logger.info("Plan reset and file deleted: %s", plan_path)
    except FileNotFoundError:
        logger.info("Plan reset (no file to delete)")
```

##### 3.2.6 _find_task (内部辅助)

```python
def _find_task(self, task_id: str) -> Task | None:
    """按 ID 查找任务, 未找到返回 None."""
    for t in self._tasks:
        if t.id == task_id:
            return t
    return None
```

#### 3.3 DAG 操作

##### 3.3.1 _compute_ready

```python
def _compute_ready(self) -> None:
    """扫描所有任务, 基于依赖状态自动转换 pending ↔ ready."""
    done_ids = {t.id for t in self._tasks if t.status == "done"}

    for task in self._tasks:
        if task.status == "pending":
            # 所有依赖都 done → 升为 ready
            if task.depends_on and all(dep in done_ids for dep in task.depends_on):
                task.status = "ready"
                task.updated_at = _now_iso()
            elif not task.depends_on:
                # 无依赖的任务也是 ready
                task.status = "ready"
                task.updated_at = _now_iso()
        elif task.status == "ready":
            # 防御: 如果依赖不再是 done (理论上不应发生), 降回 pending
            if task.depends_on and not all(dep in done_ids for dep in task.depends_on):
                task.status = "pending"
                task.updated_at = _now_iso()
```

**关键行为**：
- 无依赖的任务 (`depends_on == []`) 创建后立即变为 `ready`
- `done` 解除下游阻塞; `failed` **不**解除（只有 `done` 算完成）
- 防御性降级: `ready` → `pending` 是安全网，理论上不会触发

**D12 修订**：`failed` 不解除下游阻塞。这意味着如果 T1 failed，依赖 T1 的 T2 将保持 pending/blocked。Agent 需要在 notes 中记录失败原因，并决定是修复后重新创建计划还是跳过。

##### 3.3.2 _detect_cycle

```python
def _detect_cycle(self) -> bool:
    """DFS 三色环检测. O(V+E)."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {t.id: WHITE for t in self._tasks}
    adj = {t.id: t.depends_on for t in self._tasks}

    def dfs(node: str) -> bool:
        color[node] = GRAY
        for dep in adj.get(node, []):
            if color.get(dep) == GRAY:
                return True   # 回边 = 环
            if color.get(dep) == WHITE and dfs(dep):
                return True
        color[node] = BLACK
        return False

    return any(color[n] == WHITE and dfs(n) for n in color)
```

##### 3.3.3 _next_id

```python
def _next_id(self) -> str:
    """基于当前最大 ID 生成下一个 ID."""
    if not self._tasks:
        return "T1"
    max_num = max(int(t.id[1:]) for t in self._tasks if t.id.startswith("T") and t.id[1:].isdigit())
    return f"T{max_num + 1}"
```

#### 3.4 持久化

##### 3.4.1 save

```python
def save(self) -> None:
    """原子写入 plan.json."""
    self._persist_dir.mkdir(parents=True, exist_ok=True)
    plan_path = self._persist_dir / "plan.json"

    data = {
        "version": 1,
        "tasks": [
            {
                "id": t.id,
                "description": t.description,
                "status": t.status,
                "depends_on": t.depends_on,
                "notes": t.notes,
                "created_at": t.created_at,
                "updated_at": t.updated_at,
            }
            for t in self._tasks
        ],
    }

    # tempfile + rename 保证原子性
    fd, tmp_path = tempfile.mkstemp(dir=str(self._persist_dir), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, str(plan_path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
```

##### 3.4.2 load

```python
def load(self) -> bool:
    """从磁盘加载计划. 返回 True 表示成功加载, False 表示无文件或解析失败."""
    plan_path = self._persist_dir / "plan.json"
    if not plan_path.is_file():
        return False
    try:
        data = json.loads(plan_path.read_text(encoding="utf-8"))
        self._tasks = []
        for td in data.get("tasks", []):
            self._tasks.append(Task(
                id=td["id"],
                description=td["description"],
                status=td.get("status", "pending"),
                depends_on=td.get("depends_on", []),
                notes=td.get("notes", ""),
                created_at=td.get("created_at", ""),
                updated_at=td.get("updated_at", ""),
            ))
        self._compute_ready()
        logger.info("Loaded plan: %d tasks from %s", len(self._tasks), plan_path)
        return True
    except (json.JSONDecodeError, KeyError) as e:
        logger.warning("Failed to load plan from %s: %s", plan_path, e)
        return False
```

#### 3.5 format_summary

```python
def format_summary(self) -> str:
    """格式化 [TASK PROGRESS] 块. 无计划时返回空字符串."""
    if not self._tasks:
        return ""

    ICONS = {"done": "✓", "failed": "✗", "in_progress": "→", "ready": "◉", "pending": "○"}
    done_ids = {t.id for t in self._tasks if t.status == "done"}

    lines = ["[TASK PROGRESS]"]
    for task in self._tasks:
        icon = ICONS.get(task.status, "○")
        line = f"{icon} {task.id}: {task.description}"

        # 依赖标注
        if task.status == "pending" and task.depends_on:
            unmet = [d for d in task.depends_on if d not in done_ids]
            if unmet:
                line += f" [waiting: {', '.join(unmet)}]"
        elif task.status == "ready":
            line += " [ready]"

        # 备注
        if task.notes:
            line += f" — {task.notes}"
        lines.append(line)

    # Ready 行
    ready = [t for t in self._tasks if t.status == "ready"]
    if ready:
        lines.append(f"Ready: {', '.join(t.id for t in ready)}")

    # 进度统计
    total = len(self._tasks)
    done = sum(1 for t in self._tasks if t.status == "done")
    failed = sum(1 for t in self._tasks if t.status == "failed")
    in_prog = sum(1 for t in self._tasks if t.status == "in_progress")
    ready_n = len(ready)
    pending = total - done - failed - in_prog - ready_n
    parts = [f"{done}/{total} done"]
    if ready_n:
        parts.append(f"{ready_n} ready")
    if in_prog:
        parts.append(f"{in_prog} in progress")
    if pending:
        parts.append(f"{pending} pending")
    if failed:
        parts.append(f"{failed} failed")
    lines.append(f"Progress: {', '.join(parts)}")

    # 下一步: 优先 ready, 其次 in_progress
    next_task = None
    for t in self._tasks:
        if t.status == "ready":
            next_task = t
            break
    if next_task is None:
        for t in self._tasks:
            if t.status == "in_progress":
                next_task = t
                break
    if next_task:
        lines.append(f"Next: {next_task.id} — {next_task.description}")

    return "\n".join(lines)
```

**输出示例**：

```
[TASK PROGRESS]
✓ T1: 设计数据模型 — Task dataclass + DAG 验证
✓ T2: 实现 TaskGraphManager — 核心逻辑完成
→ T3: 写单元测试
◉ T4: 写持久化层 [ready]
○ T5: 集成到 agent loop [waiting: T3, T4]
Ready: T4
Progress: 2/5 done, 1 ready, 1 in progress, 1 pending
Next: T4 — 写持久化层
```

#### 3.6 工具定义

##### create_plan

```python
CREATE_PLAN_TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "create_plan",
        "description": (
            "Create a new task plan for a multi-step task. Replaces any "
            "existing plan. Each task can depend on earlier tasks by ID "
            "(e.g. T1, T2). Tasks with all dependencies completed will "
            "automatically become 'ready'. Use this when decomposing "
            "complex work into steps with dependencies."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "description": {
                                "type": "string",
                                "description": "What this task does",
                            },
                            "depends_on": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "IDs of tasks that must complete first "
                                    "(e.g. ['T1']). References positional IDs: "
                                    "the 1st task is T1, 2nd is T2, etc. "
                                    "Omit or empty for no dependencies."
                                ),
                            },
                            "notes": {
                                "type": "string",
                                "description": "Optional notes",
                            },
                        },
                        "required": ["description"],
                    },
                    "description": (
                        "Ordered list of tasks. IDs are auto-assigned: "
                        "1st task = T1, 2nd = T2, etc."
                    ),
                }
            },
            "required": ["tasks"],
        },
    },
}
```

##### update_task

```python
UPDATE_TASK_TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "update_task",
        "description": (
            "Update a single task's status and/or notes. Call this when you "
            "start working on a task (status='in_progress'), complete it "
            "(status='done'), or encounter an error (status='failed'). "
            "Dependencies are re-evaluated automatically after each update."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task ID to update (e.g. 'T1')",
                },
                "status": {
                    "type": "string",
                    "enum": ["in_progress", "done", "failed"],
                    "description": (
                        "New status: 'in_progress' to start, "
                        "'done' to complete, 'failed' on error."
                    ),
                },
                "notes": {
                    "type": "string",
                    "description": "Update notes (e.g. outcome summary)",
                },
            },
            "required": ["task_id"],
        },
    },
}
```

##### add_task

```python
ADD_TASK_TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "add_task",
        "description": (
            "Add a new task to the existing plan. Use when you discover "
            "additional work mid-execution. The new task gets the next "
            "available ID (e.g. T6 if T5 is the current last task)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "What this task does",
                },
                "depends_on": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "IDs of prerequisite tasks (e.g. ['T2', 'T3'])",
                },
                "notes": {
                    "type": "string",
                    "description": "Optional notes",
                },
            },
            "required": ["description"],
        },
    },
}
```

##### get_plan

```python
GET_PLAN_TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "get_plan",
        "description": (
            "Read the current task plan. Returns the formatted plan with "
            "all task statuses, dependencies, and which tasks are ready. "
            "Call this when you need to check overall progress, especially "
            "after context may have been compressed."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
}
```

#### 3.7 Handler 函数

```python
def run_create_plan(args: dict[str, Any], graph: TaskGraphManager) -> str:
    tasks = args.get("tasks", [])
    if not tasks:
        return "Error: 'tasks' must be a non-empty list."
    try:
        return graph.create_plan(tasks)
    except ValueError as e:
        return f"Error: {e}"

def run_update_task(args: dict[str, Any], graph: TaskGraphManager) -> str:
    task_id = args.get("task_id", "")
    if not task_id:
        return "Error: 'task_id' is required."
    try:
        return graph.update_task(
            task_id=task_id,
            status=args.get("status"),
            notes=args.get("notes"),
        )
    except ValueError as e:
        return f"Error: {e}"

def run_add_task(args: dict[str, Any], graph: TaskGraphManager) -> str:
    description = args.get("description", "")
    if not description:
        return "Error: 'description' is required."
    try:
        return graph.add_task(
            description=description,
            depends_on=args.get("depends_on"),
            notes=args.get("notes", ""),
        )
    except ValueError as e:
        return f"Error: {e}"

def run_get_plan(args: dict[str, Any], graph: TaskGraphManager) -> str:
    summary = graph.format_summary()
    return summary or "No active plan."

ALL_TASK_GRAPH_TOOLS = [
    (CREATE_PLAN_TOOL_DEFINITION, run_create_plan),
    (UPDATE_TASK_TOOL_DEFINITION, run_update_task),
    (ADD_TASK_TOOL_DEFINITION, run_add_task),
    (GET_PLAN_TOOL_DEFINITION, run_get_plan),
]
```

#### 3.8 完整模块骨架

```python
"""Persistent task graph with dependency tracking for multi-step planning."""

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import settings
from logger import get_logger

logger = get_logger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Task:
    # ... (如 §3.1)

class TaskGraphManager:
    # ... (如 §3.2-3.5)

# 工具定义 (如 §3.6)
# Handler 函数 (如 §3.7)
# ALL_TASK_GRAPH_TOOLS (如 §3.7)
```

### 4. `src/context_manager/pipeline.py` — 参数重命名

**修改点**:

```python
# BEFORE (L11):
from context_manager.tracker import ProgressTracker

# AFTER:
from context_manager.task_graph import TaskGraphManager
```

```python
# BEFORE (L41):
progress_tracker: ProgressTracker | None = None,

# AFTER:
task_graph: TaskGraphManager | None = None,
```

```python
# BEFORE (L59):
progress_tracker=progress_tracker,

# AFTER:
task_graph=task_graph,
```

### 5. `src/context_manager/macro_compressor.py` — 参数重命名

**修改 5 处引用**:

```python
# L49: progress_tracker=None → task_graph=None
# L54: self.progress_tracker = progress_tracker → self.task_graph = task_graph
# L84: bool(self.progress_tracker and self.progress_tracker.has_plan)
#      → bool(self.task_graph and self.task_graph.has_plan)
# L89-90: self.progress_tracker.format_summary() → self.task_graph.format_summary()
# L122-126: same pattern
```

接口名 `has_plan` 和 `format_summary()` 保持不变——TaskGraphManager 提供完全相同的 API。

### 6. `src/context_manager/meso_compressor.py` — 工具名映射

**修改 L197 附近**:

```python
# BEFORE:
if tool_name == "update_plan":
    return "updated task plan"

# AFTER:
if tool_name == "create_plan":
    n = len(args.get("tasks", []))
    return f"created plan ({n} tasks)"
if tool_name == "update_task":
    tid = args.get("task_id", "?")
    st = args.get("status", "?")
    return f"updated {tid} to {st}"
if tool_name == "add_task":
    return f"added task: {args.get('description', '?')[:50]}"
if tool_name == "get_plan":
    return "checked plan"
```

### 7. `src/agent/async_loop.py` — 替换 tracker

**修改点**:

```python
# BEFORE (L15-16):
from context_manager.tracker import (
    ProgressTracker,
    run_update_plan,
    UPDATE_PLAN_TOOL_DEFINITION,
)

# AFTER:
from context_manager.task_graph import ALL_TASK_GRAPH_TOOLS, TaskGraphManager
```

```python
# BEFORE (L53):
self.progress_tracker = ProgressTracker()

# AFTER:
self.task_graph = TaskGraphManager(sandbox_root=settings.SANDBOX_ROOT)
self.task_graph.load()  # 从磁盘恢复
```

```python
# BEFORE (L64):
progress_tracker=self.progress_tracker,

# AFTER:
task_graph=self.task_graph,
```

```python
# BEFORE (L68-71):
self.tool_registry.register(
    UPDATE_PLAN_TOOL_DEFINITION,
    lambda args: run_update_plan(args, self.progress_tracker),
)

# AFTER:
for definition, handler in ALL_TASK_GRAPH_TOOLS:
    self.tool_registry.register(
        definition,
        lambda args, h=handler: h(args, self.task_graph),
    )
```

```python
# BEFORE (L87):
self._child_registry = AsyncToolRegistry(exclude=["task", "update_plan"])

# AFTER:
self._child_registry = AsyncToolRegistry(
    exclude=["task", "create_plan", "update_task", "add_task", "get_plan"]
)
```

```python
# BEFORE (L125-126):
if self.progress_tracker.has_plan:
    summary = self.progress_tracker.format_summary()

# AFTER:
if self.task_graph.has_plan:
    summary = self.task_graph.format_summary()
```

### 8. `src/agent/loop.py` — 同步版同样改造

与 async_loop.py 相同的修改模式。额外修改 SYSTEM_PROMPT:

```python
# BEFORE (L25-28):
SYSTEM_PROMPT = """\
...
For multi-step tasks, use the update_plan tool to create a plan with numbered steps. \
...call update_plan with the revised step list. This keeps you on track for long tasks."""

# AFTER:
SYSTEM_PROMPT = """\
...
For multi-step tasks, use create_plan to define tasks and their dependencies.
Mark tasks 'in_progress' when you start them and 'done' when finished using
update_task. Tasks with all dependencies completed will automatically become
'ready' — prioritize these. Use get_plan to check overall progress. You can
add_task if you discover new work mid-execution. This keeps you on track for
long tasks."""
```

### 9. `src/context_manager/__init__.py` — 导出替换

```python
# BEFORE:
from context_manager.tracker import ProgressTracker
__all__ = [..., "ProgressTracker", ...]

# AFTER:
from context_manager.task_graph import TaskGraphManager
__all__ = [..., "TaskGraphManager", ...]
```

### 10. `src/agent/__init__.py` — 导出替换

```python
# BEFORE:
from context_manager.tracker import ProgressTracker
__all__ = [..., "ProgressTracker", ...]

# AFTER:
from context_manager.task_graph import TaskGraphManager
__all__ = [..., "TaskGraphManager", ...]
```

### 11. `src/cli/repl.py` — CLI 引用替换

```python
# L134-135 BEFORE:
if agent and agent.progress_tracker.has_plan:
    summary = agent.progress_tracker.format_summary()
# AFTER:
if agent and agent.task_graph.has_plan:
    summary = agent.task_graph.format_summary()

# L143 BEFORE:
agent.progress_tracker.reset()
# AFTER:
agent.task_graph.reset()

# L179 BEFORE:
has_plan = "✓" if agent and agent.progress_tracker.has_plan else "—"
# AFTER:
has_plan = "✓" if agent and agent.task_graph.has_plan else "—"
```

### 12. `src/cli/display.py` — 新图标解析

```python
# BEFORE:
if len(line) >= 2 and line[0] in ("✓", "→", "○", "⊘"):
    icon = line[0]
    rest = line[2:]
    style_map = {"✓": "green", "→": "cyan", "○": "dim", "⊘": "red"}

# AFTER:
if len(line) >= 2 and line[0] in ("✓", "✗", "→", "◉", "○"):
    icon = line[0]
    rest = line[2:]
    style_map = {
        "✓": "green",
        "✗": "red",
        "→": "cyan",
        "◉": "bold yellow",
        "○": "dim",
    }
```

新增 Ready 行处理:
```python
if line.startswith("Ready:"):
    self.console.print(f"[yellow]{line}[/yellow]")
    continue
```

## 5. 集成方案

### 5.1 Agent 循环生命周期

```
Agent 启动
  → __init__: TaskGraphManager(sandbox_root) + load()
  → 如果 .mini_agent/plan.json 存在: 恢复上次计划
  → 如果不存在: 空状态, 等待 create_plan

Agent 运行中 (chat loop):
  每轮:
    1. _inject_progress(): 注入 [TASK PROGRESS] 到 messages[1]
    2. LLM 看到 ready 任务列表, 决定执行哪个
    3. LLM 调用 update_task(status="in_progress") 开始任务
    4. LLM 执行工具调用 (bash, read_file, write_file...)
    5. LLM 调用 update_task(status="done", notes="...") 完成任务
    6. _compute_ready() 自动更新下游任务
    7. save() 写入 plan.json

Agent 重启:
  → load() 恢复计划, [TASK PROGRESS] 显示上次进度
```

### 5.2 Context Pipeline 集成

```
ContextPipeline (pipeline.py)
  └── MacroCompressor (macro_compressor.py)
        ├── 压缩前: 读取 task_graph.format_summary() 作为 progress_context
        ├── LLM 生成摘要时, 任务状态信息更丰富 (依赖关系、ready/blocked)
        └── 压缩后: 在 result 中注入 task_graph.format_summary() 作为 system message
```

Layer 3 从 TaskGraph 获取的信息比 ProgressTracker 更丰富:
- 旧: "→ [2/5] 实现模块 A (in progress)"
- 新: "→ T2: 实现模块 A [waiting: T1] → Ready: T3, T4"

### 5.3 Subagent 隔离

```
Parent Agent (main registry)
  ├── bash, read_file, write_file, list_directory, ...
  ├── create_plan, update_task, add_task, get_plan  ← 仅父级
  └── task

Subagent (child registry)
  ├── bash, read_file, write_file, list_directory, ...
  └── load_skill
  # 无 create_plan/update_task/add_task/get_plan
  # 无 task (禁止递归)
```

## 6. 文件变更清单

| 文件 | 变更类型 | 说明 |
|---|---|---|
| `src/context_manager/task_graph.py` | **新建** | Task + TaskGraphManager + 4 工具 + 4 handler (~350 行) |
| `src/context_manager/tracker.py` | **删除** | 被 task_graph.py 完全替代 |
| `src/config.py` | 修改 | 新增 `TASK_GRAPH_DIR` (1 行) |
| `src/context_manager/pipeline.py` | 修改 | 参数 `progress_tracker` → `task_graph` (3 处) |
| `src/context_manager/macro_compressor.py` | 修改 | 属性 `progress_tracker` → `task_graph` (5 处) |
| `src/context_manager/meso_compressor.py` | 修改 | 工具名映射 `"update_plan"` → 4 个新工具名 (~10 行) |
| `src/context_manager/__init__.py` | 修改 | 导出替换 (2 处) |
| `src/agent/async_loop.py` | 修改 | 构造、注册、注入、pipeline、exclude (6 处) |
| `src/agent/loop.py` | 修改 | 同上 + SYSTEM_PROMPT (7 处) |
| `src/agent/__init__.py` | 修改 | 导出替换 (2 处) |
| `src/cli/repl.py` | 修改 | `progress_tracker` → `task_graph` (4 处) |
| `src/cli/display.py` | 修改 | 图标集 + Ready 行处理 (~10 行) |
| `.gitignore` | 修改 | 新增 `.mini_agent/` (2 行) |
| `tests/context_manager/test_task_graph.py` | **新建** | 全面测试 (~400 行) |
| `tests/context_manager/test_tracker.py` | **删除** | 被 test_task_graph.py 替代 |
| `tests/context_manager/test_macro_compressor.py` | 修改 | ProgressTracker → TaskGraphManager |
| `tests/context_manager/test_pipeline_integration.py` | 修改 | ProgressTracker → TaskGraphManager |
| `tests/agent/test_subagent.py` | 修改 | exclude 断言更新 |
| `tests/agent/test_skill_integration.py` | 修改 | exclude 断言更新 |

## 7. 测试策略

### 7.1 TestTask — Task dataclass

| 测试 | 验证点 |
|---|---|
| `test_default_values` | status="pending", depends_on=[], notes="" |
| `test_custom_values` | 所有字段可自定义 |
| `test_valid_statuses` | VALID_STATUSES 包含 5 个值 |
| `test_terminal_statuses` | TERMINAL_STATUSES 包含 done, failed |
| `test_agent_settable` | AGENT_SETTABLE 不包含 ready |

### 7.2 TestTaskGraphManager — 核心管理

| 测试 | 验证点 |
|---|---|
| `test_initial_state` | 空 tasks, has_plan=False, format_summary()="" |
| `test_create_plan_basic` | 3 个无依赖任务, ID 自动分配 T1/T2/T3, 全部 ready |
| `test_create_plan_with_deps` | T2 depends_on=[T1], T1 ready, T2 pending |
| `test_create_plan_replaces` | 第二次 create_plan 清除旧任务 |
| `test_create_plan_cycle` | A→B→A 抛 ValueError |
| `test_create_plan_invalid_ref` | depends_on=["T99"] 抛 ValueError |
| `test_update_task_in_progress` | pending → in_progress 成功 |
| `test_update_task_done` | in_progress → done 成功 |
| `test_update_task_ready_computation` | T1 done → T2 (deps=[T1]) 自动 ready |
| `test_update_task_invalid_id` | 未知 ID 抛 ValueError |
| `test_update_task_invalid_status` | "ready" 不在 AGENT_SETTABLE 中, 抛 ValueError |
| `test_update_task_terminal_reject` | done 的任务不可再 update |
| `test_update_task_notes` | notes 被更新 |
| `test_add_task_basic` | 追加任务, ID 递增 |
| `test_add_task_with_deps` | 追加带依赖任务, ready 重算 |
| `test_add_task_no_plan` | 无计划时 add_task 抛 ValueError |
| `test_add_task_cycle` | 追加导致环的任务抛 ValueError |
| `test_reset` | 清空任务, 文件删除 |

### 7.3 TestDAGOperations — DAG 算法

| 测试 | 验证点 |
|---|---|
| `test_ready_no_deps` | 无依赖任务创建后立刻 ready |
| `test_ready_all_deps_done` | 所有 deps done → ready |
| `test_pending_some_deps_undone` | 部分 deps 未完成 → pending |
| `test_failed_does_not_unblock` | T1 failed 不解除 T2 的 blocked |
| `test_chain_dependency` | T1→T2→T3, T1 done → T2 ready, T3 仍 pending |
| `test_diamond_dependency` | T1→T3, T2→T3, 仅 T1 done 时 T3 不 ready |
| `test_parallel_branches` | T1→T2, T1→T3, T1 done → T2+T3 同时 ready |
| `test_cycle_direct` | A→B→A 检测为环 |
| `test_cycle_transitive` | A→B→C→A 检测为环 |
| `test_no_false_cycle` | A→B, A→C, B→C (合法 DAG) 不误报 |

### 7.4 TestFormatSummary — 格式输出

| 测试 | 验证点 |
|---|---|
| `test_empty_plan` | 返回空字符串 |
| `test_done_icon` | done 任务显示 ✓ |
| `test_failed_icon` | failed 任务显示 ✗ |
| `test_in_progress_icon` | in_progress 任务显示 → |
| `test_ready_icon` | ready 任务显示 ◉ |
| `test_pending_icon` | pending 任务显示 ○ |
| `test_waiting_annotation` | pending + 未完成依赖 → [waiting: T1] |
| `test_ready_annotation` | ready → [ready] |
| `test_ready_line` | Ready: T3, T4 |
| `test_progress_line_counts` | Progress 行数字正确 |
| `test_next_prefers_ready` | Next 优先显示 ready |
| `test_next_fallback_in_progress` | 无 ready 时显示 in_progress |
| `test_notes_display` | notes 在 — 后显示 |

### 7.5 TestPersistence — 持久化

| 测试 | 验证点 |
|---|---|
| `test_save_creates_file` | plan.json 存在且合法 JSON |
| `test_load_restores_tasks` | save → 新实例 load → 任务一致 |
| `test_load_no_file` | 无文件 → 返回 False |
| `test_load_corrupted_json` | 损坏文件 → 返回 False, 不崩溃 |
| `test_load_missing_fields` | 缺少字段 → 使用默认值 |
| `test_reset_deletes_file` | reset 后文件不存在 |
| `test_atomic_write_cleanup` | 异常时 tmp 文件被清理 |
| `test_auto_load_on_init` | TaskGraphManager(sandbox_root) 构造时自动 load |

### 7.6 TestToolHandlers — 工具处理函数

| 测试 | 验证点 |
|---|---|
| `test_run_create_plan_delegates` | 委托 graph.create_plan |
| `test_run_create_plan_empty_error` | 空列表返回 Error |
| `test_run_create_plan_cycle_error` | 环依赖返回 Error 消息 |
| `test_run_update_task_delegates` | 委托 graph.update_task |
| `test_run_update_task_no_id_error` | 缺 task_id 返回 Error |
| `test_run_add_task_delegates` | 委托 graph.add_task |
| `test_run_add_task_no_desc_error` | 缺 description 返回 Error |
| `test_run_get_plan_returns_summary` | 返回 format_summary 输出 |
| `test_run_get_plan_no_plan` | 无计划返回 "No active plan." |

### 7.7 现有测试更新

| 测试文件 | 修改 |
|---|---|
| `test_macro_compressor.py` L172-191 | `ProgressTracker()` → `TaskGraphManager()`，`update_plan(...)` → `create_plan([...])` |
| `test_pipeline_integration.py` L206-221 | 同上模式 |
| `test_subagent.py` L120, L247 | `assertNotIn("update_plan")` → `assertNotIn("create_plan")` + `assertNotIn("update_task")` + `assertNotIn("add_task")` + `assertNotIn("get_plan")` |
| `test_skill_integration.py` L101, L109 | child registry exclude 列表 + 断言更新 |

## 8. 迁移顺序 (7 步)

每步保证 `python -m unittest discover` 全部通过。

### Step 1: 创建新模块 (纯增量)
1. 创建 `src/context_manager/task_graph.py`
2. 创建 `tests/context_manager/test_task_graph.py`
3. 运行 `python -m unittest tests.context_manager.test_task_graph -v`
4. **退出标准**: 新测试全部通过，旧测试不受影响

### Step 2: 配置 + gitignore
1. `src/config.py` 新增 `TASK_GRAPH_DIR`
2. `.gitignore` 新增 `.mini_agent/`
3. **退出标准**: `from config import settings; settings.TASK_GRAPH_DIR` 返回 ".mini_agent"

### Step 3: 更新 context pipeline
1. `pipeline.py`: 参数 rename + import 更新
2. `macro_compressor.py`: 属性 rename
3. `meso_compressor.py`: 工具名映射
4. `context_manager/__init__.py`: 新增 TaskGraphManager 导出（暂保留 ProgressTracker）
5. 更新 `test_macro_compressor.py` 和 `test_pipeline_integration.py`
6. **退出标准**: `python -m unittest tests.context_manager -v` 全部通过

### Step 4: 更新 agent loops
1. `async_loop.py`: import, 构造, 注册, exclude, _inject_progress, pipeline
2. `loop.py`: 同上 + SYSTEM_PROMPT
3. `agent/__init__.py`: 导出更新
4. 更新 `test_subagent.py` 和 `test_skill_integration.py`
5. **退出标准**: `python -m unittest tests.agent -v` 全部通过

### Step 5: 更新 CLI
1. `cli/repl.py`: `progress_tracker` → `task_graph`
2. `cli/display.py`: 图标集 + Ready 行
3. **退出标准**: 手动验证 `/plan`、`/reset`、`/sessions`

### Step 6: 删除旧代码
1. 删除 `src/context_manager/tracker.py`
2. 删除 `tests/context_manager/test_tracker.py`
3. `context_manager/__init__.py` 移除 ProgressTracker 导出
4. `agent/__init__.py` 移除 ProgressTracker 导出
5. **退出标准**: `python -m unittest discover -s tests -t . -v` 全部通过

### Step 7: 集成验证
1. 全量测试: `python -m unittest discover -s tests -t . -v`
2. Lint: `ruff check src/ tests/`
3. 手动集成测试:
   - 启动 Agent, 给多步骤任务
   - 验证 `create_plan` 创建 DAG, `[TASK PROGRESS]` 显示 ready/blocked
   - 验证 `update_task` 推进状态, ready 自动计算
   - 验证 `.mini_agent/plan.json` 内容正确
   - 重启 Agent, 验证计划恢复
   - 验证 `/plan` 显示状态, `/reset` 清除
4. **退出标准**: 端到端工作正常

## 9. 风险与缓解

| 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|
| LLM 引用不存在的 dep ID | 中 | 低 | create_plan/add_task 验证引用存在，返回 Error 让 LLM 修正 |
| LLM 创建环依赖 | 低 | 低 | _detect_cycle() 在 create/add 时检测 |
| JSON 写入中断（进程崩溃） | 低 | 中 | tempfile + os.replace 原子写入 |
| LLM 忘记 update_task | 中 | 中 | _inject_progress 每轮注入提醒 |
| failed 任务阻塞下游 | 低 | 中 | 设计文档明确: failed 不解除阻塞。Agent 可 add_task 创建替代任务或 reset 重建计划 |
| 向后兼容测试断裂 | 中 | 高 | 7 步迁移，每步验证。先加后删 |
| TaskGraphManager 与 ProgressTracker API 不兼容 | 低 | 中 | 保持 has_plan + format_summary() 相同接口名 |
