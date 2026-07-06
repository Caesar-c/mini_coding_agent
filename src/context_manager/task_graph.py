"""Persistent task graph with dependency tracking for multi-step planning.

Replaces the flat ``ProgressTracker`` with a DAG-based ``TaskGraphManager``
that supports dependency declarations, automatic ready-state computation,
and JSON file persistence.
"""

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from config import PROJECT_ROOT, settings
from logger import get_logger

logger = get_logger(__name__)


def _now_iso() -> str:
    """Return current UTC time as ISO 8601 string (second precision)."""
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class Task:
    """A single task node in the dependency graph."""

    id: str  # "T1", "T2", ... auto-generated
    description: str
    status: str = "pending"  # pending/ready/in_progress/done/failed
    depends_on: list[str] = field(default_factory=list)
    notes: str = ""
    created_at: str = ""
    updated_at: str = ""

    VALID_STATUSES = frozenset({"pending", "ready", "in_progress", "done", "failed", "skipped"})
    TERMINAL_STATUSES = frozenset({"done", "failed", "skipped"})
    AGENT_SETTABLE = frozenset({"in_progress", "done", "failed", "skipped"})
    SATISFIED_STATUSES = frozenset({"done", "skipped"})


class TaskGraphManager:
    """Manage a DAG of tasks with dependency tracking and persistence.

    Replaces ``ProgressTracker``.  The LLM uses four granular tools:

    - ``create_plan``: initialise the graph
    - ``update_task``: change one task's status / notes
    - ``add_task``: append a task mid-execution
    - ``get_plan``: read the current formatted plan

    Tasks whose dependencies are all ``done`` auto-transition to ``ready``.
    The agent sees ready tasks and decides which to execute (and whether
    to parallelise).
    """

    def __init__(self, sandbox_root: str = ".", session_id: str | None = None) -> None:
        self._tasks: list[Task] = []
        self._sandbox_root = sandbox_root
        # Anchor relative sandbox_root against PROJECT_ROOT for stable persistence,
        # matching the LOG_FILE resolution pattern in config.py.
        root = Path(sandbox_root)
        if not root.is_absolute():
            root = PROJECT_ROOT / root
        persist_parts = [settings.TASK_GRAPH_DIR]
        if session_id:
            persist_parts.append(session_id)
        self._persist_dir = root.joinpath(*persist_parts)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def tasks(self) -> list[Task]:
        """Return a shallow copy of all tasks in insertion order."""
        return list(self._tasks)

    @property
    def has_plan(self) -> bool:
        """True if at least one task exists."""
        return len(self._tasks) > 0

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def get_ready_tasks(self) -> list[Task]:
        """Return tasks whose status is ``ready``."""
        return [t for t in self._tasks if t.status == "ready"]

    def get_blocked_tasks(self) -> list[tuple[Task, list[str]]]:
        """Return ``(task, [unmet_dep_ids])`` for pending tasks with incomplete deps."""
        satisfied_ids = {t.id for t in self._tasks if t.status in Task.SATISFIED_STATUSES}
        result: list[tuple[Task, list[str]]] = []
        for t in self._tasks:
            if t.status == "pending" and t.depends_on:
                unmet = [d for d in t.depends_on if d not in satisfied_ids]
                if unmet:
                    result.append((t, unmet))
        return result

    # ------------------------------------------------------------------
    # Mutating operations
    # ------------------------------------------------------------------

    def create_plan(self, tasks: list[dict[str, Any]]) -> str:
        """Initialise the plan, replacing any existing one.

        Each dict in *tasks* must contain ``description`` (str).  Optional
        keys: ``depends_on`` (list[str] referencing positional IDs like
        ``"T1"``), ``notes`` (str).

        Returns the formatted plan summary.

        Raises ``ValueError`` on invalid dependency references or cycles.
        """
        self._tasks = []
        now = _now_iso()

        task_num = 0
        for td in tasks:
            desc = td.get("description", "").strip()
            if not desc:
                continue
            task_num += 1
            raw_status = td.get("status", "pending")
            if raw_status not in Task.VALID_STATUSES:
                raw_status = "pending"
            self._tasks.append(
                Task(
                    id=f"T{task_num}",
                    description=desc,
                    status=raw_status,
                    depends_on=list(td.get("depends_on") or []),
                    notes=td.get("notes", ""),
                    created_at=now,
                    updated_at=now,
                )
            )

        # Validate dependency references
        valid_ids = {t.id for t in self._tasks}
        for t in self._tasks:
            for dep in t.depends_on:
                if dep not in valid_ids:
                    self._tasks = []  # rollback
                    raise ValueError(f"Task {t.id} depends on unknown task '{dep}'")

        if self._detect_cycle():
            self._tasks = []  # rollback
            raise ValueError("Cycle detected in task dependencies")

        self._compute_ready()
        self.save()
        logger.info("Plan created: %d tasks", len(self._tasks))
        return self.format_summary()

    def update_task(
        self,
        task_id: str,
        status: str | None = None,
        notes: str | None = None,
    ) -> str:
        """Update a single task's status and/or notes.

        After a status change, ``ready`` states are recomputed and the
        plan is persisted.

        Returns a confirmation string.

        Raises ``ValueError`` for unknown IDs, invalid statuses, or
        attempts to modify terminal-state tasks.
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
                raise ValueError(f"Task {task_id} is already '{task.status}' (terminal state)")
            task.status = status

        if notes is not None:
            task.notes = notes

        task.updated_at = _now_iso()
        self._compute_ready()
        self.save()

        icon = {"in_progress": "→", "done": "✓", "failed": "✗", "skipped": "⊘"}.get(
            task.status, "○"
        )
        result = f"{icon} {task.id}: {task.description} [{task.status}]"
        if task.notes:
            result += f" — {task.notes}"
        return result

    def add_task(
        self,
        description: str,
        depends_on: list[str] | None = None,
        notes: str = "",
    ) -> str:
        """Append a new task to the existing plan.

        Returns a confirmation string with the new task ID.

        Raises ``ValueError`` if no plan exists, references are invalid,
        or a cycle would be created.
        """
        if not self._tasks:
            raise ValueError("No active plan. Call create_plan first.")

        now = _now_iso()
        new_id = self._next_id()
        deps = list(depends_on or [])

        valid_ids = {t.id for t in self._tasks}
        for dep in deps:
            if dep not in valid_ids:
                raise ValueError(f"Unknown dependency: '{dep}'")

        new_task = Task(
            id=new_id,
            description=description,
            status="pending",
            depends_on=deps,
            notes=notes,
            created_at=now,
            updated_at=now,
        )
        self._tasks.append(new_task)

        if self._detect_cycle():
            self._tasks.pop()  # rollback
            raise ValueError(f"Adding {new_id} would create a cycle")

        self._compute_ready()
        self.save()
        logger.info("Task added: %s — %s", new_id, description)
        return f"Added {new_id}: {description}"

    def reset(self) -> None:
        """Clear all tasks and delete the persisted plan file."""
        self._tasks = []
        plan_path = self._persist_dir / "plan.json"
        try:
            plan_path.unlink()
            logger.info("Plan reset and file deleted: %s", plan_path)
        except FileNotFoundError:
            logger.info("Plan reset (no file to delete)")

    # ------------------------------------------------------------------
    # Formatting
    # ------------------------------------------------------------------

    def format_summary(self) -> str:
        """Format the current plan as a ``[TASK PROGRESS]`` block.

        Returns empty string if no plan exists.
        """
        if not self._tasks:
            return ""

        icons = {
            "done": "✓",
            "failed": "✗",
            "in_progress": "→",
            "ready": "◉",
            "pending": "○",
            "skipped": "⊘",
        }
        satisfied_ids = {t.id for t in self._tasks if t.status in Task.SATISFIED_STATUSES}

        lines = ["[TASK PROGRESS]"]
        for task in self._tasks:
            icon = icons.get(task.status, "○")
            line = f"{icon} {task.id}: {task.description}"

            if task.status == "pending" and task.depends_on:
                unmet = [d for d in task.depends_on if d not in satisfied_ids]
                if unmet:
                    line += f" [waiting: {', '.join(unmet)}]"
            elif task.status == "ready":
                line += " [ready]"

            if task.notes:
                line += f" — {task.notes}"
            lines.append(line)

        # Ready line
        ready = [t for t in self._tasks if t.status == "ready"]
        if ready:
            lines.append(f"Ready: {', '.join(t.id for t in ready)}")

        # Progress stats
        total = len(self._tasks)
        done = sum(1 for t in self._tasks if t.status == "done")
        skipped = sum(1 for t in self._tasks if t.status == "skipped")
        failed = sum(1 for t in self._tasks if t.status == "failed")
        in_prog = sum(1 for t in self._tasks if t.status == "in_progress")
        ready_n = len(ready)
        pending = total - done - skipped - failed - in_prog - ready_n
        completed = done + skipped
        parts = [f"{completed}/{total} done"]
        if ready_n:
            parts.append(f"{ready_n} ready")
        if in_prog:
            parts.append(f"{in_prog} in progress")
        if pending:
            parts.append(f"{pending} pending")
        if failed:
            parts.append(f"{failed} failed")
        lines.append(f"Progress: {', '.join(parts)}")

        # Next actionable: prefer ready, fall back to in_progress
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

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self) -> None:
        """Atomically write plan to ``{sandbox_root}/{TASK_GRAPH_DIR}/plan.json``."""
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

    def load(self) -> bool:
        """Load plan from disk.  Returns True if loaded, False otherwise."""
        plan_path = self._persist_dir / "plan.json"
        if not plan_path.is_file():
            return False
        try:
            data = json.loads(plan_path.read_text(encoding="utf-8"))
            self._tasks = []
            for td in data.get("tasks", []):
                self._tasks.append(
                    Task(
                        id=td["id"],
                        description=td["description"],
                        status=td.get("status", "pending"),
                        depends_on=td.get("depends_on", []),
                        notes=td.get("notes", ""),
                        created_at=td.get("created_at", ""),
                        updated_at=td.get("updated_at", ""),
                    )
                )
            self._compute_ready()
            logger.info("Loaded plan: %d tasks from %s", len(self._tasks), plan_path)
            return True
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning("Failed to load plan from %s: %s", plan_path, e)
            return False

    # ------------------------------------------------------------------
    # DAG internals
    # ------------------------------------------------------------------

    def _compute_ready(self) -> None:
        """Transition pending↔ready based on dependency statuses.

        A dependency is considered satisfied if its status is in
        ``Task.SATISFIED_STATUSES`` (done or skipped).
        """
        satisfied_ids = {t.id for t in self._tasks if t.status in Task.SATISFIED_STATUSES}

        for task in self._tasks:
            if task.status == "pending":
                if not task.depends_on:
                    # No dependencies → immediately ready
                    task.status = "ready"
                    task.updated_at = _now_iso()
                elif all(dep in satisfied_ids for dep in task.depends_on):
                    task.status = "ready"
                    task.updated_at = _now_iso()
            elif task.status == "ready":
                # Defensive downgrade if a dep is no longer satisfied
                if task.depends_on and not all(dep in satisfied_ids for dep in task.depends_on):
                    task.status = "pending"
                    task.updated_at = _now_iso()

    def _detect_cycle(self) -> bool:
        """DFS three-colour cycle detection.  Returns True if a cycle exists."""
        white, gray, black = 0, 1, 2
        color = {t.id: white for t in self._tasks}
        adj = {t.id: t.depends_on for t in self._tasks}

        def dfs(node: str) -> bool:
            color[node] = gray
            for dep in adj.get(node, []):
                if color.get(dep) == gray:
                    return True
                if color.get(dep) == white and dfs(dep):
                    return True
            color[node] = black
            return False

        return any(color[n] == white and dfs(n) for n in color)

    def _find_task(self, task_id: str) -> Task | None:
        """Look up a task by ID.  Returns None if not found."""
        for t in self._tasks:
            if t.id == task_id:
                return t
        return None

    def _next_id(self) -> str:
        """Generate the next task ID based on the current maximum."""
        if not self._tasks:
            return "T1"
        max_num = max(
            (int(t.id[1:]) for t in self._tasks if t.id.startswith("T") and t.id[1:].isdigit()),
            default=0,
        )
        return f"T{max_num + 1}"


# ---------------------------------------------------------------------------
# Tool definitions (OpenAI function-calling schemas)
# ---------------------------------------------------------------------------

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
                            "status": {
                                "type": "string",
                                "enum": [
                                    "pending",
                                    "in_progress",
                                    "done",
                                    "failed",
                                    "skipped",
                                ],
                                "description": (
                                    "Initial status (default: pending). Use when "
                                    "restructuring a plan to preserve completion state."
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
                    "enum": ["in_progress", "done", "failed", "skipped"],
                    "description": (
                        "New status: 'in_progress' to start, "
                        "'done' to complete, 'failed' on error, "
                        "'skipped' if not needed."
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


# ---------------------------------------------------------------------------
# Handler functions
# ---------------------------------------------------------------------------


def run_create_plan(args: dict[str, Any], graph: TaskGraphManager) -> str:
    """Tool handler — delegates to ``graph.create_plan``."""
    tasks = args.get("tasks", [])
    if not tasks:
        return "Error: 'tasks' must be a non-empty list."
    try:
        return graph.create_plan(tasks)
    except ValueError as e:
        return f"Error: {e}"


def run_update_task(args: dict[str, Any], graph: TaskGraphManager) -> str:
    """Tool handler — delegates to ``graph.update_task``."""
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
    """Tool handler — delegates to ``graph.add_task``."""
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
    """Tool handler — delegates to ``graph.format_summary``."""
    summary = graph.format_summary()
    return summary or "No active plan."


ALL_TASK_GRAPH_TOOLS = [
    (CREATE_PLAN_TOOL_DEFINITION, run_create_plan),
    (UPDATE_TASK_TOOL_DEFINITION, run_update_task),
    (ADD_TASK_TOOL_DEFINITION, run_add_task),
    (GET_PLAN_TOOL_DEFINITION, run_get_plan),
]
