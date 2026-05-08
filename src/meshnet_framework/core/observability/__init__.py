"""Observability subsystem for the meshnet framework.

Provides structured logging helpers and lightweight metrics counters
for tracking pipeline event flow.

Re-exported symbols
-------------------
* :func:`~meshnet_framework.core.observability.logging.setup_logging`
  – configure the root framework logger.
* :func:`~meshnet_framework.core.observability.logging.get_logger`
  – obtain a child logger in the framework namespace.
* :class:`~meshnet_framework.core.observability.metrics.EventMetrics`
  – per-stage event counters with snapshot support.
"""

from meshnet_framework.core.observability.logging import get_logger, setup_logging
from meshnet_framework.core.observability.metrics import EventMetrics

__all__ = [
    "EventMetrics",
    "get_logger",
    "setup_logging",
]
