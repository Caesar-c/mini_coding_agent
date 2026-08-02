"""Layer 3: Full context rebuild via LLM summarization.

When total context is very large (even after Layer 1 and Layer 2),
rebuild the entire conversation into a structured summary + recent
window. This is the most aggressive compression, requiring an LLM call.
"""

from context_manager.micro_compressor import estimate_tokens
from logger import get_logger

logger = get_logger(__name__)

MACRO_SUMMARY_PROMPT = """\
You are a conversation compressor for a coding agent. Given the full conversation
history, produce a structured summary that preserves all critical context.

Output EXACTLY this format (no markdown fences, no preamble):

[CONTEXT SUMMARY]
## Completed Work
<Summarize what was accomplished: files read/written, commands run, tests passed/failed>

## Current State
<Describe the current state: what files exist, what was modified, any errors>

## Key Decisions
<Important design choices, error workarounds, user corrections — if any>

Rules:
- Be factual and specific (file paths, command names, outcomes)
- Do NOT include full code snippets — describe what code does
- Keep total output under 800 words
- If the conversation is short, be correspondingly brief
"""


class MacroCompressor:
    """Rebuild entire conversation into structured summary + recent window.

    This is the most aggressive compression layer, used when total context
    is very large. Requires an LLM call for summarization.
    """

    def __init__(
        self,
        token_threshold: int = 32000,
        keep_recent: int = 12,
        llm_provider=None,
        task_graph=None,
    ):
        self.token_threshold = token_threshold
        self.keep_recent = keep_recent
        self.llm_provider = llm_provider
        self.task_graph = task_graph

    def should_compress(self, messages: list[dict]) -> bool:
        """Check if total context exceeds the macro threshold.

        Returns False if no LLM provider is available (Layer 3 requires LLM).
        """
        if not self.llm_provider:
            return False
        total_tokens = sum(estimate_tokens(m.get("content") or "") for m in messages)
        return total_tokens >= self.token_threshold

    async def compress(self, messages: list[dict]) -> list[dict]:
        """Rebuild conversation into structured summary + recent window.

        Returns:
            [system_prompt, context_summary, original_task, progress, ...recent]
        """
        if len(messages) <= 2 + self.keep_recent:
            return messages

        system_prompt = messages[0]
        recent = messages[-self.keep_recent :]

        # Build condensed history for LLM
        history_text = self._build_history_digest(messages[1 : len(messages) - self.keep_recent])
        logger.info(
            "Macro compress start: %d messages, history_digest=%d chars, has_task_graph=%s",
            len(messages),
            len(history_text),
            bool(self.task_graph and self.task_graph.has_plan),
        )

        # Add TaskGraph context
        progress_context = ""
        if self.task_graph and self.task_graph.has_plan:
            progress_context = f"\n\nCurrent task plan:\n{self.task_graph.format_summary()}"

        # Call LLM
        try:
            summary = await self._generate_summary(history_text, progress_context)
        except Exception as e:
            logger.error("Macro compression LLM call failed: %s", e)
            # Fallback: system + first_user + recent
            first_user = messages[1] if len(messages) > 1 else None
            result: list[dict] = [system_prompt]
            if first_user:
                result.append(first_user)
            result.extend(recent)
            logger.warning(
                "Macro fallback: %d -> %d messages (system + first_user + recent %d)",
                len(messages),
                len(result),
                self.keep_recent,
            )
            return result

        # Build compressed message list
        result = [
            system_prompt,
            {"role": "system", "content": summary},
        ]

        # Preserve original task (first user message)
        if len(messages) > 1 and messages[1].get("role") == "user":
            result.append(messages[1])

        # Inject progress if task graph active
        if self.task_graph and self.task_graph.has_plan:
            result.append(
                {
                    "role": "system",
                    "content": self.task_graph.format_summary(),
                }
            )

        result.extend(recent)

        before_tokens = sum(estimate_tokens(m.get("content") or "") for m in messages)
        after_tokens = sum(estimate_tokens(m.get("content") or "") for m in result)
        logger.info(
            "Macro compression: %d -> %d messages, ~%d -> ~%d tokens",
            len(messages),
            len(result),
            before_tokens,
            after_tokens,
        )
        return result

    def _build_history_digest(self, messages: list[dict]) -> str:
        """Build a condensed text representation of the history for the LLM."""
        parts: list[str] = []
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content") or ""

            if role == "system":
                if content and not content.startswith("[TASK PROGRESS]"):
                    parts.append(f"[SYSTEM] {content[:200]}")
            elif role == "user":
                parts.append(f"[USER] {content[:500]}")
            elif role == "assistant":
                if content:
                    parts.append(f"[ASSISTANT] {content[:300]}")
                if "tool_calls" in msg:
                    for tc in msg["tool_calls"]:
                        fn = tc.get("function", {})
                        args_str = fn.get("arguments", "{}")
                        if len(args_str) > 200:
                            args_str = args_str[:200] + "..."
                        parts.append(f"[TOOL_CALL] {fn.get('name', '?')}({args_str})")
            elif role == "tool":
                if content:
                    if len(content) > 300:
                        content = content[:150] + "..." + content[-150:]
                    parts.append(f"[TOOL_RESULT] {content}")

        return "\n".join(parts)

    async def _generate_summary(self, history_text: str, progress_context: str) -> str:
        """Call the LLM to generate a structured conversation summary."""
        prompt_messages = [
            {"role": "system", "content": MACRO_SUMMARY_PROMPT},
            {"role": "user", "content": history_text + progress_context},
        ]

        response = await self.llm_provider.chat_completion(
            messages=prompt_messages,
            tools=None,
            max_tokens=4096,
            temperature=0.3,
        )

        summary = getattr(response, "content", "") or ""
        if not summary.startswith("[CONTEXT SUMMARY]"):
            summary = "[CONTEXT SUMMARY]\n" + summary
        return summary
