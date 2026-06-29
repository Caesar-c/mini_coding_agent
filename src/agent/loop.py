"""Minimal agent loop with tool registry support."""

import json
import os

import openai
from dotenv import load_dotenv

from tool_registry import ToolRegistry
from typing import Optional

# Load .env from project root
load_dotenv()

SYSTEM_PROMPT = """\
You are a helpful coding assistant. You can execute bash commands and use various \
file operation tools to accomplish tasks. Use the appropriate tools for file operations. \
Think step by step, and explain what you're doing before and after each command."""


class Agent:
    def __init__(
        self,
        model: str = None,
        api_key: str = None,
        base_url: str = None,
        max_tokens: int = None,
    ):
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-4o-free")
        self.max_tokens = max_tokens or int(os.getenv("MAX_TOKENS", "4096"))

        self.client = openai.OpenAI(
            api_key=api_key or os.getenv("AIHUBMIX_API_KEY", ""),
            base_url=base_url or os.getenv("OPENAI_BASE_URL", "https://aihubmix.com/v1"),
        )
        self.messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
        ]

        # Initialize the tool registry
        self.tool_registry = ToolRegistry()

    def _call_llm(self):
        """Call OpenAI chat completions API and return the assistant message."""
        response = self.client.chat.completions.create(
            model=self.model,
            max_tokens=self.max_tokens,
            tools=self.tool_registry.definitions,  # Use all registered tools
            messages=self.messages,
        )
        return response.choices[0].message

    def _handle_tool_call(self, tool_call):
        """Execute the appropriate tool and return a tool result message in OpenAI format."""
        import json
        args = json.loads(tool_call.function.arguments)
        # Use the registry to execute the appropriate tool
        output = self.tool_registry.execute(tool_call.function.name, args)

        print(f"\n🔧 Running {tool_call.function.name}: {str(args)[:100]}{'...' if len(str(args)) > 100 else ''}")
        print(f"📤 Output: {output[:500]}{'...' if len(output) > 500 else ''}")
        return {
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": output,
        }

    def chat(self, user_input: str) -> str:
        """Run one turn of the agent loop, returns the final text response."""
        self.messages.append({"role": "user", "content": user_input})

        while True:
            message = self._call_llm()
            # Append assistant message to history if it has content
            if message.content:
                self.messages.append(message.model_dump(exclude_unset=True))

            if not message.tool_calls:
                # No tool calls — return text
                return message.content or ""

            # Execute all tool calls and feed results back
            for tool_call in message.tool_calls:
                result = self._handle_tool_call(tool_call)
                self.messages.append(result)


def main():
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
