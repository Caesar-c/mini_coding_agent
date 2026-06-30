"""Tests for the ProgressTracker and update_plan tool."""

import unittest

from context_manager.tracker import (
    ProgressTracker,
    Step,
    run_update_plan,
)


class TestStep(unittest.TestCase):
    """Tests for the Step dataclass."""

    def test_defaults(self):
        step = Step(description="Do something")
        self.assertEqual(step.description, "Do something")
        self.assertEqual(step.status, "pending")
        self.assertEqual(step.notes, "")

    def test_custom_values(self):
        step = Step(description="Task", status="done", notes="Completed")
        self.assertEqual(step.status, "done")
        self.assertEqual(step.notes, "Completed")


class TestProgressTracker(unittest.TestCase):
    """Tests for ProgressTracker."""

    def setUp(self):
        self.tracker = ProgressTracker()

    def test_initial_state(self):
        self.assertFalse(self.tracker.has_plan)
        self.assertEqual(self.tracker.steps, [])
        self.assertEqual(self.tracker.format_summary(), "")

    def test_update_plan_creates_steps(self):
        steps = [
            {"description": "Step 1"},
            {"description": "Step 2", "status": "in_progress"},
            {"description": "Step 3", "status": "done", "notes": "Finished"},
        ]
        result = self.tracker.update_plan(steps)
        self.assertTrue(self.tracker.has_plan)
        self.assertEqual(len(self.tracker.steps), 3)
        self.assertEqual(self.tracker.steps[0].status, "pending")
        self.assertEqual(self.tracker.steps[1].status, "in_progress")
        self.assertEqual(self.tracker.steps[2].status, "done")
        self.assertEqual(self.tracker.steps[2].notes, "Finished")
        self.assertIn("[TASK PROGRESS]", result)

    def test_update_plan_replaces_existing(self):
        self.tracker.update_plan([{"description": "Old step"}])
        self.assertEqual(len(self.tracker.steps), 1)

        self.tracker.update_plan([{"description": "New A"}, {"description": "New B"}])
        self.assertEqual(len(self.tracker.steps), 2)
        self.assertEqual(self.tracker.steps[0].description, "New A")

    def test_update_plan_skips_empty_descriptions(self):
        steps = [
            {"description": "Valid"},
            {"description": ""},
            {"description": "   "},
        ]
        self.tracker.update_plan(steps)
        # Empty string is skipped, whitespace-only is kept (it's truthy)
        descriptions = [s.description for s in self.tracker.steps]
        self.assertIn("Valid", descriptions)
        self.assertNotIn("", descriptions)

    def test_update_plan_rejects_invalid_status(self):
        steps = [{"description": "Task", "status": "invalid_status"}]
        self.tracker.update_plan(steps)
        self.assertEqual(self.tracker.steps[0].status, "pending")

    def test_format_summary_status_icons(self):
        steps = [
            {"description": "Done task", "status": "done"},
            {"description": "Active task", "status": "in_progress"},
            {"description": "Pending task", "status": "pending"},
            {"description": "Skipped task", "status": "skipped"},
        ]
        self.tracker.update_plan(steps)
        summary = self.tracker.format_summary()

        self.assertIn("✓", summary)
        self.assertIn("→", summary)
        self.assertIn("○", summary)
        self.assertIn("⊘", summary)
        self.assertIn("[TASK PROGRESS]", summary)
        self.assertIn("Progress: 2/4 steps complete", summary)

    def test_format_summary_shows_next_step(self):
        steps = [
            {"description": "Done", "status": "done"},
            {"description": "Current", "status": "in_progress"},
            {"description": "Later", "status": "pending"},
        ]
        self.tracker.update_plan(steps)
        summary = self.tracker.format_summary()
        self.assertIn("Next: Current", summary)

    def test_format_summary_next_pending_when_no_in_progress(self):
        steps = [
            {"description": "Done", "status": "done"},
            {"description": "Next up", "status": "pending"},
        ]
        self.tracker.update_plan(steps)
        summary = self.tracker.format_summary()
        self.assertIn("Next: Next up", summary)

    def test_format_summary_shows_notes(self):
        steps = [
            {"description": "Task", "status": "done", "notes": "All tests passed"},
        ]
        self.tracker.update_plan(steps)
        summary = self.tracker.format_summary()
        self.assertIn("All tests passed", summary)

    def test_reset_clears_plan(self):
        self.tracker.update_plan([{"description": "Step 1"}])
        self.assertTrue(self.tracker.has_plan)

        self.tracker.reset()
        self.assertFalse(self.tracker.has_plan)
        self.assertEqual(self.tracker.steps, [])
        self.assertEqual(self.tracker.format_summary(), "")

    def test_steps_property_returns_copy(self):
        self.tracker.update_plan([{"description": "Step 1"}])
        steps1 = self.tracker.steps
        steps2 = self.tracker.steps
        self.assertIsNot(steps1, steps2)
        self.assertEqual(steps1, steps2)


class TestRunUpdatePlan(unittest.TestCase):
    """Tests for the run_update_plan handler function."""

    def test_delegates_to_tracker(self):
        tracker = ProgressTracker()
        result = run_update_plan({"steps": [{"description": "Step 1"}]}, tracker)
        self.assertTrue(tracker.has_plan)
        self.assertIn("[TASK PROGRESS]", result)

    def test_empty_steps_returns_error(self):
        tracker = ProgressTracker()
        result = run_update_plan({"steps": []}, tracker)
        self.assertIn("Error", result)
        self.assertFalse(tracker.has_plan)

    def test_missing_steps_key_returns_error(self):
        tracker = ProgressTracker()
        result = run_update_plan({}, tracker)
        self.assertIn("Error", result)


if __name__ == "__main__":
    unittest.main()
