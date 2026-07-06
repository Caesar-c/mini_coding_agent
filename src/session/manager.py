"""Session manager — manages multiple named AsyncAgent instances."""

from typing import Any

from agent.async_loop import AsyncAgent
from logger import get_logger

logger = get_logger(__name__)


class SessionManager:
    """Manages multiple concurrent chat sessions.

    Each session is a named :class:`AsyncAgent` instance with its own
    conversation history, task graph, and context state. Users
    can create, switch between, and list sessions.
    """

    def __init__(self):
        self._sessions: dict[str, AsyncAgent] = {}
        self._active: str | None = None

    def create(self, name: str, **kwargs: Any) -> AsyncAgent:
        """Create a new session and make it active.

        Args:
            name: Unique name for the session.
            **kwargs: Forwarded to :class:`AsyncAgent` constructor.

        Returns:
            The newly created agent.

        Raises:
            ValueError: If a session with this name already exists.
        """
        if name in self._sessions:
            raise ValueError(f"Session '{name}' already exists.")
        kwargs.setdefault("session_id", name)
        agent = AsyncAgent(**kwargs)
        self._sessions[name] = agent
        self._active = name
        logger.info("Session created: '%s' (active)", name)
        return agent

    def switch(self, name: str) -> AsyncAgent:
        """Switch to an existing session.

        Returns:
            The agent for the switched-to session.

        Raises:
            KeyError: If no session with this name exists.
        """
        if name not in self._sessions:
            raise KeyError(f"Session '{name}' not found.")
        self._active = name
        logger.info("Switched to session: '%s'", name)
        return self._sessions[name]

    def remove(self, name: str) -> None:
        """Remove a session.

        Raises:
            KeyError: If no session with this name exists.
        """
        if name not in self._sessions:
            raise KeyError(f"Session '{name}' not found.")
        del self._sessions[name]
        if self._active == name:
            self._active = next(iter(self._sessions), None)
        logger.info("Session removed: '%s' (active now: %s)", name, self._active)

    def list_sessions(self) -> list[str]:
        """Return names of all active sessions."""
        return list(self._sessions.keys())

    @property
    def active(self) -> AsyncAgent | None:
        """Return the currently active agent, or None if no sessions exist."""
        if self._active is None:
            return None
        return self._sessions.get(self._active)

    @property
    def active_name(self) -> str | None:
        """Return the name of the currently active session."""
        return self._active

    def get(self, name: str) -> AsyncAgent | None:
        """Get a session by name without switching."""
        return self._sessions.get(name)
