# 设计方案：Skill Loading — 按需加载的领域知识注入

## Context

当前 Agent 的全部领域知识硬编码在 `SYSTEM_PROMPT` 常量中（`loop.py` lines 19-27，~600 字符）。要让 Agent 掌握 git 提交规范、代码审查清单、测试最佳实践等领域知识，唯一的方法是把指令写进系统提示。这导致 token 浪费（10 个 Skill × 2000 token = 20,000 token 固定开销）、注意力稀释（LLM 被无关指令分散）、不可扩展（新增 Skill 必须改代码）。

**核心问题**：领域知识全量预加载到 system prompt，无法按需获取。

**解决方案**：引入 `load_skill` 工具，采用两层注入模型。Layer 1 在 system prompt 中放置 Skill 名称和一行描述（~100 token/skill，始终在场）。Layer 2 在 `tool_result` 中按需返回完整 Skill 内容（~2000 token，仅在 LLM 调用 `load_skill("name")` 时出现）。

**前置依赖**：s03（工具系统 `ToolRegistry` / `AsyncToolRegistry`）、s04（Subagent `_child_registry` + `make_task_handler` 模式）。

## 方案概览

| 层 | 组件 | 职责 | 复杂度 |
|---|---|---|---|
| 1 | `skills/loader.py` — `SkillLoader` + `SkillEntry` | 扫描目录、解析 SKILL.md、提供查询接口 | ~120 行新模块 |
| 2 | `skills/skill_tool.py` — `LOAD_SKILL_TOOL_DEFINITION` + handler | 工具定义 + handler 闭包 | ~40 行新模块 |
| 3 | `async_loop.py` — SkillLoader 初始化 + 注册 | 构建增强 system prompt、注册到 main + child registry | ~15 行改动 |
| 4 | `loop.py` — 同步 Agent 同等集成 | 同上但同步版本 | ~12 行改动 |
| 5 | `config.py` — 2 个新配置项 | `SKILL_DIRS`、`SKILL_MAX_CONTENT_CHARS` | ~3 行改动 |
| 6 | `skills/*.md` — 2 个内置 Skill | git-workflow + code-review 示例 | 2 个 SKILL.md 文件 |

### 设计决策

| # | 决策 | 选择 | 理由 |
|---|---|---|---|
| D1 | YAML frontmatter 解析方案 | 引入 `pyyaml` 依赖 | `tags: [a, b]` 列表语法手写解析器容易出错；pyyaml 是 Python 最成熟的 YAML 库，体积 ~600KB，风险极低 |
| D2 | `load_skill` 工具归属 | CHILD tool（父子均可用） | Skill 是只读知识、无副作用；subagent 执行具体任务时更需要领域知识（如 subagent 做 code review） |
| D3 | child registry 注册方式 | 手动注册到 `_child_registry` | `load_skill` 不在 `ASYNC_ALL_TOOLS` 中（它是手动注册的工具），`_child_registry` 不会自动包含它，必须在 `__init__` 中显式注册 |
| D4 | Skill 目录结构 | 目录名/SKILL.md（非目录名.md） | 与 Claude Code 等主流工具的 Skill 格式一致；目录可放置额外资源文件 |
| D5 | Skill 内容 vs 上下文压缩 | 可被压缩，需要时重新加载 | "按需加载"哲学的自然延伸；不引入 pinned messages 机制，保持 `ContextCompactor` 简洁 |
| D6 | 默认 Skill 目录 | `./skills/`（项目级） + `~/.config/mini-agent/skills/`（用户级） | 项目可携带 Skill；用户可有跨项目的个人 Skill；项目级覆盖用户级 |
| D7 | `SKILL_DIRS` 配置项语义 | 逗号分隔的额外路径，插入到默认目录列表最前面（最高优先级） | 允许用户指定外部 Skill 目录，且外部目录优先于内置目录 |
| D8 | frontmatter 必填字段 | 仅 `name` + `description` | `version`/`author`/`tags` 可选；最小化 Skill 作者的心智负担 |
| D9 | Subagent system prompt 中注入 Skill 目录 | 是，`SUBAGENT_SYSTEM_PROMPT` 动态拼接 Skill 目录 | Subagent 需要知道有哪些 Skill 可用才能调用 `load_skill` |
| D10 | 同步 Agent 支持 | 本迭代同步实现 | SkillLoader 本身是同步的（文件 I/O 在 `__init__` 中完成），同步 Agent 改动量极小，一并完成 |

## 架构总览

```
System Prompt (Layer 1 — always present, cheap)
┌──────────────────────────────────────────────────────────────┐
│ You are a helpful coding assistant...                        │
│                                                              │
│ Available skills:                                            │
│ • code-review (v1.0) — Systematic code review checklist      │
│ • git-workflow (v1.0) — Git commit & branch conventions     │
│ Use load_skill("name") to load full skill content.           │
│                                                              │
│ Cost: ~100 tokens/skill × 2 skills ≈ 200 tokens (fixed)     │
└───────────────────────────┬──────────────────────────────────┘
                            │
                            │ LLM calls load_skill("code-review")
                            ▼
Tool Result (Layer 2 — on demand, only when LLM asks)
┌──────────────────────────────────────────────────────────────┐
│ # Code Review Skill                                          │
│ ## 1. Correctness                                            │
│ - [ ] Logic errors: off-by-one, wrong operator...            │
│ ## 2. Design & Maintainability                               │
│ ...                                                          │
│ (~2000 tokens)                                               │
└──────────────────────────────────────────────────────────────┘
```

**组件交互**：

```
                skills/                      ~/.config/mini-agent/skills/
                ┌──────────┐                 ┌──────────┐
                │ code-    │                 │ my-      │
                │ review/  │                 │ custom/  │
                │ SKILL.md │                 │ SKILL.md │
                └────┬─────┘                 └────┬─────┘
                     │                            │
                     └──────────┬─────────────────┘
                                │ scan + parse (startup)
                                ▼
                        ┌──────────────┐
                        │ SkillLoader  │
                        │ _skills:     │
                        │  {name →     │
                        │   SkillEntry}│
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

**工具集划分**（改造后）：

```python
# 基础工具 — 自动注册（来自 ASYNC_ALL_TOOLS）
AUTO_TOOLS = ["bash", "read_file", "write_file",
              "list_directory", "create_directory", "file_exists"]

# 手动注册 — 父子共享
SHARED_MANUAL_TOOLS = ["update_plan", "load_skill"]

# 手动注册 — 仅父 Agent
PARENT_ONLY_TOOLS = ["task"]

# 父 Agent 工具集 = AUTO + update_plan + load_skill + task  (9 tools)
# Subagent 工具集 = AUTO + load_skill                       (7 tools)
```

## 文件变更详设

### 1. `pyproject.toml` — 添加 pyyaml 依赖

在 `dependencies` 列表中新增一行：

```toml
dependencies = [
    "openai>=1.0",
    "python-dotenv>=1.0",
    "typer>=0.12",
    "rich>=13.0",
    "prompt-toolkit>=3.0",
    "pyyaml>=6.0",          # ★ 新增：Skill frontmatter YAML 解析
]
```

同时在 `[tool.hatch.build.targets.wheel]` 的 `packages` 中新增 `src/skills`：

```toml
[tool.hatch.build.targets.wheel]
packages = [
    "src/cli",
    "src/agent",
    "src/llm",
    "src/logger",
    "src/context_manager",
    "src/session",
    "src/skills",           # ★ 新增
    "src/config.py",
]
```

在 `[tool.ruff.lint.isort]` 的 `known-first-party` 中新增 `skills`：

```toml
known-first-party = ["agent", "llm", "cli", "config", "context_manager", "logger", "session", "skills"]
```

**改动范围**：3 处各增 1 行。

---

### 2. `src/config.py` — 新增 2 个配置项

在 `Settings` 类的 `# ---- Subagent ----` 区域之后、`# ---- Sandbox ----` 之前新增：

```python
# ---- Skill Loading ----
SKILL_DIRS: str = os.getenv("SKILL_DIRS", "")
SKILL_MAX_CONTENT_CHARS: int = int(os.getenv("SKILL_MAX_CONTENT_CHARS", "10000"))
```

| 配置项 | 环境变量 | 默认值 | 说明 |
|---|---|---|---|
| `SKILL_DIRS` | `SKILL_DIRS` | `""` | 逗号分隔的额外 Skill 搜索路径，插入到默认目录列表最前面（最高优先级） |
| `SKILL_MAX_CONTENT_CHARS` | `SKILL_MAX_CONTENT_CHARS` | 10000 | 单个 Skill body 的最大字符数，超出则截断 |

**改动范围**：新增 1 个注释行 + 2 个属性行。

---

### 3. `src/skills/__init__.py` — 包入口

```python
"""Skill loading system — on-demand domain knowledge injection.

Skills are markdown files with YAML frontmatter stored in skill directories.
The :class:`SkillLoader` scans directories at startup and provides a query
interface. The :func:`make_load_skill_handler` factory creates a tool handler
closure bound to a loader instance.
"""

from skills.loader import SkillEntry, SkillLoader, SkillParseError
from skills.skill_tool import LOAD_SKILL_TOOL_DEFINITION, make_load_skill_handler

__all__ = [
    "LOAD_SKILL_TOOL_DEFINITION",
    "SkillEntry",
    "SkillLoader",
    "SkillParseError",
    "make_load_skill_handler",
]
```

---

### 4. `src/skills/loader.py` — 核心加载器

#### 4.1 模块结构

```python
"""Skill directory scanner and SKILL.md parser.

Scans one or more directories for ``SKILL.md`` files, parses their YAML
frontmatter, and provides a query interface for skill descriptions (system
prompt injection) and full content (tool_result injection).
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from logger import get_logger

logger = get_logger(__name__)

# Name validation: lowercase alphanumeric + hyphens, no leading hyphen
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# Maximum description length before truncation
_MAX_DESC_LEN = 100


class SkillParseError(Exception):
    """Raised when a SKILL.md file cannot be parsed."""


@dataclass(frozen=True)
class SkillEntry:
    """A parsed skill entry."""
    name: str
    description: str
    body: str                # Markdown body (without frontmatter)
    source: str              # SKILL.md file path (for logging)
    version: str = "1.0"
    author: str = ""
    tags: list[str] = field(default_factory=list)


class SkillLoader:
    """Scans skill directories, parses SKILL.md files, provides query API."""

    def __init__(self, skill_dirs: list[Path] | None = None):
        self._skills: dict[str, SkillEntry] = {}
        self._scan(skill_dirs if skill_dirs is not None else self._default_dirs())

    # --- Public API ---

    def get_descriptions(self) -> str:
        """Return formatted skill catalog for system prompt injection.

        Returns:
            Multi-line string with one line per skill, or empty string if
            no skills are loaded.
        """
        if not self._skills:
            return ""
        lines = ["Available skills:"]
        for entry in sorted(self._skills.values(), key=lambda e: e.name):
            ver = f" (v{entry.version})" if entry.version else ""
            lines.append(f"• {entry.name}{ver} — {entry.description}")
        lines.append(
            'Use load_skill("name") to load full skill content when needed.'
        )
        return "\n".join(lines)

    def get_content(self, name: str, max_chars: int = 10000) -> str | None:
        """Return skill body content, truncated to max_chars. None if not found."""
        entry = self._skills.get(name)
        if entry is None:
            return None
        body = entry.body
        if len(body) > max_chars:
            body = body[:max_chars] + "\n\n... [truncated]"
        return body

    def list_names(self) -> list[str]:
        """Return sorted list of all loaded skill names."""
        return sorted(self._skills.keys())

    @property
    def count(self) -> int:
        return len(self._skills)

    # --- Internal ---

    @staticmethod
    def _default_dirs() -> list[Path]:
        """Return default skill directories (high priority first)."""
        dirs = []
        # Project-level (highest priority)
        project_skills = Path.cwd() / "skills"
        if project_skills.is_dir():
            dirs.append(project_skills)
        # User-level
        user_skills = Path.home() / ".config" / "mini-agent" / "skills"
        if user_skills.is_dir():
            dirs.append(user_skills)
        return dirs

    def _scan(self, dirs: list[Path]) -> None:
        """Scan all directories, parse SKILL.md files, populate _skills."""
        for skill_dir in dirs:
            if not skill_dir.is_dir():
                logger.debug("Skill dir not found, skipping: %s", skill_dir)
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
                except SkillParseError as e:
                    logger.warning("Skipping malformed skill %s: %s", child.name, e)
                    continue

                if entry.name in self._skills:
                    logger.warning(
                        "Skill name collision: '%s' from %s overridden by %s",
                        entry.name,
                        self._skills[entry.name].source,
                        skill_file,
                    )
                self._skills[entry.name] = entry
                logger.info("Loaded skill: %s from %s", entry.name, skill_file)

        logger.info(
            "Skill scan complete: %d skills loaded from %d directories",
            len(self._skills), len(dirs),
        )

    @staticmethod
    def _parse_skill_file(path: Path) -> SkillEntry:
        """Parse a single SKILL.md file into a SkillEntry."""
        content = path.read_text(encoding="utf-8")
        meta, body = SkillLoader._split_frontmatter(content)

        # Validate required fields
        if "name" not in meta:
            raise SkillParseError("Missing required field: 'name'")
        if "description" not in meta:
            raise SkillParseError("Missing required field: 'description'")

        name = str(meta["name"])
        if not _NAME_RE.match(name):
            raise SkillParseError(f"Invalid name format: '{name}'")

        description = str(meta["description"])
        if len(description) > _MAX_DESC_LEN:
            description = description[:_MAX_DESC_LEN] + "..."

        tags = meta.get("tags", [])
        if not isinstance(tags, list):
            tags = [str(tags)]
        else:
            tags = [str(t) for t in tags]

        return SkillEntry(
            name=name,
            description=description,
            body=body.strip(),
            source=str(path),
            version=str(meta.get("version", "1.0")),
            author=str(meta.get("author", "")),
            tags=tags,
        )

    @staticmethod
    def _split_frontmatter(content: str) -> tuple[dict, str]:
        """Split YAML frontmatter from markdown body.

        Expects content to start with ``---``, followed by YAML, followed
        by another ``---``, followed by the markdown body.
        """
        if not content.startswith("---"):
            raise SkillParseError(
                "File must start with '---' YAML frontmatter delimiter"
            )
        parts = content.split("---", 2)
        if len(parts) < 3:
            raise SkillParseError("Missing closing '---' for YAML frontmatter")

        yaml_str = parts[1]
        body = parts[2]

        meta = yaml.safe_load(yaml_str) or {}
        if not isinstance(meta, dict):
            raise SkillParseError("Frontmatter must be a YAML mapping")

        return meta, body
```

#### 4.2 关键设计点

| 点 | 说明 |
|---|---|
| `frozen=True` dataclass | `SkillEntry` 不可变，保证线程安全（多个 agent 实例可共享同一个 loader） |
| `_default_dirs` 用 `Path.cwd()` | 项目级 Skill 相对于当前工作目录解析，而非 `src/` 目录 |
| 后扫到的覆盖先扫到的 | `_scan` 遍历 dirs 时按顺序处理，后出现的同名 Skill 覆盖先出现的。由于 dirs 列表是高优先级在前，这意味着**低优先级的先写入、高优先级的后覆盖**——最终效果是高优先级目录胜出 |
| `yaml.safe_load` | 使用安全加载，防止 YAML 反序列化攻击 |
| `get_content` 截断 | 接受 `max_chars` 参数，默认值与 `SKILL_MAX_CONTENT_CHARS` 一致；handler 调用时传入配置值 |
| `_NAME_RE` 正则 | `^[a-z0-9][a-z0-9-]*$` — 防止路径注入（如 `../etc/passwd`）、特殊字符 |
| tags 类型安全 | `tags` 可以是 YAML 列表 `[a, b]` 或单个字符串 `"a"`，解析时统一转为 `list[str]` |

---

### 5. `src/skills/skill_tool.py` — 工具定义与 Handler

#### 5.1 模块结构

```python
"""load_skill tool definition and handler factory.

The :func:`make_load_skill_handler` creates a closure bound to a
:class:`SkillLoader` instance, suitable for registration in both
sync and async tool registries.
"""

from skills.loader import SkillLoader
from logger import get_logger

logger = get_logger(__name__)


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
                    "description": (
                        "The skill name to load (e.g., 'code-review'). "
                        "See system prompt for available skills."
                    ),
                }
            },
            "required": ["name"],
        },
    },
}


def make_load_skill_handler(
    skill_loader: SkillLoader,
    max_chars: int = 10000,
):
    """Create a load_skill handler closure bound to a SkillLoader instance.

    Args:
        skill_loader: The SkillLoader instance to query.
        max_chars: Maximum character count for skill body content.

    Returns:
        A callable ``(args: dict) -> str`` suitable for tool registry registration.
    """

    def load_skill(args: dict) -> str:
        name = args.get("name", "").strip()
        if not name:
            available = ", ".join(skill_loader.list_names()) or "(none)"
            return f"Error: 'name' is required. Available skills: {available}"

        content = skill_loader.get_content(name, max_chars=max_chars)
        if content is None:
            available = ", ".join(skill_loader.list_names()) or "(none)"
            logger.warning("Skill not found: %s (available: %s)", name, available)
            return f"Error: Skill '{name}' not found. Available skills: {available}"

        logger.info("Skill loaded: %s, content_len=%d", name, len(content))
        return content

    return load_skill
```

#### 5.2 关键设计点

| 点 | 说明 |
|---|---|
| 同步 handler | `load_skill` handler 是普通函数（非 async），因为 Skill 数据已在内存中，无 I/O 等待。`AsyncToolRegistry.execute()` 会检测返回值是否为 coroutine，同步返回值直接使用 |
| 闭包模式 | 与 `make_task_handler(agent)` 同源的设计——闭包绑定 loader 实例，避免全局变量 |
| `max_chars` 参数化 | handler 工厂接受 `max_chars`，由 Agent `__init__` 传入 `settings.SKILL_MAX_CONTENT_CHARS` |
| 错误信息含可用列表 | 名称错误或为空时返回可用 Skill 列表，帮助 LLM 自纠错 |

---

### 6. `src/agent/async_loop.py` — AsyncAgent 改造

#### 6.1 新增 imports

在现有 imports 区域（line 10 `from agent.subagent import ...` 之后）新增：

```python
from skills import LOAD_SKILL_TOOL_DEFINITION, SkillLoader, make_load_skill_handler
```

#### 6.2 `__init__` 改造

在现有 `__init__` 方法中插入 Skill 加载逻辑。**改造后的完整 `__init__`**：

```python
def __init__(
    self,
    llm_provider_type: LLMProviderType = None,
    display: DisplayHandler | None = None,
):
    self.llm_provider = create_llm_provider(
        llm_provider_type or LLMProviderType(settings.LLM_PROVIDER)
    )

    # --- Skill loading (★ 新增) ---
    self.skill_loader = self._build_skill_loader()

    # Build enhanced system prompt with skill catalog
    enhanced_prompt = SYSTEM_PROMPT
    skill_catalog = self.skill_loader.get_descriptions()
    if skill_catalog:
        enhanced_prompt += f"\n\n{skill_catalog}"

    self.messages = [
        {"role": "system", "content": enhanced_prompt},
    ]

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

    # --- Skill tool (★ 新增) ---
    skill_handler = make_load_skill_handler(
        self.skill_loader, max_chars=settings.SKILL_MAX_CONTENT_CHARS
    )
    self.tool_registry.register(LOAD_SKILL_TOOL_DEFINITION, skill_handler)

    # --- Subagent support ---
    self._child_registry = AsyncToolRegistry(exclude=["task", "update_plan"])
    # Register load_skill to child registry (subagents can load skills too)
    self._child_registry.register(LOAD_SKILL_TOOL_DEFINITION, skill_handler)

    # Register task tool to main registry (parent Agent only)
    self.tool_registry.register(
        TASK_TOOL_DEFINITION,
        make_task_handler(self),
    )
```

#### 6.3 新增 `_build_skill_loader` 辅助方法

```python
@staticmethod
def _build_skill_loader() -> SkillLoader:
    """Build SkillLoader from settings.SKILL_DIRS + default directories."""
    from pathlib import Path

    dirs: list[Path] = []
    # Extra dirs from config (highest priority, prepended)
    if settings.SKILL_DIRS:
        for d in settings.SKILL_DIRS.split(","):
            d = d.strip()
            if d:
                dirs.append(Path(d))
    # Default dirs (project-level + user-level)
    project_skills = Path.cwd() / "skills"
    if project_skills.is_dir():
        dirs.append(project_skills)
    user_skills = Path.home() / ".config" / "mini-agent" / "skills"
    if user_skills.is_dir():
        dirs.append(user_skills)

    return SkillLoader(skill_dirs=dirs)
```

#### 6.4 改造前后对比

| 区域 | 改造前 (s04) | 改造后 (s05) |
|---|---|---|
| imports | 无 skills 相关 | `from skills import LOAD_SKILL_TOOL_DEFINITION, SkillLoader, make_load_skill_handler` |
| messages 初始化 | `content: SYSTEM_PROMPT` | `content: SYSTEM_PROMPT + skill_catalog`（有 Skill 时追加） |
| 主 registry 工具数 | 8（6 auto + update_plan + task） | 9（+load_skill） |
| child registry 工具数 | 6（auto only） | 7（+load_skill 手动注册） |
| 新增实例属性 | 无 | `self.skill_loader: SkillLoader` |
| 新增方法 | 无 | `self._build_skill_loader()` 静态方法 |

#### 6.5 `chat()` / `_call_llm()` / `_handle_tool_call()` — 无改动

与 s04 subagent 相同的设计：当 LLM 返回 `load_skill` 工具调用时，`_handle_tool_call` 通过 `self.tool_registry.execute("load_skill", args)` 分发到 handler 闭包。结果作为普通 tool result 追加到 messages 中。整个 agent 循环逻辑无需任何修改。

---

### 7. `src/agent/loop.py` — 同步 Agent 改造

#### 7.1 新增 imports

在现有 imports 区域新增：

```python
from skills import LOAD_SKILL_TOOL_DEFINITION, SkillLoader, make_load_skill_handler
```

#### 7.2 `__init__` 改造

**改造后的完整 `__init__`**：

```python
def __init__(
    self,
    llm_provider_type: LLMProviderType = None,
):
    self.llm_provider = create_llm_provider(
        llm_provider_type or LLMProviderType(settings.LLM_PROVIDER)
    )

    # --- Skill loading (★ 新增) ---
    self.skill_loader = AsyncAgent._build_skill_loader()

    enhanced_prompt = SYSTEM_PROMPT
    skill_catalog = self.skill_loader.get_descriptions()
    if skill_catalog:
        enhanced_prompt += f"\n\n{skill_catalog}"

    self.messages = [
        {"role": "system", "content": enhanced_prompt},
    ]

    self.tool_registry = ToolRegistry()
    self.progress_tracker = ProgressTracker()
    self.context_compactor = ContextCompactor(
        max_messages=settings.CONTEXT_MAX_MESSAGES,
        keep_recent=settings.CONTEXT_KEEP_RECENT,
    )

    self.tool_registry.register(
        UPDATE_PLAN_TOOL_DEFINITION,
        lambda args: run_update_plan(args, self.progress_tracker),
    )

    # --- Skill tool (★ 新增) ---
    self.tool_registry.register(
        LOAD_SKILL_TOOL_DEFINITION,
        make_load_skill_handler(
            self.skill_loader, max_chars=settings.SKILL_MAX_CONTENT_CHARS
        ),
    )
```

**注意**：同步 Agent 复用 `AsyncAgent._build_skill_loader()` 静态方法，避免重复代码。这需要 `loop.py` 从 `async_loop.py` 导入（或者将 `_build_skill_loader` 提取为 `skills` 包内的工具函数）。

**替代方案**：将 `_build_skill_loader` 提取到 `skills/loader.py` 中作为模块级函数：

```python
# skills/loader.py 中新增
def build_skill_loader() -> SkillLoader:
    """Build SkillLoader from settings.SKILL_DIRS + default directories."""
    dirs: list[Path] = []
    if settings.SKILL_DIRS:
        for d in settings.SKILL_DIRS.split(","):
            d = d.strip()
            if d:
                dirs.append(Path(d))
    project_skills = Path.cwd() / "skills"
    if project_skills.is_dir():
        dirs.append(project_skills)
    user_skills = Path.home() / ".config" / "mini-agent" / "skills"
    if user_skills.is_dir():
        dirs.append(user_skills)
    return SkillLoader(skill_dirs=dirs)
```

**推荐采用此替代方案**。两个 agent 都调用 `build_skill_loader()`，避免 `loop.py` 反向导入 `async_loop.py`。

最终 `async_loop.py` 中的 `_build_skill_loader` 改为：

```python
# async_loop.py 中
from skills.loader import build_skill_loader
# ...
self.skill_loader = build_skill_loader()
```

`loop.py` 中：

```python
# loop.py 中
from skills.loader import build_skill_loader
# ...
self.skill_loader = build_skill_loader()
```

---

### 8. `src/agent/subagent.py` — Subagent system prompt 改造

Subagent 需要知道有哪些 Skill 可用，才能调用 `load_skill`。当前 `SUBAGENT_SYSTEM_PROMPT` 是硬编码常量（`subagent.py` lines 18-28），无法动态注入 Skill 目录。

**方案**：在 `make_task_handler` 中动态拼接 Skill 目录到 subagent system prompt。

```python
def make_task_handler(agent):
    """Create task handler closure bound to parent agent's provider and config."""

    llm_provider = agent.llm_provider
    child_registry = agent._child_registry

    # Build subagent system prompt with skill catalog (★ 新增)
    subagent_prompt = SUBAGENT_SYSTEM_PROMPT
    if hasattr(agent, "skill_loader") and agent.skill_loader.count > 0:
        subagent_prompt += f"\n\n{agent.skill_loader.get_descriptions()}"

    async def run_task(args: dict) -> str:
        prompt = args.get("prompt", "")
        if not prompt:
            return "Error: 'prompt' is required."

        logger.info("Task tool invoked: prompt_len=%d", len(prompt))

        result = await run_subagent(
            prompt=prompt,
            llm_provider=llm_provider,
            child_tools=child_registry.definitions,
            child_registry=child_registry,
            system_prompt=subagent_prompt,   # ← 使用增强版 prompt
            max_iterations=settings.SUBAGENT_MAX_ITERATIONS,
            max_output_chars=settings.SUBAGENT_MAX_OUTPUT,
            max_tool_output=settings.SUBAGENT_MAX_TOOL_OUTPUT,
        )
        return result

    return run_task
```

**改动范围**：
- `make_task_handler` 中新增 3 行（构建 `subagent_prompt`）
- `run_subagent` 调用中 `system_prompt=subagent_prompt`（替换原来的 `SUBAGENT_SYSTEM_PROMPT`）
- `run_subagent` 函数本身不变

**向后兼容**：`hasattr(agent, "skill_loader")` 防御性检查——如果 agent 没有 skill_loader（如测试中的 mock agent），则不注入 Skill 目录。

---

### 9. `src/agent/__init__.py` — 导出新增

```python
from agent.async_loop import AsyncAgent
from agent.display import DisplayHandler, SilentDisplayHandler
from agent.loop import Agent
from agent.subagent import run_subagent
from context_manager.tracker import ProgressTracker
from skills import SkillLoader                              # ★ 新增

__all__ = [
    "Agent",
    "AsyncAgent",
    "DisplayHandler",
    "ProgressTracker",
    "SilentDisplayHandler",
    "SkillLoader",                                          # ★ 新增
    "run_subagent",
]
```

---

### 10. `skills/code-review/SKILL.md` — 内置 Skill

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

---

### 11. `skills/git-workflow/SKILL.md` — 内置 Skill

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

---

### 12. `tests/skills/__init__.py` — 测试包入口

空文件。

---

### 13. `tests/skills/test_loader.py` — SkillLoader 单元测试

```python
"""Unit tests for SkillLoader and SkillEntry."""

import tempfile
import unittest
from pathlib import Path

from skills.loader import SkillEntry, SkillLoader, SkillParseError


def _write_skill(parent: Path, dir_name: str, content: str) -> Path:
    """Helper: create a skill directory with SKILL.md content."""
    skill_dir = parent / dir_name
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    return skill_dir


VALID_SKILL = """\
---
name: test-skill
description: A test skill for unit testing
version: "2.0"
author: tester
tags: [test, unit]
---

# Test Skill

This is the body content.
"""

MINIMAL_SKILL = """\
---
name: minimal
description: Minimal skill
---

Minimal body.
"""


class TestSkillLoaderParsing(unittest.TestCase):
    """Test SKILL.md parsing logic."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.skills_path = Path(self.tmpdir)

    def test_parse_valid_skill(self):
        _write_skill(self.skills_path, "test-skill", VALID_SKILL)
        loader = SkillLoader(skill_dirs=[self.skills_path])
        self.assertEqual(loader.count, 1)
        entry = loader._skills["test-skill"]
        self.assertEqual(entry.name, "test-skill")
        self.assertEqual(entry.description, "A test skill for unit testing")
        self.assertEqual(entry.version, "2.0")
        self.assertEqual(entry.author, "tester")
        self.assertEqual(entry.tags, ["test", "unit"])
        self.assertIn("This is the body content.", entry.body)

    def test_parse_minimal_skill(self):
        _write_skill(self.skills_path, "minimal", MINIMAL_SKILL)
        loader = SkillLoader(skill_dirs=[self.skills_path])
        self.assertEqual(loader.count, 1)
        entry = loader._skills["minimal"]
        self.assertEqual(entry.name, "minimal")
        self.assertEqual(entry.version, "1.0")  # default

    def test_parse_missing_name_raises(self):
        content = "---\ndescription: no name\n---\nbody"
        _write_skill(self.skills_path, "bad", content)
        loader = SkillLoader(skill_dirs=[self.skills_path])
        self.assertEqual(loader.count, 0)  # skipped, not raised

    def test_parse_missing_description_raises(self):
        content = "---\nname: no-desc\n---\nbody"
        _write_skill(self.skills_path, "bad", content)
        loader = SkillLoader(skill_dirs=[self.skills_path])
        self.assertEqual(loader.count, 0)

    def test_parse_invalid_name_format(self):
        content = "---\nname: Invalid Name!\ndescription: bad\n---\nbody"
        _write_skill(self.skills_path, "bad", content)
        loader = SkillLoader(skill_dirs=[self.skills_path])
        self.assertEqual(loader.count, 0)

    def test_parse_no_frontmatter(self):
        content = "# Just a markdown file\nNo frontmatter here."
        _write_skill(self.skills_path, "bad", content)
        loader = SkillLoader(skill_dirs=[self.skills_path])
        self.assertEqual(loader.count, 0)

    def test_description_truncation(self):
        long_desc = "A" * 200
        content = f"---\nname: long\ndescription: {long_desc}\n---\nbody"
        _write_skill(self.skills_path, "long", content)
        loader = SkillLoader(skill_dirs=[self.skills_path])
        entry = loader._skills["long"]
        self.assertLessEqual(len(entry.description), 103)  # 100 + "..."
        self.assertTrue(entry.description.endswith("..."))


class TestSkillLoaderScanning(unittest.TestCase):
    """Test directory scanning and name collision behavior."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.dir_a = Path(self.tmpdir) / "a"
        self.dir_b = Path(self.tmpdir) / "b"
        self.dir_a.mkdir()
        self.dir_b.mkdir()

    def test_scan_multiple_dirs(self):
        _write_skill(self.dir_a, "skill-a", VALID_SKILL.replace("test-skill", "skill-a"))
        _write_skill(self.dir_b, "skill-b", MINIMAL_SKILL.replace("minimal", "skill-b"))
        loader = SkillLoader(skill_dirs=[self.dir_a, self.dir_b])
        self.assertEqual(loader.count, 2)
        self.assertIn("skill-a", loader.list_names())
        self.assertIn("skill-b", loader.list_names())

    def test_name_collision_later_wins(self):
        """Later dirs override earlier dirs for the same skill name."""
        content_a = "---\nname: dup\ndescription: from A\n---\nA body"
        content_b = "---\nname: dup\ndescription: from B\n---\nB body"
        _write_skill(self.dir_a, "dup", content_a)
        _write_skill(self.dir_b, "dup", content_b)
        # dir_a first (lower priority), dir_b second (higher priority overrides)
        loader = SkillLoader(skill_dirs=[self.dir_a, self.dir_b])
        self.assertEqual(loader.count, 1)
        self.assertEqual(loader._skills["dup"].description, "from B")

    def test_empty_dir(self):
        loader = SkillLoader(skill_dirs=[self.dir_a])
        self.assertEqual(loader.count, 0)
        self.assertEqual(loader.get_descriptions(), "")

    def test_missing_dir(self):
        loader = SkillLoader(skill_dirs=[Path("/nonexistent/path")])
        self.assertEqual(loader.count, 0)

    def test_dir_without_skill_md(self):
        (self.dir_a / "not-a-skill").mkdir()
        loader = SkillLoader(skill_dirs=[self.dir_a])
        self.assertEqual(loader.count, 0)


class TestSkillLoaderAPI(unittest.TestCase):
    """Test public query API."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.skills_path = Path(self.tmpdir)
        _write_skill(self.skills_path, "test-skill", VALID_SKILL)
        self.loader = SkillLoader(skill_dirs=[self.skills_path])

    def test_get_descriptions_format(self):
        desc = self.loader.get_descriptions()
        self.assertIn("Available skills:", desc)
        self.assertIn("• test-skill (v2.0) — A test skill for unit testing", desc)
        self.assertIn("load_skill", desc)

    def test_get_descriptions_empty(self):
        empty_loader = SkillLoader(skill_dirs=[])
        self.assertEqual(empty_loader.get_descriptions(), "")

    def test_get_content_existing(self):
        content = self.loader.get_content("test-skill")
        self.assertIsNotNone(content)
        self.assertIn("This is the body content.", content)

    def test_get_content_nonexistent(self):
        self.assertIsNone(self.loader.get_content("nonexistent"))

    def test_get_content_truncation(self):
        content = self.loader.get_content("test-skill", max_chars=10)
        self.assertIsNotNone(content)
        self.assertTrue(content.endswith("... [truncated]"))

    def test_list_names(self):
        self.assertEqual(self.loader.list_names(), ["test-skill"])


if __name__ == "__main__":
    unittest.main()
```

---

### 14. `tests/skills/test_skill_tool.py` — Handler 单元测试

```python
"""Unit tests for load_skill tool handler."""

import tempfile
import unittest
from pathlib import Path

from skills.loader import SkillLoader
from skills.skill_tool import make_load_skill_handler


VALID_SKILL = """\
---
name: my-skill
description: Test skill
---

# My Skill Body

Detailed content here.
"""


class TestLoadSkillHandler(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        skills_path = Path(self.tmpdir)
        skill_dir = skills_path / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(VALID_SKILL, encoding="utf-8")
        self.loader = SkillLoader(skill_dirs=[skills_path])
        self.handler = make_load_skill_handler(self.loader, max_chars=10000)

    def test_load_skill_success(self):
        result = self.handler({"name": "my-skill"})
        self.assertIn("My Skill Body", result)
        self.assertIn("Detailed content here.", result)

    def test_load_skill_not_found(self):
        result = self.handler({"name": "nonexistent"})
        self.assertIn("Error", result)
        self.assertIn("nonexistent", result)
        self.assertIn("my-skill", result)  # lists available skills

    def test_load_skill_empty_name(self):
        result = self.handler({"name": ""})
        self.assertIn("Error", result)
        self.assertIn("required", result)

    def test_load_skill_whitespace_name(self):
        result = self.handler({"name": "   "})
        self.assertIn("Error", result)

    def test_load_skill_missing_name_key(self):
        result = self.handler({})
        self.assertIn("Error", result)

    def test_load_skill_truncation(self):
        handler = make_load_skill_handler(self.loader, max_chars=5)
        result = handler({"name": "my-skill"})
        self.assertTrue(result.endswith("... [truncated]"))


if __name__ == "__main__":
    unittest.main()
```

---

### 15. `tests/agent/test_skill_integration.py` — 集成测试

```python
"""Integration tests: SkillLoader + AsyncAgent system prompt + tool registry."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from skills.loader import SkillLoader, build_skill_loader


VALID_SKILL = """\
---
name: integration-test
description: Integration test skill
---

# Integration Test Body
"""


class TestSkillIntegration(unittest.TestCase):
    """Test that skills integrate correctly with agent components."""

    def test_build_skill_loader_with_env(self):
        """build_skill_loader respects SKILL_DIRS env var."""
        tmpdir = tempfile.mkdtemp()
        skill_dir = Path(tmpdir) / "env-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(VALID_SKILL, encoding="utf-8")

        with patch.dict("os.environ", {"SKILL_DIRS": tmpdir}):
            # Reload settings to pick up patched env
            from config import Settings
            patched_settings = Settings()
            with patch("skills.loader.settings", patched_settings):
                loader = build_skill_loader()
        self.assertGreaterEqual(loader.count, 1)
        self.assertIn("integration-test", loader.list_names())

    def test_skill_in_tool_registry(self):
        """load_skill tool is registered in AsyncToolRegistry."""
        import asyncio
        asyncio.run(self._test_skill_in_tool_registry())

    async def _test_skill_in_tool_registry(self):
        tmpdir = tempfile.mkdtemp()
        skill_dir = Path(tmpdir) / "reg-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(VALID_SKILL, encoding="utf-8")

        loader = SkillLoader(skill_dirs=[Path(tmpdir)])
        from agent.async_tool_registry import AsyncToolRegistry
        from skills.skill_tool import LOAD_SKILL_TOOL_DEFINITION, make_load_skill_handler

        registry = AsyncToolRegistry()
        handler = make_load_skill_handler(loader)
        registry.register(LOAD_SKILL_TOOL_DEFINITION, handler)

        self.assertIn("load_skill", registry.get_tool_names())
        result = await registry.execute("load_skill", {"name": "integration-test"})
        self.assertIn("Integration Test Body", result)

    def test_child_registry_has_load_skill(self):
        """Child registry (for subagents) includes load_skill."""
        import asyncio
        asyncio.run(self._test_child_registry())

    async def _test_child_registry(self):
        tmpdir = tempfile.mkdtemp()
        skill_dir = Path(tmpdir) / "child-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(VALID_SKILL, encoding="utf-8")

        loader = SkillLoader(skill_dirs=[Path(tmpdir)])
        from agent.async_tool_registry import AsyncToolRegistry
        from skills.skill_tool import LOAD_SKILL_TOOL_DEFINITION, make_load_skill_handler

        child_registry = AsyncToolRegistry(exclude=["task", "update_plan"])
        child_registry.register(
            LOAD_SKILL_TOOL_DEFINITION,
            make_load_skill_handler(loader),
        )

        self.assertIn("load_skill", child_registry.get_tool_names())
        self.assertNotIn("task", child_registry.get_tool_names())
        self.assertNotIn("update_plan", child_registry.get_tool_names())


if __name__ == "__main__":
    unittest.main()
```

---

## 测试策略

### 单元测试矩阵

| 文件 | 测试数 | 覆盖要点 |
|---|---|---|
| `test_loader.py` | 17 | frontmatter 解析（有效/最小/缺字段/格式错误）、目录扫描（多目录/空/缺失）、名称冲突、描述截断、内容截断、API 查询 |
| `test_skill_tool.py` | 6 | handler 正常返回、未找到、空名称、纯空白、缺失 key、截断 |
| `test_skill_integration.py` | 3 | `build_skill_loader` + 环境变量、工具注册 + 执行、child registry 含 load_skill |

### 测试实现模式

遵循现有项目模式：`unittest.TestCase` + `asyncio.run()` + `unittest.mock` + `tempfile.mkdtemp()` 隔离文件系统。

## 数据流：Skill Loading 完整生命周期

```
Agent 启动
  │
  ├─ build_skill_loader()
  │   ├─ SKILL_DIRS 环境变量 → 额外路径列表
  │   ├─ ./skills/ → 项目级目录
  │   ├─ ~/.config/mini-agent/skills/ → 用户级目录
  │   │
  │   ├─ 扫描每个目录:
  │   │   ├─ skills/code-review/SKILL.md → parse → SkillEntry("code-review")
  │   │   ├─ skills/git-workflow/SKILL.md → parse → SkillEntry("git-workflow")
  │   │   └─ (无效文件 → logger.warning, 跳过)
  │   │
  │   └─ return SkillLoader(count=2)
  │
  ├─ skill_loader.get_descriptions()
  │   → "Available skills:\n• code-review (v1.0) — ...\n• git-workflow (v1.0) — ...\nUse load_skill..."
  │
  ├─ enhanced_prompt = SYSTEM_PROMPT + "\n\n" + skill_catalog
  │
  ├─ messages = [{"role": "system", "content": enhanced_prompt}]
  │
  └─ tool_registry.register(load_skill, handler)
     _child_registry.register(load_skill, handler)

运行时 (第 N 轮对话)
  │
  ├─ 用户: "帮我做一次代码审查"
  │
  ├─ _call_llm() → LLM 返回:
  │   assistant: "我来加载代码审查技能..."
  │   tool_calls: [{name: "load_skill", args: {name: "code-review"}}]
  │
  ├─ _handle_tool_call(load_skill_call)
  │   ├─ tool_registry.execute("load_skill", {name: "code-review"})
  │   │   └─ handler(args)
  │   │       ├─ skill_loader.get_content("code-review", max_chars=10000)
  │   │       │   └─ return "# Code Review Skill\n## 1. Correctness\n..."
  │   │       ├─ logger.info("Skill loaded: code-review, content_len=1842")
  │   │       └─ return skill body content
  │   │
  │   └─ return tool_result: {role: "tool", content: "# Code Review Skill..."}
  │
  ├─ messages.append(tool_result)
  │
  ├─ _call_llm() → LLM 基于 Skill 内容执行代码审查
  │   assistant: "根据代码审查清单，我来检查以下几点..."
  │   tool_calls: [{name: "read_file", ...}, ...]
  │
  └─ (继续正常的工具调用循环)

上下文压缩 (第 50 轮)
  │
  ├─ ContextCompactor.compact(messages)
  │   ├─ messages[0] (system + skill catalog) → 保留 ← 始终保留
  │   ├─ messages[N] (tool: code-review 内容) → 被移除 ← 在中间部分
  │   └─ messages[-12:] (recent) → 保留
  │
  └─ 如果第 51 轮还需要 code-review 知识:
      → LLM 看到 system prompt 中的 skill 目录
      → 再次调用 load_skill("code-review")
      → 内容重新注入 messages
```

## 边界情况处理

| 场景 | 处理方式 |
|---|---|
| `skills/` 目录不存在 | `Path.is_dir()` 返回 False，`_scan` 跳过该目录，`SkillLoader.count == 0`，system prompt 不追加 Skill 目录 |
| SKILL.md 为空文件 | `content.startswith("---")` 为 True（空字符串不以 `---` 开头），抛 `SkillParseError`，跳过并记录 warning |
| SKILL.md frontmatter 中 `name` 含特殊字符 | `_NAME_RE` 正则拒绝，抛 `SkillParseError`，跳过 |
| 两个目录有同名 Skill | 后扫描的目录覆盖先扫描的（`_scan` 中 `self._skills[name] = entry`），记录 warning 日志 |
| `SKILL_DIRS` 指向不存在的路径 | `_scan` 中 `skill_dir.is_dir()` 为 False，静默跳过，不影响其他目录 |
| `SKILL_DIRS` 为空字符串 | `settings.SKILL_DIRS.split(",")` 得到 `[""]`，`d.strip()` 为空，被 `if d:` 过滤 |
| Skill body 内容超长 | `get_content(max_chars=SKILL_MAX_CONTENT_CHARS)` 截断并追加 `"\n\n... [truncated]"` |
| LLM 调用 `load_skill` 时传入不存在的名称 | handler 返回 `"Error: Skill 'xxx' not found. Available skills: ..."` 帮助 LLM 自纠错 |
| LLM 从未调用 `load_skill` | 正常行为——Skill 目录在 system prompt 中仅占 ~200 token，不加载不产生额外开销 |
| Skill 内容被 `ContextCompactor` 压缩后 LLM 需要重新加载 | 预期行为——LLM 看到 system prompt 中的目录，再次调用 `load_skill`，成本为 1 轮 LLM 调用 + ~2000 token |
| Subagent 中使用 `load_skill` | child registry 已注册 `load_skill`，subagent system prompt 已注入 Skill 目录，正常工作 |
| `pyyaml` 未安装 | `import yaml` 失败，`SkillLoader` 无法初始化。这是硬依赖，安装时必须包含 `pyyaml` |
| YAML frontmatter 中含复杂嵌套结构 | `yaml.safe_load` 完整支持 YAML 规范，可正确处理。但 Skill 作者应避免复杂结构——frontmatter 仅需简单键值对 |

## 风险与缓解

| 风险 | 概率 | 影响 | 缓解措施 |
|---|---|---|---|
| Skill 注入攻击：恶意 SKILL.md 包含误导性指令 | 低 | 高 | Skill 内容作为 `tool_result` 注入（非 system message），LLM 对 tool_result 信任度天然低于 system prompt；`name` 正则防止路径穿越；Skill 仅从本地文件系统读取 |
| Token 预算溢出：加载过多 Skill | 中 | 中 | `SKILL_MAX_CONTENT_CHARS=10000` 限制单个 Skill body；system prompt 中描述限制 100 字符；LLM 自主决定加载哪些（自我调节） |
| `pyyaml` 依赖引入 | 低 | 低 | pyyaml 是 Python 最成熟的库之一（2006 年至今），维护活跃；若不愿引入可降级为手写解析器 |
| LLM 不主动调用 `load_skill` | 中 | 中 | system prompt 明确指引 "Use load_skill to load full skill content when needed"；Skill description 使用吸引性描述 |
| `Path.cwd()` 在不同运行环境下不一致 | 中 | 低 | CLI 通过 `PYTHONPATH=src` 在项目根目录运行，`Path.cwd()` 指向项目根；若在其他目录运行，用户可通过 `SKILL_DIRS` 环境变量显式指定 |
| 大量 Skill 导致启动变慢 | 低 | 低 | 扫描是一次性文件 I/O，100 个 Skill 的扫描耗时 < 10ms；日志记录扫描耗时 |
| Skill 内容被上下文压缩后 LLM 忘记重新加载 | 中 | 低 | "按需加载"的固有特征；system prompt 中的目录始终在场，提醒 LLM Skill 的存在 |

## 实现顺序

按依赖关系分 4 个阶段：

**阶段 1：基础设施（零风险）**
1. `pyproject.toml` — 添加 `pyyaml>=6.0` 依赖 + `src/skills` 包 + isort known-first-party
2. `src/config.py` — 新增 `SKILL_DIRS`、`SKILL_MAX_CONTENT_CHARS`

**阶段 2：核心模块**
3. `src/skills/__init__.py` — 包入口 + 导出
4. `src/skills/loader.py` — `SkillParseError` + `SkillEntry` + `SkillLoader` + `build_skill_loader()`
5. `src/skills/skill_tool.py` — `LOAD_SKILL_TOOL_DEFINITION` + `make_load_skill_handler()`

**阶段 3：集成**
6. `src/agent/async_loop.py` — imports + `__init__` 改造 + Skill 目录注入 system prompt + 注册到 main + child registry
7. `src/agent/loop.py` — imports + `__init__` 改造
8. `src/agent/subagent.py` — `make_task_handler` 中动态拼接 Skill 目录到 subagent prompt
9. `src/agent/__init__.py` — 导出 `SkillLoader`

**阶段 4：Skill 内容 + 测试**
10. `skills/code-review/SKILL.md` — 内置 Skill
11. `skills/git-workflow/SKILL.md` — 内置 Skill
12. `tests/skills/__init__.py` — 测试包
13. `tests/skills/test_loader.py` — SkillLoader 单元测试
14. `tests/skills/test_skill_tool.py` — handler 单元测试
15. `tests/agent/test_skill_integration.py` — 集成测试

## 验证方式

1. **单元测试**：
   ```bash
   PYTHONPATH=src .venv/bin/python -m unittest tests.skills.test_loader -v
   PYTHONPATH=src .venv/bin/python -m unittest tests.skills.test_skill_tool -v
   PYTHONPATH=src .venv/bin/python -m unittest tests.agent.test_skill_integration -v
   ```

2. **全量回归**：
   ```bash
   PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
   ```
   确保 pyyaml 引入和 Skill 注册不破坏现有测试。

3. **集成测试（手动）**：启动 CLI，验证：
   ```bash
   PYTHONPATH=src .venv/bin/python -m cli.main chat
   ```
   - 输入 `"What skills are available?"` → Agent 应列出 code-review 和 git-workflow
   - 输入 `"Load the code-review skill"` → Agent 应调用 `load_skill("code-review")` 并展示内容
   - 输入 `"Do a code review of src/config.py"` → Agent 应先加载 code-review skill 再审查

4. **日志验证**：
   ```bash
   grep -E "Skill|skill" logs/mini_agent.log
   ```
   检查：
   - 启动时 `Loaded skill: code-review from ...` 和 `Loaded skill: git-workflow from ...`
   - 加载时 `Skill loaded: code-review, content_len=XXXX`

5. **Pre-commit 检查**：
   ```bash
   .venv/bin/pre-commit run --all-files
   ```
   确保 ruff lint（含 `TID252` ban-relative-imports）和 format 通过。

## 文件变更清单总览

| 文件 | 类型 | 行数估算 | 说明 |
|---|---|---|---|
| `pyproject.toml` | 修改 | +3 | pyyaml 依赖 + skills 包 + isort |
| `src/config.py` | 修改 | +3 | `SKILL_DIRS` + `SKILL_MAX_CONTENT_CHARS` |
| `src/skills/__init__.py` | **新建** | ~15 | 包入口 |
| `src/skills/loader.py` | **新建** | ~170 | SkillParseError + SkillEntry + SkillLoader + build_skill_loader |
| `src/skills/skill_tool.py` | **新建** | ~55 | LOAD_SKILL_TOOL_DEFINITION + make_load_skill_handler |
| `src/agent/async_loop.py` | 修改 | +18 | imports + __init__ 改造 |
| `src/agent/loop.py` | 修改 | +15 | imports + __init__ 改造 |
| `src/agent/subagent.py` | 修改 | +4 | make_task_handler 动态 prompt |
| `src/agent/__init__.py` | 修改 | +2 | 导出 SkillLoader |
| `skills/code-review/SKILL.md` | **新建** | ~60 | 内置 Skill |
| `skills/git-workflow/SKILL.md` | **新建** | ~50 | 内置 Skill |
| `tests/skills/__init__.py` | **新建** | 0 | 空文件 |
| `tests/skills/test_loader.py` | **新建** | ~160 | SkillLoader 单元测试 |
| `tests/skills/test_skill_tool.py` | **新建** | ~60 | handler 单元测试 |
| `tests/agent/test_skill_integration.py` | **新建** | ~80 | 集成测试 |

**总计**：6 个新文件（不含 SKILL.md 和空 `__init__`），9 个修改文件，~600 行新代码 + ~300 行测试。
