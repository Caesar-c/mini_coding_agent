"""Layer 2: Section-level summarization of completed tool exchanges.

Groups consecutive tool-call + tool-result pairs in the middle section
and replaces them with structured summaries, preserving key outcomes
(file paths, command results, errors) instead of dropping everything.
"""

import json

from context_manager.micro_compressor import estimate_tokens
from logger import get_logger

logger = get_logger(__name__)


class MesoCompressor:
    """Summarize groups of completed tool exchanges into brief prose.

    Replaces the current approach (drop tool results, strip tool_calls)
    with structured summaries that preserve key outcomes.
    """

    def __init__(
        self,
        meso_message_threshold: int = 20,
        meso_token_threshold: int = 8000,
        keep_recent: int = 12,
        use_llm: bool = False,
        llm_provider=None,
    ):
        self.meso_message_threshold = meso_message_threshold
        self.meso_token_threshold = meso_token_threshold
        self.keep_recent = keep_recent
        self.use_llm = use_llm
        self.llm_provider = llm_provider

    def should_compress(self, messages: list[dict]) -> bool:
        """Check if the middle section needs compression.

        Fires when middle section exceeds message count OR token threshold.
        """
        middle = self._get_middle(messages)
        if not middle:
            return False
        if len(middle) >= self.meso_message_threshold:
            return True
        middle_tokens = sum(estimate_tokens(m.get("content") or "") for m in middle)
        return middle_tokens >= self.meso_token_threshold

    async def compress(self, messages: list[dict]) -> list[dict]:
        """Compress middle section's tool exchanges into summaries.

        Returns: head[0:2] + compressed_middle + tail[-keep_recent:]
        """
        if len(messages) <= 2 + self.keep_recent:
            return messages

        head = messages[:2]
        tail = messages[-self.keep_recent :]
        middle = messages[2 : len(messages) - self.keep_recent]

        groups = self._group_tool_exchanges(middle)
        tool_groups = sum(1 for g in groups if g["type"] == "tool_exchange")
        other_groups = sum(1 for g in groups if g["type"] == "other")
        logger.info(
            "Meso compress start: middle=%d msgs, %d tool groups, %d other groups, mode=%s",
            len(middle),
            tool_groups,
            other_groups,
            "llm" if self.use_llm else "rule-based",
        )

        compressed_middle: list[dict] = []
        for group in groups:
            if group["type"] == "tool_exchange":
                summary = await self._summarize_group(group["messages"])
                if summary:
                    compressed_middle.append(
                        {"role": "assistant", "content": f"[SUMMARY] {summary}"}
                    )
            else:
                compressed_middle.extend(group["messages"])

        result = head + compressed_middle + tail

        if len(result) >= len(messages):
            logger.warning("Meso compression produced no reduction, skipping")
            return messages

        logger.info(
            "Meso compression: %d -> %d messages (middle: %d -> %d)",
            len(messages),
            len(result),
            len(middle),
            len(compressed_middle),
        )
        return result

    # ---- Internal helpers ----

    def _get_middle(self, messages: list[dict]) -> list[dict]:
        """Extract the middle section (between head and tail)."""
        if len(messages) <= 2 + self.keep_recent:
            return []
        return messages[2 : len(messages) - self.keep_recent]

    def _group_tool_exchanges(self, messages: list[dict]) -> list[dict]:
        """Group consecutive tool-call + tool-result pairs.

        Returns list of:
        - ``{"type": "tool_exchange", "messages": [...]}``
        - ``{"type": "other", "messages": [...]}``
        """
        groups: list[dict] = []
        current_group: list[dict] = []
        current_type: str | None = None

        for msg in messages:
            role = msg.get("role", "")
            is_tool_exchange = (role == "assistant" and "tool_calls" in msg) or (role == "tool")
            msg_type = "tool_exchange" if is_tool_exchange else "other"

            if msg_type == current_type:
                current_group.append(msg)
            else:
                if current_group:
                    groups.append({"type": current_type, "messages": current_group})
                current_group = [msg]
                current_type = msg_type

        if current_group:
            groups.append({"type": current_type, "messages": current_group})

        return groups

    async def _summarize_group(self, messages: list[dict]) -> str:
        """Summarize a group of tool exchange messages."""
        if self.use_llm and self.llm_provider:
            try:
                return await self._llm_summarize(messages)
            except Exception as e:
                logger.warning("LLM summarization failed, falling back to rule-based: %s", e)
        return self._rule_based_summarize(messages)

    def _rule_based_summarize(self, messages: list[dict]) -> str:
        """Extract structured facts from tool exchanges without LLM."""
        facts: list[str] = []
        # Map tool_call_id → index in facts list for parallel tool result matching
        tc_id_to_fact_index: dict[str, int] = {}
        for msg in messages:
            role = msg.get("role", "")
            if role == "assistant" and "tool_calls" in msg:
                for tc in msg["tool_calls"]:
                    fn = tc.get("function", {})
                    name = fn.get("name", "unknown")
                    tc_id = tc.get("id", "")
                    try:
                        args = json.loads(fn.get("arguments", "{}"))
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    tc_id_to_fact_index[tc_id] = len(facts)
                    facts.append(self._format_tool_fact(name, args))
            elif role == "tool":
                content = msg.get("content", "") or ""
                outcome = self._extract_outcome(content)
                if outcome:
                    tc_id = msg.get("tool_call_id", "")
                    idx = tc_id_to_fact_index.get(tc_id)
                    if idx is not None:
                        facts[idx] += f" → {outcome}"
                    elif facts:
                        facts[-1] += f" → {outcome}"

        if not facts:
            return ""
        return "Completed: " + "; ".join(facts)

    def _format_tool_fact(self, tool_name: str, args: dict) -> str:
        """Format a tool call as a brief fact string."""
        if tool_name == "bash":
            cmd = args.get("command", "")
            if len(cmd) > 80:
                cmd = cmd[:77] + "..."
            return f"ran `{cmd}`"
        if tool_name == "read_file":
            return f"read `{args.get('path', '?')}`"
        if tool_name == "write_file":
            path = args.get("path", "?")
            content = args.get("content", "")
            return f"wrote {len(content)} chars to `{path}`"
        if tool_name == "list_directory":
            return f"listed `{args.get('path', '.')}`"
        if tool_name == "create_directory":
            return f"created dir `{args.get('path', '?')}`"
        if tool_name == "file_exists":
            return f"checked `{args.get('path', '?')}`"
        if tool_name == "create_plan":
            n = len(args.get("tasks", []))
            return f"created plan ({n} tasks)"
        if tool_name == "update_task":
            tid = args.get("task_id", "?")
            st = args.get("status", "?")
            return f"updated {tid} to {st}"
        if tool_name == "add_task":
            return f"added task: {args.get('description', '?')[:50]}"
        if tool_name == "get_plan":
            return "checked plan"
        if tool_name == "load_skill":
            return f"loaded skill `{args.get('name', '?')}`"
        return f"called {tool_name}"

    def _extract_outcome(self, content: str) -> str:
        """Extract a brief outcome summary from tool result content."""
        if not content:
            return ""
        if content.startswith("Error"):
            return f"error: {content[:100]}"
        if "exit code" in content:
            return content.strip()[:80]
        lines = content.count("\n") + 1
        if lines > 5:
            return f"{lines} lines"
        if "Successfully wrote" in content:
            return content.strip()[:80]
        first_line = content.split("\n")[0]
        if len(first_line) > 80:
            return first_line[:77] + "..."
        return first_line

    async def _llm_summarize(self, messages: list[dict]) -> str:
        """Use LLM to generate a natural summary of tool exchanges."""
        condensed: list[str] = []
        for msg in messages:
            role = msg.get("role", "")
            if role == "assistant" and "tool_calls" in msg:
                for tc in msg["tool_calls"]:
                    fn = tc.get("function", {})
                    condensed.append(
                        f"Tool call: {fn.get('name', '?')} ({fn.get('arguments', '{}')[:200]})"
                    )
                if msg.get("content"):
                    condensed.append(f"Assistant said: {msg['content'][:200]}")
            elif role == "tool":
                content = msg.get("content") or ""
                if len(content) > 300:
                    content = content[:150] + "..." + content[-150:]
                condensed.append(f"Tool result: {content}")

        prompt_messages = [
            {
                "role": "system",
                "content": (
                    "You are a summarizer. Given a sequence of tool calls and "
                    "results from a coding agent, write a concise 2-4 sentence "
                    "summary of what was accomplished. Focus on: files read/written, "
                    "commands run and their outcomes, any errors encountered. "
                    "Do NOT include code snippets. Start with '[SUMMARY]'. "
                    "Be factual and brief."
                ),
            },
            {"role": "user", "content": "\n".join(condensed)},
        ]

        response = await self.llm_provider.chat_completion(
            messages=prompt_messages,
            tools=None,
            max_tokens=256,
            temperature=0.3,
        )
        summary = getattr(response, "content", "") or ""
        if not summary.startswith("[SUMMARY]"):
            summary = "[SUMMARY] " + summary
        return summary
