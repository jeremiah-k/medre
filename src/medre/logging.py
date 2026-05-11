"""Structured logging helpers for the MEDRE runtime.

Provides adapter-scoped loggers, startup/shutdown summary formatters,
duration helpers, and secret-filtering utilities.

This module is the *single source of truth* for structured log output.
CLI commands and runtime components should use these helpers so that
adapter_id, transport, and timing context appear consistently in every
log line.

**Invariant:** No secrets, tokens, device keys, or crypto material
ever appear in log output produced by this module.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import asdict, is_dataclass
from typing import Any, Mapping

__all__ = [
    "adapter_logger",
    "format_duration_ms",
    "startup_summary",
    "shutdown_summary",
    "sanitize_for_log",
]

# ---------------------------------------------------------------------------
# Secret-key detection
# ---------------------------------------------------------------------------

# Patterns match the set in medre.core.runtime.diagnostic_contract
# duplicated here to avoid a circular or heavy import at the logging layer.
_SECRET_KEY_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"^password$",
        r"^secret",
        r"^private_?key",
        r"^access_?token",
        r"^auth_?token",
        r"^api_?key",
        r"^credentials?$",
        r"^session_?secret",
        r"^encryption_?key",
        r"^device_?key",
        r"^signing_?key",
        r"^identity_?key",
    )
)

_SAFE_SCALAR = (bool, int, float, str, type(None))


def _is_secret_key(key: str) -> bool:
    """Return True if *key* matches a known secret/token pattern."""
    return any(p.search(key) for p in _SECRET_KEY_PATTERNS)


def _sanitize_value(value: Any) -> Any:
    """Coerce *value* into a log-safe form."""
    if isinstance(value, _SAFE_SCALAR):
        return value
    if isinstance(value, dict):
        return sanitize_for_log(value)
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_sanitize_value(v) for v in value]
    try:
        return f"<{type(value).__name__}>"
    except Exception:
        return "<object>"


def sanitize_for_log(data: Mapping[str, Any] | dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *data* with secret keys removed and values sanitized.

    This is the public entry-point for stripping tokens/passwords/keys
    before emitting structured log records.
    """
    out: dict[str, Any] = {}
    for key, value in data.items():
        if _is_secret_key(key):
            continue
        out[key] = _sanitize_value(value)
    return out


# ---------------------------------------------------------------------------
# Adapter-scoped logger factory
# ---------------------------------------------------------------------------

_ADAPTER_LOGGER_CACHE: dict[str, logging.LoggerAdapter[logging.Logger]] = {}


def adapter_logger(
    name: str,
    adapter_id: str,
    transport: str,
) -> logging.LoggerAdapter[logging.Logger]:
    """Return a :class:`logging.LoggerAdapter` that injects adapter context.

    Every message logged through the returned adapter automatically carries
    ``extra={"adapter_id": ..., "transport": ...}`` so that formatters and
    handlers can include structured context without manual effort.

    Parameters
    ----------
    name:
        Base logger name (e.g. ``"medre.adapters"``).
    adapter_id:
        Unique adapter identifier (e.g. ``"matrix.main"``).
    transport:
        Transport type (e.g. ``"matrix"``, ``"meshtastic"``).

    Returns
    -------
    logging.LoggerAdapter
        Adapter-scoped logger with structured extra context.
    """
    cache_key = f"{name}:{adapter_id}:{transport}"
    if cache_key in _ADAPTER_LOGGER_CACHE:
        return _ADAPTER_LOGGER_CACHE[cache_key]

    base = logging.getLogger(name)
    extra: dict[str, str] = {
        "adapter_id": adapter_id,
        "transport": transport,
    }
    adapter = logging.LoggerAdapter(base, extra)
    _ADAPTER_LOGGER_CACHE[cache_key] = adapter
    return adapter


# ---------------------------------------------------------------------------
# Duration formatting
# ---------------------------------------------------------------------------

def format_duration_ms(start_time: float, end_time: float | None = None) -> str:
    """Return a human-readable duration string from *start_time*.

    Parameters
    ----------
    start_time:
        A ``time.monotonic()`` value captured before the operation.
    end_time:
        A ``time.monotonic()`` value captured after the operation.
        Defaults to ``time.monotonic()`` (i.e. "now").

    Returns
    -------
    str
        Human-friendly duration like ``"123ms"``, ``"1.2s"``, or ``"45µs"``.
    """
    if end_time is None:
        end_time = time.monotonic()
    elapsed_ms = (end_time - start_time) * 1000.0
    if elapsed_ms < 1.0:
        return f"{elapsed_ms * 1000:.0f}µs"
    if elapsed_ms < 1000.0:
        return f"{elapsed_ms:.0f}ms"
    return f"{elapsed_ms / 1000:.1f}s"


# ---------------------------------------------------------------------------
# Startup / shutdown summaries
# ---------------------------------------------------------------------------

def startup_summary(
    results: list[tuple[str, str, bool, float, str | None]],
) -> str:
    """Build a multi-line startup summary string.

    Parameters
    ----------
    results:
        List of ``(adapter_id, transport, success, duration_seconds, error)``
        tuples — one per adapter startup attempt.

    Returns
    -------
    str
        Formatted multi-line summary ready for printing.
    """
    if not results:
        return "Runtime starting with 0 adapters."

    lines: list[str] = []
    ids = ", ".join(r[0] for r in results)
    lines.append(f"Runtime starting with {len(results)} adapter(s): {ids}")

    succeeded = 0
    failed = 0
    for adapter_id, transport, success, duration_s, error in results:
        dur = format_duration_ms(0.0, duration_s) if duration_s > 0 else "0ms"
        if success:
            lines.append(f"  \u2713 {transport}.{adapter_id} started ({dur})")
            succeeded += 1
        else:
            err_msg = error or "unknown error"
            lines.append(f"  \u2717 {transport}.{adapter_id}: failed ({err_msg})")
            failed += 1

    if failed == 0:
        lines.append(f"Runtime ready ({succeeded} adapter(s) running)")
    else:
        lines.append(
            f"Runtime ready ({succeeded}/{succeeded + failed} adapter(s) running, {failed} failed)"
        )

    return "\n".join(lines)


def shutdown_summary(
    adapter_ids: list[str],
    errors: list[tuple[str, str]] | None = None,
) -> str:
    """Build a multi-line shutdown summary string.

    Parameters
    ----------
    adapter_ids:
        List of adapter IDs that were shut down.
    errors:
        Optional list of ``(adapter_id, error_message)`` tuples for
        adapters that failed during shutdown.

    Returns
    -------
    str
        Formatted multi-line summary.
    """
    lines: list[str] = ["Runtime shutting down"]

    for aid in adapter_ids:
        lines.append(f"  stopping {aid}")

    if errors:
        for aid, err in errors:
            lines.append(f"  \u2717 {aid}: shutdown error ({err})")

    lines.append("Runtime stopped")
    return "\n".join(lines)
