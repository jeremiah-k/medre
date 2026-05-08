"""Observability subsystem for the medre.

Provides structured logging helpers and lightweight metrics counters
for tracking pipeline event flow.

Re-exported symbols
-------------------
* :func:`~medre.core.observability.logging.setup_logging`
  – configure the root framework logger.
* :func:`~medre.core.observability.logging.get_logger`
  – obtain a child logger in the framework namespace.
* :class:`~medre.core.observability.metrics.EventMetrics`
  – per-stage event counters with snapshot support.
"""

from medre.core.observability.logging import get_logger, setup_logging
from medre.core.observability.metrics import EventMetrics

__all__ = [
    "EventMetrics",
    "get_logger",
    "setup_logging",
]
