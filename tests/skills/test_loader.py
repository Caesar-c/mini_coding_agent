"""Unit tests for SkillLoader and SkillEntry."""

import tempfile
import unittest
from pathlib import Path

from config import PROJECT_ROOT
from skills.loader import SkillLoader


def _write_skill(parent: Path, dir_name: str, content: str) -> Path:
    """Helper: create a skill directory with SKILL.md content."""
    skill_dir = parent / dir_name
    skill_dir.mkdir(parents=True, exist_ok=True)
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
        self._tmpdir = tempfile.TemporaryDirectory()
        self.skills_path = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_parse_valid_skill(self):
        _write_skill(self.skills_path, "test-skill", VALID_SKILL)
        loader = SkillLoader(skill_dirs=[self.skills_path])
        self.assertEqual(loader.count, 1)
        names = loader.list_names()
        self.assertEqual(names, ["test-skill"])
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
        self._tmpdir = tempfile.TemporaryDirectory()
        base = Path(self._tmpdir.name)
        self.dir_a = base / "a"
        self.dir_b = base / "b"
        self.dir_a.mkdir()
        self.dir_b.mkdir()

    def tearDown(self):
        self._tmpdir.cleanup()

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
        self._tmpdir = tempfile.TemporaryDirectory()
        self.skills_path = Path(self._tmpdir.name)
        _write_skill(self.skills_path, "test-skill", VALID_SKILL)
        self.loader = SkillLoader(skill_dirs=[self.skills_path])

    def tearDown(self):
        self._tmpdir.cleanup()

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


class TestSkillLoaderDefaultDirs(unittest.TestCase):
    """Test default skill directory resolution."""

    def test_default_dirs_uses_project_root_skills(self):
        dirs = SkillLoader._default_dirs()
        project_skills = PROJECT_ROOT / "skills"
        if project_skills.is_dir():
            self.assertIn(project_skills, dirs)
            self.assertEqual(dirs[0], project_skills)


if __name__ == "__main__":
    unittest.main()
