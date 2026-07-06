"""Tests for the TaskGraphManager, Task dataclass, DAG operations, and tool handlers."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from context_manager.task_graph import (
    ADD_TASK_TOOL_DEFINITION,
    ALL_TASK_GRAPH_TOOLS,
    CREATE_PLAN_TOOL_DEFINITION,
    GET_PLAN_TOOL_DEFINITION,
    UPDATE_TASK_TOOL_DEFINITION,
    Task,
    TaskGraphManager,
    run_add_task,
    run_create_plan,
    run_get_plan,
    run_update_task,
)


class TestTask(unittest.TestCase):
    """Tests for the Task dataclass."""

    def test_default_values(self):
        task = Task(id="T1", description="Do something")
        self.assertEqual(task.status, "pending")
        self.assertEqual(task.depends_on, [])
        self.assertEqual(task.notes, "")
        self.assertEqual(task.created_at, "")
        self.assertEqual(task.updated_at, "")

    def test_custom_values(self):
        task = Task(
            id="T2",
            description="Build module",
            status="done",
            depends_on=["T1"],
            notes="Completed successfully",
            created_at="2026-07-05T10:00:00",
            updated_at="2026-07-05T10:05:00",
        )
        self.assertEqual(task.id, "T2")
        self.assertEqual(task.status, "done")
        self.assertEqual(task.depends_on, ["T1"])
        self.assertEqual(task.notes, "Completed successfully")

    def test_valid_statuses(self):
        self.assertEqual(
            Task.VALID_STATUSES,
            frozenset({"pending", "ready", "in_progress", "done", "failed", "skipped"}),
        )

    def test_terminal_statuses(self):
        self.assertEqual(Task.TERMINAL_STATUSES, frozenset({"done", "failed", "skipped"}))

    def test_agent_settable(self):
        self.assertEqual(
            Task.AGENT_SETTABLE, frozenset({"in_progress", "done", "failed", "skipped"})
        )
        self.assertNotIn("ready", Task.AGENT_SETTABLE)

    def test_satisfied_statuses(self):
        self.assertEqual(Task.SATISFIED_STATUSES, frozenset({"done", "skipped"}))


class TestTaskGraphManager(unittest.TestCase):
    """Tests for the core TaskGraphManager."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.graph = TaskGraphManager(sandbox_root=self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_initial_state(self):
        self.assertFalse(self.graph.has_plan)
        self.assertEqual(self.graph.tasks, [])
        self.assertEqual(self.graph.format_summary(), "")

    def test_create_plan_basic(self):
        result = self.graph.create_plan(
            [
                {"description": "Step A"},
                {"description": "Step B"},
                {"description": "Step C"},
            ]
        )
        self.assertTrue(self.graph.has_plan)
        tasks = self.graph.tasks
        self.assertEqual(len(tasks), 3)
        self.assertEqual(tasks[0].id, "T1")
        self.assertEqual(tasks[1].id, "T2")
        self.assertEqual(tasks[2].id, "T3")
        # No deps → all should be ready
        for t in tasks:
            self.assertEqual(t.status, "ready")
        self.assertIn("[TASK PROGRESS]", result)

    def test_create_plan_with_dependencies(self):
        self.graph.create_plan(
            [
                {"description": "Design"},
                {"description": "Implement", "depends_on": ["T1"]},
                {"description": "Test", "depends_on": ["T2"]},
            ]
        )
        tasks = self.graph.tasks
        self.assertEqual(tasks[0].status, "ready")  # T1: no deps
        self.assertEqual(tasks[1].status, "pending")  # T2: depends on T1
        self.assertEqual(tasks[2].status, "pending")  # T3: depends on T2

    def test_create_plan_replaces_existing(self):
        self.graph.create_plan([{"description": "Old A"}])
        self.assertEqual(len(self.graph.tasks), 1)
        self.graph.create_plan([{"description": "New A"}, {"description": "New B"}])
        self.assertEqual(len(self.graph.tasks), 2)
        self.assertEqual(self.graph.tasks[0].description, "New A")

    def test_create_plan_cycle_detection(self):
        with self.assertRaises(ValueError) as ctx:
            self.graph.create_plan(
                [
                    {"description": "A", "depends_on": ["T2"]},
                    {"description": "B", "depends_on": ["T1"]},
                ]
            )
        self.assertIn("Cycle", str(ctx.exception))
        # Should rollback to empty
        self.assertFalse(self.graph.has_plan)

    def test_create_plan_invalid_dep_reference(self):
        with self.assertRaises(ValueError) as ctx:
            self.graph.create_plan(
                [
                    {"description": "A"},
                    {"description": "B", "depends_on": ["T99"]},
                ]
            )
        self.assertIn("unknown task", str(ctx.exception))
        self.assertFalse(self.graph.has_plan)

    def test_create_plan_skips_empty_descriptions(self):
        self.graph.create_plan(
            [
                {"description": "Real task"},
                {"description": ""},
                {"description": "  "},
            ]
        )
        self.assertEqual(len(self.graph.tasks), 1)

    def test_create_plan_no_id_gaps_with_empty_descriptions(self):
        """Empty descriptions are skipped but IDs remain contiguous (T1, T2)."""
        self.graph.create_plan(
            [
                {"description": ""},
                {"description": "Setup"},
                {"description": "Build", "depends_on": ["T1"]},
            ]
        )
        tasks = self.graph.tasks
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0].id, "T1")
        self.assertEqual(tasks[0].description, "Setup")
        self.assertEqual(tasks[1].id, "T2")
        self.assertEqual(tasks[1].depends_on, ["T1"])

    def test_create_plan_with_initial_status(self):
        """create_plan accepts optional status field per task."""
        self.graph.create_plan(
            [
                {"description": "Already done", "status": "done"},
                {"description": "In flight", "status": "in_progress"},
                {"description": "Next", "depends_on": ["T1"]},
            ]
        )
        tasks = self.graph.tasks
        self.assertEqual(tasks[0].status, "done")
        self.assertEqual(tasks[1].status, "in_progress")
        # T3 depends on T1 which is done → ready
        self.assertEqual(tasks[2].status, "ready")

    def test_create_plan_invalid_status_defaults_to_pending(self):
        """Invalid status values in create_plan fall back to pending."""
        self.graph.create_plan(
            [
                {"description": "Bad status", "status": "bogus"},
            ]
        )
        # Invalid status → pending → ready (no deps)
        self.assertEqual(self.graph.tasks[0].status, "ready")

    def test_update_task_status_transition(self):
        self.graph.create_plan([{"description": "Step 1"}])
        # ready → in_progress
        result = self.graph.update_task("T1", status="in_progress")
        self.assertIn("in_progress", result)
        self.assertEqual(self.graph.tasks[0].status, "in_progress")
        # in_progress → done
        result = self.graph.update_task("T1", status="done")
        self.assertIn("done", result)
        self.assertEqual(self.graph.tasks[0].status, "done")

    def test_update_task_ready_computation(self):
        self.graph.create_plan(
            [
                {"description": "Step A"},
                {"description": "Step B", "depends_on": ["T1"]},
            ]
        )
        self.assertEqual(self.graph.tasks[1].status, "pending")
        # Mark T1 done → T2 should become ready
        self.graph.update_task("T1", status="in_progress")
        self.graph.update_task("T1", status="done")
        self.assertEqual(self.graph.tasks[1].status, "ready")

    def test_update_task_invalid_id(self):
        self.graph.create_plan([{"description": "Step 1"}])
        with self.assertRaises(ValueError) as ctx:
            self.graph.update_task("T99", status="done")
        self.assertIn("Unknown task", str(ctx.exception))

    def test_update_task_invalid_status(self):
        self.graph.create_plan([{"description": "Step 1"}])
        with self.assertRaises(ValueError) as ctx:
            self.graph.update_task("T1", status="ready")
        self.assertIn("Invalid status", str(ctx.exception))

    def test_update_task_terminal_reject(self):
        self.graph.create_plan([{"description": "Step 1"}])
        self.graph.update_task("T1", status="in_progress")
        self.graph.update_task("T1", status="done")
        with self.assertRaises(ValueError) as ctx:
            self.graph.update_task("T1", status="in_progress")
        self.assertIn("terminal state", str(ctx.exception))

    def test_update_task_notes(self):
        self.graph.create_plan([{"description": "Step 1"}])
        self.graph.update_task("T1", status="in_progress")
        self.graph.update_task("T1", status="done", notes="All tests passed")
        self.assertEqual(self.graph.tasks[0].notes, "All tests passed")

    def test_update_task_notes_only(self):
        self.graph.create_plan([{"description": "Step 1"}])
        self.graph.update_task("T1", notes="Just a note")
        self.assertEqual(self.graph.tasks[0].notes, "Just a note")
        # Status should remain ready (no deps, auto-ready)
        self.assertEqual(self.graph.tasks[0].status, "ready")

    def test_add_task(self):
        self.graph.create_plan([{"description": "Step 1"}])
        result = self.graph.add_task("Step 2")
        self.assertIn("T2", result)
        self.assertEqual(len(self.graph.tasks), 2)
        self.assertEqual(self.graph.tasks[1].id, "T2")

    def test_add_task_with_deps(self):
        self.graph.create_plan([{"description": "Step 1"}])
        self.graph.add_task("Step 2", depends_on=["T1"])
        tasks = self.graph.tasks
        self.assertEqual(tasks[1].depends_on, ["T1"])
        # T1 is ready (no deps), not done → T2 should be pending
        self.assertEqual(tasks[1].status, "pending")

    def test_add_task_no_plan(self):
        with self.assertRaises(ValueError) as ctx:
            self.graph.add_task("Orphan task")
        self.assertIn("No active plan", str(ctx.exception))

    def test_add_task_invalid_dep(self):
        self.graph.create_plan([{"description": "Step 1"}])
        with self.assertRaises(ValueError) as ctx:
            self.graph.add_task("Step 2", depends_on=["T99"])
        self.assertIn("Unknown dependency", str(ctx.exception))
        self.assertEqual(len(self.graph.tasks), 1)  # rollback

    def test_add_task_chain_no_false_cycle(self):
        """Adding T1→T2→T3 is valid, not a cycle."""
        self.graph.create_plan(
            [
                {"description": "A"},
                {"description": "B", "depends_on": ["T1"]},
            ]
        )
        self.graph.add_task("C", depends_on=["T2"])
        self.assertEqual(len(self.graph.tasks), 3)
        self.assertEqual(self.graph.tasks[2].depends_on, ["T2"])
        self.assertEqual(self.graph.tasks[2].status, "pending")

    def test_reset(self):
        self.graph.create_plan([{"description": "Step 1"}])
        self.assertTrue(self.graph.has_plan)
        self.graph.reset()
        self.assertFalse(self.graph.has_plan)
        self.assertEqual(self.graph.tasks, [])

    def test_get_ready_tasks(self):
        self.graph.create_plan(
            [
                {"description": "A"},
                {"description": "B", "depends_on": ["T1"]},
            ]
        )
        ready = self.graph.get_ready_tasks()
        self.assertEqual(len(ready), 1)
        self.assertEqual(ready[0].id, "T1")

    def test_get_blocked_tasks(self):
        self.graph.create_plan(
            [
                {"description": "A"},
                {"description": "B", "depends_on": ["T1"]},
                {"description": "C", "depends_on": ["T1", "T2"]},
            ]
        )
        blocked = self.graph.get_blocked_tasks()
        self.assertEqual(len(blocked), 2)
        # T2 blocked by T1
        self.assertEqual(blocked[0][0].id, "T2")
        self.assertEqual(blocked[0][1], ["T1"])
        # T3 blocked by T1 and T2
        self.assertEqual(blocked[1][0].id, "T3")
        self.assertEqual(blocked[1][1], ["T1", "T2"])


class TestDAGOperations(unittest.TestCase):
    """Tests for dependency graph algorithms."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.graph = TaskGraphManager(sandbox_root=self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_ready_no_deps(self):
        self.graph.create_plan([{"description": "Solo task"}])
        self.assertEqual(self.graph.tasks[0].status, "ready")

    def test_ready_all_deps_done(self):
        self.graph.create_plan(
            [
                {"description": "A"},
                {"description": "B", "depends_on": ["T1"]},
            ]
        )
        self.graph.update_task("T1", status="in_progress")
        self.graph.update_task("T1", status="done")
        self.assertEqual(self.graph.tasks[1].status, "ready")

    def test_pending_some_deps_undone(self):
        self.graph.create_plan(
            [
                {"description": "A"},
                {"description": "B"},
                {"description": "C", "depends_on": ["T1", "T2"]},
            ]
        )
        self.graph.update_task("T1", status="in_progress")
        self.graph.update_task("T1", status="done")
        # T2 still ready (not done) → T3 stays pending
        self.assertEqual(self.graph.tasks[2].status, "pending")

    def test_failed_does_not_unblock(self):
        """D12 revision: failed does NOT count as 'done' for dependency resolution."""
        self.graph.create_plan(
            [
                {"description": "A"},
                {"description": "B", "depends_on": ["T1"]},
            ]
        )
        self.graph.update_task("T1", status="in_progress")
        self.graph.update_task("T1", status="failed")
        # T2 should remain pending since T1 failed (not done)
        self.assertEqual(self.graph.tasks[1].status, "pending")

    def test_skipped_unblocks_dependents(self):
        """Skipped tasks satisfy dependencies (like done, unlike failed)."""
        self.graph.create_plan(
            [
                {"description": "Install deps (unnecessary)"},
                {"description": "Build", "depends_on": ["T1"]},
            ]
        )
        self.graph.update_task("T1", status="in_progress")
        self.graph.update_task("T1", status="skipped")
        # T2 should become ready since T1 is skipped (satisfied)
        self.assertEqual(self.graph.tasks[1].status, "ready")

    def test_skipped_counts_in_progress(self):
        """Skipped tasks count toward completion in format_summary."""
        self.graph.create_plan(
            [
                {"description": "A"},
                {"description": "B"},
            ]
        )
        self.graph.update_task("T1", status="in_progress")
        self.graph.update_task("T1", status="skipped")
        summary = self.graph.format_summary()
        self.assertIn("1/2 done", summary)
        self.assertIn("⊘", summary)

    def test_chain_dependency(self):
        self.graph.create_plan(
            [
                {"description": "A"},
                {"description": "B", "depends_on": ["T1"]},
                {"description": "C", "depends_on": ["T2"]},
            ]
        )
        self.assertEqual(self.graph.tasks[0].status, "ready")
        self.assertEqual(self.graph.tasks[1].status, "pending")
        self.assertEqual(self.graph.tasks[2].status, "pending")

        self.graph.update_task("T1", status="in_progress")
        self.graph.update_task("T1", status="done")
        self.assertEqual(self.graph.tasks[1].status, "ready")
        self.assertEqual(self.graph.tasks[2].status, "pending")

        self.graph.update_task("T2", status="in_progress")
        self.graph.update_task("T2", status="done")
        self.assertEqual(self.graph.tasks[2].status, "ready")

    def test_diamond_dependency(self):
        self.graph.create_plan(
            [
                {"description": "A"},
                {"description": "B"},
                {"description": "C", "depends_on": ["T1", "T2"]},
            ]
        )
        # T1 and T2 both ready (no deps)
        self.graph.update_task("T1", status="in_progress")
        self.graph.update_task("T1", status="done")
        self.assertEqual(self.graph.tasks[2].status, "pending")  # T2 not done yet

        self.graph.update_task("T2", status="in_progress")
        self.graph.update_task("T2", status="done")
        self.assertEqual(self.graph.tasks[2].status, "ready")  # both done

    def test_parallel_branches(self):
        self.graph.create_plan(
            [
                {"description": "Setup"},
                {"description": "Branch A", "depends_on": ["T1"]},
                {"description": "Branch B", "depends_on": ["T1"]},
            ]
        )
        self.graph.update_task("T1", status="in_progress")
        self.graph.update_task("T1", status="done")
        # Both T2 and T3 should be ready simultaneously
        self.assertEqual(self.graph.tasks[1].status, "ready")
        self.assertEqual(self.graph.tasks[2].status, "ready")

    def test_cycle_direct(self):
        with self.assertRaises(ValueError):
            self.graph.create_plan(
                [
                    {"description": "A", "depends_on": ["T2"]},
                    {"description": "B", "depends_on": ["T1"]},
                ]
            )

    def test_cycle_transitive(self):
        with self.assertRaises(ValueError):
            self.graph.create_plan(
                [
                    {"description": "A", "depends_on": ["T3"]},
                    {"description": "B", "depends_on": ["T1"]},
                    {"description": "C", "depends_on": ["T2"]},
                ]
            )

    def test_no_false_cycle(self):
        """A→B, A→C, B→C is a valid DAG, not a cycle."""
        self.graph.create_plan(
            [
                {"description": "A"},
                {"description": "B", "depends_on": ["T1"]},
                {"description": "C", "depends_on": ["T1", "T2"]},
            ]
        )
        self.assertEqual(len(self.graph.tasks), 3)  # no rollback


class TestFormatSummary(unittest.TestCase):
    """Tests for the [TASK PROGRESS] format output."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.graph = TaskGraphManager(sandbox_root=self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_empty_plan_returns_empty_string(self):
        self.assertEqual(self.graph.format_summary(), "")

    def test_status_icons(self):
        self.graph.create_plan(
            [
                {"description": "Done task"},
                {"description": "In progress", "depends_on": ["T1"]},
            ]
        )
        self.graph.update_task("T1", status="in_progress")
        self.graph.update_task("T1", status="done", notes="OK")
        self.graph.update_task("T2", status="in_progress")

        summary = self.graph.format_summary()
        self.assertIn("✓ T1:", summary)
        self.assertIn("→ T2:", summary)

    def test_failed_icon(self):
        self.graph.create_plan([{"description": "Will fail"}])
        self.graph.update_task("T1", status="in_progress")
        self.graph.update_task("T1", status="failed", notes="Error occurred")

        summary = self.graph.format_summary()
        self.assertIn("✗ T1:", summary)
        self.assertIn("Error occurred", summary)

    def test_waiting_annotation(self):
        self.graph.create_plan(
            [
                {"description": "A"},
                {"description": "B", "depends_on": ["T1"]},
            ]
        )
        summary = self.graph.format_summary()
        self.assertIn("[waiting: T1]", summary)

    def test_ready_annotation(self):
        self.graph.create_plan([{"description": "Solo"}])
        summary = self.graph.format_summary()
        self.assertIn("[ready]", summary)

    def test_ready_line(self):
        self.graph.create_plan(
            [
                {"description": "A"},
                {"description": "B"},
            ]
        )
        summary = self.graph.format_summary()
        self.assertIn("Ready: T1, T2", summary)

    def test_progress_line(self):
        self.graph.create_plan(
            [
                {"description": "A"},
                {"description": "B", "depends_on": ["T1"]},
                {"description": "C", "depends_on": ["T1"]},
            ]
        )
        self.graph.update_task("T1", status="in_progress")
        summary = self.graph.format_summary()
        self.assertIn("0/3 done", summary)
        self.assertIn("1 in progress", summary)
        self.assertIn("2 pending", summary)

    def test_next_prefers_ready(self):
        self.graph.create_plan(
            [
                {"description": "A"},
                {"description": "B"},
            ]
        )
        summary = self.graph.format_summary()
        self.assertIn("Next: T1", summary)

    def test_next_fallback_in_progress(self):
        self.graph.create_plan(
            [
                {"description": "A"},
                {"description": "B", "depends_on": ["T1"]},
            ]
        )
        self.graph.update_task("T1", status="in_progress")
        summary = self.graph.format_summary()
        self.assertIn("Next: T1", summary)

    def test_notes_display(self):
        self.graph.create_plan([{"description": "Step"}])
        self.graph.update_task("T1", status="in_progress")
        self.graph.update_task("T1", status="done", notes="All good")
        summary = self.graph.format_summary()
        self.assertIn("— All good", summary)

    def test_header_present(self):
        self.graph.create_plan([{"description": "A"}])
        summary = self.graph.format_summary()
        self.assertTrue(summary.startswith("[TASK PROGRESS]"))


class TestPersistence(unittest.TestCase):
    """Tests for JSON file I/O."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_save_creates_file(self):
        graph = TaskGraphManager(sandbox_root=self.tmpdir)
        graph.create_plan([{"description": "Step 1"}])
        plan_path = Path(self.tmpdir) / ".mini_agent" / "plan.json"
        self.assertTrue(plan_path.is_file())
        data = json.loads(plan_path.read_text())
        self.assertEqual(data["version"], 1)
        self.assertEqual(len(data["tasks"]), 1)

    def test_load_restores_tasks(self):
        graph1 = TaskGraphManager(sandbox_root=self.tmpdir)
        graph1.create_plan(
            [
                {"description": "A"},
                {"description": "B", "depends_on": ["T1"]},
            ]
        )
        graph1.update_task("T1", status="in_progress")
        graph1.update_task("T1", status="done", notes="Finished")

        graph2 = TaskGraphManager(sandbox_root=self.tmpdir)
        result = graph2.load()
        self.assertTrue(result)
        self.assertEqual(len(graph2.tasks), 2)
        self.assertEqual(graph2.tasks[0].status, "done")
        self.assertEqual(graph2.tasks[0].notes, "Finished")
        self.assertEqual(graph2.tasks[1].depends_on, ["T1"])
        # Ready should be recomputed on load
        self.assertEqual(graph2.tasks[1].status, "ready")

    def test_load_no_file_returns_false(self):
        graph = TaskGraphManager(sandbox_root=self.tmpdir)
        self.assertFalse(graph.load())

    def test_load_corrupted_json_returns_false(self):
        persist_dir = Path(self.tmpdir) / ".mini_agent"
        persist_dir.mkdir(parents=True)
        (persist_dir / "plan.json").write_text("{invalid json", encoding="utf-8")
        graph = TaskGraphManager(sandbox_root=self.tmpdir)
        self.assertFalse(graph.load())
        self.assertFalse(graph.has_plan)

    def test_load_missing_fields_uses_defaults(self):
        persist_dir = Path(self.tmpdir) / ".mini_agent"
        persist_dir.mkdir(parents=True)
        data = {"version": 1, "tasks": [{"id": "T1", "description": "Minimal"}]}
        (persist_dir / "plan.json").write_text(json.dumps(data), encoding="utf-8")
        graph = TaskGraphManager(sandbox_root=self.tmpdir)
        self.assertTrue(graph.load())
        task = graph.tasks[0]
        self.assertEqual(task.status, "ready")  # pending → ready (no deps)
        self.assertEqual(task.depends_on, [])
        self.assertEqual(task.notes, "")

    def test_reset_deletes_file(self):
        graph = TaskGraphManager(sandbox_root=self.tmpdir)
        graph.create_plan([{"description": "Step 1"}])
        plan_path = Path(self.tmpdir) / ".mini_agent" / "plan.json"
        self.assertTrue(plan_path.is_file())
        graph.reset()
        self.assertFalse(plan_path.is_file())

    def test_atomic_write_no_tmp_leftover(self):
        graph = TaskGraphManager(sandbox_root=self.tmpdir)
        graph.create_plan([{"description": "Step 1"}])
        persist_dir = Path(self.tmpdir) / ".mini_agent"
        tmp_files = list(persist_dir.glob("*.tmp"))
        self.assertEqual(len(tmp_files), 0)

    def test_session_id_scopes_persistence(self):
        """Different session_ids write to different plan.json files."""
        graph_a = TaskGraphManager(sandbox_root=self.tmpdir, session_id="session-a")
        graph_b = TaskGraphManager(sandbox_root=self.tmpdir, session_id="session-b")
        graph_a.create_plan([{"description": "Plan A"}])
        graph_b.create_plan([{"description": "Plan B"}])

        # Verify separate files exist
        plan_a = Path(self.tmpdir) / ".mini_agent" / "session-a" / "plan.json"
        plan_b = Path(self.tmpdir) / ".mini_agent" / "session-b" / "plan.json"
        self.assertTrue(plan_a.is_file())
        self.assertTrue(plan_b.is_file())

        # Load each and verify they're independent
        loaded_a = TaskGraphManager(sandbox_root=self.tmpdir, session_id="session-a")
        loaded_a.load()
        self.assertEqual(loaded_a.tasks[0].description, "Plan A")

        loaded_b = TaskGraphManager(sandbox_root=self.tmpdir, session_id="session-b")
        loaded_b.load()
        self.assertEqual(loaded_b.tasks[0].description, "Plan B")

    def test_no_session_id_uses_shared_path(self):
        """Without session_id, plan goes to TASK_GRAPH_DIR/plan.json."""
        graph = TaskGraphManager(sandbox_root=self.tmpdir)
        graph.create_plan([{"description": "Shared"}])
        plan_path = Path(self.tmpdir) / ".mini_agent" / "plan.json"
        self.assertTrue(plan_path.is_file())

    def test_next_id_with_nonstandard_ids(self):
        """_next_id handles loaded tasks with non-standard IDs gracefully."""
        # Simulate a plan loaded from disk with non-standard IDs
        persist_dir = Path(self.tmpdir) / ".mini_agent"
        persist_dir.mkdir(parents=True)
        data = {
            "version": 1,
            "tasks": [
                {"id": "step_1", "description": "Non-standard ID task"},
            ],
        }
        (persist_dir / "plan.json").write_text(json.dumps(data), encoding="utf-8")

        graph = TaskGraphManager(sandbox_root=self.tmpdir)
        graph.load()
        self.assertEqual(len(graph.tasks), 1)
        self.assertEqual(graph.tasks[0].id, "step_1")

        # add_task should not crash — _next_id falls back to T1
        result = graph.add_task("New task")
        self.assertIn("T1", result)


class TestToolHandlers(unittest.TestCase):
    """Tests for the run_* handler functions."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.graph = TaskGraphManager(sandbox_root=self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_run_create_plan_delegates(self):
        result = run_create_plan(
            {"tasks": [{"description": "A"}, {"description": "B"}]}, self.graph
        )
        self.assertIn("[TASK PROGRESS]", result)
        self.assertEqual(len(self.graph.tasks), 2)

    def test_run_create_plan_empty_returns_error(self):
        result = run_create_plan({"tasks": []}, self.graph)
        self.assertIn("Error", result)

    def test_run_create_plan_cycle_returns_error(self):
        result = run_create_plan(
            {
                "tasks": [
                    {"description": "A", "depends_on": ["T2"]},
                    {"description": "B", "depends_on": ["T1"]},
                ]
            },
            self.graph,
        )
        self.assertIn("Error", result)
        self.assertIn("Cycle", result)

    def test_run_update_task_delegates(self):
        self.graph.create_plan([{"description": "Step 1"}])
        result = run_update_task({"task_id": "T1", "status": "in_progress"}, self.graph)
        self.assertIn("in_progress", result)

    def test_run_update_task_no_id_returns_error(self):
        result = run_update_task({}, self.graph)
        self.assertIn("Error", result)

    def test_run_update_task_invalid_id_returns_error(self):
        self.graph.create_plan([{"description": "Step 1"}])
        result = run_update_task({"task_id": "T99", "status": "done"}, self.graph)
        self.assertIn("Error", result)

    def test_run_add_task_delegates(self):
        self.graph.create_plan([{"description": "Step 1"}])
        result = run_add_task({"description": "Step 2"}, self.graph)
        self.assertIn("T2", result)

    def test_run_add_task_no_desc_returns_error(self):
        result = run_add_task({}, self.graph)
        self.assertIn("Error", result)

    def test_run_add_task_no_plan_returns_error(self):
        result = run_add_task({"description": "Orphan"}, self.graph)
        self.assertIn("Error", result)

    def test_run_get_plan_returns_summary(self):
        self.graph.create_plan([{"description": "Step 1"}])
        result = run_get_plan({}, self.graph)
        self.assertIn("[TASK PROGRESS]", result)

    def test_run_get_plan_no_plan(self):
        result = run_get_plan({}, self.graph)
        self.assertEqual(result, "No active plan.")


class TestToolDefinitions(unittest.TestCase):
    """Tests for the OpenAI function-calling schemas."""

    def test_all_tools_has_four_entries(self):
        self.assertEqual(len(ALL_TASK_GRAPH_TOOLS), 4)

    def test_tool_names(self):
        names = {defn["function"]["name"] for defn, _ in ALL_TASK_GRAPH_TOOLS}
        self.assertEqual(names, {"create_plan", "update_task", "add_task", "get_plan"})

    def test_create_plan_schema(self):
        schema = CREATE_PLAN_TOOL_DEFINITION["function"]["parameters"]
        self.assertIn("tasks", schema["properties"])
        self.assertEqual(schema["required"], ["tasks"])

    def test_update_task_schema(self):
        schema = UPDATE_TASK_TOOL_DEFINITION["function"]["parameters"]
        self.assertIn("task_id", schema["properties"])
        self.assertIn("status", schema["properties"])
        self.assertEqual(schema["required"], ["task_id"])
        status_enum = schema["properties"]["status"]["enum"]
        self.assertEqual(sorted(status_enum), ["done", "failed", "in_progress", "skipped"])

    def test_add_task_schema(self):
        schema = ADD_TASK_TOOL_DEFINITION["function"]["parameters"]
        self.assertIn("description", schema["properties"])
        self.assertEqual(schema["required"], ["description"])

    def test_get_plan_schema(self):
        schema = GET_PLAN_TOOL_DEFINITION["function"]["parameters"]
        self.assertEqual(schema["properties"], {})


if __name__ == "__main__":
    unittest.main()
