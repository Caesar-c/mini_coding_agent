"""Layer 1: Per-message smart truncation (rule-based, no LLM).

Replaces the current dumb prefix truncation (``output[:max_output]``)
with tool-specific strategies that preserve the most informative
parts of each result.
"""

from logger import get_logger

logger = get_logger(__name__)


def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token for English/code."""
    return len(text) // 4


class MicroCompressor:
    """Intelligently compress individual tool result messages.

    Unlike the current dumb prefix truncation (``output[:max_output]``),
    this applies tool-specific strategies to preserve the most
    informative parts of each result.
    """

    def __init__(
        self,
        max_chars: int = 4000,
        keep_head_lines: int = 10,
        keep_tail_lines: int = 15,
        max_dir_entries: int = 50,
    ):
        self.max_chars = max_chars
        self.keep_head_lines = keep_head_lines
        self.keep_tail_lines = keep_tail_lines
        self.max_dir_entries = max_dir_entries

    def compress(self, tool_name: str, content: str) -> str:
        """Compress a tool result based on tool type.

        If content is already within *max_chars*, return unchanged.
        Otherwise dispatch to a tool-specific strategy.
        """
        if len(content) <= self.max_chars:
            return content

        strategy = self._STRATEGIES.get(tool_name)
        if strategy is not None:
            compressed = strategy(self, content)
        else:
            compressed = self._generic_compress(content)

        # Hard cap: if the strategy didn't reduce enough (e.g. single-line
        # content that bypassed _head_tail), enforce a character limit.
        if len(compressed) > self.max_chars * 2:
            before_cap = len(compressed)
            compressed = compressed[: self.max_chars * 2] + (
                f"\n... [hard-capped, {len(content)} chars total]"
            )
            logger.warning(
                "MicroCompressor: hard cap applied: %s %d -> %d chars (original %d chars)",
                tool_name,
                before_cap,
                len(compressed),
                len(content),
            )

        if len(compressed) < len(content):
            logger.info(
                "MicroCompressor: %s %d -> %d chars (%.0f%% reduction)",
                tool_name,
                len(content),
                len(compressed),
                (1 - len(compressed) / len(content)) * 100,
            )
        return compressed

    def compress_message(self, msg: dict) -> dict:
        """Compress a tool result message (returns new dict).

        Used by ContextPipeline's defensive re-pass to catch any
        uncompressed tool results in existing messages.
        """
        if msg.get("role") != "tool":
            return msg
        content = msg.get("content") or ""
        if len(content) <= self.max_chars:
            return msg
        logger.debug(
            "MicroCompressor: defensive compress on tool message, %d -> ~%d chars",
            len(content),
            min(len(content), self.max_chars * 2),
        )
        return {**msg, "content": self._generic_compress(content)}

    # ---- Internal strategies ----

    def _head_tail(self, content: str, head: int | None = None, tail: int | None = None) -> str:
        """Keep first *head* and last *tail* lines, elide middle."""
        head = head or self.keep_head_lines
        tail = tail or self.keep_tail_lines
        lines = content.split("\n")
        if len(lines) <= head + tail:
            return content
        omitted = len(lines) - head - tail
        return "\n".join(lines[:head] + [f"\n[... {omitted} lines omitted ...]\n"] + lines[-tail:])

    def _compress_read_file(self, content: str) -> str:
        """File reads: keep head + tail lines."""
        return self._head_tail(content)

    def _compress_bash(self, content: str) -> str:
        """Bash output: preserve stderr, keep tail of stdout."""
        lines = content.split("\n")
        stderr_lines: list[str] = []
        stdout_lines: list[str] = []
        in_stderr = False
        for line in lines:
            if line.startswith("STDERR:"):
                in_stderr = True
            if in_stderr:
                stderr_lines.append(line)
            else:
                stdout_lines.append(line)

        if len(stdout_lines) > self.keep_tail_lines:
            omitted = len(stdout_lines) - self.keep_tail_lines
            stdout_part = [f"[... {omitted} stdout lines omitted ...]"] + stdout_lines[
                -self.keep_tail_lines :
            ]
        else:
            stdout_part = stdout_lines

        result = "\n".join(stdout_part)
        if stderr_lines:
            # Cap stderr to same limit as stdout tail to prevent unbounded growth
            if len(stderr_lines) > self.keep_tail_lines:
                omitted = len(stderr_lines) - self.keep_tail_lines
                stderr_part = [f"[... {omitted} stderr lines omitted ...]"] + stderr_lines[
                    -self.keep_tail_lines :
                ]
            else:
                stderr_part = stderr_lines
            result += "\n" + "\n".join(stderr_part)
        return result

    def _compress_list_directory(self, content: str) -> str:
        """Directory listings: cap entries."""
        lines = content.split("\n")
        if len(lines) <= self.max_dir_entries:
            return content
        kept = lines[: self.max_dir_entries]
        omitted = len(lines) - self.max_dir_entries
        return "\n".join(kept + [f"[... {omitted} more entries omitted ...]"])

    def _generic_compress(self, content: str) -> str:
        """Fallback: head-tail compression for unknown tools."""
        return self._head_tail(content)

    _STRATEGIES: dict = {
        "read_file": _compress_read_file,
        "bash": _compress_bash,
        "list_directory": _compress_list_directory,
        "load_skill": _compress_read_file,
    }
