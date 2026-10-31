"""Shared SQL and row mapping for durable ingress storage paths."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

SELECT_NATIVE_EVENT_ID = """
SELECT event_id FROM native_message_refs
WHERE adapter = ? AND native_channel_id IS ? AND native_message_id = ?
"""

SELECT_CANONICAL_EVENT_ID = "SELECT event_id FROM canonical_events WHERE event_id = ?"

SELECT_INGRESS_WORK_STATE = (
    "SELECT provenance, status FROM durable_ingress_work WHERE event_id = ?"
)

INSERT_INGRESS_WORK = """
INSERT INTO durable_ingress_work
    (event_id, provenance, status, attempts, created_at, updated_at)
VALUES (?, ?, ?, 0, ?, ?)
"""

CLAIM_INGRESS_SELECT = """
SELECT w.event_id, w.provenance, w.status, w.attempts,
       NULLIF(COALESCE((
           SELECT d.diagnostic
           FROM delivery_evidence_diagnostics d
           WHERE d.family = 'ingress' AND d.row_id = w.event_id
             AND d.field = 'last_error'
           ORDER BY d.diag_id DESC LIMIT 1
       ), NULLIF(w.last_error, '')), '') AS last_error,
       w.created_at, w.updated_at, w.locked_at, w.lease_until, w.worker_id
FROM durable_ingress_work w
WHERE w.status = 'pending'
   OR (w.status = 'processing' AND w.lease_until IS NOT NULL AND w.lease_until <= ?)
ORDER BY w.created_at, w.event_id
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
