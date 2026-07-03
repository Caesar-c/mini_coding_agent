"""load_skill tool definition and handler factory.

The :func:`make_load_skill_handler` creates a closure bound to a
:class:`SkillLoader` instance, suitable for registration in both
sync and async tool registries.
"""

from collections.abc import Callable

from logger import get_logger
from skills.loader import SkillLoader

logger = get_logger(__name__)

LOAD_SKILL_TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "load_skill",
        "description": (
            "Load the full content of a domain knowledge skill by name. "
            "Available skills are listed in the system prompt. "
            "Use this when you need specialized knowledge for a task — "
            "e.g., load 'code-review' before reviewing code, "
            "or 'git-workflow' before managing branches."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "The skill name to load (e.g., 'code-review'). "
                        "See system prompt for available skills."
                    ),
                }
            },
            "required": ["name"],
        },
    },
}


def make_load_skill_handler(
    skill_loader: SkillLoader,
    max_chars: int = 10000,
) -> Callable:
    """Create a load_skill handler closure bound to a SkillLoader instance.

    Args:
        skill_loader: The SkillLoader instance to query.
        max_chars: Maximum character count for skill body content.

    Returns:
        A callable ``(args: dict) -> str`` suitable for tool registry registration.
    """

    def load_skill(args: dict) -> str:
        name = args.get("name", "").strip()
        if not name:
            available = ", ".join(skill_loader.list_names()) or "(none)"
            return f"Error: 'name' is required. Available skills: {available}"

        content = skill_loader.get_content(name, max_chars=max_chars)
        if content is None:
            available = ", ".join(skill_loader.list_names()) or "(none)"
            logger.warning("Skill not found: %s (available: %s)", name, available)
            return f"Error: Skill '{name}' not found. " f"Available skills: {available}"

        logger.info("Skill loaded: %s, content_len=%d", name, len(content))
        return content

    return load_skill
