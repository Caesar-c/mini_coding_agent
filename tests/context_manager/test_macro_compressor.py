"""Unit tests for Layer 3: MacroCompressor."""

import unittest
from unittest.mock import MagicMock

from context_manager.macro_compressor import MacroCompressor
from context_manager.tracker import ProgressTracker


def _make_messages(n_middle=30, keep_recent=12):
    """Build synthetic conversation with large middle section."""
    msgs = [
        {"role": "system", "content": "You are a coding assistant."},
        {"role": "user", "content": "Refactor the auth module."},
    ]
    for i in range(n_middle):
        if i % 2 == 0:
            msgs.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"tc_{i}",
                            "function": {
                                "name": "read_file",
                                "arguments": f'{{"path": "src/file_{i}.py"}}',
                            },
                        }
                    ],
                }
            )
        else:
            msgs.append(
                {
                    "role": "tool",
                    "tool_call_id": f"tc_{i-1}",
                    "content": "x" * 2000,
                }
            )
    # Tail
    for i in range(keep_recent):
        msgs.append({"role": "user", "content": f"recent_{i}"})
    # Ensure tail is exactly keep_recent
    tail_start = len(msgs) - keep_recent
    msgs = msgs[:2] + msgs[2:tail_start] + msgs[tail_start:]
    return msgs


class TestShouldCompress(unittest.TestCase):
    def test_no_llm_provider_returns_false(self):
        mc = MacroCompressor(token_threshold=100, llm_provider=None)
        msgs = _make_messages()
        self.assertFalse(mc.should_compress(msgs))

    def test_below_threshold_returns_false(self):
        mock_provider = MagicMock()
        mc = MacroCompressor(token_threshold=999999, llm_provider=mock_provider)
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
        ]
        self.assertFalse(mc.should_compress(msgs))

    def test_above_threshold_returns_true(self):
        mock_provider = MagicMock()
        mc = MacroCompressor(token_threshold=10, llm_provider=mock_provider)
        msgs = [
            {"role": "system", "content": "x" * 100},
            {"role": "user", "content": "x" * 100},
        ]
        self.assertTrue(mc.should_compress(msgs))


class TestBuildHistoryDigest(unittest.TestCase):
    def test_format_markers(self):
        mc = MacroCompressor()
        msgs = [
            {"role": "system", "content": "some system msg"},
            {"role": "user", "content": "user question"},
            {
                "role": "assistant",
                "content": "thinking...",
                "tool_calls": [
                    {
                        "id": "tc1",
                        "function": {"name": "bash", "arguments": '{"command": "ls"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "tc1", "content": "file1\nfile2"},
        ]
        digest = mc._build_history_digest(msgs)
        self.assertIn("[SYSTEM]", digest)
        self.assertIn("[USER]", digest)
        self.assertIn("[ASSISTANT]", digest)
        self.assertIn("[TOOL_CALL]", digest)
        self.assertIn("[TOOL_RESULT]", digest)

    def test_task_progress_skipped(self):
        mc = MacroCompressor()
        msgs = [
            {"role": "system", "content": "[TASK PROGRESS]\n○ Step 1"},
            {"role": "user", "content": "question"},
        ]
        digest = mc._build_history_digest(msgs)
        self.assertNotIn("TASK PROGRESS", digest)
        self.assertIn("[USER]", digest)

    def test_long_content_truncated(self):
        mc = MacroCompressor()
        msgs = [{"role": "user", "content": "x" * 1000}]
        digest = mc._build_history_digest(msgs)
        # User messages capped at 500 + prefix
        self.assertLess(len(digest), 600)


class TestCompress(unittest.TestCase):
    def test_with_mock_llm(self):
        mock_provider = MagicMock()
        mock_response = MagicMock()
        mock_response.content = "[CONTEXT SUMMARY]\n## Completed Work\nRefactored auth."
        mock_response.tool_calls = []
        mock_provider.chat_completion.return_value = mock_response

        mc = MacroCompressor(
            token_threshold=10,
            keep_recent=4,
            llm_provider=mock_provider,
        )
        msgs = _make_messages(n_middle=20, keep_recent=4)
        result = mc.compress(msgs)

        # System prompt preserved
        self.assertEqual(result[0]["role"], "system")
        self.assertEqual(result[0]["content"], "You are a coding assistant.")
        # Summary inserted
        self.assertIn("[CONTEXT SUMMARY]", result[1]["content"])
        # Original task preserved
        self.assertEqual(result[2]["content"], "Refactor the auth module.")
        # Recent preserved
        tail = result[-4:]
        self.assertEqual(len(tail), 4)

    def test_short_conversation_noop(self):
        mock_provider = MagicMock()
        mc = MacroCompressor(token_threshold=10, keep_recent=4, llm_provider=mock_provider)
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "task"},
        ]
        result = mc.compress(msgs)
        self.assertEqual(result, msgs)

    def test_llm_failure_fallback(self):
        mock_provider = MagicMock()
        mock_provider.chat_completion.side_effect = RuntimeError("API down")

        mc = MacroCompressor(
            token_threshold=10,
            keep_recent=4,
            llm_provider=mock_provider,
        )
        msgs = _make_messages(n_middle=20, keep_recent=4)
        result = mc.compress(msgs)

        # Fallback: system + first_user + recent
        self.assertEqual(result[0]["role"], "system")
        self.assertEqual(result[1]["content"], "Refactor the auth module.")
        self.assertEqual(len(result[-4:]), 4)

    def test_progress_tracker_integration(self):
        mock_provider = MagicMock()
        mock_response = MagicMock()
        mock_response.content = "[CONTEXT SUMMARY]\n## Completed Work\nDone."
        mock_response.tool_calls = []
        mock_provider.chat_completion.return_value = mock_response

        tracker = ProgressTracker()
        tracker.update_plan(
            [
                {"description": "Read files", "status": "done"},
                {"description": "Refactor", "status": "in_progress"},
            ]
        )

        mc = MacroCompressor(
            token_threshold=10,
            keep_recent=4,
            llm_provider=mock_provider,
            progress_tracker=tracker,
        )
        msgs = _make_messages(n_middle=20, keep_recent=4)
        result = mc.compress(msgs)

        # Progress summary should be injected
        progress_msgs = [m for m in result if m.get("content", "").startswith("[TASK PROGRESS]")]
        self.assertGreater(len(progress_msgs), 0)

    def test_summary_prefix_auto_added(self):
        mock_provider = MagicMock()
        mock_response = MagicMock()
        mock_response.content = "## Completed Work\nSomething done."
        mock_response.tool_calls = []
        mock_provider.chat_completion.return_value = mock_response

        mc = MacroCompressor(token_threshold=10, keep_recent=4, llm_provider=mock_provider)
        msgs = _make_messages(n_middle=20, keep_recent=4)
        result = mc.compress(msgs)
        self.assertIn("[CONTEXT SUMMARY]", result[1]["content"])


if __name__ == "__main__":
    unittest.main()
