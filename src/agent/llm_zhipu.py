"""Zhipu AI LLM Provider Implementation."""

from typing import List, Dict, Any, Optional
from .llm_interface import LLMProvider, MessageWrapper


class ZhipuAILLMProvider(LLMProvider):
    """Implementation of LLMProvider for Zhipu AI API."""

    def __init__(self, api_key: str = None):
        self.api_key = api_key or ""
        # Import here to avoid requirement unless specifically used
        try:
            from zai import ZhipuAiClient
            self.client = ZhipuAiClient(api_key=self.api_key)
        except ImportError:
            raise ImportError("Please install zai package: pip install zai")

    def chat_completion(self,
                       messages: List[Dict[str, str]],
                       tools: Optional[List[Dict[str, Any]]] = None,
                       model: str = "glm-4",
                       max_tokens: int = 8192,
                       temperature: float = 0.95,
                       **kwargs) -> MessageWrapper:
        """
        Generate a chat completion using Zhipu AI API.

        Args:
            messages: List of message dictionaries with 'role' and 'content'
            tools: Optional list of tool definitions
            model: Model identifier (default: glm-4)
            max_tokens: Maximum number of tokens to generate
            temperature: Sampling temperature
            **kwargs: Additional parameters

        Returns:
            MessageWrapper object wrapping the response
        """
        # Prepare the API call - include tools if provided
        api_params = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        # Add tools to parameters if provided
        if tools:
            # Zhipu AI supports tools similarly to OpenAI
            api_params["tools"] = tools

        # Add any additional parameters from kwargs
        api_params.update(kwargs)

        # Remove None values
        api_params = {k: v for k, v in api_params.items() if v is not None}

        # Make the API call
        try:
            response = self.client.chat.completions.create(**api_params)

            # Extract the message from the response
            message = response.choices[0].message

            # Convert the response to match the expected interface
            # We need to check if the response has tool_calls
            tool_calls = []
            if hasattr(message, 'tool_calls') and message.tool_calls:
                for tc in message.tool_calls:
                    tool_calls.append(tc)

            # Return a wrapped response
            response_data = {
                'role': message.role,
                'content': message.content,
                'tool_calls': tool_calls if tool_calls else []
            }

            return MessageWrapper(response_data)
        except Exception as e:
            # Return an error message wrapped in MessageWrapper
            error_response = {
                'role': 'assistant',
                'content': f"Error calling Zhipu AI API: {str(e)}",
                'tool_calls': []
            }
            return MessageWrapper(error_response)