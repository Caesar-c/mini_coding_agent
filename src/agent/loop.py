"""Minimal agent loop with pluggable LLM provider support."""

import json
import os
from typing import Optional

from dotenv import load_dotenv
from .tool_registry import ToolRegistry
from ..llm import MessageWrapper, create_llm_provider, LLMProviderType

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
        llm_provider_type: LLMProviderType = LLMProviderType.OPENAI,
        temperature: float = 0.7
    ):
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-4o-free")
        self.max_tokens = max_tokens or int(os.getenv("MAX_TOKENS", "4096"))
        self.temperature = temperature

        # Create the LLM provider based on type
        self.llm_provider = create_llm_provider(
            llm_provider_type,
            api_key=api_key or os.getenv(f"{llm_provider_type.value.upper()}_API_KEY"),
            base_url=base_url or os.getenv("OPENAI_BASE_URL", "https://aihubmix.com/v1")
        )

        self.messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
        ]

        # Initialize the tool registry
        self.tool_registry = ToolRegistry()

    def _call_llm(self):
        """Call LLM provider API and return the assistant message."""
        response = self.llm_provider.chat_completion(
            messages=self.messages,
            tools=self.tool_registry.definitions,  # Use all registered tools
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=self.temperature
        )
        return response

    def _handle_tool_call(self, tool_call):
        """Execute the appropriate tool and return a tool result message in OpenAI format."""
        import json
        # Handle both OpenAI-style tool calls and our wrapper
        if hasattr(tool_call, 'function'):
            # OpenAI style
            args = json.loads(tool_call.function.arguments)
            tool_name = tool_call.function.name
        else:
            # Our wrapper style
            args = json.loads(tool_call.arguments)
            tool_name = tool_call.name

        # Use the registry to execute the appropriate tool
        output = self.tool_registry.execute(tool_name, args)

        print(f"\n🔧 Running {tool_name}: {str(args)[:100]}{'...' if len(str(args)) > 100 else ''}")
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
            if hasattr(message, 'content') and message.content:
                # Different providers might have different message formats
                if hasattr(message, 'model_dump'):
                    self.messages.append(message.model_dump(exclude_unset=True))
                else:
                    # Handle our wrapper
                    msg_dict = {
                        'role': getattr(message, 'role', 'assistant'),
                        'content': message.content
                    }
                    if hasattr(message, 'tool_calls') and message.tool_calls:
                        msg_dict['tool_calls'] = message.tool_calls
                    self.messages.append(msg_dict)

            # Check if the message has tool calls - handle differently based on provider
            if hasattr(message, 'tool_calls'):
                tool_calls = message.tool_calls
            else:
                # For our wrapper, access the data directly
                tool_calls = getattr(message, 'data', {}).get('tool_calls', [])

            if not tool_calls:
                # No tool calls — return text
                content = getattr(message, 'content', '')
                if content:
                    return content
                return ""

            # Execute all tool calls and feed results back
            for tool_call in tool_calls:
                result = self._handle_tool_call(tool_call)
                self.messages.append(result)


def main():
    # Default to OpenAI provider, can be changed based on environment
    provider_type_str = os.getenv("LLM_PROVIDER", "openai").lower()
    provider_type = LLMProviderType(provider_type_str)

    agent = Agent(llm_provider_type=provider_type)
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