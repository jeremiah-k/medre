"""Structured logging setup for the meshnet framework runtime.

Provides a simple, stdlib-based logging configuration with optional
JSON-structured output for machine parsing.  All framework loggers
live under the ``meshnet_framework`` logging namespace.

Public symbols
--------------
* :func:`setup_logging` – configure the root framework logger.
* :func:`get_logger` – obtain a child logger within the framework namespace.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# JSON formatter
# ---------------------------------------------------------------------------


class _JsonFormatter(logging.Formatter):
    """Minimal JSON log formatter for structured output.

    Each log record is serialised as a single JSON object on one line.
    """

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[1] is not None:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def setup_logging(level: str = "INFO", json_format: bool = False) -> None:
    """Configure structured logging for the meshnet framework runtime.

    Creates a :class:`logging.StreamHandler` writing to *stdout* and
    attaches it to the ``meshnet_framework`` root logger.  Repeated
    calls are no-ops (duplicate handlers are avoided).

    Parameters
    ----------
    level:
        One of ``DEBUG``, ``INFO``, ``WARNING``, ``ERROR``, ``CRITICAL``.
        Case-insensitive; defaults to ``INFO`` for unrecognised values.
    json_format:
        If ``True``, use JSON-structured log output suitable for machine
        parsing (log aggregators, structured log files).  Otherwise use
        a human-readable format.
    """
    root = logging.getLogger("meshnet_framework")

    # Avoid duplicate handlers on repeated calls.
    if root.handlers:
        return

    handler = logging.StreamHandler(sys.stdout)

    if json_format:
        handler.setFormatter(_JsonFormatter())
    else:
        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
        handler.setFormatter(formatter)

    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    """Return a logger within the ``meshnet_framework`` namespace.

    Parameters
    ----------
    name:
        Dot-separated name appended to the ``meshnet_framework`` prefix.

    Returns
    -------
    logging.Logger
        A child logger suitable for use in any framework subsystem.
    """
    return logging.getLogger(f"meshnet_framework.{name}")
