"""Startup-backlog suppression utilities for Meshtastic rxTime semantics.

Provides pure, stateless helpers for deciding whether a received packet
should be discarded as stale backlog from before the adapter started.

* :func:`extract_meshtastic_rx_time` — pull ``rxTime`` (Unix epoch seconds)
  from a Meshtastic packet mapping and return a timezone-aware UTC
  :class:`~datetime.datetime`.
* :func:`should_suppress_startup_backlog` — decide whether a packet
  timestamp falls inside the suppression window.

**Design notes**

* No adapter wiring — callers import these helpers and supply the
  ``adapter_start_time`` themselves.
* MeshCore extraction is *not* included; it has no receive-time field
  and its ``sender_timestamp`` is unreliable for this purpose.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Mapping

__all__ = [
    "extract_meshtastic_rx_time",
    "should_suppress_startup_backlog",
]

# ---------------------------------------------------------------------------
# Meshtastic rxTime extraction
# ---------------------------------------------------------------------------


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

    return datetime.fromtimestamp(float(raw), tz=timezone.utc)


# ---------------------------------------------------------------------------
# Suppression decision
# ---------------------------------------------------------------------------


def should_suppress_startup_backlog(
    packet_time: datetime | None,
    adapter_start_time: datetime,
    suppress_seconds: float,
) -> bool:
    """Decide whether *packet_time* falls inside the startup-backlog
    suppression window.

    Parameters
    ----------
    packet_time:
        The receive timestamp of the packet (UTC).  ``None`` means the
        timestamp could not be determined.
    adapter_start_time:
        The wall-clock time when the adapter started.  Must be
        timezone-aware UTC; a naive datetime is conservatively treated
        as UTC but callers should pass aware values.
    suppress_seconds:
        Width of the suppression window in seconds.  A window of zero
        or a negative value **disables** suppression entirely.

    Returns
    -------
    bool
        ``True`` if the packet should be suppressed (stale backlog),
        ``False`` if it should be allowed through.

    Semantics
    ---------
    * **Disabled** when ``suppress_seconds <= 0`` → always ``False``.
    * **Missing timestamp** (``packet_time is None``) → ``False`` (no
      evidence of staleness).
    * ``cutoff = adapter_start_time - suppress_seconds``
    * Suppress only when ``packet_time < cutoff`` (strictly less).
      A packet exactly *at* the cutoff is **not** suppressed.
    * **Future timestamps** are never suppressed — they indicate a
      clock skew scenario, not stale backlog.
    """
    # Disabled — nothing is suppressed.
    if suppress_seconds <= 0:
        return False

    # No timestamp available — no evidence of staleness.
    if packet_time is None:
        return False

    # Normalise: treat naive datetimes conservatively as UTC.
    start_aware = _ensure_utc(adapter_start_time)
    pkt_aware = _ensure_utc(packet_time)

    cutoff_ts = start_aware.timestamp() - suppress_seconds
    pkt_ts = pkt_aware.timestamp()

    # Future packets are never suppressed regardless of the window.
    if pkt_ts > start_aware.timestamp():
        return False

    # Suppress only when strictly before the cutoff.
    return pkt_ts < cutoff_ts


def _ensure_utc(dt: datetime) -> datetime:
    """Return a timezone-aware UTC datetime.

    If *dt* is already timezone-aware it is converted to UTC.
    If *dt* is naive it is *assumed* to be UTC (conservative default).
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
