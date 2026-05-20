"""Observability subsystem for the medre.

Provides structured logging helpers and lightweight metrics counters
for tracking pipeline event flow.

Package-level imports
---------------------
* :func:`~medre.core.observability.logging.setup_logging`
  – configure the root framework logger.
* :func:`~medre.core.observability.logging.get_logger`
  – obtain a child logger in the framework namespace.
* :class:`~medre.core.observability.metrics.EventMetrics`
  – per-stage event counters with snapshot support.
* :class:`~medre.core.observability.metrics.RouteMetrics`
  – per-route delivery counters with snapshot support.
* :func:`~medre.core.observability.logging.log_route_matched`
  – log route match event.
* :func:`~medre.core.observability.logging.log_route_delivered`
  – log route delivery success.
* :func:`~medre.core.observability.logging.log_route_failed`
  – log route delivery failure.
* :func:`~medre.core.observability.logging.log_route_loop_prevented`
  – log route loop prevention.
"""

from medre.core.observability.logging import (
    get_logger,
    log_route_delivered,
    log_route_failed,
    log_route_loop_prevented,
    log_route_matched,
    setup_logging,
)
from medre.core.observability.metrics import EventMetrics, RouteMetrics
from medre.core.observability.sanitization import sanitize_error, sanitize_for_log

__all__ = [
    "EventMetrics",
    "RouteMetrics",
    "get_logger",
    "log_route_delivered",
    "log_route_failed",
    "log_route_loop_prevented",
    "log_route_matched",
    "sanitize_error",
    "sanitize_for_log",
    "setup_logging",
]
