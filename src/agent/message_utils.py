"""Shared helpers for converting LLM responses and parsing tool calls.

Eliminates duplication across ``agent.loop``, ``agent.async_loop``, and
``agent.subagent`` — all three process LLM responses with the same
message-formatting and tool-call extraction logic.
"""

import json

# Sentinel used by tc_attr to distinguish "key missing" from "key present but None".
_MISSING = object()


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


def response_to_dict(message) -> dict:
    """Convert an LLM response object to a dict suitable for appending to messages.

    Handles both Pydantic models (with ``model_dump``) and lightweight
    wrappers like :class:`MessageWrapper`. Appends the message whenever
    there is *either* content or tool_calls — not just when content is
    truthy, since OpenAI returns ``content=None`` alongside tool_calls.

    Returns:
        A dict with at least ``role`` and ``content`` keys. May include
        ``tool_calls`` if present on the response.
    """
    if hasattr(message, "model_dump"):
        return message.model_dump(exclude_unset=True)

    msg_dict = {
        "role": getattr(message, "role", "assistant"),
        "content": getattr(message, "content", None),
    }
    tool_calls = extract_tool_calls(message)
    if tool_calls:
        msg_dict["tool_calls"] = tool_calls
    return msg_dict


def extract_tool_calls(message) -> list:
    """Extract tool_calls from an LLM response, handling multiple formats.

    Tries direct attribute access first (Pydantic / MessageWrapper), then
    falls back to ``message.data["tool_calls"]`` for raw dict wrappers.

    Returns:
        A list of tool call objects, or an empty list if none are present.
    """
    if hasattr(message, "tool_calls"):
        return message.tool_calls or []
    return getattr(message, "data", {}).get("tool_calls", [])


def parse_tool_call(tool_call) -> tuple[str, dict, str]:
    """Destructure a tool call object into (tool_name, args_dict, tool_call_id).

    Uses :func:`tc_attr` for dot-notation access that works with both
    dict and object representations.

    Returns:
        A tuple of ``(tool_name, parsed_args, tool_call_id)``.
    """
    tool_name = tc_attr(tool_call, "function.name", "")
    raw_args = tc_attr(tool_call, "function.arguments", "{}")
    args = json.loads(raw_args) if isinstance(raw_args, str) and raw_args else {}
    tc_id = tc_attr(tool_call, "id", "")
    return tool_name, args, tc_id
