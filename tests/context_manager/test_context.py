"""Tests for the ContextCompactor."""

import unittest

from context_manager.context import ContextCompactor


class TestContextCompactor(unittest.TestCase):
    """Tests for ContextCompactor."""

    def setUp(self):
        self.compactor = ContextCompactor(max_messages=10, keep_recent=4)

    def _make_messages(self, count):
        """Build a synthetic message list for testing."""
        messages = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "Original task"},
        ]
        for i in range(count - 2):
            if i % 2 == 0:
                messages.append(
                    {
                        "role": "assistant",
                        "content": f"Working on step {i}",
                        "tool_calls": [
                            {
                                "id": f"call_{i}",
                                "type": "function",
                                "function": {
                                    "name": "bash",
                                    "arguments": '{"command": "ls"}',
                                },
                            }
                        ],
                    }
                )
            else:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": f"call_{i - 1}",
                        "content": "x" * 300,  # Long enough to compact
                    }
                )
        return messages

    def test_should_compact_below_threshold(self):
        messages = self._make_messages(8)
        self.assertFalse(self.compactor.should_compact(messages))

    def test_should_compact_at_threshold(self):
        messages = self._make_messages(10)
        self.assertFalse(self.compactor.should_compact(messages))

    def test_should_compact_above_threshold(self):
        messages = self._make_messages(12)
        self.assertTrue(self.compactor.should_compact(messages))

    def test_compact_preserves_head_and_tail(self):
        messages = self._make_messages(16)
        result = self.compactor.compact(messages)

        # System prompt preserved
        self.assertEqual(result[0]["role"], "system")
        self.assertEqual(result[0]["content"], "System prompt")

        # First user message preserved
        self.assertEqual(result[1]["role"], "user")
        self.assertEqual(result[1]["content"], "Original task")

        # Tail messages preserved verbatim
        tail = result[-self.compactor.keep_recent :]
        original_tail = messages[-self.compactor.keep_recent :]
        for compacted, original in zip(tail, original_tail, strict=False):
            self.assertEqual(compacted, original)

    def test_compact_reduces_message_count(self):
        messages = self._make_messages(20)
        original_count = len(messages)
        result = self.compactor.compact(messages)
        self.assertLess(len(result), original_count)

    def test_compact_removes_tool_messages_from_middle(self):
        messages = self._make_messages(16)
        result = self.compactor.compact(messages)

        # Tool result messages should be removed from the middle
        middle = result[2 : len(result) - self.compactor.keep_recent]
        tool_in_middle = [m for m in middle if m.get("role") == "tool"]
        self.assertEqual(len(tool_in_middle), 0)

        # Assistant messages in the middle should have tool_calls stripped
        assistant_in_middle = [m for m in middle if m.get("role") == "assistant"]
        for msg in assistant_in_middle:
            self.assertNotIn("tool_calls", msg)

    def test_compact_preserves_tool_results_in_tail(self):
        """Tool result messages in the tail (recent) are kept verbatim."""
        messages = [
            {"role": "system", "content": "System"},
            {"role": "user", "content": "Task"},
        ]
        # Add enough messages to trigger compaction
        for i in range(15):
            messages.append(
                {
                    "role": "assistant",
                    "content": f"Step {i}",
                    "tool_calls": [
                        {
                            "id": f"call_{i}",
                            "type": "function",
                            "function": {
                                "name": "bash",
                                "arguments": '{"command": "ls"}',
                            },
                        }
                    ],
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": f"call_{i}",
                    "content": "result",
                }
            )

        result = self.compactor.compact(messages)
        # Tool results in the tail should be preserved
        tail = result[-self.compactor.keep_recent :]
        tool_in_tail = [m for m in tail if m.get("role") == "tool"]
        self.assertGreater(len(tool_in_tail), 0)
        for msg in tool_in_tail:
            self.assertEqual(msg["content"], "result")

    def test_compact_idempotent(self):
        messages = self._make_messages(16)
        result1 = self.compactor.compact(messages)
        # If result1 is still above threshold, compact again
        if self.compactor.should_compact(result1):
            result2 = self.compactor.compact(result1)
            # Second compaction should produce same or smaller result
            self.assertLessEqual(len(result2), len(result1))
        else:
            # Already below threshold, compact returns unchanged
            result2 = self.compactor.compact(result1)
            self.assertIs(result2, result1)

    def test_compact_returns_original_when_too_small(self):
        messages = self._make_messages(5)
        result = self.compactor.compact(messages)
        self.assertIs(result, messages)

    def test_compact_returns_original_when_head_plus_tail_covers_all(self):
        # Only 7 messages with max=10, keep=4 → 2+4=6, middle=1
        messages = self._make_messages(7)
        result = self.compactor.compact(messages)
        # If compaction can't reduce count, return original
        self.assertIs(result, messages)

    def test_custom_constructor_params(self):
        compactor = ContextCompactor(max_messages=50, keep_recent=20)
        self.assertEqual(compactor.max_messages, 50)
        self.assertEqual(compactor.keep_recent, 20)

    def test_default_constructor_params(self):
        compactor = ContextCompactor()
        self.assertEqual(compactor.max_messages, 40)
        self.assertEqual(compactor.keep_recent, 12)


if __name__ == "__main__":
    unittest.main()
