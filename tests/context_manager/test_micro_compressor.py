"""Unit tests for Layer 1: MicroCompressor."""

import unittest

from context_manager.micro_compressor import MicroCompressor, estimate_tokens


class TestEstimateTokens(unittest.TestCase):
    def test_empty_string(self):
        self.assertEqual(estimate_tokens(""), 0)

    def test_english_text(self):
        # "hello world" = 11 chars → 11 // 4 = 2
        self.assertEqual(estimate_tokens("hello world"), 2)

    def test_large_text(self):
        text = "x" * 4000
        self.assertEqual(estimate_tokens(text), 1000)


class TestMicroCompressorNoOp(unittest.TestCase):
    """Content within max_chars is returned unchanged."""

    def setUp(self):
        self.mc = MicroCompressor(max_chars=100)

    def test_short_content_unchanged(self):
        content = "short content"
        self.assertEqual(self.mc.compress("read_file", content), content)

    def test_exact_threshold_unchanged(self):
        content = "x" * 100
        self.assertEqual(self.mc.compress("read_file", content), content)

    def test_empty_content(self):
        self.assertEqual(self.mc.compress("bash", ""), "")


class TestMicroCompressorReadFile(unittest.TestCase):
    """read_file strategy: head + tail lines."""

    def setUp(self):
        self.mc = MicroCompressor(max_chars=100, keep_head_lines=3, keep_tail_lines=3)

    def test_head_tail_preserved(self):
        lines = [f"line_{i}" for i in range(20)]
        content = "\n".join(lines)
        result = self.mc.compress("read_file", content)
        # Head: line_0, line_1, line_2
        self.assertIn("line_0", result)
        self.assertIn("line_1", result)
        self.assertIn("line_2", result)
        # Tail: line_17, line_18, line_19
        self.assertIn("line_17", result)
        self.assertIn("line_18", result)
        self.assertIn("line_19", result)
        # Omitted marker
        self.assertIn("lines omitted", result)

    def test_middle_omitted(self):
        lines = [f"line_{i}" for i in range(20)]
        content = "\n".join(lines)
        result = self.mc.compress("read_file", content)
        self.assertNotIn("line_10", result)

    def test_few_lines_no_compression(self):
        content = "line_0\nline_1\nline_2"
        result = self.mc.compress("read_file", content)
        self.assertEqual(result, content)


class TestMicroCompressorBash(unittest.TestCase):
    """bash strategy: preserve stderr, keep tail of stdout."""

    def setUp(self):
        self.mc = MicroCompressor(max_chars=100, keep_head_lines=2, keep_tail_lines=3)

    def test_stderr_preserved(self):
        content = (
            "stdout_line_1\nstdout_line_2\nstdout_line_3\nstdout_line_4\n"
            "STDERR: error happened\nSTDERR: more error"
        )
        result = self.mc.compress("bash", content)
        self.assertIn("STDERR: error happened", result)
        self.assertIn("STDERR: more error", result)

    def test_stdout_tail_kept(self):
        lines = [f"out_{i}" for i in range(20)]
        content = "\n".join(lines)
        result = self.mc.compress("bash", content)
        self.assertIn("out_19", result)
        self.assertIn("out_18", result)
        self.assertIn("out_17", result)

    def test_stdout_head_omitted(self):
        lines = [f"out_{i}" for i in range(20)]
        content = "\n".join(lines)
        result = self.mc.compress("bash", content)
        self.assertNotIn("out_0\n", result)
        self.assertIn("stdout lines omitted", result)


class TestMicroCompressorListDirectory(unittest.TestCase):
    """list_directory strategy: cap entries."""

    def setUp(self):
        self.mc = MicroCompressor(max_chars=100, max_dir_entries=5)

    def test_entries_capped(self):
        lines = [f"file_{i}.py" for i in range(20)]
        content = "\n".join(lines)
        result = self.mc.compress("list_directory", content)
        self.assertIn("file_0.py", result)
        self.assertIn("file_4.py", result)
        self.assertIn("more entries omitted", result)

    def test_few_entries_no_compression(self):
        content = "file_a.py\nfile_b.py"
        result = self.mc.compress("list_directory", content)
        self.assertEqual(result, content)


class TestMicroCompressorGeneric(unittest.TestCase):
    """Unknown tool names use head-tail fallback."""

    def setUp(self):
        self.mc = MicroCompressor(max_chars=100, keep_head_lines=2, keep_tail_lines=2)

    def test_unknown_tool_uses_head_tail(self):
        lines = [f"line_{i}" for i in range(20)]
        content = "\n".join(lines)
        result = self.mc.compress("some_unknown_tool", content)
        self.assertIn("line_0", result)
        self.assertIn("line_19", result)
        self.assertIn("lines omitted", result)


class TestMicroCompressorMessage(unittest.TestCase):
    """compress_message preserves metadata."""

    def setUp(self):
        self.mc = MicroCompressor(max_chars=50, keep_head_lines=2, keep_tail_lines=2)

    def test_non_tool_message_unchanged(self):
        msg = {"role": "assistant", "content": "hello"}
        result = self.mc.compress_message(msg)
        self.assertIs(result, msg)

    def test_small_tool_message_unchanged(self):
        msg = {"role": "tool", "tool_call_id": "tc_1", "content": "small"}
        result = self.mc.compress_message(msg)
        self.assertIs(result, msg)

    def test_large_tool_message_compressed(self):
        lines = [f"line_{i}" for i in range(30)]
        msg = {
            "role": "tool",
            "tool_call_id": "tc_1",
            "content": "\n".join(lines),
        }
        result = self.mc.compress_message(msg)
        self.assertNotEqual(result, msg)
        self.assertEqual(result["role"], "tool")
        self.assertEqual(result["tool_call_id"], "tc_1")
        self.assertLess(len(result["content"]), len(msg["content"]))


class TestMicroCompressorLoadSkill(unittest.TestCase):
    """load_skill uses same strategy as read_file."""

    def setUp(self):
        self.mc = MicroCompressor(max_chars=100, keep_head_lines=3, keep_tail_lines=3)

    def test_load_skill_uses_head_tail(self):
        lines = [f"skill_line_{i}" for i in range(20)]
        content = "\n".join(lines)
        result = self.mc.compress("load_skill", content)
        self.assertIn("skill_line_0", result)
        self.assertIn("skill_line_19", result)
        self.assertIn("lines omitted", result)


if __name__ == "__main__":
    unittest.main()


class TestMicroCompressorHardCap(unittest.TestCase):
    """Regression: single-line or few-line content of any size must be capped."""

    def setUp(self):
        self.mc = MicroCompressor(max_chars=100, keep_head_lines=10, keep_tail_lines=15)

    def test_single_line_large_content_capped(self):
        # 200KB single-line output — must be hard-capped
        content = "x" * 200_000
        result = self.mc.compress("bash", content)
        self.assertLess(len(result), 1000)
        self.assertIn("hard-capped", result)

    def test_few_lines_large_content_capped(self):
        # 3 lines, each 50KB — head_tail returns unchanged (3 <= 25)
        content = ("x" * 50_000 + "\n") * 3
        result = self.mc.compress("read_file", content)
        self.assertLess(len(result), 1000)

    def test_stderr_bounded(self):
        # 500 lines of stderr must be capped to keep_tail_lines
        stdout = "ok\n"
        stderr_lines = [f"STDERR: error_{i}" for i in range(500)]
        content = stdout + "\n".join(stderr_lines)
        result = self.mc.compress("bash", content)
        # stderr should be capped to keep_tail_lines + 1 marker line
        result_lines = result.split("\n")
        self.assertLess(len(result_lines), 30)
        self.assertIn("stderr lines omitted", result)
