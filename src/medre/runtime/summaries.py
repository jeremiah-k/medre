"""Startup/shutdown summary formatters and duration helpers.

Provides:
* :func:`startup_summary` — multi-line startup summary for the runtime.
* :func:`shutdown_summary` — multi-line shutdown summary for the runtime.
* :func:`format_duration_ms` — human-readable duration from monotonic timestamps.
"""

from __future__ import annotations

import time

__all__ = [
    "format_duration_ms",
    "format_duration_seconds",
    "startup_summary",
    "shutdown_summary",
]


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
        Human-friendly duration like ``"123ms"``, ``"1.2s"``, or ``"45us"``.
    """
    if end_time is None:
        end_time = time.monotonic()
    elapsed_ms = (end_time - start_time) * 1000.0
    if elapsed_ms < 1.0:
        return f"{elapsed_ms * 1000:.0f}\u00b5s"
    if elapsed_ms < 1000.0:
        return f"{elapsed_ms:.0f}ms"
    return f"{elapsed_ms / 1000:.1f}s"


def format_duration_seconds(duration_s: float) -> str:
    """Format an elapsed duration in seconds.

    Args:
        duration_s: Elapsed wall-clock time in seconds.

    Returns:
        Formatted duration string like ``"250µs"``, ``"42ms"``, or ``"1.2s"``.
    """
    return format_duration_ms(0.0, duration_s)


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
        tuples -- one per adapter startup attempt.

    Returns
    -------
    str
        Formatted multi-line summary ready for printing.
    """
    if not results:
        return "Runtime starting with 0 adapters."

    lines: list[str] = []

    succeeded = 0
    failed = 0
    for adapter_id, transport, success, duration_s, error in results:
        dur = format_duration_seconds(duration_s) if duration_s > 0 else "0ms"
        if success:
            lines.append(f"  started {transport}.{adapter_id} ({dur})")
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
    lines: list[str] = []

    if errors:
        lines.append("Runtime stopped with errors")
        for aid, err in errors:
            lines.append(f"  \u2717 {aid}: shutdown error ({err})")
    else:
        lines.append(f"Runtime stopped ({len(adapter_ids)} adapter(s))")

    return "\n".join(lines)
