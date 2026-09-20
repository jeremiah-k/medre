"""Shared adapter diagnostics keys.

The ``best_effort`` replay CLI waits for adapters' in-flight outbound
deliveries to go terminal before teardown by reading each started
adapter's ``diagnostics()`` report.  These constants are the contract
between adapters that expose outbound-work observations and that drain:
producers MUST use them verbatim, and an adapter that exposes neither key
simply gets no pre-stop grace (delivery truth is never inferred from the
drain — it is recorded only by real terminal callbacks through the
lifecycle authority).

* ``PENDING_DELIVERY_COUNT`` — async-transfer adapters (LXMF): number of
  outbound deliveries not yet terminal, exposed under ``session``.
* ``QUEUE_PENDING`` — queue-backed adapters (Meshtastic): number of items
  still in the adapter-local outbound queue, exposed at the top level.
"""

from __future__ import annotations

PENDING_DELIVERY_COUNT = "pending_delivery_count"
QUEUE_PENDING = "queue_pending"

__all__ = ["PENDING_DELIVERY_COUNT", "QUEUE_PENDING"]
