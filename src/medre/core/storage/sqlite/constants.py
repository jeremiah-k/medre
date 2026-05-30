"""Outbox-related tuning constants.

These values control grace periods and thresholds for the delivery
outbox reclaim logic.
"""

from __future__ import annotations

#: Grace period (in seconds) before a ``queued`` outbox item is considered
#: stale and eligible for reclaim by
#: :meth:`~medre.core.storage.sqlite.storage.SQLiteStorage.claim_due_outbox_items`.
#: A conservative value avoids reclaiming items that are legitimately waiting
#: in an adapter-local queue (e.g. Meshtastic) while still preventing
#: permanently-stuck rows when the adapter never finalizes.
STALE_QUEUED_GRACE_SECONDS: int = 300  # 5 minutes
