"""Centralized application settings loaded from environment / ``.env`` file.

Usage::

    from src.config import settings

    print(settings.OPENAI_MODEL)
    print(settings.MAX_TOKENS)
    print(settings.get("SOME_OTHER_VAR", "default"))

Each class attribute name mirrors the underlying environment variable name
(e.g. ``OPENAI_BASE_URL`` -> ``settings.OPENAI_BASE_URL``).

The ``.env`` file is searched in multiple locations (first match wins):

1. Current working directory (``./.env``) — for installed / packaged usage.
2. ``~/.config/mini-agent/.env`` — user-global config.
3. Project root (parent of ``src/``) — for development.

:data:`settings` is a process-wide singleton: ``Settings()`` always
returns the same instance.
"""

import os
from pathlib import Path

# ── .env 搜索顺序 ──────────────────────────────────────────────
# 开发时 PROJECT_ROOT 指向仓库根目录；安装/打包后指向 site-packages 上层，
# 所以把 cwd 和用户配置目录放在更前面。
PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_SEARCH_PATHS = [
    Path.cwd() / ".env",
    Path.home() / ".config" / "mini-agent" / ".env",
    PROJECT_ROOT / ".env",
]

try:
    from dotenv import load_dotenv

    # 按优先级从高到低加载所有找到的 .env 文件。
    # override=False（默认值）确保先加载的文件优先：
    # cwd 的变量 > ~/.config 的变量 > PROJECT_ROOT 的变量。
    # 这样即使 cwd/.env 缺少某些变量（如 API key），
    # ~/.config/mini-agent/.env 或 PROJECT_ROOT/.env 中的值仍会被加载。
    for _env_path in _ENV_SEARCH_PATHS:
        if _env_path.is_file():
            load_dotenv(_env_path, override=False)
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
    # Transient API errors (timeout / connection / 429 / 5xx) are retried up to
    # this many times with exponential backoff. Auth/bad-request errors are not
    # retried.
    LLM_MAX_RETRIES: int = int(os.getenv("LLM_MAX_RETRIES", "3"))
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

    # ---- Task Graph ----
    TASK_GRAPH_DIR: str = os.getenv("TASK_GRAPH_DIR", ".mini_agent")

    # ---- Logging ----
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    _log_file_raw: str = os.getenv("LOG_FILE", str(PROJECT_ROOT / "logs" / "mini_agent.log"))
    # Resolve relative paths against PROJECT_ROOT so the log location
    # doesn't depend on the current working directory at startup.
    LOG_FILE: str = (
        _log_file_raw if os.path.isabs(_log_file_raw) else str(PROJECT_ROOT / _log_file_raw)
    )

    # ---- Generic accessor (for ad-hoc env vars not declared above) ----

    # ---- Generic accessor (for ad-hoc env vars not declared above) ----

    # 显式映射：provider type value → 对应的 Settings 属性名。
    # 仅用于不符合 {TYPE.upper()}_API_KEY 命名约定的情况。
    _API_KEY_ATTR: dict[str, str] = {
        "openai": "OPENAI_API_KEY",
        "zhipu_ai": "ZHIPU_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
    }

    def get(self, name: str, default: str | None = None) -> str | None:
        """Read an arbitrary env var. Useful for one-off lookups."""
        return os.getenv(name, default)

    def api_key_for(self, provider_type_value: str) -> str:
        """Resolve the API key for a provider type.

        Uses an explicit mapping when available, falling back to the
        convention ``{PROVIDER_TYPE_VALUE.upper()}_API_KEY``.
        E.g. ``api_key_for("openai")`` returns ``OPENAI_API_KEY``,
        ``api_key_for("zhipu_ai")`` returns ``ZHIPU_API_KEY``.
        """
        attr = self._API_KEY_ATTR.get(provider_type_value)
        if attr:
            return getattr(self, attr, "") or ""
        return os.getenv(f"{provider_type_value.upper()}_API_KEY") or ""


# Process-wide singleton. Import this directly in consuming code.
settings = Settings()
