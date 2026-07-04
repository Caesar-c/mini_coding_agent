"""Skill directory scanner and SKILL.md parser.

Scans one or more directories for ``SKILL.md`` files, parses their YAML
frontmatter, and provides a query interface for skill descriptions (system
prompt injection) and full content (tool_result injection).
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from config import PROJECT_ROOT, settings
from logger import get_logger

logger = get_logger(__name__)

# Name validation: lowercase alphanumeric + hyphens, no leading hyphen
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# Maximum description length before truncation
_MAX_DESC_LEN = 100


class SkillParseError(Exception):
    """Raised when a SKILL.md file cannot be parsed."""


@dataclass(frozen=True)
class SkillEntry:
    """A parsed skill entry."""

    name: str
    description: str
    body: str  # Markdown body (without frontmatter)
    source: str  # SKILL.md file path (for logging)
    version: str = "1.0"
    author: str = ""
    tags: list[str] = field(default_factory=list)


class SkillLoader:
    """Scans skill directories, parses SKILL.md files, provides query API."""

    def __init__(self, skill_dirs: list[Path] | None = None):
        self._skills: dict[str, SkillEntry] = {}
        self._scan(skill_dirs if skill_dirs is not None else self._default_dirs())

    # --- Public API ---

    def get_descriptions(self) -> str:
        """Return formatted skill catalog for system prompt injection.

        Returns:
            Multi-line string with one line per skill, or empty string if
            no skills are loaded.
        """
        if not self._skills:
            return ""
        lines = ["Available skills:"]
        for entry in sorted(self._skills.values(), key=lambda e: e.name):
            ver = f" (v{entry.version})" if entry.version else ""
            lines.append(f"• {entry.name}{ver} — {entry.description}")
        lines.append('Use load_skill("name") to load full skill content when needed.')
        return "\n".join(lines)

    def get_content(self, name: str, max_chars: int = 10000) -> str | None:
        """Return skill body content, truncated to max_chars.

        Returns:
            The skill body (possibly truncated), or None if not found.
        """
        entry = self._skills.get(name)
        if entry is None:
            return None
        body = entry.body
        if len(body) > max_chars:
            body = body[:max_chars] + "\n\n... [truncated]"
        return body

    def list_names(self) -> list[str]:
        """Return sorted list of all loaded skill names."""
        return sorted(self._skills.keys())

    def get_skill(self, name: str) -> SkillEntry | None:
        """Return the SkillEntry for the given name, or None if not found."""
        return self._skills.get(name)

    @property
    def count(self) -> int:
        """Number of loaded skills."""
        return len(self._skills)

    # --- Internal ---

    @staticmethod
    def _default_dirs() -> list[Path]:
        """Return default skill directories (high priority first)."""
        dirs: list[Path] = []
        # Project-level (highest priority)
        project_skills = PROJECT_ROOT / "skills"
        if project_skills.is_dir():
            dirs.append(project_skills)
        # User-level
        user_skills = Path.home() / ".config" / "mini-agent" / "skills"
        if user_skills.is_dir():
            dirs.append(user_skills)
        return dirs

    def _scan(self, dirs: list[Path]) -> None:
        """Scan all directories, parse SKILL.md files, populate _skills."""
        for skill_dir in dirs:
            if not skill_dir.is_dir():
                logger.debug("Skill dir not found, skipping: %s", skill_dir)
                continue
            for child in sorted(skill_dir.iterdir()):
                if not child.is_dir():
                    continue
                skill_file = child / "SKILL.md"
                if not skill_file.is_file():
                    logger.debug("Skipping %s: no SKILL.md", child.name)
                    continue
                try:
                    entry = self._parse_skill_file(skill_file)
                except (SkillParseError, OSError, UnicodeDecodeError) as e:
                    logger.warning("Skipping skill %s: %s", child.name, e)
                    continue

                if entry.name in self._skills:
                    logger.warning(
                        "Skill name collision: '%s' from %s overridden by %s",
                        entry.name,
                        self._skills[entry.name].source,
                        skill_file,
                    )
                self._skills[entry.name] = entry
                logger.info("Loaded skill: %s from %s", entry.name, skill_file)

        logger.info(
            "Skill scan complete: %d skills loaded from %d directories",
            len(self._skills),
            len(dirs),
        )

    @staticmethod
    def _parse_skill_file(path: Path) -> SkillEntry:
        """Parse a single SKILL.md file into a SkillEntry."""
        content = path.read_text(encoding="utf-8")
        meta, body = SkillLoader._split_frontmatter(content)

        # Validate required fields
        if "name" not in meta:
            raise SkillParseError("Missing required field: 'name'")
        if "description" not in meta:
            raise SkillParseError("Missing required field: 'description'")

        name = str(meta["name"])
        if not _NAME_RE.match(name):
            raise SkillParseError(f"Invalid name format: '{name}'")

        description = str(meta["description"])
        if len(description) > _MAX_DESC_LEN:
            description = description[:_MAX_DESC_LEN] + "..."

        tags = meta.get("tags", [])
        if not isinstance(tags, list):
            tags = [str(tags)]
        else:
            tags = [str(t) for t in tags]

        return SkillEntry(
            name=name,
            description=description,
            body=body.strip(),
            source=str(path),
            version=str(meta.get("version", "1.0")),
            author=str(meta.get("author", "")),
            tags=tags,
        )

    @staticmethod
    def _split_frontmatter(content: str) -> tuple[dict, str]:
        """Split YAML frontmatter from markdown body.

        Expects content to start with ``---``, followed by YAML, followed
        by another ``---``, followed by the markdown body.
        """
        if not content.startswith("---"):
            raise SkillParseError("File must start with '---' YAML frontmatter delimiter")
        parts = content.split("---", 2)
        if len(parts) < 3:
            raise SkillParseError("Missing closing '---' for YAML frontmatter")

        yaml_str = parts[1]
        body = parts[2]

        meta = yaml.safe_load(yaml_str) or {}
        if not isinstance(meta, dict):
            raise SkillParseError("Frontmatter must be a YAML mapping")

        return meta, body


def build_skill_loader() -> SkillLoader:
    """Build a SkillLoader from settings.SKILL_DIRS + default directories.

    Extra directories from ``SKILL_DIRS`` (comma-separated) are prepended
    to the default directory list, giving them the highest priority.
    """
    dirs: list[Path] = []
    # Extra dirs from config (highest priority, prepended)
    if settings.SKILL_DIRS:
        for d in settings.SKILL_DIRS.split(","):
            d = d.strip()
            if d:
                dirs.append(Path(d))
    # Default dirs (reuses _default_dirs to avoid path duplication)
    dirs.extend(SkillLoader._default_dirs())

    return SkillLoader(skill_dirs=dirs)


def build_system_prompt(base_prompt: str) -> tuple[str, "SkillLoader"]:
    """Build a SkillLoader and enhance the system prompt with the skill catalog.

    Shared helper used by both ``AsyncAgent.__init__`` and ``Agent.__init__``
    to avoid duplicating the skill-loading bootstrap sequence.

    Args:
        base_prompt: The base system prompt (e.g. ``SYSTEM_PROMPT``).

    Returns:
        A tuple of ``(enhanced_prompt, skill_loader)``.  If no skills are
        found the prompt is returned unchanged.
    """
    skill_loader = build_skill_loader()
    enhanced_prompt = base_prompt
    skill_catalog = skill_loader.get_descriptions()
    if skill_catalog:
        enhanced_prompt += f"\n\n{skill_catalog}"
    return enhanced_prompt, skill_loader
