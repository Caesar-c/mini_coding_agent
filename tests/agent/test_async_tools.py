"""Tests for async tool implementations."""

import asyncio
import unittest


class TestAsyncBash(unittest.TestCase):
    """Tests for async_run_bash."""

    def test_simple_command(self):
        from agent.async_tools import async_run_bash

        result = asyncio.run(async_run_bash({"command": "echo hello"}))
        self.assertIn("hello", result)

    def test_command_with_stderr(self):
        from agent.async_tools import async_run_bash

        result = asyncio.run(async_run_bash({"command": "echo err >&2"}))
        self.assertIn("STDERR", result)

    def test_dangerous_command_blocked(self):
        from agent.async_tools import async_run_bash

        result = asyncio.run(async_run_bash({"command": "rm -rf /"}))
        self.assertIn("Error", result)
        self.assertIn("Dangerous", result)

    def test_timeout(self):
        from agent.async_tools import async_run_bash

        result = asyncio.run(async_run_bash({"command": "sleep 10", "timeout": 1}))
        self.assertIn("timed out", result)

    def test_exit_code_on_empty_output(self):
        from agent.async_tools import async_run_bash

        result = asyncio.run(async_run_bash({"command": "true"}))
        self.assertIn("exit code 0", result)


class TestAsyncFileOps(unittest.TestCase):
    """Tests for async file operation tools."""

    def test_async_run_file_exists(self):
        from agent.async_tools import async_run_file_exists

        result = asyncio.run(async_run_file_exists({"path": "pyproject.toml"}))
        self.assertIn("exists", result)

    def test_async_run_file_not_exists(self):
        from agent.async_tools import async_run_file_exists

        result = asyncio.run(async_run_file_exists({"path": "nonexistent_file_xyz.txt"}))
        self.assertIn("does not exist", result)

    def test_async_run_list_directory(self):
        from agent.async_tools import async_run_list_directory

        result = asyncio.run(async_run_list_directory({"path": "."}))
        # Should contain at least the project config file
        self.assertIn("pyproject.toml", result)


if __name__ == "__main__":
    unittest.main()
