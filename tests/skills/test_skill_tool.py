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
    """Test the load_skill handler closure."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        skills_path = Path(self._tmpdir.name)
        _skill_dir = skills_path / "my-skill"
        _skill_dir.mkdir()
        (_skill_dir / "SKILL.md").write_text(VALID_SKILL, encoding="utf-8")
        self.loader = SkillLoader(skill_dirs=[skills_path])
        self.handler = make_load_skill_handler(self.loader, max_chars=10000)

    def tearDown(self):
        self._tmpdir.cleanup()

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
