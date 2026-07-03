"""Subagent — context-isolated subtask execution.

The parent agent dispatches a subagent via the ``task`` tool. The subagent
runs in its own messages list, executes child tools (bash, file ops), and
returns only a text summary. All intermediate messages are discarded.
"""

import asyncio
from collections.abc import Callable

from agent.async_tool_registry import AsyncToolRegistry
from agent.message_utils import extract_tool_calls, parse_tool_call, response_to_dict
from config import settings
from logger import get_logger

logger = get_logger(__name__)

SUBAGENT_SYSTEM_PROMPT = """\
You are a subagent executing a specific task. You have access to bash \
commands and file operation tools. Complete the task thoroughly but \
concisely. Your final text response will be returned to the parent agent \
as a summary — make it clear and self-contained.

Rules:
- Do NOT ask the user questions — work autonomously.
- Return a concise summary of your findings or actions.
- If you encounter errors, report them clearly.
"""

TASK_TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "task",
        "description": (
            "Spawn a subagent with fresh context to perform a subtask. "
            "The subagent has access to bash, file read/write, and directory "
            "tools but NOT the task tool (no recursive spawning). "
            "Only the subagent's final text summary is returned — all "
            "intermediate tool calls and outputs are discarded. "
            "Use this for multi-step research, file exploration, or any "
            "task that would clutter the main conversation context."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "The task description for the subagent to execute.",
                }
            },
            "required": ["prompt"],
        },
    },
}


async def run_subagent(
    prompt: str,
    llm_provider,
    child_tools: list[dict],
    child_registry: AsyncToolRegistry,
    system_prompt: str,
    max_iterations: int = 30,
    max_output_chars: int = 2000,
    max_tool_output: int = 50000,
) -> str:
    """Spawn a subagent with fresh context, execute tasks, return summary text.

    The subagent runs in its own independent ``messages`` list. All intermediate
    tool calls and outputs are discarded after the subagent completes — only the
    final text summary is returned to the caller.

    Args:
        prompt: The task description for the subagent.
        llm_provider: LLMProvider instance (reused from parent agent).
        child_tools: Tool definitions available to the subagent (no ``task``).
        child_registry: AsyncToolRegistry with only child tools registered.
        system_prompt: System prompt for the subagent.
        max_iterations: Maximum number of LLM call iterations.
        max_output_chars: Maximum characters in the returned summary.
        max_tool_output: Maximum characters per tool result in sub-context.

    Returns:
        A text summary of the subagent's findings or actions.
    """
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt},
    ]

    logger.info("Subagent spawned: prompt=%.2000s, max_iter=%d", prompt, max_iterations)

    last_text = ""
    for iteration in range(1, max_iterations + 1):
        response = await asyncio.to_thread(
            llm_provider.chat_completion,
            messages=messages,
            tools=child_tools,
        )

        # Extract tool_calls first so we know whether to append the message.
        # OpenAI returns content=None alongside tool_calls — the message MUST
        # be appended even when content is falsy, otherwise tool results will
        # be orphaned and the next API call will fail.
        tool_calls = extract_tool_calls(response)
        content = getattr(response, "content", None)

        logger.info(
            "Subagent iteration %d: content_len=%d, tool_calls=%d",
            iteration,
            len(content or ""),
            len(tool_calls),
        )

        # Append assistant message when it carries content or tool_calls
        if content or tool_calls:
            messages.append(response_to_dict(response))

        # Track partial text even when tool_calls are present (for iteration-limit fallback)
        if content:
            last_text = content

        if not tool_calls:
            # No tool calls — subagent is done
            last_text = content or ""
            logger.info(
                "Subagent finished: iterations=%d, result=%.2000s",
                iteration,
                last_text,
            )
            break

        # Execute tool calls sequentially (clearer logs, simpler error handling)
        for tc in tool_calls:
            tool_name, args, tc_id = parse_tool_call(tc)

            logger.info("Subagent tool call: %s, args=%s", tool_name, str(args)[:2000])

            output = await child_registry.execute(tool_name, args)

            # Per-tool output cap with logging (consistent with parent agents)
            if len(output) > max_tool_output:
                logger.warning(
                    "Subagent tool output truncated: %d chars (max %d)",
                    len(output),
                    max_tool_output,
                )
                output = output[:max_tool_output] + f"\n... [truncated, {len(output)} chars total]"

            logger.info(
                "Subagent tool result: %s, output_len=%d, output=%.2000s",
                tool_name,
                len(output),
                output,
            )

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": output,
                }
            )

    else:
        # Loop exhausted without break — hit iteration limit
        logger.warning("Subagent hit iteration limit: %d", max_iterations)
        last_text = last_text or "(subagent reached iteration limit without completing)"

    # Truncate final output
    if len(last_text) > max_output_chars:
        last_text = last_text[:max_output_chars] + "\n... [truncated]"

    return last_text


def make_task_handler(agent) -> Callable:
    """Create a task handler closure bound to the parent agent's provider and config.

    Captures only the specific attributes needed (``llm_provider`` and
    ``_child_registry``) rather than the entire agent object, avoiding a
    reference cycle that would keep ``agent.messages`` alive.

    Args:
        agent: The parent AsyncAgent instance.

    Returns:
        An async handler function for the ``task`` tool.
    """
    # Capture minimal references to avoid agent → registry → handler → agent cycle
    llm_provider = agent.llm_provider
    child_registry = agent._child_registry

    # Build subagent system prompt with skill catalog (if available)
    subagent_prompt = SUBAGENT_SYSTEM_PROMPT
    try:
        if agent.skill_loader.count > 0:
            subagent_prompt += f"\n\n{agent.skill_loader.get_descriptions()}"
    except (AttributeError, TypeError):
        logger.debug("Skill catalog not available for subagent prompt")

    async def run_task(args: dict) -> str:
        prompt = args.get("prompt", "")
        if not prompt:
            return "Error: 'prompt' is required."

        logger.info("Task tool invoked: prompt=%.2000s", prompt)

        return await run_subagent(
            prompt=prompt,
            llm_provider=llm_provider,
            child_tools=child_registry.definitions,
            child_registry=child_registry,
            system_prompt=subagent_prompt,
            max_iterations=settings.SUBAGENT_MAX_ITERATIONS,
            max_output_chars=settings.SUBAGENT_MAX_OUTPUT,
            max_tool_output=settings.SUBAGENT_MAX_TOOL_OUTPUT,
        )

    return run_task
