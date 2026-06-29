"""Tests for :mod:`src.llm.interface`."""

import unittest

from src.llm.interface import LLMProvider, MessageWrapper


class TestLLMProviderAbstract(unittest.TestCase):
    """Verify that :class:`LLMProvider` is properly abstract."""

    def test_cannot_instantiate_directly(self):
        with self.assertRaises(TypeError):
            LLMProvider()

    def test_concrete_subclass_must_implement_chat_completion(self):
        class Incomplete(LLMProvider):
            pass

        with self.assertRaises(TypeError):
            Incomplete()

    def test_concrete_subclass_with_implementation(self):
        class Complete(LLMProvider):
            def chat_completion(self, messages, **kwargs):
                return MessageWrapper({"role": "assistant", "content": "ok"})

        provider = Complete()
        result = provider.chat_completion([{"role": "user", "content": "hi"}])
        self.assertEqual(result.content, "ok")


class TestMessageWrapper(unittest.TestCase):
    """Verify :class:`MessageWrapper` exposes a uniform interface."""

    def test_content_property(self):
        wrapper = MessageWrapper({"content": "hello", "role": "assistant"})
        self.assertEqual(wrapper.content, "hello")

    def test_role_property(self):
        wrapper = MessageWrapper({"role": "user", "content": "hi"})
        self.assertEqual(wrapper.role, "user")

    def test_role_defaults_to_assistant(self):
        wrapper = MessageWrapper({"content": "x"})
        self.assertEqual(wrapper.role, "assistant")

    def test_tool_calls_defaults_to_empty(self):
        wrapper = MessageWrapper({"content": "x"})
        self.assertEqual(wrapper.tool_calls, [])

    def test_tool_calls_returned_when_present(self):
        calls = [{"id": "1", "function": {"name": "foo", "arguments": "{}"}}]
        wrapper = MessageWrapper({"content": "x", "tool_calls": calls})
        self.assertEqual(wrapper.tool_calls, calls)

    def test_model_dump_returns_underlying_data(self):
        data = {"role": "assistant", "content": "ok"}
        wrapper = MessageWrapper(data)
        self.assertEqual(wrapper.model_dump(), data)

    def test_content_returns_none_when_missing(self):
        wrapper = MessageWrapper({})
        self.assertIsNone(wrapper.content)


if __name__ == "__main__":
    unittest.main()
