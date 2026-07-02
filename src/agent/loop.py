"""Minimal agent loop with pluggable LLM provider support."""

import time

from agent.message_utils import extract_tool_calls, parse_tool_call, response_to_dict
from agent.tool_registry import ToolRegistry
from config import settings
from context_manager.context import ContextCompactor
from context_manager.tracker import (
    UPDATE_PLAN_TOOL_DEFINITION,
    ProgressTracker,
    run_update_plan,
)
from llm import LLMProviderType, create_llm_provider
from logger import get_logger

logger = get_logger(__name__)

SYSTEM_PROMPT = """\
You are a helpful coding assistant. You can execute bash commands and use various \
file operation tools to accomplish tasks. Use the appropriate tools for file operations. \
Think step by step, and explain what you're doing before and after each command.

For multi-step tasks, use the update_plan tool to create a plan with numbered steps. \
Update the plan as you complete each step by marking it "done" and moving the next \
step to "in_progress". If the user corrects your plan or asks you to change steps, \
call update_plan with the revised step list. This keeps you on track for long tasks."""


class Agent:
    def __init__(
        self,
        llm_provider_type: LLMProviderType = None,
    ):
        # Create the LLM provider based on type
        self.llm_provider = create_llm_provider(
            llm_provider_type or LLMProviderType(settings.LLM_PROVIDER)
        )

        self.messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
        ]

        # Initialize the tool registry
        self.tool_registry = ToolRegistry()

        # Progress tracking and context management
        self.progress_tracker = ProgressTracker()
        self.context_compactor = ContextCompactor(
            max_messages=settings.CONTEXT_MAX_MESSAGES,
            keep_recent=settings.CONTEXT_KEEP_RECENT,
        )

        # Register the update_plan tool with a closure bound to this agent's tracker
        self.tool_registry.register(
            UPDATE_PLAN_TOOL_DEFINITION,
            lambda args: run_update_plan(args, self.progress_tracker),
        )

    def _call_llm(self):
        """Call LLM provider API and return the assistant message."""
        logger.info("LLM call start: messages=%d", len(self.messages))
        t0 = time.monotonic()
        response = self.llm_provider.chat_completion(
            messages=self.messages,
            tools=self.tool_registry.definitions,
        )
        elapsed = time.monotonic() - t0
        logger.info("LLM call end: %.2fs", elapsed)
        return response

    def _inject_progress(self):
        """Remove old progress summary and inject current one as a system message."""
        # Remove any previously injected progress messages
        self.messages = [
            m
            for m in self.messages
            if not (
                m.get("role") == "system"
                and isinstance(m.get("content"), str)
                and m["content"].startswith("[TASK PROGRESS]")
            )
        ]
        # Inject fresh progress if a plan exists
        if self.progress_tracker.has_plan:
            summary = self.progress_tracker.format_summary()
            self.messages.insert(1, {"role": "system", "content": summary})

    def _handle_tool_call(self, tool_call):
        """Execute the appropriate tool and return a tool result message in OpenAI format."""
        tool_name, args, tc_id = parse_tool_call(tool_call)

        logger.info("Tool call: %s, args=%s", tool_name, str(args)[:2000])

        # Use the registry to execute the appropriate tool
        output = self.tool_registry.execute(tool_name, args)

        # --- Tool result truncation ---
        max_output = settings.MAX_TOOL_OUTPUT
        if len(output) > max_output:
            logger.warning("Tool output truncated: %d chars (max %d)", len(output), max_output)
            output = output[:max_output] + f"\n... [truncated, {len(output)} chars total]"

        logger.info(
            "Tool result: %s, output_len=%d, output=%.2000s",
            tool_name,
            len(output),
            output,
        )
        print(f"\n🔧 Running {tool_name}: {str(args)[:100]}{'...' if len(str(args)) > 100 else ''}")
        print(f"📤 Output: {output[:500]}{'...' if len(output) > 500 else ''}")
        return {
            "role": "tool",
            "tool_call_id": tc_id,
            "content": output,
        }

    def chat(self, user_input: str) -> str:
        """Run one turn of the agent loop, returns the final text response."""
        self.messages.append({"role": "user", "content": user_input})
        logger.info(
            "Chat turn start: user_input=%.2000s, messages=%d",
            user_input,
            len(self.messages),
        )

        iteration = 0
        while True:
            iteration += 1

            # --- Progress reinforcement ---
            self._inject_progress()

            # --- Context compaction ---
            if self.context_compactor.should_compact(self.messages):
                before = len(self.messages)
                self.messages = self.context_compactor.compact(self.messages)
                logger.info("Context compacted: %d -> %d messages", before, len(self.messages))

            message = self._call_llm()

            # Extract tool_calls first — needed to decide whether to append.
            # OpenAI returns content=None alongside tool_calls; the message MUST
            # be appended even when content is falsy to avoid orphaned tool results.
            tool_calls = extract_tool_calls(message)
            content = getattr(message, "content", None)

            # Append assistant message when it carries content or tool_calls
            if content or tool_calls:
                self.messages.append(response_to_dict(message))

            if not tool_calls:
                logger.info(
                    "Chat turn end: iterations=%d, response=%.2000s",
                    iteration,
                    content or "",
                )
                if content:
                    return content
                return ""

            # Execute all tool calls and feed results back
            logger.info("Executing %d tool call(s) in iteration %d", len(tool_calls), iteration)
            for tool_call in tool_calls:
                result = self._handle_tool_call(tool_call)
                self.messages.append(result)


def main():
    # Provider is resolved from settings.llm_provider (env: LLM_PROVIDER).
    agent = Agent()
    print("🤖 Mini Coding Agent (type 'quit' to exit)\n")

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not user_input or user_input.lower() == "quit":
            break

        response = agent.chat(user_input)
        print(f"\n🤖 {response}\n")


if __name__ == "__main__":
    main()
