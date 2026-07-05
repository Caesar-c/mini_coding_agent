"""Centralized application settings loaded from environment / ``.env`` file.

Usage::

    from src.config import settings

    print(settings.OPENAI_MODEL)
    print(settings.MAX_TOKENS)
    print(settings.get("SOME_OTHER_VAR", "default"))

Each class attribute name mirrors the underlying environment variable name
(e.g. ``OPENAI_BASE_URL`` -> ``settings.OPENAI_BASE_URL``). The ``.env``
file at the project root (parent of ``src/``) is loaded once when this
module is first imported.

:data:`settings` is a process-wide singleton: ``Settings()`` always
returns the same instance.
"""

import os
from pathlib import Path

# Load .env from project root (one level above src/). Only the first call
# mutates the process env; subsequent imports are cheap no-ops.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
try:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:  # python-dotenv is optional; fall back to raw env
    pass


class Settings:
    # ---- OpenAI / OpenAI-compatible API ----
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    OPENAI_BASE_URL: str = os.getenv("OPENAI_BASE_URL", "https://aihubmix.com/v1")
    OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o-free")

    # ---- Zhipu AI (智谱) ----
    ZHIPU_API_KEY: str = os.getenv("ZHIPU_API_KEY", "")
    ZHIPU_MODEL: str = os.getenv("ZHIPU_MODEL", "")

    # ---- Anthropic ----
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    ANTHROPIC_BASE_URL: str = os.getenv("ANTHROPIC_BASE_URL", "")
    ANTHROPIC_MODEL: str = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-6")

    # ---- Agent behaviour ----
    MAX_TOKENS: int = int(os.getenv("MAX_TOKENS", "4096"))
    LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "openai").lower()
    MAX_TOOL_OUTPUT: int = int(os.getenv("MAX_TOOL_OUTPUT", "8000"))
    CONTEXT_MAX_MESSAGES: int = int(os.getenv("CONTEXT_MAX_MESSAGES", "40"))
    CONTEXT_KEEP_RECENT: int = int(os.getenv("CONTEXT_KEEP_RECENT", "12"))

    # ---- Subagent ----
    SUBAGENT_MAX_ITERATIONS: int = int(os.getenv("SUBAGENT_MAX_ITERATIONS", "15"))
    SUBAGENT_MAX_OUTPUT: int = int(os.getenv("SUBAGENT_MAX_OUTPUT", "4096"))
    SUBAGENT_MAX_TOOL_OUTPUT: int = int(os.getenv("SUBAGENT_MAX_TOOL_OUTPUT", "50000"))

    # ---- Skill Loading ----
    SKILL_DIRS: str = os.getenv("SKILL_DIRS", "")
    SKILL_MAX_CONTENT_CHARS: int = int(os.getenv("SKILL_MAX_CONTENT_CHARS", "10000"))

    # ---- Context Compression (Three-Layer) ----
    # Layer 1: Micro — per-message smart truncation
    CONTEXT_MICRO_MAX_CHARS: int = int(os.getenv("CONTEXT_MICRO_MAX_CHARS", "4000"))
    CONTEXT_MICRO_KEEP_HEAD_LINES: int = int(os.getenv("CONTEXT_MICRO_KEEP_HEAD_LINES", "10"))
    CONTEXT_MICRO_KEEP_TAIL_LINES: int = int(os.getenv("CONTEXT_MICRO_KEEP_TAIL_LINES", "15"))
    # Layer 2: Meso — section-level summarization
    CONTEXT_MESO_MESSAGE_THRESHOLD: int = int(os.getenv("CONTEXT_MESO_MESSAGE_THRESHOLD", "20"))
    CONTEXT_MESO_TOKEN_THRESHOLD: int = int(os.getenv("CONTEXT_MESO_TOKEN_THRESHOLD", "8000"))
    CONTEXT_MESO_USE_LLM: bool = os.getenv("CONTEXT_MESO_USE_LLM", "false").lower() == "true"
    # Layer 3: Macro — full context rebuild
    CONTEXT_MACRO_TOKEN_THRESHOLD: int = int(os.getenv("CONTEXT_MACRO_TOKEN_THRESHOLD", "32000"))

    # ---- Sandbox ----
    SANDBOX_ROOT: str = os.getenv("SANDBOX_ROOT", ".")

    # ---- Logging ----
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    _log_file_raw: str = os.getenv("LOG_FILE", str(PROJECT_ROOT / "logs" / "mini_agent.log"))
    # Resolve relative paths against PROJECT_ROOT so the log location
    # doesn't depend on the current working directory at startup.
    LOG_FILE: str = (
        _log_file_raw if os.path.isabs(_log_file_raw) else str(PROJECT_ROOT / _log_file_raw)
    )

    # ---- Generic accessor (for ad-hoc env vars not declared above) ----

    def get(self, name: str, default: str | None = None) -> str | None:
        """Read an arbitrary env var. Useful for one-off lookups."""
        return os.getenv(name, default)

    def api_key_for(self, provider_type_value: str) -> str:
        """Resolve the API key for a provider type by convention.

        Looks up ``{PROVIDER_TYPE_VALUE.upper()}_API_KEY``.
        E.g. ``api_key_for("openai")`` returns ``OPENAI_API_KEY``.
        """
        return os.getenv(f"{provider_type_value.upper()}_API_KEY") or ""


# Process-wide singleton. Import this directly in consuming code.
settings = Settings()
