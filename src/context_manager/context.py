"""Context window management — compacts old messages to prevent context bloat."""

from logger import get_logger

logger = get_logger(__name__)


class ContextCompactor:
    """Reduces context size by removing old tool results from the middle.

    Strategy: tool result messages older than *keep_recent* positions are
    removed, and assistant messages in the middle have their tool_calls
    stripped. Recent messages are kept verbatim so the LLM has full
    detail for current work.

    The compaction is rule-based (no LLM call).
    """

    def __init__(self, max_messages: int = 40, keep_recent: int = 12):
        self.max_messages = max_messages
        self.keep_recent = keep_recent

    def should_compact(self, messages: list[dict]) -> bool:
        """Return True if message count exceeds the threshold."""
        return len(messages) > self.max_messages

    def compact(self, messages: list[dict]) -> list[dict]:
        """Return a compacted copy of the message list.

        Preserves:
          - ``messages[0]`` (system prompt)
          - ``messages[1]`` (first user message = original task)
          - ``messages[-keep_recent:]`` (recent context, verbatim)

        Compacts the middle:
          - Tool result messages → removed
          - Assistant messages with tool_calls → tool_calls stripped
            (content preserved as summary; empty content gets a placeholder)
          - Other messages → kept as-is

        If compaction cannot reduce the count, returns the original list.
        """
        if len(messages) <= self.max_messages:
            return messages

        # Need at least head(2) + some middle + tail(keep_recent)
        min_preserved = 2 + self.keep_recent
        if len(messages) <= min_preserved:
            return messages

        head = messages[:2]
        tail = messages[-self.keep_recent :]
        middle = messages[2 : len(messages) - self.keep_recent]

        compacted_middle = []
        for msg in middle:
            role = msg.get("role", "")

            # Remove tool result messages from the middle
            if role == "tool":
                continue

            # Strip tool_calls from assistant messages and summarize
            if role == "assistant" and "tool_calls" in msg:
                compacted = {k: v for k, v in msg.items() if k != "tool_calls"}
                content = compacted.get("content") or ""
                if not content:
                    compacted["content"] = "[compacted] (executed tools)"
                compacted_middle.append(compacted)
            else:
                compacted_middle.append(msg)

        result = head + compacted_middle + tail

        # Safety guard: compaction should never increase message count
        if len(result) >= len(messages):
            logger.warning(
                "Compaction produced no reduction (%d -> %d), skipping", len(messages), len(result)
            )
            return messages

        logger.info("Context compacted: %d -> %d messages", len(messages), len(result))
        return result
