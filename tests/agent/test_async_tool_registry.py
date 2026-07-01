"""Tests for AsyncToolRegistry."""

import asyncio
import unittest


class TestAsyncToolRegistry(unittest.TestCase):
    """Tests for AsyncToolRegistry."""

    def test_register_and_execute_async_handler(self):
        """Async handlers are awaited correctly."""
        from agent.async_tool_registry import AsyncToolRegistry

        registry = AsyncToolRegistry()

        async def async_echo(args):
            return f"echo: {args.get('text', '')}"

        definition = {
            "type": "function",
            "function": {
                "name": "async_echo",
                "description": "test",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        registry.register(definition, async_echo)

        result = asyncio.run(registry.execute("async_echo", {"text": "hello"}))
        self.assertEqual(result, "echo: hello")

    def test_register_and_execute_sync_handler(self):
        """Sync handlers work transparently in the async registry."""
        from agent.async_tool_registry import AsyncToolRegistry

        registry = AsyncToolRegistry()

        def sync_echo(args):
            return f"sync: {args.get('text', '')}"

        definition = {
            "type": "function",
            "function": {
                "name": "sync_echo",
                "description": "test",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        registry.register(definition, sync_echo)

        result = asyncio.run(registry.execute("sync_echo", {"text": "world"}))
        self.assertEqual(result, "sync: world")

    def test_unknown_tool_returns_error(self):
        from agent.async_tool_registry import AsyncToolRegistry

        registry = AsyncToolRegistry()
        result = asyncio.run(registry.execute("nonexistent", {}))
        self.assertIn("Error", result)

    def test_handler_exception_returns_error(self):
        from agent.async_tool_registry import AsyncToolRegistry

        registry = AsyncToolRegistry()

        async def failing_handler(args):
            raise RuntimeError("boom")

        definition = {
            "type": "function",
            "function": {
                "name": "fail",
                "description": "test",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        registry.register(definition, failing_handler)

        result = asyncio.run(registry.execute("fail", {}))
        self.assertIn("Error", result)
        self.assertIn("boom", result)

    def test_definitions_property(self):
        from agent.async_tool_registry import AsyncToolRegistry

        registry = AsyncToolRegistry()
        defs = registry.definitions
        # Should have at least the 6 built-in async tools
        self.assertGreaterEqual(len(defs), 6)
        names = [d["function"]["name"] for d in defs]
        self.assertIn("bash", names)
        self.assertIn("read_file", names)

    def test_mixed_sync_async_handlers(self):
        """Sync and async handlers can coexist in the same registry."""
        from agent.async_tool_registry import AsyncToolRegistry

        registry = AsyncToolRegistry()

        async def async_add(args):
            return str(args.get("a", 0) + args.get("b", 0))

        def sync_mul(args):
            return str(args.get("a", 0) * args.get("b", 0))

        for name, handler in [("add", async_add), ("mul", sync_mul)]:
            registry.register(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": "test",
                        "parameters": {"type": "object", "properties": {}},
                    },
                },
                handler,
            )

        self.assertEqual(asyncio.run(registry.execute("add", {"a": 2, "b": 3})), "5")
        self.assertEqual(asyncio.run(registry.execute("mul", {"a": 2, "b": 3})), "6")


if __name__ == "__main__":
    unittest.main()
