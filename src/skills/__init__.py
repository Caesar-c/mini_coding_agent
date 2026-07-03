"""Skill loading system — on-demand domain knowledge injection.

Skills are markdown files with YAML frontmatter stored in skill directories.
The :class:`SkillLoader` scans directories at startup and provides a query
interface. The :func:`make_load_skill_handler` factory creates a tool handler
closure bound to a loader instance.
"""

from skills.loader import (
    SkillEntry,
    SkillLoader,
    SkillParseError,
    build_skill_loader,
    build_system_prompt,
)
from skills.skill_tool import LOAD_SKILL_TOOL_DEFINITION, make_load_skill_handler

__all__ = [
    "LOAD_SKILL_TOOL_DEFINITION",
    "SkillEntry",
    "SkillLoader",
    "SkillParseError",
    "build_skill_loader",
    "build_system_prompt",
    "make_load_skill_handler",
]
