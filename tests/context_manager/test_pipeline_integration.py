"""End-to-end integration tests for ContextPipeline.

Builds realistic conversations and verifies the full three-layer
compression pipeline reduces message count while preserving critical context.
"""

import json
import unittest

from context_manager.pipeline import ContextPipeline
from context_manager.task_graph import TaskGraphManager


def _make_realistic_conversation(
    n_file_reads=8,
    n_bash_runs=6,
    n_writes=5,
    keep_recent=12,
):
    """Build a realistic 60+ message conversation.

    Structure:
    - messages[0]: system prompt
    - messages[1]: original user task
    - messages[2..middle]: file reads, bash commands, writes, plan updates
    - messages[-keep_recent:]: recent user/assistant exchanges
    """
    msgs = [
        {"role": "system", "content": "You are a helpful coding assistant."},
        {"role": "user", "content": "Refactor the auth module to use bcrypt."},
    ]

    # File reads (large content)
    for i in range(n_file_reads):
        file_lines = [f"# src/file_{i}.py"] + [f"def func_{i}_{j}():" for j in range(50)]
        msgs.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"tc_read_{i}",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": f"src/file_{i}.py"}),
                        },
                    }
                ],
            }
        )
        msgs.append(
            {
                "role": "tool",
                "tool_call_id": f"tc_read_{i}",
                "content": "\n".join(file_lines),
            }
        )

    # Bash commands
    for i in range(n_bash_runs):
        msgs.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"tc_bash_{i}",
                        "function": {
                            "name": "bash",
                            "arguments": json.dumps({"command": f"pytest tests/test_{i}.py"}),
                        },
                    }
                ],
            }
        )
        msgs.append(
            {
                "role": "tool",
                "tool_call_id": f"tc_bash_{i}",
                "content": "12 passed, 0 failed\nexit code 0",
            }
        )

    # Writes
    for i in range(n_writes):
        content = f"# Modified file {i}\n" + "x = 1\n" * 100
        msgs.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"tc_write_{i}",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps(
                                {"path": f"src/out_{i}.py", "content": content}
                            ),
                        },
                    }
                ],
            }
        )
        msgs.append(
            {
                "role": "tool",
                "tool_call_id": f"tc_write_{i}",
                "content": f"Successfully wrote {len(content)} chars to src/out_{i}.py",
            }
        )

    # Plan update
    msgs.append(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "tc_plan",
                    "function": {
                        "name": "create_plan",
                        "arguments": json.dumps(
                            {
                                "tasks": [
                                    {
                                        "description": "Read files",
                                    },
                                    {
                                        "description": "Run tests",
                                        "depends_on": ["T1"],
                                    },
                                    {
                                        "description": "Refactor auth",
                                        "depends_on": ["T2"],
                                    },
                                    {
                                        "description": "Update docs",
                                        "depends_on": ["T3"],
                                    },
                                ]
                            }
                        ),
                    },
                }
            ],
        }
    )
    msgs.append({"role": "tool", "tool_call_id": "tc_plan", "content": "[TASK PROGRESS]..."})

    # Recent tail (user questions + assistant answers)
    for i in range(keep_recent // 2):
        msgs.append({"role": "user", "content": f"How about function {i}?"})
        msgs.append(
            {
                "role": "assistant",
                "content": f"I've updated function {i} to use bcrypt.",
            }
        )

    return msgs


class TestPipelineIntegration(unittest.TestCase):
    def test_full_pipeline_reduces_messages(self):
        """60+ message conversation compressed to <25 messages."""
        pipeline = ContextPipeline(
            micro_max_chars=2000,
            micro_keep_head_lines=5,
            micro_keep_tail_lines=5,
            meso_message_threshold=10,
            meso_token_threshold=8000,
            macro_token_threshold=999999,  # Don't trigger Layer 3
            keep_recent=12,
        )

        msgs = _make_realistic_conversation(keep_recent=12)
        self.assertGreater(len(msgs), 50)

        self.assertTrue(pipeline.should_compact(msgs))
        result = pipeline.compact(msgs)

        # Significantly reduced
        self.assertLess(len(result), 25)

        # System prompt preserved
        self.assertEqual(result[0]["role"], "system")
        self.assertEqual(result[0]["content"], "You are a helpful coding assistant.")

        # Original task preserved
        self.assertEqual(result[1]["role"], "user")
        self.assertIn("Refactor the auth module", result[1]["content"])

        # Recent tail preserved (last 12 messages)
        tail = result[-12:]
        self.assertEqual(len(tail), 12)
        tail_contents = " ".join(m.get("content", "") for m in tail)
        self.assertIn("How about function 0", tail_contents)

        # Stats recorded
        stats = pipeline.stats
        # Note: micro_compressions counts compress_tool_result calls,
        # not defensive micro pass (which runs inside compact()).
        self.assertGreater(stats["meso_compressions"], 0)

    def test_pipeline_with_task_graph(self):
        """TaskGraphManager state is accessible through pipeline."""
        import tempfile

        tmpdir = tempfile.mkdtemp()
        graph = TaskGraphManager(sandbox_root=tmpdir)
        graph.create_plan(
            [
                {"description": "Read files"},
                {"description": "Refactor", "depends_on": ["T1"]},
            ]
        )
        graph.update_task("T1", status="in_progress")
        graph.update_task("T1", status="done")
        graph.update_task("T2", status="in_progress")

        pipeline = ContextPipeline(
            micro_max_chars=2000,
            meso_message_threshold=10,
            macro_token_threshold=999999,
            keep_recent=12,
            task_graph=graph,
        )

        msgs = _make_realistic_conversation(keep_recent=12)
        result = pipeline.compact(msgs)

        # Pipeline should still work normally
        self.assertLess(len(result), len(msgs))

        import shutil

        shutil.rmtree(tmpdir, ignore_errors=True)

    def test_pipeline_no_llm_still_works(self):
        """Without LLM provider, Layer 1 + Layer 2 (rule-based) work fine."""
        pipeline = ContextPipeline(
            micro_max_chars=2000,
            meso_message_threshold=10,
            macro_token_threshold=10,  # Would trigger, but no LLM
            keep_recent=12,
            llm_provider=None,
        )

        msgs = _make_realistic_conversation(keep_recent=12)
        result = pipeline.compact(msgs)

        # Layer 1 + 2 still reduce messages
        self.assertLess(len(result), len(msgs))
        # Layer 3 didn't fire (no LLM)
        self.assertEqual(pipeline.stats["macro_compressions"], 0)

    def test_short_conversation_no_compression(self):
        """Short conversations are not compressed."""
        pipeline = ContextPipeline(
            meso_message_threshold=20,
            macro_token_threshold=32000,
            keep_recent=12,
        )
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]
        self.assertFalse(pipeline.should_compact(msgs))
        result = pipeline.compact(msgs)
        self.assertEqual(result, msgs)


if __name__ == "__main__":
    unittest.main()
