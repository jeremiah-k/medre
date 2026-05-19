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
# Dependency logger defaults
# ---------------------------------------------------------------------------

# Sensible levels for noisy third-party loggers.  Applied by setup_logging()
# before user overrides so that overrides take precedence.
_DEPENDENCY_DEFAULTS: dict[str, int] = {
    "nio": logging.WARNING,
    "nio.crypto.log": logging.ERROR,
    "aiohttp": logging.WARNING,
    "meshtastic": logging.WARNING,
    "peewee": logging.WARNING,
    "urllib3": logging.WARNING,
    "serial": logging.WARNING,
}

# Valid logging level names accepted by setup_logging / overrides.
_VALID_LEVEL_NAMES: frozenset[str] = frozenset(
    {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
)

# Private attribute used to mark the MEDRE-managed console handler on the
# root logger so it can be identified and updated across repeated calls.
_MEDRE_HANDLER_ATTR = "_medre_console_handler"

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


def setup_logging(
    level: str = "INFO",
    json_format: bool = False,
    overrides: dict[str, str] | None = None,
) -> None:
    """Configure structured logging for the medre runtime.

    Creates **one** MEDRE-managed :class:`logging.StreamHandler` writing
    to *stdout* and attaches it to the **Python root logger**.  The
    handler is marked with a private ``_medre_console_handler`` attribute
    so that repeated calls can find, update, and reuse it without
    creating duplicates.

    Handler topology:

    * The handler level is ``NOTSET`` — all filtering happens at the
      individual logger level.
    * The Python root logger level is set to ``NOTSET`` so that records
      which pass their originating logger's level check are forwarded
      to the root and processed by the MEDRE-managed handler.
    * The ``medre`` namespace logger is set to the configured *level*
      with ``propagate=True``.  It does **not** carry its own handler;
      records flow up to the root handler.
    * Any MEDRE-managed handlers previously attached to the ``medre``
      logger (from older versions) are removed while non-MEDRE user
      handlers are preserved.

    The *level* parameter controls **only** the ``medre`` namespace.
    Dependency loggers (``nio``, ``aiohttp``, etc.) receive sensible
    defaults (see ``_DEPENDENCY_DEFAULTS``) which suppress DEBUG noise.
    User-supplied *overrides* take precedence over defaults.

    Parameters
    ----------
    level:
        One of ``DEBUG``, ``INFO``, ``WARNING``, ``ERROR``, ``CRITICAL``.
        Case-insensitive; controls the ``medre.*`` namespace only.
        Defaults to ``INFO`` for unrecognised values.
    json_format:
        If ``True``, use JSON-structured log output suitable for machine
        parsing (log aggregators, structured log files).  Otherwise use
        a human-readable format.
    overrides:
        Per-logger level overrides keyed by logger name.  Applied *after*
        dependency defaults so that user-supplied values take precedence.
        Values must be valid level name strings (e.g. ``"WARNING"``).
        Invalid level names raise :class:`ValueError`.

    Raises
    ------
    ValueError
        If any override level name is not a recognised logging level.
    """
    # 1. Locate or create the single MEDRE-managed handler on the root logger.
    root = logging.getLogger()
    medre_handler: logging.Handler | None = None
    for h in root.handlers:
        if getattr(h, _MEDRE_HANDLER_ATTR, False):
            medre_handler = h
            break

    if medre_handler is None:
        medre_handler = logging.StreamHandler(sys.stdout)
        setattr(medre_handler, _MEDRE_HANDLER_ATTR, True)
        medre_handler.setLevel(logging.NOTSET)
        root.addHandler(medre_handler)

    # 2. Update formatter (supports repeated calls with different modes).
    if json_format:
        medre_handler.setFormatter(_JsonFormatter())
    else:
        medre_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S%z",
            )
        )

    # 3. Set root logger to NOTSET so all records that pass their
    #    originating logger's level check reach our handler.
    root.setLevel(logging.NOTSET)

    # 4. Configure the medre namespace logger.
    medre_logger = logging.getLogger("medre")
    medre_logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    medre_logger.propagate = True

    # Remove any MEDRE-managed handlers left on medre_logger by a
    # previous version of setup_logging.  Preserve non-MEDRE handlers.
    medre_logger.handlers = [
        h for h in medre_logger.handlers
        if not getattr(h, _MEDRE_HANDLER_ATTR, False)
    ]

    # 5. Apply dependency defaults.
    for logger_name, default_level in _DEPENDENCY_DEFAULTS.items():
        logging.getLogger(logger_name).setLevel(default_level)

    # 6. Apply user overrides on top of defaults.
    if overrides:
        for logger_name, level_str in overrides.items():
            upper = level_str.upper()
            if not hasattr(logging, upper) or upper not in _VALID_LEVEL_NAMES:
                raise ValueError(
                    f"Invalid logging level {level_str!r} for logger "
                    f"{logger_name!r}.  Must be one of: "
                    f"{', '.join(sorted(_VALID_LEVEL_NAMES))}"
                )
            logging.getLogger(logger_name).setLevel(getattr(logging, upper))


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
        " ".join(f"{k}={v!r}" for k, v in safe_context.items()) if safe_context else "",
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
