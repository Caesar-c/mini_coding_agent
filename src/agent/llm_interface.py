"""Abstract interface for LLM providers to enable pluggable LLM backends."""

from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional
from openai.types.chat import ChatCompletionMessage
from openai.types.chat.chat_completion_message_tool_call import ChatCompletionMessageToolCall


class LLMProvider(ABC):
    """Abstract base class for LLM providers."""

    @abstractmethod
    def chat_completion(self,
                       messages: List[Dict[str, str]],
                       tools: Optional[List[Dict[str, Any]]] = None,
                       model: str = None,
                       max_tokens: int = 4096,
                       temperature: float = 0.7,
                       **kwargs) -> ChatCompletionMessage:
        """
        Generate a chat completion.

        Args:
            messages: List of message dictionaries with 'role' and 'content'
            tools: Optional list of tool definitions
            model: Model identifier
            max_tokens: Maximum number of tokens to generate
            temperature: Sampling temperature
            **kwargs: Additional provider-specific parameters

        Returns:
            ChatCompletionMessage object
        """
        pass


class ToolCallWrapper:
    """Wrapper for tool calls to provide consistent interface across providers."""

    def __init__(self, tool_call_data):
        self.data = tool_call_data

    @property
    def id(self) -> str:
        return getattr(self.data, 'id', str(id(self.data)))

    @property
    def function(self):
        return getattr(self.data, 'function', self.data.get('function', {}))

    @property
    def name(self) -> str:
        if hasattr(self.function, 'name'):
            return self.function.name
        return self.function.get('name', '')

    @property
    def arguments(self) -> str:
        if hasattr(self.function, 'arguments'):
            return self.function.arguments
        return self.function.get('arguments', '{}')


class MessageWrapper:
    """Wrapper for messages to provide consistent interface across providers."""

    def __init__(self, message_data):
        self.data = message_data

    @property
    def content(self) -> Optional[str]:
        if hasattr(self.data, 'content'):
            return self.data.content
        return self.data.get('content', None)

    @property
    def tool_calls(self):
        if hasattr(self.data, 'tool_calls'):
            return self.data.tool_calls
        # Handle different formats
        tool_calls_data = self.data.get('tool_calls', [])
        if tool_calls_data:
            return [ToolCallWrapper(tc) for tc in tool_calls_data]
        return []

    def model_dump(self, **kwargs):
        """Provide compatible interface with OpenAI response objects."""
        return self.data