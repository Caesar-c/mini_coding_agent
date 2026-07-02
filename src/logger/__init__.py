"""Centralized logging configuration for mini-coding-agent.

Usage::

    from logger import get_logger
    logger = get_logger(__name__)
    logger.info("something happened")

Call :func:`setup_logging` once at application startup (typically in the CLI
entry point) to configure handlers, level, and optional file output.
"""

import logging
import sys
from logging.handlers import RotatingFileHandler

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Track whether setup_logging has been called to avoid duplicate handlers.
_initialized = False


def setup_logging(
    level: str = "INFO",
    log_file: str = "",
) -> None:
    """Configure the root logger with console and optional file handlers.

    Args:
        level: Logging level name (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        log_file: Path to a log file. When non-empty, a
            :class:`RotatingFileHandler` is added (5 MB, 3 backups).
    """
    global _initialized
    if _initialized:
        return
    _initialized = True

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    # Console handler — writes to stderr so it doesn't interfere with piped stdout.
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    root.addHandler(console)

    # Optional rotating file handler.
    if log_file:
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=5 * 1024 * 1024,  # 5 MB
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)


def get_logger(name: str) -> logging.Logger:
    """Return a named logger.

    Typically called as ``get_logger(__name__)`` so the logger name
    mirrors the module hierarchy (e.g. ``agent.loop``, ``llm.factory``).
    """
    return logging.getLogger(name)
