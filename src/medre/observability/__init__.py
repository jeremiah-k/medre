"""User-facing observability helpers for the MEDRE runtime.

Re-exports the public API from sub-modules so that consumers can import
everything from a single namespace::

    from medre.observability import (
        adapter_logger,
        format_duration_ms,
        sanitize_error,
        sanitize_for_log,
        startup_summary,
        shutdown_summary,
    )
"""

from medre.observability.logging import adapter_logger
from medre.observability.sanitization import sanitize_error, sanitize_for_log
from medre.observability.summaries import (
    format_duration_ms,
    shutdown_summary,
    startup_summary,
)

__all__ = [
    "adapter_logger",
    "format_duration_ms",
    "sanitize_error",
    "sanitize_for_log",
    "shutdown_summary",
    "startup_summary",
]
