"""Tests for Subagent — context-isolated subtask execution."""

import asyncio
import unittest
from unittest.mock import MagicMock, patch


def _make_mock_response(content="", tool_calls=None, has_model_dump=False):
    """Create a mock LLM response mimicking MessageWrapper interface."""
    resp = MagicMock()
    resp.content = content
    resp.role = "assistant"
    resp.tool_calls = tool_calls or []

    if has_model_dump:
        resp.model_dump = MagicMock(
            return_value={
                "role": "assistant",
                "content": content,
                **({"tool_calls": tool_calls} if tool_calls else {}),
            }
        )
    else:
        # Remove model_dump so hasattr check falls through to manual dict path
        del resp.model_dump

    return resp


def _make_tool_call(name="read_file", arguments='{"path": "test.py"}', tc_id="tc_1"):
    """Create a mock tool call object."""
    tc = MagicMock()
    tc.function.name = name
    tc.function.arguments = arguments
    tc.id = tc_id
    return tc


class TestSubagentReturnsSummary(unittest.TestCase):
    """run_subagent returns text summary when LLM responds without tool calls."""

    def test_returns_summary(self):
        asyncio.run(self._run())

    async def _run(self):
        from agent.async_tool_registry import AsyncToolRegistry
        from agent.subagent import SUBAGENT_SYSTEM_PROMPT, run_subagent

        mock_provider = MagicMock()
        mock_provider.chat_completion.return_value = _make_mock_response(
            content="Project uses pytest."
        )

        registry = AsyncToolRegistry()
        result = await run_subagent(
            prompt="What test framework?",
            llm_provider=mock_provider,
            child_tools=registry.definitions,
            child_registry=registry,
            system_prompt=SUBAGENT_SYSTEM_PROMPT,
            max_iterations=5,
            max_output_chars=2000,
        )
        self.assertEqual(result, "Project uses pytest.")
        mock_provider.chat_completion.assert_called_once()


class TestSubagentContextIsolation(unittest.TestCase):
    """Subagent messages do not leak into the parent agent."""

    def test_context_isolation(self):
        asyncio.run(self._run())

    async def _run(self):
        from agent.async_tool_registry import AsyncToolRegistry
        from agent.subagent import SUBAGENT_SYSTEM_PROMPT, run_subagent

        tc = _make_tool_call()

        mock_provider = MagicMock()
        # First call: returns tool_call, second call: returns final text
        mock_provider.chat_completion.side_effect = [
            _make_mock_response(content="", tool_calls=[tc]),
            _make_mock_response(content="Summary of findings."),
        ]

        registry = AsyncToolRegistry()

        # Mock execute to avoid real file system access
        async def mock_execute(tool_name, args):
            return "file content"

        registry.execute = mock_execute

        result = await run_subagent(
            prompt="Explore project",
            llm_provider=mock_provider,
            child_tools=registry.definitions,
            child_registry=registry,
            system_prompt=SUBAGENT_SYSTEM_PROMPT,
            max_iterations=5,
            max_output_chars=2000,
        )

        # Subagent returns only the summary, not intermediate data
        self.assertEqual(result, "Summary of findings.")
        # The LLM was called exactly twice (1 tool call + 1 final response)
        self.assertEqual(mock_provider.chat_completion.call_count, 2)


class TestSubagentNoTaskTool(unittest.TestCase):
    """Subagent's tool definitions do not include 'task'."""

    def test_no_task_tool_in_child_definitions(self):
        from agent.async_tool_registry import AsyncToolRegistry

        registry = AsyncToolRegistry()
        tool_names = [d["function"]["name"] for d in registry.definitions]
        self.assertNotIn("task", tool_names)
        self.assertNotIn("update_plan", tool_names)


class TestSubagentIterationLimit(unittest.TestCase):
    """Subagent stops and returns warning when max_iterations is reached."""

    def test_iteration_limit(self):
        asyncio.run(self._run())

    async def _run(self):
        from agent.async_tool_registry import AsyncToolRegistry
        from agent.subagent import SUBAGENT_SYSTEM_PROMPT, run_subagent

        tc = _make_tool_call()

        mock_provider = MagicMock()
        # Always return a tool call — subagent never finishes naturally
        mock_provider.chat_completion.return_value = _make_mock_response(
            content="", tool_calls=[tc]
        )

        registry = AsyncToolRegistry()

        # Mock execute to avoid real file system access
        async def mock_execute(tool_name, args):
            return "file content"

        registry.execute = mock_execute

        result = await run_subagent(
            prompt="Infinite task",
            llm_provider=mock_provider,
            child_tools=registry.definitions,
            child_registry=registry,
            system_prompt=SUBAGENT_SYSTEM_PROMPT,
            max_iterations=3,
            max_output_chars=2000,
        )

        self.assertIn("iteration limit", result)
        self.assertEqual(mock_provider.chat_completion.call_count, 3)


class TestSubagentOutputTruncation(unittest.TestCase):
    """Subagent output exceeding max_output_chars is truncated."""

    def test_output_truncation(self):
        asyncio.run(self._run())

    async def _run(self):
        from agent.async_tool_registry import AsyncToolRegistry
        from agent.subagent import SUBAGENT_SYSTEM_PROMPT, run_subagent

        long_text = "A" * 5000
        mock_provider = MagicMock()
        mock_provider.chat_completion.return_value = _make_mock_response(content=long_text)

        registry = AsyncToolRegistry()
        result = await run_subagent(
            prompt="Generate long output",
            llm_provider=mock_provider,
            child_tools=registry.definitions,
            child_registry=registry,
            system_prompt=SUBAGENT_SYSTEM_PROMPT,
            max_iterations=5,
            max_output_chars=100,
        )

        self.assertLessEqual(len(result), 120)  # 100 + truncation suffix
        self.assertIn("[truncated]", result)
        self.assertTrue(result.startswith("A" * 100))


class TestTaskToolHandler(unittest.TestCase):
    """task tool handler correctly delegates to run_subagent."""

    def test_handler_calls_run_subagent(self):
        asyncio.run(self._run())

    async def _run(self):
        from agent.subagent import make_task_handler

        mock_agent = MagicMock()
        mock_agent._child_registry = MagicMock()
        mock_agent._child_registry.definitions = []
        mock_agent.skill_loader = MagicMock(count=0)

        with patch("agent.subagent.run_subagent", return_value="subagent result") as mock_run:
            handler = make_task_handler(mock_agent)
            result = await handler({"prompt": "Test task"})

            self.assertEqual(result, "subagent result")
            mock_run.assert_called_once()
            call_kwargs = mock_run.call_args
            self.assertEqual(call_kwargs.kwargs["prompt"], "Test task")


class TestEmptyPromptRejected(unittest.TestCase):
    """task handler returns error for empty prompt."""

    def test_empty_prompt(self):
        asyncio.run(self._run())

    async def _run(self):
        from agent.subagent import make_task_handler

        mock_agent = MagicMock()
        mock_agent.skill_loader = MagicMock(count=0)
        handler = make_task_handler(mock_agent)

        result = await handler({"prompt": ""})
        self.assertIn("Error", result)

        result2 = await handler({})
        self.assertIn("Error", result2)


class TestChildRegistryHasNoTask(unittest.TestCase):
    """_child_registry does not contain task or update_plan tools."""

    def test_child_registry_excludes_parent_tools(self):
        from agent.async_tool_registry import AsyncToolRegistry

        registry = AsyncToolRegistry()
        tool_names = registry.get_tool_names()

        self.assertNotIn("task", tool_names)
        self.assertNotIn("update_plan", tool_names)


class TestChildRegistryHasChildTools(unittest.TestCase):
    """_child_registry contains all 6 base tools."""

    def test_child_registry_has_all_child_tools(self):
        from agent.async_tool_registry import AsyncToolRegistry

        registry = AsyncToolRegistry()
        tool_names = set(registry.get_tool_names())

        expected = {
            "bash",
            "read_file",
            "write_file",
            "list_directory",
            "create_directory",
            "file_exists",
        }
        self.assertEqual(tool_names, expected)


class TestSubagentExecutesTools(unittest.TestCase):
    """Subagent executes tool calls and appends results to its own context."""

    def test_executes_tools_and_returns_summary(self):
        asyncio.run(self._run())

    async def _run(self):
        from agent.async_tool_registry import AsyncToolRegistry
        from agent.subagent import SUBAGENT_SYSTEM_PROMPT, run_subagent

        tc = _make_tool_call(name="bash", arguments='{"command": "echo hello"}', tc_id="tc_42")

        mock_provider = MagicMock()
        # First response: tool call; second response: final summary
        mock_provider.chat_completion.side_effect = [
            _make_mock_response(content="", tool_calls=[tc]),
            _make_mock_response(content="Done: hello"),
        ]

        registry = AsyncToolRegistry()

        # Patch the registry.execute to avoid actually running bash
        async def mock_execute(tool_name, args):
            return "hello"

        registry.execute = mock_execute

        result = await run_subagent(
            prompt="Run echo hello",
            llm_provider=mock_provider,
            child_tools=registry.definitions,
            child_registry=registry,
            system_prompt=SUBAGENT_SYSTEM_PROMPT,
            max_iterations=5,
            max_output_chars=2000,
        )

        self.assertEqual(result, "Done: hello")
        # Verify LLM was called twice
        self.assertEqual(mock_provider.chat_completion.call_count, 2)

        # Verify the second LLM call includes the tool result in messages
        second_call_args = mock_provider.chat_completion.call_args_list[1]
        messages = second_call_args.kwargs.get("messages") or second_call_args[1].get("messages")
        # messages should be: system, user, assistant(tool_call), tool(result)
        tool_messages = [m for m in messages if m.get("role") == "tool"]
        self.assertEqual(len(tool_messages), 1)
        self.assertEqual(tool_messages[0]["content"], "hello")
        self.assertEqual(tool_messages[0]["tool_call_id"], "tc_42")


class TestAsyncToolRegistryExclude(unittest.TestCase):
    """AsyncToolRegistry exclude parameter works correctly."""

    def test_exclude_skips_specified_tools(self):
        from agent.async_tool_registry import AsyncToolRegistry

        registry = AsyncToolRegistry(exclude=["bash", "read_file"])
        tool_names = set(registry.get_tool_names())

        self.assertNotIn("bash", tool_names)
        self.assertNotIn("read_file", tool_names)
        self.assertIn("write_file", tool_names)
        self.assertIn("list_directory", tool_names)

    def test_exclude_none_registers_all(self):
        from agent.async_tool_registry import AsyncToolRegistry

        registry = AsyncToolRegistry(exclude=None)
        self.assertEqual(len(registry.get_tool_names()), 6)

    def test_exclude_empty_list_registers_all(self):
        from agent.async_tool_registry import AsyncToolRegistry

        registry = AsyncToolRegistry(exclude=[])
        self.assertEqual(len(registry.get_tool_names()), 6)

    def test_default_no_exclude(self):
        from agent.async_tool_registry import AsyncToolRegistry

        registry = AsyncToolRegistry()
        self.assertEqual(len(registry.get_tool_names()), 6)


if __name__ == "__main__":
    unittest.main()
