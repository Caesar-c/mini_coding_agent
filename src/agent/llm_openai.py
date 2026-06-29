"""OpenAI LLM Provider Implementation."""

from typing import List, Dict, Any, Optional
from openai import OpenAI
from openai.types.chat import ChatCompletionMessage
from llm_interface import LLMProvider, MessageWrapper


class OpenAILLMProvider(LLMProvider):
    """Implementation of LLMProvider for OpenAI-compatible APIs."""

    def __init__(self, api_key: str = None, base_url: str = None):
        self.client = OpenAI(
            api_key=api_key or "",
            base_url=base_url or "https://api.openai.com/v1"
        )

    def chat_completion(self,
                       messages: List[Dict[str, str]],
                       tools: Optional[List[Dict[str, Any]]] = None,
                       model: str = "gpt-3.5-turbo",
                       max_tokens: int = 4096,
                       temperature: float = 0.7,
                       **kwargs) -> ChatCompletionMessage:
        """
        Generate a chat completion using OpenAI API.

        Args:
            messages: List of message dictionaries with 'role' and 'content'
            tools: Optional list of tool definitions
            model: Model identifier
            max_tokens: Maximum number of tokens to generate
            temperature: Sampling temperature
            **kwargs: Additional parameters

        Returns:
            ChatCompletionMessage object
        """
        # Prepare the API call
        api_params = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        # Add tools if provided
        if tools:
            api_params["tools"] = tools

        # Add any additional parameters
        api_params.update(kwargs)

        # Make the API call
        response = self.client.chat.completions.create(**api_params)
        return response.choices[0].message