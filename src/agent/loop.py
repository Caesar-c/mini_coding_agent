"""Minimal agent loop with pluggable LLM provider support."""

import json

from agent.tool_registry import ToolRegistry
from config import settings
from context_manager.context import ContextCompactor
from context_manager.tracker import (
    UPDATE_PLAN_TOOL_DEFINITION,
    ProgressTracker,
    run_update_plan,
)
from llm import LLMProviderType, create_llm_provider

# Sentinel used by tc_attr to distinguish "key missing" from "key present but None".
_MISSING = object()

SYSTEM_PROMPT = """\
You are a helpful coding assistant. You can execute bash commands and use various \
file operation tools to accomplish tasks. Use the appropriate tools for file operations. \
Think step by step, and explain what you're doing before and after each command.

For multi-step tasks, use the update_plan tool to create a plan with numbered steps. \
Update the plan as you complete each step by marking it "done" and moving the next \
step to "in_progress". If the user corrects your plan or asks you to change steps, \
call update_plan with the revised step list. This keeps you on track for long tasks."""


def tc_attr(tool_call, attr: str, default=None):
    """Access a tool-call field whether *tool_call* is an object or a dict.

    Supports nested paths like ``"function.name"`` / ``"function.arguments"``
    via dot notation.  Uses an internal sentinel so that a key explicitly set
    to ``None`` still returns *default* instead of ``None``.
    """
    parts = attr.split(".")
    obj = tool_call
    for p in parts:
        if isinstance(obj, dict):
            obj = obj.get(p, _MISSING)
        else:
            obj = getattr(obj, p, _MISSING)
        if obj is _MISSING or obj is None:
            return default
    return obj


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
        response = self.llm_provider.chat_completion(
            messages=self.messages,
            tools=self.tool_registry.definitions,  # Use all registered tools
        )
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
        tool_name = tc_attr(tool_call, "function.name", "")
        raw_args = tc_attr(tool_call, "function.arguments", "{}")
        args = json.loads(raw_args) if raw_args else {}
        tc_id = tc_attr(tool_call, "id", "")

        # Use the registry to execute the appropriate tool
        output = self.tool_registry.execute(tool_name, args)

        # --- Tool result truncation ---
        max_output = settings.MAX_TOOL_OUTPUT
        if len(output) > max_output:
            output = output[:max_output] + f"\n... [truncated, {len(output)} chars total]"

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

        while True:
            # --- Progress reinforcement ---
            self._inject_progress()

            # --- Context compaction ---
            if self.context_compactor.should_compact(self.messages):
                self.messages = self.context_compactor.compact(self.messages)

            message = self._call_llm()
            # Append assistant message to history if it has content
            if hasattr(message, "content") and message.content:
                # Different providers might have different message formats
                if hasattr(message, "model_dump"):
                    self.messages.append(message.model_dump(exclude_unset=True))
                else:
                    # Handle our wrapper
                    msg_dict = {
                        "role": getattr(message, "role", "assistant"),
                        "content": message.content,
                    }
                    if hasattr(message, "tool_calls") and message.tool_calls:
                        msg_dict["tool_calls"] = message.tool_calls
                    self.messages.append(msg_dict)

            # Check if the message has tool calls - handle differently based on provider
            if hasattr(message, "tool_calls"):
                tool_calls = message.tool_calls
            else:
                # For our wrapper, access the data directly
                tool_calls = getattr(message, "data", {}).get("tool_calls", [])

            if not tool_calls:
                # No tool calls — return text
                content = getattr(message, "content", "")
                if content:
                    return content
                return ""

            # Execute all tool calls and feed results back
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
