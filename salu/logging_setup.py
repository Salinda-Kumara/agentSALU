"""Structured logging with mandatory secret redaction.

Format:  2026-10-03 10:32:14 INFO  salu.agent  Tool selected: system_status
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from collections import deque
from pathlib import Path

from .security.redact import redact

FORMAT = "%(asctime)s %(levelname)-5s %(name)s  %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"


class RedactingFilter(logging.Filter):
    """Redacts secrets from every record before any handler formats it."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        record.msg = redact(msg)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


class MemoryLogHandler(logging.Handler):
    """Keeps recent formatted log lines for the UI log panel."""

    def __init__(self, capacity: int = 500):
        super().__init__()
        self.lines: deque[str] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(self.format(record))
        except Exception:
            self.handleError(record)


memory_handler = MemoryLogHandler()


def setup_logging(level: str = "INFO", file: Path | None = None, max_bytes: int = 2_000_000,
                  backup_count: int = 5, console: bool = True) -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(logging.DEBUG)

    fmt = logging.Formatter(FORMAT, DATEFMT)
    redactor = RedactingFilter()
    lvl = getattr(logging, level.upper(), logging.INFO)

    handlers: list[logging.Handler] = [memory_handler]
    if console:
        handlers.append(logging.StreamHandler(sys.stderr))
    if file is not None:
        file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"))

    for h in handlers:
        h.setLevel(lvl)
        h.setFormatter(fmt)
        h.addFilter(redactor)
        root.addHandler(h)

    # Third-party noise.
    for noisy in ("httpx", "httpcore", "asyncio", "mcp", "uvicorn.access", "watchfiles"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
