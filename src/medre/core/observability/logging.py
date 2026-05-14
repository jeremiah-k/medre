"""Structured logging setup for the medre runtime.

Provides a simple, stdlib-based logging configuration with optional
JSON-structured output for machine parsing.  All framework loggers
live under the ``medre`` logging namespace.

Public symbols
--------------
* :func:`setup_logging` – configure the root framework logger.
* :func:`get_logger` – obtain a child logger within the framework namespace.
* :func:`log_route_matched` – log that a route was matched for an event.
* :func:`log_route_delivered` – log successful delivery to a route.
* :func:`log_route_failed` – log failed delivery to a route.
* :func:`log_route_loop_prevented` – log loop-prevention skip.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

from medre.observability.sanitization import sanitize_error, sanitize_for_log

# ---------------------------------------------------------------------------
# Log-record internals filter
# ---------------------------------------------------------------------------

# Attributes injected by the logging module itself — never include these as
# extra fields in structured JSON output.
_LOG_RECORD_INTERNALS: frozenset[str] = frozenset(
    {
        "name",
        "msg",
        "args",
        "created",
        "relativeCreated",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "filename",
        "module",
        "pathname",
        "thread",
        "threadName",
        "process",
        "processName",
        "levelname",
        "levelno",
        "message",
        "msecs",
        "taskName",
    }
)


# ---------------------------------------------------------------------------
# JSON formatter
# ---------------------------------------------------------------------------


class _JsonFormatter(logging.Formatter):
    """Minimal JSON log formatter for structured output.

    Each log record is serialised as a single JSON object on one line.
    Safe extra fields attached to the :class:`logging.LogRecord` are
    included under the ``"extra"`` key, with sensitive values redacted.
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

        # Collect safe extra fields (not internal logging attributes).
        extra_fields: dict[str, Any] = {}
        for key, value in record.__dict__.items():
            if key.startswith("_"):
                continue
            if key in _LOG_RECORD_INTERNALS:
                continue
            if key in entry:
                continue
            extra_fields[key] = value
        if extra_fields:
            entry["extra"] = sanitize_for_log(extra_fields)

        return json.dumps(entry, default=str)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def setup_logging(level: str = "INFO", json_format: bool = False) -> None:
    """Configure structured logging for the medre runtime.

    Creates a :class:`logging.StreamHandler` writing to *stdout* and
    attaches it to the ``medre`` root logger.  Repeated
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
    root = logging.getLogger("medre")

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
    """Return a logger within the ``medre`` namespace.

    Parameters
    ----------
    name:
        Dot-separated name appended to the ``medre`` prefix.

    Returns
    -------
    logging.Logger
        A child logger suitable for use in any framework subsystem.
    """
    return logging.getLogger(f"medre.{name}")


# ---------------------------------------------------------------------------
# Diagnostic events
# ---------------------------------------------------------------------------


_diagnostic_logger = logging.getLogger("medre.diagnostics")


def diagnostic_event(
    event_id: str,
    category: str,
    message: str,
    **context: Any,
) -> None:
    """Emit a structured diagnostic log entry.

    Diagnostic events are distinct from regular application logs: they
    carry an explicit *category* and optional key–value *context* so that
    downstream log aggregators can filter and alert on specific failure
    modes.

    Parameters
    ----------
    event_id:
        The canonical event ID this diagnostic relates to.
    category:
        A dot-namespaced category string (e.g. ``"adapter_failure"``,
        ``"replay_skip"``).
    message:
        Human-readable description of the diagnostic condition.
    **context:
        Arbitrary key–value pairs appended to the log entry.
    """
    safe_context = sanitize_for_log(context) if context else {}
    _diagnostic_logger.warning(
        "diagnostic event_id=%s category=%s message=%s %s",
        event_id,
        category,
        message,
        " ".join(f"{k}={v!r}" for k, v in safe_context.items())
        if safe_context
        else "",
    )


# ---------------------------------------------------------------------------
# Route-aware logging
# ---------------------------------------------------------------------------


_route_logger = logging.getLogger("medre.route")


def log_route_matched(*, route_id: str, event_id: str) -> None:
    """Log that an event was matched to a route.

    Parameters
    ----------
    route_id:
        The route that matched.
    event_id:
        The canonical event ID being dispatched.
    """
    _route_logger.debug(
        "route_matched route_id=%s event_id=%s",
        route_id,
        event_id,
    )


def log_route_delivered(*, route_id: str, event_id: str) -> None:
    """Log successful delivery to a route.

    Parameters
    ----------
    route_id:
        The route that was delivered to.
    event_id:
        The canonical event ID that was delivered.
    """
    _route_logger.debug(
        "route_delivered route_id=%s event_id=%s",
        route_id,
        event_id,
    )


def log_route_failed(
    *,
    route_id: str,
    event_id: str,
    error: str,
) -> None:
    """Log a failed delivery to a route.

    Parameters
    ----------
    route_id:
        The route that failed.
    event_id:
        The canonical event ID that failed.
    error:
        Human-readable error description.  Sanitised before logging.
    """
    safe_error = sanitize_error(error)
    _route_logger.warning(
        "route_failed route_id=%s event_id=%s error=%s",
        route_id,
        event_id,
        safe_error,
    )


def log_route_loop_prevented(*, route_id: str, event_id: str) -> None:
    """Log that a delivery was skipped due to loop prevention.

    Parameters
    ----------
    route_id:
        The route whose delivery was prevented.
    event_id:
        The canonical event ID that triggered the loop guard.
    """
    _route_logger.warning(
        "route_loop_prevented route_id=%s event_id=%s",
        route_id,
        event_id,
    )
