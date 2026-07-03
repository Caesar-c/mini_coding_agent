"""Integration tests: SkillLoader + tool registry + agent components."""

import asyncio
import tempfile
import unittest
from pathlib import Path

from skills.loader import SkillLoader

VALID_SKILL = """\
---
name: integration-test
description: Integration test skill
---

# Integration Test Body
"""


class TestBuildSkillLoader(unittest.TestCase):
    """Test build_skill_loader and SkillLoader with custom directories."""

    def test_load_skills_from_custom_dir(self):
        """SkillLoader loads skills from a custom directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = Path(tmpdir) / "env-skill"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(VALID_SKILL, encoding="utf-8")

            loader = SkillLoader(skill_dirs=[Path(tmpdir)])
            self.assertGreaterEqual(loader.count, 1)
            self.assertIn("integration-test", loader.list_names())

    def test_build_with_empty_skill_dirs(self):
        """build_skill_loader handles missing dirs gracefully."""
        # Build with a nonexistent directory — should not raise
        loader = SkillLoader(skill_dirs=[Path("/nonexistent/path")])
        self.assertEqual(loader.count, 0)
        self.assertIsInstance(loader, SkillLoader)

    def test_load_from_multiple_dirs(self):
        """SkillLoader loads skills from multiple directories."""
        with tempfile.TemporaryDirectory() as tmpdir1, tempfile.TemporaryDirectory() as tmpdir2:
            skill_dir = Path(tmpdir1) / "multi-skill"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(VALID_SKILL, encoding="utf-8")

            loader = SkillLoader(skill_dirs=[Path(tmpdir1), Path(tmpdir2)])
            self.assertGreaterEqual(loader.count, 1)


class TestSkillInRegistry(unittest.TestCase):
    """Test load_skill tool registration in AsyncToolRegistry."""

    def test_skill_in_tool_registry(self):
        """load_skill tool is registered and executable in AsyncToolRegistry."""
        asyncio.run(self._test_skill_in_tool_registry())

    async def _test_skill_in_tool_registry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = Path(tmpdir) / "reg-skill"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(VALID_SKILL, encoding="utf-8")

            loader = SkillLoader(skill_dirs=[Path(tmpdir)])
            from agent.async_tool_registry import AsyncToolRegistry
            from skills.skill_tool import (
                LOAD_SKILL_TOOL_DEFINITION,
                make_load_skill_handler,
            )

            registry = AsyncToolRegistry()
            handler = make_load_skill_handler(loader)
            registry.register(LOAD_SKILL_TOOL_DEFINITION, handler)

            self.assertIn("load_skill", registry.get_tool_names())
            result = await registry.execute("load_skill", {"name": "integration-test"})
            self.assertIn("Integration Test Body", result)


class TestChildRegistryHasLoadSkill(unittest.TestCase):
    """Test that child registry (for subagents) includes load_skill."""

    def test_child_registry_has_load_skill(self):
        """Child registry includes load_skill but excludes task/update_plan."""
        asyncio.run(self._test_child_registry())

    async def _test_child_registry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = Path(tmpdir) / "child-skill"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(VALID_SKILL, encoding="utf-8")

            loader = SkillLoader(skill_dirs=[Path(tmpdir)])
            from agent.async_tool_registry import AsyncToolRegistry
            from skills.skill_tool import (
                LOAD_SKILL_TOOL_DEFINITION,
                make_load_skill_handler,
            )

            child_registry = AsyncToolRegistry(exclude=["task", "update_plan"])
            child_registry.register(
                LOAD_SKILL_TOOL_DEFINITION,
                make_load_skill_handler(loader),
            )

            self.assertIn("load_skill", child_registry.get_tool_names())
            self.assertNotIn("task", child_registry.get_tool_names())
            self.assertNotIn("update_plan", child_registry.get_tool_names())

            # Verify it actually works
            result = await child_registry.execute("load_skill", {"name": "integration-test"})
            self.assertIn("Integration Test Body", result)


if __name__ == "__main__":
    unittest.main()
