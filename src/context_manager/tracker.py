"""Track multi-step task progress to prevent LLM drift in long conversations."""

from dataclasses import dataclass
from typing import Any


@dataclass
class Step:
    """A single step in the task plan."""

    description: str
    status: str = "pending"  # pending | in_progress | done | skipped
    notes: str = ""


class ProgressTracker:
    """Manages a structured plan for the current task.

    The LLM calls update_plan (a registered tool) to create or revise the plan.
    The agent loop queries the tracker to inject progress summaries into the
    conversation, keeping the LLM anchored on remaining work.

    Plans persist across conversation turns — the LLM manages the lifecycle
    via update_plan (create, modify, mark done). Users can correct plans
    mid-task and the LLM will call update_plan accordingly.
    """

    def __init__(self):
        self._steps: list[Step] = []

    @property
    def steps(self) -> list[Step]:
        """Return a copy of the current steps."""
        return list(self._steps)

    @property
    def has_plan(self) -> bool:
        """True if a plan has been created."""
        return len(self._steps) > 0

    def update_plan(self, steps: list[dict[str, Any]]) -> str:
        """Replace the plan with a new step list.

        Args:
            steps: List of dicts with keys:
                - description (str, required)
                - status (str, optional, default "pending")
                - notes (str, optional)

        Returns:
            Confirmation string with the formatted plan.
        """
        self._steps = []
        for step_dict in steps:
            description = step_dict.get("description", "")
            if not description:
                continue
            status = step_dict.get("status", "pending")
            if status not in ("pending", "in_progress", "done", "skipped"):
                status = "pending"
            notes = step_dict.get("notes", "")
            self._steps.append(Step(description=description, status=status, notes=notes))

        return self.format_summary() or "Plan updated (no valid steps)."

    def format_summary(self) -> str:
        """Format the current plan as a progress summary for injection.

        Returns empty string if no plan exists. Otherwise returns a
        ``[TASK PROGRESS]`` block showing status of each step.
        """
        if not self._steps:
            return ""

        status_icons = {
            "pending": "○",
            "in_progress": "→",
            "done": "✓",
            "skipped": "⊘",
        }

        total = len(self._steps)
        done_count = sum(1 for s in self._steps if s.status == "done")
        skipped_count = sum(1 for s in self._steps if s.status == "skipped")

        lines = ["[TASK PROGRESS]"]
        for i, step in enumerate(self._steps, 1):
            icon = status_icons.get(step.status, "○")
            line = f"{icon} [{i}/{total}] {step.description}"
            if step.status == "in_progress":
                line += " (in progress)"
            if step.notes:
                line += f" — {step.notes}"
            lines.append(line)

        completed = done_count + skipped_count
        lines.append(f"Progress: {completed}/{total} steps complete")

        # Find the next actionable step
        next_step = None
        for step in self._steps:
            if step.status == "in_progress":
                next_step = step.description
                break
        if next_step is None:
            for step in self._steps:
                if step.status == "pending":
                    next_step = step.description
                    break
        if next_step:
            lines.append(f"Next: {next_step}")

        return "\n".join(lines)

    def reset(self):
        """Clear the plan."""
        self._steps = []


# ---------------------------------------------------------------------------
# Tool definition for the LLM
# ---------------------------------------------------------------------------

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
                            "description": {
                                "type": "string",
                                "description": "What this step does",
                            },
                            "status": {
                                "type": "string",
                                "enum": [
                                    "pending",
                                    "in_progress",
                                    "done",
                                    "skipped",
                                ],
                                "description": "Current status (default: pending)",
                            },
                            "notes": {
                                "type": "string",
                                "description": "Brief notes on outcome (for done steps)",
                            },
                        },
                        "required": ["description"],
                    },
                    "description": "The full list of steps for the current task",
                }
            },
            "required": ["steps"],
        },
    },
}


def run_update_plan(args: dict[str, Any], tracker: ProgressTracker) -> str:
    """Tool handler — delegates to tracker.update_plan."""
    steps = args.get("steps", [])
    if not steps:
        return "Error: 'steps' must be a non-empty list."
    return tracker.update_plan(steps)
