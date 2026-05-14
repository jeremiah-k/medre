"""Adapter-scoped logger factory.

Provides:
* :func:`adapter_logger` -- return a ``LoggerAdapter`` that injects
  ``adapter_id`` and ``transport`` into every log record's ``extra`` dict.
"""

from __future__ import annotations

import logging

__all__ = ["adapter_logger"]

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
