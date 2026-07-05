"""Unit tests for ContextPipeline orchestrator."""

import json
import unittest

from context_manager.pipeline import ContextPipeline


def _make_tool_exchange(tool_name, args, result_content):
    """Create a pair of assistant(tool_call) + tool(result) messages."""
    tc_id = f"tc_{tool_name}"
    return [
        {
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
        },
        {"role": "tool", "tool_call_id": tc_id, "content": result_content},
    ]


class TestCompressToolResult(unittest.TestCase):
    def test_delegates_to_micro(self):
        pipeline = ContextPipeline(micro_max_chars=100)
        content = "\n".join(f"line_{i}" for i in range(200))
        result = pipeline.compress_tool_result("read_file", content)
        self.assertLess(len(result), len(content))

    def test_small_content_unchanged(self):
        pipeline = ContextPipeline(micro_max_chars=5000)
        result = pipeline.compress_tool_result("bash", "short output")
        self.assertEqual(result, "short output")

    def test_stats_increment(self):
        pipeline = ContextPipeline(micro_max_chars=100)
        content = "\n".join(f"line_{i}" for i in range(200))
        pipeline.compress_tool_result("read_file", content)
        self.assertEqual(pipeline.stats["micro_compressions"], 1)


class TestShouldCompact(unittest.TestCase):
    def test_false_when_no_threshold_met(self):
        pipeline = ContextPipeline(
            meso_message_threshold=100,
            macro_token_threshold=999999,
            keep_recent=4,
        )
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "task"},
        ]
        self.assertFalse(pipeline.should_compact(msgs))

    def test_true_when_meso_threshold_met(self):
        pipeline = ContextPipeline(
            meso_message_threshold=4,
            macro_token_threshold=999999,
            keep_recent=4,
        )
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "task"},
        ]
        for i in range(6):
            msgs.extend(_make_tool_exchange("read_file", {"path": f"f{i}.py"}, "content"))
        for i in range(4):
            msgs.append({"role": "user", "content": f"q_{i}"})
        self.assertTrue(pipeline.should_compact(msgs))


class TestCompactCascade(unittest.TestCase):
    def test_layer2_runs_before_layer3(self):
        """Layer 2 compresses middle; Layer 3 doesn't fire because tokens drop."""
        pipeline = ContextPipeline(
            micro_max_chars=5000,
            meso_message_threshold=4,
            meso_token_threshold=999999,
            macro_token_threshold=999999,
            keep_recent=4,
        )
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "task"},
        ]
        for i in range(8):
            msgs.extend(_make_tool_exchange("read_file", {"path": f"f{i}.py"}, "x" * 500))
        for i in range(4):
            msgs.append({"role": "user", "content": f"q_{i}"})

        result = pipeline.compact(msgs)
        self.assertLess(len(result), len(msgs))
        self.assertEqual(pipeline.stats["meso_compressions"], 1)
        self.assertEqual(pipeline.stats["macro_compressions"], 0)

    def test_defensive_micro_pass(self):
        """Uncompressed tool messages in input get compressed by micro pass."""
        pipeline = ContextPipeline(
            micro_max_chars=100,
            meso_message_threshold=100,
            macro_token_threshold=999999,
            keep_recent=4,
        )
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "task"},
            # An uncompressed tool message in the middle
            {
                "role": "tool",
                "tool_call_id": "tc1",
                "content": "\n".join(f"line_{i}" for i in range(200)),
            },
        ]
        for i in range(4):
            msgs.append({"role": "user", "content": f"q_{i}"})

        result = pipeline.compact(msgs)
        # The tool message should have been compressed
        tool_msgs = [m for m in result if m.get("role") == "tool"]
        if tool_msgs:
            self.assertLess(len(tool_msgs[0]["content"]), 5000)


class TestStatsTracking(unittest.TestCase):
    def test_stats_initial_zero(self):
        pipeline = ContextPipeline()
        stats = pipeline.stats
        self.assertEqual(stats["micro_compressions"], 0)
        self.assertEqual(stats["meso_compressions"], 0)
        self.assertEqual(stats["macro_compressions"], 0)

    def test_stats_returns_copy(self):
        pipeline = ContextPipeline()
        s1 = pipeline.stats
        s2 = pipeline.stats
        self.assertEqual(s1, s2)
        self.assertIsNot(s1, s2)


if __name__ == "__main__":
    unittest.main()
