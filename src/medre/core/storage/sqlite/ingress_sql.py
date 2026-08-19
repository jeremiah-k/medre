"""Shared SQL and row mapping for durable ingress storage paths."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

SELECT_NATIVE_EVENT_ID = """
SELECT event_id FROM native_message_refs
WHERE adapter = ? AND native_channel_id IS ? AND native_message_id = ?
"""

SELECT_CANONICAL_EVENT_ID = (
    "SELECT event_id FROM canonical_events WHERE event_id = ?"
)

SELECT_INGRESS_WORK_STATE = (
    "SELECT provenance, status FROM durable_ingress_work WHERE event_id = ?"
)

INSERT_INGRESS_WORK = """
INSERT INTO durable_ingress_work
    (event_id, provenance, status, attempts, created_at, updated_at)
VALUES (?, ?, ?, 0, ?, ?)
"""

CLAIM_INGRESS_SELECT = """
SELECT event_id, provenance, status, attempts, last_error,
       created_at, updated_at, locked_at, lease_until, worker_id
FROM durable_ingress_work
WHERE status = 'pending'
   OR (status = 'processing' AND lease_until IS NOT NULL AND lease_until <= ?)
ORDER BY created_at, event_id
LIMIT ?
"""

CLAIM_INGRESS_UPDATE = """
UPDATE durable_ingress_work
SET status='processing', attempts=attempts+1,
    locked_at=?, lease_until=?, worker_id=?, updated_at=?
WHERE event_id=?
"""

def claimed_ingress_row(
    row: Sequence[Any], *, now_iso: str, lease_until: str, worker_id: str
) -> dict[str, Any]:
    """Map one claimed SQLite row to the backend-neutral work shape."""
    return {
        "event_id": str(row[0]),
        "provenance": row[1],
        "status": "processing",
        "attempts": int(row[3]) + 1,
        "last_error": row[4],
        "created_at": row[5],
        "updated_at": now_iso,
        "locked_at": now_iso,
        "lease_until": lease_until,
        "worker_id": worker_id,
    }
