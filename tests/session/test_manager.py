"""Tests for SessionManager."""

import unittest
from unittest.mock import MagicMock, patch


class TestSessionManager(unittest.TestCase):
    """Tests for SessionManager."""

    def _make_manager(self):
        from session.manager import SessionManager

        return SessionManager()

    @patch("session.manager.AsyncAgent")
    def test_create_session(self, mock_agent_cls):
        mock_agent_cls.return_value = MagicMock()
        manager = self._make_manager()
        agent = manager.create("test")
        self.assertIsNotNone(agent)
        self.assertEqual(manager.active_name, "test")
        self.assertEqual(manager.list_sessions(), ["test"])

    @patch("session.manager.AsyncAgent")
    def test_switch_session(self, mock_agent_cls):
        mock_agent_cls.return_value = MagicMock()
        manager = self._make_manager()
        manager.create("s1")
        manager.create("s2")
        manager.switch("s1")
        self.assertEqual(manager.active_name, "s1")

    @patch("session.manager.AsyncAgent")
    def test_switch_nonexistent_raises(self, mock_agent_cls):
        manager = self._make_manager()
        with self.assertRaises(KeyError):
            manager.switch("nonexistent")

    @patch("session.manager.AsyncAgent")
    def test_create_duplicate_raises(self, mock_agent_cls):
        mock_agent_cls.return_value = MagicMock()
        manager = self._make_manager()
        manager.create("dup")
        with self.assertRaises(ValueError):
            manager.create("dup")

    @patch("session.manager.AsyncAgent")
    def test_remove_session(self, mock_agent_cls):
        mock_agent_cls.return_value = MagicMock()
        manager = self._make_manager()
        manager.create("to_remove")
        manager.remove("to_remove")
        self.assertEqual(manager.list_sessions(), [])
        self.assertIsNone(manager.active)

    @patch("session.manager.AsyncAgent")
    def test_remove_active_switches_to_another(self, mock_agent_cls):
        mock_agent_cls.return_value = MagicMock()
        manager = self._make_manager()
        manager.create("s1")
        manager.create("s2")
        # s2 is active
        manager.remove("s2")
        # active should now be s1
        self.assertEqual(manager.active_name, "s1")

    def test_active_none_when_empty(self):
        manager = self._make_manager()
        self.assertIsNone(manager.active)
        self.assertIsNone(manager.active_name)

    @patch("session.manager.AsyncAgent")
    def test_get_session(self, mock_agent_cls):
        mock_agent_cls.return_value = MagicMock()
        manager = self._make_manager()
        manager.create("s1")
        agent = manager.get("s1")
        self.assertIsNotNone(agent)
        self.assertIsNone(manager.get("nonexistent"))

    @patch("session.manager.AsyncAgent")
    def test_list_sessions(self, mock_agent_cls):
        mock_agent_cls.return_value = MagicMock()
        manager = self._make_manager()
        manager.create("a")
        manager.create("b")
        manager.create("c")
        self.assertEqual(manager.list_sessions(), ["a", "b", "c"])


if __name__ == "__main__":
    unittest.main()
