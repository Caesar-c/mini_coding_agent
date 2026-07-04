"""Unit tests for the /skills slash command in the REPL."""

import unittest
from unittest.mock import MagicMock, patch

from cli.repl import HELP_TEXT, SLASH_COMMANDS, _handle_slash_command
from skills.loader import SkillEntry, SkillLoader


def _make_skill_entry(
    name: str = "test-skill",
    description: str = "A test skill",
    version: str = "1.0",
    tags: list[str] | None = None,
) -> SkillEntry:
    """Helper: create a SkillEntry with sensible defaults."""
    return SkillEntry(
        name=name,
        description=description,
        body="Body content",
        source=f"/fake/path/{name}/SKILL.md",
        version=version,
        author="tester",
        tags=tags or [],
    )


def _make_mock_display() -> MagicMock:
    """Helper: create a mock RichDisplayHandler."""
    display = MagicMock()
    display.console = MagicMock()
    return display


def _make_session_manager_with_agent(
    skill_loader: SkillLoader | None = None,
) -> MagicMock:
    """Helper: create a SessionManager mock with an active agent."""
    agent = MagicMock()
    agent.skill_loader = skill_loader or MagicMock(spec=SkillLoader, count=0)
    manager = MagicMock()
    manager.active = agent
    return manager


class TestSkillsCommandRegistration(unittest.TestCase):
    """Test that /skills is properly registered in slash command lists."""

    def test_skills_in_slash_commands(self):
        self.assertIn("/skills", SLASH_COMMANDS)

    def test_skills_in_help_text(self):
        self.assertIn("/skills", HELP_TEXT)
        self.assertIn("skills", HELP_TEXT.lower())


class TestSkillsCommandNoSession(unittest.TestCase):
    """Test /skills with no active session."""

    def test_no_active_session_shows_error(self):
        display = _make_mock_display()
        manager = MagicMock()
        manager.active = None

        result = _handle_slash_command("/skills", manager, display)

        self.assertFalse(result)
        display.show_error.assert_called_once()


class TestSkillsCommandNoSkillsLoaded(unittest.TestCase):
    """Test /skills when the agent has no skills loaded."""

    def test_empty_skill_loader_shows_info(self):
        display = _make_mock_display()
        loader = MagicMock(spec=SkillLoader)
        loader.count = 0
        manager = _make_session_manager_with_agent(loader)

        result = _handle_slash_command("/skills", manager, display)

        self.assertFalse(result)
        display.show_info.assert_called_once_with("No skills loaded.")


class TestSkillsCommandWithSkills(unittest.TestCase):
    """Test /skills when skills are loaded."""

    def test_renders_table_with_skills(self):
        display = _make_mock_display()

        # Build a real SkillLoader-like object with skills
        entry_a = _make_skill_entry("code-review", "Review code quality", "1.0", ["review"])
        entry_b = _make_skill_entry("git-workflow", "Git branching guide", "2.1", ["git", "vcs"])
        loader = MagicMock(spec=SkillLoader)
        loader.count = 2
        loader.list_names.return_value = ["code-review", "git-workflow"]
        loader.get_skill.side_effect = lambda name: {
            "code-review": entry_a,
            "git-workflow": entry_b,
        }.get(name)

        manager = _make_session_manager_with_agent(loader)

        with patch("rich.table.Table") as MockTable:
            mock_table = MockTable.return_value
            result = _handle_slash_command("/skills", manager, display)

            self.assertFalse(result)
            MockTable.assert_called_once_with(title="Loaded Skills", show_lines=False)
            # 4 columns: Name, Description, Version, Tags
            self.assertEqual(mock_table.add_column.call_count, 4)
            # 2 rows for 2 skills
            self.assertEqual(mock_table.add_row.call_count, 2)
            display.console.print.assert_called_once_with(mock_table)

    def test_renders_skill_without_tags(self):
        display = _make_mock_display()

        entry = _make_skill_entry("minimal", "Minimal skill", "1.0", [])
        loader = MagicMock(spec=SkillLoader)
        loader.count = 1
        loader.list_names.return_value = ["minimal"]
        loader.get_skill.return_value = entry

        manager = _make_session_manager_with_agent(loader)

        with patch("rich.table.Table") as MockTable:
            mock_table = MockTable.return_value
            _handle_slash_command("/skills", manager, display)

            # Check that tags column shows "—" for empty tags
            call_args = mock_table.add_row.call_args_list[0]
            self.assertEqual(call_args[0][3], "—")

    def test_renders_skill_with_empty_version(self):
        display = _make_mock_display()

        entry = _make_skill_entry("no-ver", "No version", "", ["misc"])
        loader = MagicMock(spec=SkillLoader)
        loader.count = 1
        loader.list_names.return_value = ["no-ver"]
        loader.get_skill.return_value = entry

        manager = _make_session_manager_with_agent(loader)

        with patch("rich.table.Table") as MockTable:
            mock_table = MockTable.return_value
            _handle_slash_command("/skills", manager, display)

            # Check that version column shows "—" for empty version
            call_args = mock_table.add_row.call_args_list[0]
            self.assertEqual(call_args[0][2], "—")


if __name__ == "__main__":
    unittest.main()
