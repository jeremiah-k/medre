"""Meshtastic-specific startup-backlog rxTime extraction.

Provides pure, stateless helpers for pulling the ``rxTime`` field from a
Meshtastic packet mapping and converting it to a timezone-aware UTC
:class:`~datetime.datetime`.

Transport-neutral suppression logic lives in
:mod:`medre.core.policies.startup_backlog_suppress`.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Mapping

__all__ = [
    "extract_meshtastic_rx_time",
]


def extract_meshtastic_rx_time(
    packet: Mapping[str, object],
) -> datetime | None:
    """Extract ``rxTime`` from a Meshtastic packet as a UTC datetime.

    Parameters
    ----------
    packet:
        A read-only mapping representing a Meshtastic packet.  The
        function looks for a top-level ``"rxTime"`` key whose value is
        a Unix epoch timestamp in seconds.

    Returns
    -------
    datetime | None
        A timezone-aware (UTC) datetime if ``rxTime`` is present and
        valid; ``None`` otherwise.

    Accepts
    -------
    Finite positive ``int`` or ``float`` epoch seconds.

    Rejects (returns ``None``)
    --------------------------
    * Missing ``rxTime`` key.
    * ``bool`` values (``True`` / ``False`` — ``bool`` is a subclass of
      ``int`` in Python).
    * Non-numeric types (``str``, ``dict``, ``None``, …).
    * ``float('nan')``, ``float('inf')``, ``float('-inf')``.
    * Zero or negative values (``<= 0``).
    * Values that cause :class:`OverflowError`, :class:`OSError`, or
      :class:`ValueError` when converted (e.g. extremely large epochs).
    """
    raw = packet.get("rxTime")
    if raw is None:
        return None

    # Reject bool (subclass of int) before the isinstance int check.
    if isinstance(raw, bool):
        return None

    if not isinstance(raw, (int, float)):
        return None

    # Reject NaN / inf.
    if isinstance(raw, float) and not math.isfinite(raw):
        return None

    # Reject non-positive epochs.
    if raw <= 0:
        return None

    try:
        return datetime.fromtimestamp(float(raw), tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
