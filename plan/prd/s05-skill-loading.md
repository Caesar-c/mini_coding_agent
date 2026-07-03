# PRD: Skill Loading — 按需加载的领域知识注入

> **阶段**: s05 · **前置**: s04 (Subagent) · **后续**: s06 (上下文压缩增强)
>
> 核心理念: *"领域知识按需加载, 不预塞进 system prompt"* — 通过 `load_skill` 工具在 tool_result 中注入完整知识, system prompt 只保留廉价目录。

## 1. 背景与动机

### 问题

当前 Agent 的全部领域知识硬编码在 `SYSTEM_PROMPT` 常量中 (`loop.py`, ~27 行)。随着能力扩展, 这种方式的弊端日益明显:

- **Token 浪费**: 所有知识不分场景全量注入 system prompt, 即使本次对话完全用不到。一个包含 5 个领域指南的 system prompt 可能消耗 8000+ tokens, 每轮 LLM 调用都要重复付费
- **不可扩展**: 每增加一项能力就要修改 `SYSTEM_PROMPT` 源码, 重新部署。用户无法自行添加领域知识
- **注意力稀释**: LLM 的注意力被大量无关知识稀释, 对当前任务真正需要的知识反而关注不足。这是 "lost in the middle" 问题的变体
- **缺乏模块化**: 知识以纯文本形式散布在 prompt 中, 无法独立版本管理、复用、分享

### 典型场景

```
场景: 用户让 Agent 做 code review

当前行为 (无 skill loading):
  → system prompt 硬塞了 git-workflow、code-review、testing、security、deploy 全部指南
  → 总计 8000 tokens 的知识, 每轮 LLM 调用都重复发送
  → Agent 实际只需要 code-review 指南
  → 其余 4 个指南 (~6000 tokens) 是纯粹的浪费

期望行为 (有 skill loading):
  → system prompt 只包含 skill 目录: "code-review: 代码审查最佳实践" (~50 tokens)
  → Agent 判断需要 code review 知识, 调用 load_skill("code-review")
  → tool_result 返回完整的 code-review 指南 (~2000 tokens)
  → 只在需要时加载, 其余 4 个 skill 的完整内容从未消耗 token
```

### 与 s04 Subagent 的关系

s04 解决了"大任务拆小, 上下文隔离"的问题。s05 解决的是正交问题: "知识按需加载, 不预塞"。两者结合后:
- 父 Agent 可以 `load_skill("code-review")` 获取审查指南, 然后通过 `task` 工具派生 subagent 执行具体的审查工作
- Subagent 也可以自行 `load_skill` — 因为 skill 是只读知识, 对子上下文安全

## 2. 目标与非目标

### 目标

| #   | 目标           | 衡量标准                                                   |
| --- | ------------ | ------------------------------------------------------ |
| G1  | Token 效率     | system prompt 中每个 skill 仅占 ~100 tokens (目录项); 完整内容仅在 tool_result 中出现 |
| G2  | 可扩展性        | 用户通过在 `./skills/` 目录下放置 `SKILL.md` 文件即可添加新 skill, 无需修改源码 |
| G3  | 优雅降级        | 格式错误的 skill 文件被记录日志并跳过, 不阻塞 Agent 启动                       |
| G4  | 透明可观测       | 日志记录 skill 扫描结果 (成功/跳过/失败数量)、每次 `load_skill` 调用         |
| G5  | 与现有架构一致     | `load_skill` 作为普通工具注册在 dispatch map 中, 无需修改 Agent 循环逻辑  |

### 非目标

- **不做** 技能热重载 (运行中添加 skill 文件不自动生效, 需重启 Agent)
- **不做** 技能版本解析/依赖管理 (frontmatter 中的 version 仅供人类阅读)
- **不做** 内置技能市场/远程下载
- **不做** 技能参数化 (skill 内容是静态 markdown, 不接受模板变量)
- **不做** 技能执行 (skill 是给 LLM 阅读的知识文档, 不是可执行脚本)

## 3. 架构设计

### 3.1 两层注入模型

```
┌──────────────────────────────────────────────────────────────────┐
│ System Prompt (Layer 1 — 始终在场, 廉价)                            │
│                                                                  │
│ "...You have access to the following skills:                     │
│  • code-review — 代码审查最佳实践与常见问题清单 (v1.0)              │
│  • git-workflow — Git 分支策略、commit 规范与 PR 流程 (v1.0)       │
│  • testing — pytest 测试组织、fixture 设计与覆盖率目标 (v1.0)      │
│  Use load_skill(name) to load full skill content."              │
│                                                                  │
│ 成本: ~50 tokens/skill × N skills ≈ 几百 tokens (固定开销)         │
└──────────────────────────────────────────────────────────────────┘
                              │
                     LLM 调用 load_skill("code-review")
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│ Tool Result (Layer 2 — 按需加载, 仅在使用时出现)                      │
│                                                                  │
│ "# Code Review Skill                                            │
│  ## 审查清单                                                     │
│  1. 正确性: 逻辑是否正确? 边界条件是否处理?                          │
│  2. 性能: 是否有 N+1 查询? 是否有不必要的循环?                       │
│  ... (完整 markdown 文档, ~2000 tokens)                          │
│  ## 常见反模式                                                    │
│  ..."                                                           │
│                                                                  │
│ 成本: ~2000 tokens (仅在 LLM 主动调用 load_skill 时产生)            │
└──────────────────────────────────────────────────────────────────┘
```

### 3.2 组件交互

```
                    skills/                    ~/.config/mini-agent/skills/
                    ┌─────────┐                ┌─────────┐
                    │ code-   │                │ my-     │
                    │ review/ │                │ custom/ │
                    │ SKILL.md│                │ SKILL.md│
                    └────┬────┘                └────┬────┘
                         │                          │
                         └──────────┬───────────────┘
                                    │ scan + parse
                                    ▼
                            ┌──────────────┐
                            │ SkillLoader  │
                            │              │
                            │ _skills:     │
                            │  {name: {    │
                            │   meta: {},  │
                            │   body: str  │
                            │  }, ...}     │
                            └──┬───────┬───┘
                               │       │
              get_descriptions()│       │get_content(name)
                               │       │
                               ▼       ▼
                    ┌──────────┐   ┌──────────────┐
                    │ SYSTEM   │   │ load_skill   │
                    │ PROMPT   │   │ tool_result  │
                    │ (Layer 1)│   │ (Layer 2)    │
                    └──────────┘   └──────────────┘
                         │               │
                         └───────┬───────┘
                                 ▼
                          ┌────────────┐
                          │ AsyncAgent │
                          │ messages[] │
                          └────────────┘
```

### 3.3 工具集归属

```python
# load_skill 是基础工具, 与 bash/read_file 同级
# 父子 Agent 都可以使用 (知识加载是安全的只读操作)

# CHILD_TOOL_NAMES 扩展:
CHILD_TOOL_NAMES = ["bash", "read_file", "write_file",
                    "list_directory", "create_directory",
                    "file_exists", "load_skill"]  # ← 新增

# 父 Agent 独有工具不变:
PARENT_ONLY_TOOLS = ["task"]
```

**设计决策**: `load_skill` 放入 CHILD 工具集而非 PARENT 独有, 因为:
- Skill 是只读知识, 不改变外部状态
- Subagent 执行具体任务时往往更需要领域知识 (如 subagent 做 code review 时需要 code-review skill)
- 不增加复杂度 — 只是多一个工具注册

## 4. 详细设计

### 4.1 Skill 文件格式 (SKILL.md)

每个 skill 是一个目录, 包含一个 `SKILL.md` 文件:

```
skills/
├── code-review/
│   └── SKILL.md
├── git-workflow/
│   └── SKILL.md
└── testing/
    └── SKILL.md
```

`SKILL.md` 采用 YAML frontmatter + Markdown body 格式:

```markdown
---
name: code-review
description: 代码审查最佳实践与常见问题清单
version: "1.0"
author: mini-agent
tags: [review, quality, best-practices]
---

# Code Review Skill

## 审查清单

1. **正确性**: 逻辑是否正确? 边界条件是否处理?
2. **性能**: 是否有 N+1 查询? 是否有不必要的循环?
3. **安全性**: 输入是否验证? 是否有注入风险?
4. **可维护性**: 命名是否清晰? 是否有魔法数字?
...
```

#### Frontmatter 字段规范

| 字段          | 类型          | 必填  | 说明                                       |
| ----------- | ----------- | --- | ---------------------------------------- |
| `name`      | string      | ✅   | 唯一标识符, 用于 `load_skill(name)` 调用。小写+连字符     |
| `description` | string    | ✅   | 一行描述, 用于 system prompt 目录。建议 ≤80 字符       |
| `version`   | string      | ❌   | 语义版本号, 仅供人类阅读 (默认 "1.0")                 |
| `author`    | string      | ❌   | 作者名                                     |
| `tags`      | list[string] | ❌   | 标签列表, 用于未来分类/搜索 (当前不影响功能)             |

**约束**:
- `name` 必须匹配正则 `^[a-z0-9][a-z0-9-]*$` (防止路径注入)
- `description` 超过 100 字符时截断并加 `...`
- `name` 在全部 skill 源中必须唯一 (冲突时 project-level 覆盖 user-level, 并记录警告)

### 4.2 SkillLoader

```python
class SkillLoader:
    """扫描 skill 目录, 解析 SKILL.md, 提供目录和内容查询接口."""

    def __init__(self, skill_dirs: list[Path] | None = None):
        """
        Args:
            skill_dirs: 要扫描的目录列表, 按优先级从高到低排列。
                       默认: [./skills, ~/.config/mini-agent/skills]
        """
        self._skills: dict[str, SkillEntry] = {}
        self._scan(skill_dirs or self._default_dirs())

    def _default_dirs(self) -> list[Path]:
        """返回默认 skill 目录列表 (高优先级在前)."""
        dirs = []
        # 项目级 (最高优先级)
        project_skills = Path.cwd() / "skills"
        if project_skills.is_dir():
            dirs.append(project_skills)
        # 用户级
        user_skills = Path.home() / ".config" / "mini-agent" / "skills"
        if user_skills.is_dir():
            dirs.append(user_skills)
        return dirs

    def _scan(self, dirs: list[Path]):
        """扫描所有目录, 解析 SKILL.md, 填充 _skills."""
        for skill_dir in dirs:
            if not skill_dir.is_dir():
                continue
            for child in sorted(skill_dir.iterdir()):
                if not child.is_dir():
                    continue
                skill_file = child / "SKILL.md"
                if not skill_file.is_file():
                    logger.debug("Skipping %s: no SKILL.md", child.name)
                    continue
                try:
                    entry = self._parse_skill_file(skill_file)
                    if entry.name in self._skills:
                        logger.warning(
                            "Skill name collision: '%s' from %s overridden by %s",
                            entry.name,
                            self._skills[entry.name].source,
                            skill_file,
                        )
                    self._skills[entry.name] = entry
                    logger.info("Loaded skill: %s from %s", entry.name, skill_file)
                except SkillParseError as e:
                    logger.warning("Skipping malformed skill %s: %s", child.name, e)

    def _parse_skill_file(self, path: Path) -> SkillEntry:
        """解析单个 SKILL.md 文件, 分离 frontmatter 和 body."""
        content = path.read_text(encoding="utf-8")
        meta, body = self._split_frontmatter(content)
        # 验证必填字段
        if "name" not in meta:
            raise SkillParseError("Missing required field: 'name'")
        if "description" not in meta:
            raise SkillParseError("Missing required field: 'description'")
        # 验证 name 格式
        if not re.match(r"^[a-z0-9][a-z0-9-]*$", meta["name"]):
            raise SkillParseError(f"Invalid name format: '{meta['name']}'")
        return SkillEntry(
            name=meta["name"],
            description=meta["description"][:100],
            version=meta.get("version", "1.0"),
            author=meta.get("author", ""),
            tags=meta.get("tags", []),
            body=body.strip(),
            source=str(path),
        )

    @staticmethod
    def _split_frontmatter(content: str) -> tuple[dict, str]:
        """手动分离 YAML frontmatter 和 markdown body (零依赖)."""
        if not content.startswith("---"):
            raise SkillParseError("File must start with '---' YAML frontmatter delimiter")
        parts = content.split("---", 2)
        if len(parts) < 3:
            raise SkillParseError("Missing closing '---' for YAML frontmatter")
        yaml_str = parts[1]
        body = parts[2]
        # 使用 yaml.safe_load 解析 (stdlib 无 yaml, 需引入或手写简易解析)
        # 方案 A: 引入 pyyaml 依赖 (推荐, 成熟可靠)
        # 方案 B: 手写简易 key: value 解析 (零依赖, 但不支持复杂 YAML)
        meta = yaml.safe_load(yaml_str) or {}
        if not isinstance(meta, dict):
            raise SkillParseError("Frontmatter must be a YAML mapping")
        return meta, body

    def get_descriptions(self) -> str:
        """返回所有 skill 的目录描述, 用于注入 system prompt.

        格式示例:
        Available skills:
        • code-review (v1.0) — 代码审查最佳实践与常见问题清单
        • git-workflow (v1.0) — Git 分支策略、commit 规范与 PR 流程
        Use load_skill("name") to load full skill content when needed.
        """
        if not self._skills:
            return ""
        lines = ["Available skills:"]
        for entry in sorted(self._skills.values(), key=lambda e: e.name):
            ver = f" (v{entry.version})" if entry.version else ""
            lines.append(f"• {entry.name}{ver} — {entry.description}")
        lines.append('Use load_skill("name") to load full skill content when needed.')
        return "\n".join(lines)

    def get_content(self, name: str) -> str | None:
        """返回指定 skill 的完整 body 内容, 未找到返回 None."""
        entry = self._skills.get(name)
        return entry.body if entry else None

    def list_names(self) -> list[str]:
        """返回所有已加载 skill 的名称列表."""
        return sorted(self._skills.keys())

    @property
    def count(self) -> int:
        return len(self._skills)
```

### 4.3 SkillEntry 数据类

```python
@dataclass(frozen=True)
class SkillEntry:
    """已解析的 skill 条目."""
    name: str
    description: str
    version: str
    author: str
    tags: list[str]
    body: str           # Markdown body (不含 frontmatter)
    source: str         # SKILL.md 文件路径 (用于日志/调试)
```

### 4.4 `load_skill` 工具定义

```python
LOAD_SKILL_TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "load_skill",
        "description": (
            "Load the full content of a domain knowledge skill by name. "
            "Available skills are listed in the system prompt. "
            "Use this when you need specialized knowledge for a task — "
            "e.g., load 'code-review' before reviewing code, "
            "or 'git-workflow' before managing branches."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The skill name to load (e.g., 'code-review')",
                }
            },
            "required": ["name"],
        },
    },
}
```

### 4.5 `load_skill` Handler

```python
def make_load_skill_handler(skill_loader: SkillLoader) -> Callable:
    """创建 load_skill handler 闭包, 绑定到 SkillLoader 实例."""

    def load_skill(args: dict) -> str:
        name = args.get("name", "").strip()
        if not name:
            return "Error: 'name' is required. Available skills: " + \
                   ", ".join(skill_loader.list_names())

        content = skill_loader.get_content(name)
        if content is None:
            available = ", ".join(skill_loader.list_names()) or "(none)"
            logger.warning("Skill not found: %s (available: %s)", name, available)
            return f"Error: Skill '{name}' not found. Available skills: {available}"

        logger.info("Skill loaded: %s, content_len=%d", name, len(content))
        return content

    return load_skill
```

### 4.6 System Prompt 注入

在 `AsyncAgent.__init__` (和 `Agent.__init__`) 中:

```python
# 在 SYSTEM_PROMPT 之后, messages 初始化之前:
self.skill_loader = SkillLoader()

# 构建增强版 system prompt
enhanced_system_prompt = SYSTEM_PROMPT
skill_catalog = self.skill_loader.get_descriptions()
if skill_catalog:
    enhanced_system_prompt += f"\n\n{skill_catalog}"

self.messages = [
    {"role": "system", "content": enhanced_system_prompt},
]

# 注册 load_skill 工具
self.tool_registry.register(
    LOAD_SKILL_TOOL_DEFINITION,
    make_load_skill_handler(self.skill_loader),
)
```

### 4.7 配置项 (`config.py`)

```python
# ---- Skill Loading ----
SKILL_DIRS: str = os.getenv("SKILL_DIRS", "")  # 逗号分隔的额外 skill 目录
SKILL_MAX_CONTENT_CHARS: int = int(os.getenv("SKILL_MAX_CONTENT_CHARS", "10000"))
```

- `SKILL_DIRS`: 允许用户指定额外的 skill 目录 (逗号分隔), 插入到默认目录列表最前面 (最高优先级)
- `SKILL_MAX_CONTENT_CHARS`: 单个 skill body 的最大字符数, 超过则截断 (防御性上限)

## 5. 集成方案

### 5.1 AsyncAgent 改造

```python
# async_loop.py 修改

class AsyncAgent:
    def __init__(self, llm_provider_type=None, display=None):
        self.llm_provider = create_llm_provider(...)

        # --- Skill loading (新增) ---
        self.skill_loader = SkillLoader()
        enhanced_prompt = SYSTEM_PROMPT
        skill_catalog = self.skill_loader.get_descriptions()
        if skill_catalog:
            enhanced_prompt += f"\n\n{skill_catalog}"

        self.messages = [
            {"role": "system", "content": enhanced_prompt},
        ]

        # --- 工具注册 (改造) ---
        self.tool_registry = AsyncToolRegistry()
        self.progress_tracker = ProgressTracker()
        self.context_compactor = ContextCompactor(...)
        self.display = display or SilentDisplayHandler()

        self.tool_registry.register(UPDATE_PLAN_TOOL_DEFINITION, ...)

        # 注册 load_skill 工具 (父子共享)
        self.tool_registry.register(
            LOAD_SKILL_TOOL_DEFINITION,
            make_load_skill_handler(self.skill_loader),
        )

        # Subagent: child registry 现在自动包含 load_skill
        # (因为 load_skill 不在 exclude 列表中)
        self._child_registry = AsyncToolRegistry(exclude=["task", "update_plan"])
        self._child_tool_definitions = self._child_registry.definitions

        self.tool_registry.register(
            TASK_TOOL_DEFINITION,
            make_task_handler(self),
        )
```

**关键变化**: `_child_registry` 的 `exclude` 列表保持 `["task", "update_plan"]`。`load_skill` 不在排除列表中, 因此 subagent 自动获得 `load_skill` 能力。但 subagent 需要自己的 `SkillLoader` 实例或共享父 Agent 的 — 推荐共享 (skill 是无状态的只读数据)。

### 5.2 Subagent 中的 Skill 支持

Subagent 的 `make_task_handler` 需要让 child registry 能访问 `load_skill`:

```python
def make_task_handler(agent):
    llm_provider = agent.llm_provider
    child_registry = agent._child_registry
    # child_registry 已包含 load_skill (通过 AsyncToolRegistry 自动注册)
    # 但需确保 handler 绑定了正确的 skill_loader
    # → 在 AsyncAgent.__init__ 中, load_skill handler 已注册到 child_registry?
    # 不对 — AsyncToolRegistry() 自动注册 ASYNC_ALL_TOOLS, 不含 load_skill
    # load_skill 是手动注册的, 只在 main registry 中

    # 解决方案: 手动将 load_skill 也注册到 child_registry
    ...
```

**实际方案**: 在 `AsyncAgent.__init__` 中, 手动将 `load_skill` 注册到 `_child_registry`:

```python
# 注册 load_skill 到 child registry (共享 skill_loader)
self._child_registry.register(
    LOAD_SKILL_TOOL_DEFINITION,
    make_load_skill_handler(self.skill_loader),
)
```

这确保 subagent 的 `tools` 参数中包含 `load_skill` 定义, 且 handler 指向同一个 `SkillLoader` 实例。

### 5.3 同步 Agent (Agent) 改造

同 `AsyncAgent`, 但使用同步 `SkillLoader` (无需 async, 文件 I/O 在初始化时完成):

```python
# loop.py 修改

class Agent:
    def __init__(self, llm_provider_type=None):
        self.llm_provider = create_llm_provider(...)

        # --- Skill loading ---
        self.skill_loader = SkillLoader()
        enhanced_prompt = SYSTEM_PROMPT
        skill_catalog = self.skill_loader.get_descriptions()
        if skill_catalog:
            enhanced_prompt += f"\n\n{skill_catalog}"

        self.messages = [
            {"role": "system", "content": enhanced_prompt},
        ]

        self.tool_registry = ToolRegistry()
        self.progress_tracker = ProgressTracker()
        self.context_compactor = ContextCompactor(...)

        self.tool_registry.register(UPDATE_PLAN_TOOL_DEFINITION, ...)
        self.tool_registry.register(
            LOAD_SKILL_TOOL_DEFINITION,
            make_load_skill_handler(self.skill_loader),
        )
```

### 5.4 与上下文压缩的交互

当前 `ContextCompactor` 的策略是移除中间部分的 tool result 消息。`load_skill` 返回的 skill 内容作为普通 tool result 存在于 `messages[]` 中:

- **会被压缩**: 当消息数超过 `CONTEXT_MAX_MESSAGES` (默认 40) 时, 旧的 skill 内容会被移除
- **这是可接受的**: 如果 LLM 再次需要该 skill 的知识, 可以重新调用 `load_skill` — 这正是 "按需加载" 的设计意图
- **不做特殊保护**: s05 不引入 "pinned messages" 机制。保持 `ContextCompactor` 的简单规则不变

```
场景: Agent 在第 3 轮加载了 code-review skill, 到第 50 轮时该 tool_result 被压缩掉

第 3 轮:
  messages: [..., assistant(load_skill("code-review")), tool(完整skill内容), ...]

第 50 轮 (压缩后):
  messages: [system, first_user, ...compacted..., assistant("(executed tools)"), ...recent...]
  → skill 内容已不在 messages 中

如果第 51 轮还需要 code review 知识:
  → LLM 可以再次调用 load_skill("code-review")
  → tool_result 重新注入完整内容
  → 成本: 一次额外的 LLM round-trip + ~2000 tokens
```

### 5.5 依赖管理

**YAML 解析方案选择**:

| 方案                   | 优点            | 缺点                  |
| -------------------- | ------------- | ------------------- |
| **A: 引入 pyyaml**    | 成熟可靠, 支持完整 YAML | 新增一个依赖 (轻量, ~600KB) |
| B: 手写简易 key-value 解析 | 零依赖           | 不支持列表/嵌套, 易出 bug   |

**推荐方案 A**: 引入 `pyyaml`。它是最广泛使用的 Python YAML 库, 体积极小, 且 `tags: [a, b]` 等列表语法手写解析器容易出错。

在 `pyproject.toml` 中添加:
```toml
dependencies = [
    ...
    "pyyaml>=6.0",
]
```

## 6. 文件变更清单

| 文件                                        | 变更类型 | 说明                                                                 |
| ----------------------------------------- | ---- | ------------------------------------------------------------------ |
| `src/skills/__init__.py`                  | **新建** | skills 包初始化                                                        |
| `src/skills/loader.py`                    | **新建** | `SkillLoader` 类 + `SkillEntry` 数据类 + `SkillParseError` 异常        |
| `src/skills/skill_tool.py`                | **新建** | `LOAD_SKILL_TOOL_DEFINITION` + `make_load_skill_handler()`           |
| `src/agent/async_loop.py`                 | 修改   | `__init__` 中初始化 `SkillLoader`, 增强 system prompt, 注册 `load_skill` 到 main + child registry |
| `src/agent/loop.py`                       | 修改   | `__init__` 中初始化 `SkillLoader`, 增强 system prompt, 注册 `load_skill` |
| `src/config.py`                           | 修改   | 新增 `SKILL_DIRS`, `SKILL_MAX_CONTENT_CHARS`                         |
| `pyproject.toml`                          | 修改   | 添加 `pyyaml>=6.0` 依赖                                               |
| `skills/code-review/SKILL.md`             | **新建** | 示例 skill: 代码审查指南 (同时作为文档和可加载 skill)                           |
| `skills/git-workflow/SKILL.md`            | **新建** | 示例 skill: Git 工作流指南                                              |
| `tests/skills/test_loader.py`             | **新建** | SkillLoader 单元测试                                                    |
| `tests/skills/test_skill_tool.py`         | **新建** | load_skill 工具 handler 测试                                          |
| `tests/agent/test_skill_integration.py`   | **新建** | 集成测试: skill 注入 system prompt + tool 调用                           |

## 7. 测试策略

### 7.1 单元测试

| 测试                                          | 验证点                                                         |
| ------------------------------------------- | ----------------------------------------------------------- |
| `test_parse_valid_skill_file`               | 正确解析含完整 frontmatter 的 SKILL.md                           |
| `test_parse_minimal_skill_file`             | 仅有 name + description 也能成功解析                              |
| `test_parse_missing_name_raises`            | 缺少 name 字段时抛出 `SkillParseError`                           |
| `test_parse_missing_description_raises`     | 缺少 description 字段时抛出 `SkillParseError`                    |
| `test_parse_invalid_name_format`            | name 含大写/空格/特殊字符时拒绝                                     |
| `test_parse_no_frontmatter_raises`          | 文件不以 `---` 开头时拒绝                                          |
| `test_scan_project_skills_dir`              | 正确扫描 `./skills/` 下的多个 skill 目录                            |
| `test_scan_user_skills_dir`                 | 正确扫描 `~/.config/mini-agent/skills/` 下的 skill               |
| `test_project_overrides_user`               | 同名 skill, 项目级覆盖用户级                                         |
| `test_malformed_skill_skipped`              | 格式错误的 skill 被跳过, 不影响其他 skill 的加载                          |
| `test_empty_skills_dir`                     | 空目录不报错, `count == 0`                                       |
| `test_get_descriptions_format`              | 返回格式符合 "• name (vX.X) — description" 模式                  |
| `test_get_descriptions_empty`               | 无 skill 时返回空字符串                                              |
| `test_get_content_existing`                 | 返回已存在 skill 的完整 body                                       |
| `test_get_content_nonexistent`              | 返回 `None`                                                    |
| `test_description_truncation`               | description 超过 100 字符时截断                                    |
| `test_body_content_limit`                   | body 超过 `SKILL_MAX_CONTENT_CHARS` 时截断                        |

### 7.2 工具 Handler 测试

| 测试                                     | 验证点                                              |
| -------------------------------------- | ------------------------------------------------ |
| `test_load_skill_success`              | 正确返回 skill body 内容                               |
| `test_load_skill_not_found`            | 返回错误信息, 包含可用 skill 列表                            |
| `test_load_skill_empty_name`           | 空 name 返回错误信息                                    |
| `test_load_skill_whitespace_name`      | 纯空白 name 返回错误信息                                  |

### 7.3 集成测试

```
测试场景: Agent 启动 + skill 加载 + 调用

前置: 在 ./skills/ 下创建 test-skill/SKILL.md

期望行为:
  1. AsyncAgent.__init__() 完成后:
     - skill_loader.count == 1 (至少包含 test-skill)
     - messages[0]["content"] 包含 "Available skills:" 和 "test-skill"
     - tool_registry 中包含 "load_skill" 工具

  2. LLM 调用 load_skill("test-skill"):
     日志: INFO skills.skill_tool: Skill loaded: test-skill, content_len=XXX
     tool_result: skill body 的完整 markdown 内容

  3. LLM 调用 load_skill("nonexistent"):
     tool_result: "Error: Skill 'nonexistent' not found. Available skills: test-skill"

  4. messages 状态:
     +1 assistant message (含 load_skill tool_call)
     +1 tool message (skill body 内容)
     → skill 内容作为普通 tool_result 存在于 messages 中
```

```
测试场景: Subagent 中加载 skill

前置: 同上

期望行为:
  1. 父 Agent 调用 task, subagent 的 tools 列表中包含 load_skill
  2. Subagent 调用 load_skill("test-skill"), 成功获取 skill body
  3. Subagent 返回的摘要中可包含 skill 知识的应用结果
```

## 8. 风险与缓解

| 风险                                  | 概率 | 影响 | 缓解措施                                                                                          |
| ----------------------------------- | -- | -- | --------------------------------------------------------------------------------------------- |
| **Skill 注入攻击**: 恶意 SKILL.md 包含误导性指令 | 低  | 高   | Skill 内容作为 tool_result 而非 system message 注入; LLM 对 tool_result 的信任度天然低于 system prompt; `name` 格式校验防止路径穿越 |
| **Token 预算溢出**: 加载过多 skill 导致上下文膨胀    | 中  | 中   | `SKILL_MAX_CONTENT_CHARS` 限制单个 skill body 大小; system prompt 中的目录项限制描述长度; LLM 自主决定加载哪些 skill (自我调节) |
| **Name 冲突**: 不同目录下同名的 skill           | 低  | 低   | 明确的优先级规则 (project > user); 冲突时记录 WARNING 日志; 不报错, 不中断启动                                                |
| **pyyaml 依赖引入风险**                    | 低  | 低   | pyyaml 是 Python 生态最成熟的库之一, 维护活跃; 若不愿引入, 可降级为手写简易解析器 (仅支持 key: value 和简单列表)                    |
| **Skill 扫描慢 (大量 skill 目录)**       | 低  | 低   | 启动时一次性扫描, 不影响运行时性能; 日志记录扫描耗时; 未来可加缓存                                                               |
| **LLM 不主动调用 load_skill**             | 中  | 中   | system prompt 中明确指引 "when you need domain knowledge, use load_skill"; 在 skill description 中使用吸引性描述 |
| **Skill 内容被上下文压缩清除后 LLM 忘记重新加载**  | 中  | 低   | 这是 "按需加载" 的固有特征而非 bug; LLM 可通过 system prompt 中的目录重新发现 skill 并再次加载; 在极端情况下用户可提醒 |

## 附录 A: 目录结构总览

```
项目根目录/
├── src/
│   ├── skills/                    ← 新增包
│   │   ├── __init__.py
│   │   ├── loader.py              ← SkillLoader + SkillEntry + SkillParseError
│   │   └── skill_tool.py          ← LOAD_SKILL_TOOL_DEFINITION + make_load_skill_handler
│   ├── agent/
│   │   ├── async_loop.py          ← 修改: 集成 SkillLoader
│   │   ├── loop.py                ← 修改: 集成 SkillLoader
│   │   ├── subagent.py            ← 不变 (load_skill 通过 child_registry 自动可用)
│   │   └── ...
│   └── config.py                  ← 修改: 新增 SKILL_DIRS, SKILL_MAX_CONTENT_CHARS
├── skills/                        ← 项目级 skill 目录 (用户创建)
│   ├── code-review/
│   │   └── SKILL.md
│   └── git-workflow/
│       └── SKILL.md
├── tests/
│   └── skills/                    ← 新增测试目录
│       ├── test_loader.py
│       └── test_skill_tool.py
└── pyproject.toml                 ← 修改: 添加 pyyaml 依赖

用户级 skill 目录 (运行时扫描):
~/.config/mini-agent/skills/
└── my-custom-skill/
    └── SKILL.md
```

## 附录 B: 内置 Skill 示例

以下两个 Skill 随项目发布, 既是开箱即用的能力, 也是 SKILL.md 格式的参考模板。

### B.1 `skills/git-workflow/SKILL.md`

```markdown
---
name: git-workflow
description: Git commit, branch, and PR workflow conventions
version: "1.0"
author: mini-agent
tags: [git, workflow, vcs]
---

# Git Workflow Skill

## Commit Messages

Use Conventional Commits format:

| Type       | Usage                                          |
| ---------- | ---------------------------------------------- |
| `feat`     | A new feature                                  |
| `fix`      | A bug fix                                      |
| `refactor` | Code change that neither fixes a bug nor adds a feature |
| `docs`     | Documentation only changes                     |
| `test`     | Adding or updating tests                       |
| `chore`    | Maintenance tasks (deps, config, CI)           |

Format: `<type>(<scope>): <short summary>`
Example: `feat(auth): add OAuth2 login support`

Rules:
- Summary line ≤ 72 characters
- Use imperative mood: "add feature" not "added feature"
- Do not capitalize first letter of summary
- No period at the end

## Branch Naming

- Feature: `feat/<short-description>`
- Bugfix: `fix/<issue-number>-<short-description>`
- Release: `release/<version>`
- Hotfix: `hotfix/<short-description>`

## Before Commit Checklist

1. Run all tests and ensure they pass
2. Check for untracked files: `git status`
3. Review your diff: `git diff --staged`
4. Remove debug prints and temporary comments
5. Write a clear commit message following the format above
6. End commit message with: `Co-Authored-By: Claude <noreply@anthropic.com>`

## Pull Request Guidelines

1. **Title**: summarize the change in one line
2. **Body**: explain WHY (motivation), WHAT (changes), HOW (testing)
3. Link related issues with `Closes #123`
4. Keep PRs small — under 400 lines of diff when possible
5. Add screenshots for UI changes
```

### B.2 `skills/code-review/SKILL.md`

```markdown
---
name: code-review
description: Systematic code review checklist for correctness, style, and security
version: "1.0"
author: mini-agent
tags: [review, quality, best-practices]
---

# Code Review Skill

## Review Process

Follow this checklist systematically. Don't just look for bugs — review for maintainability too.

## 1. Correctness

- [ ] Logic errors: off-by-one, wrong operator, missing edge cases
- [ ] Null/undefined handling: are all nullable paths covered?
- [ ] Error handling: are exceptions caught at the right level?
- [ ] Concurrency: race conditions, deadlocks, shared mutable state
- [ ] Resource leaks: unclosed files, connections, or processes
- [ ] Boundary conditions: empty lists, zero values, max values

## 2. Design & Maintainability

- [ ] Single responsibility: each function/class does one thing
- [ ] Naming: variables and functions have clear, descriptive names
- [ ] Duplication: no copy-pasted logic that should be extracted
- [ ] Complexity: no function longer than ~50 lines or deeper than 3 nesting levels
- [ ] Comments: explain WHY, not WHAT (the code explains what)
- [ ] API design: interfaces are minimal and intuitive

## 3. Security

- [ ] Input validation: user inputs are sanitized before use
- [ ] Authentication: endpoints check auth before processing
- [ ] Secrets: no hardcoded API keys, passwords, or tokens
- [ ] SQL injection: queries use parameterized statements
- [ ] Path traversal: file operations validate paths stay within bounds
- [ ] Dependencies: no known vulnerable packages

## 4. Testing

- [ ] Test coverage: new logic has corresponding tests
- [ ] Edge cases: boundary values, empty inputs, error paths
- [ ] Test quality: assertions are meaningful (not just "no crash")
- [ ] Test isolation: tests don't depend on execution order or external state

## 5. Performance (when relevant)

- [ ] N+1 queries: database access in loops
- [ ] Unnecessary allocations: large objects created in hot paths
- [ ] Missing indexes: queries on unindexed columns
- [ ] Caching opportunities: expensive computations that could be memoized

## Output Format

Structure your review as:

1. **Summary**: one-paragraph overall assessment
2. **Critical issues**: bugs or security problems that must be fixed
3. **Suggestions**: improvements that would make the code better
4. **Positive notes**: what was done well (always include at least one)
```
