"""Unit tests for Layer 2: MesoCompressor."""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock

from context_manager.meso_compressor import MesoCompressor


def _make_tool_exchange(tool_name, args, result_content):
    """Create a pair of assistant(tool_call) + tool(result) messages."""
    tc_id = f"tc_{tool_name}"
    assistant_msg = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": tc_id,
                "function": {
                    "name": tool_name,
                    "arguments": json.dumps(args),
                },
            }
        ],
    }
    tool_msg = {
        "role": "tool",
        "tool_call_id": tc_id,
        "content": result_content,
    }
    return [assistant_msg, tool_msg]


def _make_conversation(middle_exchanges=10, keep_recent=12):
    """Build a synthetic conversation with tool exchanges in the middle."""
    msgs = [
        {"role": "system", "content": "You are a coding assistant."},
        {"role": "user", "content": "Refactor the auth module."},
    ]
    for i in range(middle_exchanges):
        msgs.extend(
            _make_tool_exchange(
                "read_file",
                {"path": f"src/file_{i}.py"},
                f"class Foo{i}:\n" + "    pass\n" * 50,
            )
        )
    # Add recent tail messages
    for i in range(keep_recent):
        msgs.append({"role": "user", "content": f"recent question {i}"})
        msgs.append({"role": "assistant", "content": f"recent answer {i}"})
    # Trim tail to exactly keep_recent
    tail_start = len(msgs) - keep_recent
    msgs = msgs[:2] + msgs[2:tail_start] + msgs[tail_start:]
    return msgs


class TestGroupToolExchanges(unittest.TestCase):
    def test_consecutive_tool_pairs(self):
        mc = MesoCompressor()
        msgs = []
        msgs.extend(_make_tool_exchange("bash", {"command": "ls"}, "output"))
        msgs.extend(_make_tool_exchange("read_file", {"path": "f.py"}, "content"))
        groups = mc._group_tool_exchanges(msgs)
        # All 4 messages are consecutive tool exchanges → 1 group
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["type"], "tool_exchange")
        self.assertEqual(len(groups[0]["messages"]), 4)

    def test_mixed_messages(self):
        mc = MesoCompressor()
        msgs = []
        msgs.extend(_make_tool_exchange("bash", {"command": "ls"}, "output"))
        msgs.append({"role": "assistant", "content": "Now I'll read the file."})
        msgs.extend(_make_tool_exchange("read_file", {"path": "f.py"}, "content"))
        groups = mc._group_tool_exchanges(msgs)
        # tool_exchange(2) + other(1) + tool_exchange(2) = 3 groups
        self.assertEqual(len(groups), 3)
        self.assertEqual(groups[0]["type"], "tool_exchange")
        self.assertEqual(groups[1]["type"], "other")
        self.assertEqual(groups[2]["type"], "tool_exchange")


class TestRuleBasedSummarize(unittest.TestCase):
    def setUp(self):
        self.mc = MesoCompressor()

    def test_bash_fact(self):
        msgs = _make_tool_exchange("bash", {"command": "pytest tests/"}, "12 passed")
        summary = self.mc._rule_based_summarize(msgs)
        self.assertIn("ran `pytest tests/`", summary)

    def test_read_file_fact(self):
        msgs = _make_tool_exchange(
            "read_file", {"path": "src/auth.py"}, "class Auth:\n" + "    pass\n" * 10
        )
        summary = self.mc._rule_based_summarize(msgs)
        self.assertIn("read `src/auth.py`", summary)

    def test_write_file_fact(self):
        msgs = _make_tool_exchange(
            "write_file",
            {"path": "src/utils.py", "content": "x" * 1234},
            "Successfully wrote 1234 chars",
        )
        summary = self.mc._rule_based_summarize(msgs)
        self.assertIn("wrote 1234 chars to `src/utils.py`", summary)

    def test_list_directory_fact(self):
        msgs = _make_tool_exchange("list_directory", {"path": "src/"}, "file1.py\nfile2.py")
        summary = self.mc._rule_based_summarize(msgs)
        self.assertIn("listed `src/`", summary)

    def test_load_skill_fact(self):
        msgs = _make_tool_exchange("load_skill", {"name": "code-review"}, "# Code Review Skill...")
        summary = self.mc._rule_based_summarize(msgs)
        self.assertIn("loaded skill `code-review`", summary)

    def test_outcome_appended(self):
        msgs = _make_tool_exchange(
            "read_file", {"path": "src/main.py"}, "line1\nline2\nline3\nline4\nline5\nline6\nline7"
        )
        summary = self.mc._rule_based_summarize(msgs)
        self.assertIn("→", summary)
        self.assertIn("lines", summary)

    def test_error_outcome(self):
        msgs = _make_tool_exchange("bash", {"command": "npm install"}, "Error: package not found")
        summary = self.mc._rule_based_summarize(msgs)
        self.assertIn("error:", summary)


class TestExtractOutcome(unittest.TestCase):
    def setUp(self):
        self.mc = MesoCompressor()

    def test_empty_content(self):
        self.assertEqual(self.mc._extract_outcome(""), "")

    def test_error_prefix(self):
        result = self.mc._extract_outcome("Error: file not found")
        self.assertTrue(result.startswith("error:"))

    def test_exit_code(self):
        result = self.mc._extract_outcome("some output\nexit code 0")
        self.assertIn("exit code", result)

    def test_line_count(self):
        result = self.mc._extract_outcome("a\nb\nc\nd\ne\nf\ng")
        self.assertIn("lines", result)

    def test_write_success(self):
        result = self.mc._extract_outcome("Successfully wrote 500 chars to file.py")
        self.assertIn("Successfully wrote", result)

    def test_first_line_fallback(self):
        result = self.mc._extract_outcome("short result\nmore stuff")
        self.assertEqual(result, "short result")


class TestShouldCompress(unittest.TestCase):
    def test_below_threshold(self):
        mc = MesoCompressor(meso_message_threshold=20, keep_recent=4)
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "task"},
        ]
        # Add 10 middle messages + 4 tail
        for _ in range(5):
            msgs.extend(_make_tool_exchange("bash", {"command": "ls"}, "out"))
        for _ in range(4):
            msgs.append({"role": "user", "content": "q"})
        self.assertFalse(mc.should_compress(msgs))

    def test_above_threshold(self):
        mc = MesoCompressor(meso_message_threshold=10, keep_recent=4)
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "task"},
        ]
        for _ in range(10):
            msgs.extend(_make_tool_exchange("bash", {"command": "ls"}, "out"))
        for _ in range(4):
            msgs.append({"role": "user", "content": "q"})
        self.assertTrue(mc.should_compress(msgs))


class TestCompress(unittest.TestCase):
    def test_preserves_head_and_tail(self):
        mc = MesoCompressor(meso_message_threshold=4, keep_recent=4)
        msgs = [
            {"role": "system", "content": "SYSTEM PROMPT"},
            {"role": "user", "content": "ORIGINAL TASK"},
        ]
        for i in range(6):
            msgs.extend(_make_tool_exchange("read_file", {"path": f"f{i}.py"}, "content"))
        for i in range(4):
            msgs.append({"role": "user", "content": f"recent_{i}"})

        result = asyncio.run(mc.compress(msgs))
        self.assertEqual(result[0]["content"], "SYSTEM PROMPT")
        self.assertEqual(result[1]["content"], "ORIGINAL TASK")
        # Tail preserved
        tail_contents = [m["content"] for m in result[-4:]]
        self.assertIn("recent_0", tail_contents)
        self.assertIn("recent_3", tail_contents)

    def test_reduces_message_count(self):
        mc = MesoCompressor(meso_message_threshold=4, keep_recent=4)
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "task"},
        ]
        for i in range(10):
            msgs.extend(_make_tool_exchange("bash", {"command": f"cmd_{i}"}, "output"))
        for i in range(4):
            msgs.append({"role": "user", "content": f"q_{i}"})

        result = asyncio.run(mc.compress(msgs))
        self.assertLess(len(result), len(msgs))

    def test_summary_has_prefix(self):
        mc = MesoCompressor(meso_message_threshold=4, keep_recent=4)
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "task"},
        ]
        for i in range(6):
            msgs.extend(_make_tool_exchange("read_file", {"path": f"f{i}.py"}, "content"))
        for i in range(4):
            msgs.append({"role": "user", "content": f"q_{i}"})

        result = asyncio.run(mc.compress(msgs))
        summaries = [m for m in result if m.get("content", "").startswith("[SUMMARY]")]
        self.assertGreater(len(summaries), 0)

    def test_short_conversation_noop(self):
        mc = MesoCompressor(keep_recent=4)
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "answer"},
        ]
        result = asyncio.run(mc.compress(msgs))
        self.assertEqual(result, msgs)


class TestLLMSummarize(unittest.TestCase):
    def test_llm_fallback_on_error(self):
        """When LLM fails, rule-based summarization is used as fallback."""
        mock_provider = AsyncMock()
        mock_provider.chat_completion.side_effect = RuntimeError("API error")

        mc = MesoCompressor(
            meso_message_threshold=2,
            keep_recent=2,
            use_llm=True,
            llm_provider=mock_provider,
        )
        msgs = _make_tool_exchange("bash", {"command": "ls"}, "file1\nfile2")
        # Should not raise, should fallback to rule-based
        summary = asyncio.run(mc._summarize_group(msgs))
        self.assertIn("ran `ls`", summary)


if __name__ == "__main__":
    unittest.main()


class TestParallelToolResults(unittest.TestCase):
    """Regression: parallel tool calls must have outcomes matched by tool_call_id."""

    def setUp(self):
        self.mc = MesoCompressor()

    def test_parallel_results_attributed_correctly(self):
        # One assistant message with 3 parallel tool calls
        assistant_msg = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "tc_a",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path": "a.py"}',
                    },
                },
                {
                    "id": "tc_b",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path": "b.py"}',
                    },
                },
                {
                    "id": "tc_c",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path": "c.py"}',
                    },
                },
            ],
        }
        # Three tool results
        tool_msgs = [
            {
                "role": "tool",
                "tool_call_id": "tc_a",
                "content": "a\nb\nc\nd\ne\nf",
            },
            {
                "role": "tool",
                "tool_call_id": "tc_b",
                "content": "Error: not found",
            },
            {
                "role": "tool",
                "tool_call_id": "tc_c",
                "content": "x\ny\nz\nw\nv\nu\nt\ns",
            },
        ]
        summary = self.mc._rule_based_summarize([assistant_msg] + tool_msgs)
        # Each fact should have its own outcome
        self.assertIn("read `a.py` →", summary)
        self.assertIn("read `b.py` → error:", summary)
        self.assertIn("read `c.py` →", summary)
