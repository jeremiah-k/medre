"""Prepared SQL statement strings for the SQLite storage backend.

Every statement is a module-level constant.  Parameter placeholders use
``?`` (qmark style) for use with the Python ``sqlite3`` parameterised
execution APIs.
"""

from __future__ import annotations

from medre.core.storage.sqlite._content_identity import event_content_denied_sql

# ---------------------------------------------------------------------------
# INSERT statements
# ---------------------------------------------------------------------------

_INSERT_EVENT = """
INSERT INTO canonical_events
    (event_id, event_kind, schema_version, timestamp,
     source_adapter, source_transport_id, source_channel_id,
     parent_event_id, lineage, payload, metadata, depth,
     trace_id, root_event_id, conversation_id,
     source_native_adapter, source_native_channel_id,
     source_native_message_id, source_native_thread_id,
     created_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_RELATION = """
INSERT INTO event_relations
    (event_id, relation_type, target_event_id,
     target_native_adapter, target_native_channel_id,
     target_native_message_id, target_native_thread_id,
     key, metadata, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_NATIVE_REF = """
INSERT OR IGNORE INTO native_message_refs
    (id, event_id, adapter, native_channel_id,
     native_message_id, native_thread_id, native_relation_id,
     direction, metadata, created_at)
SELECT ?, ?, ?, ?, ?, ?, ?, ?,
    CASE WHEN EXISTS (
        SELECT 1 FROM event_content_access_policies p
        WHERE p.event_id=? AND p.hard_ceiling<=?
    ) OR EXISTS (
        SELECT 1 FROM event_content_ceiling_log WHERE event_id=?
    ) THEN '{}' ELSE ? END, ?
WHERE NOT EXISTS (
    SELECT 1 FROM native_message_refs prior
    WHERE prior.adapter=? AND prior.native_channel_id IS ?
      AND prior.native_message_id=?
)
"""

_INSERT_NATIVE_REF_STRICT = """
INSERT INTO native_message_refs
    (id, event_id, adapter, native_channel_id,
     native_message_id, native_thread_id, native_relation_id,
     direction, metadata, created_at)
SELECT ?, ?, ?, ?, ?, ?, ?, ?,
    CASE WHEN EXISTS (
        SELECT 1 FROM event_content_access_policies p
        WHERE p.event_id=? AND p.hard_ceiling<=?
    ) OR EXISTS (
        SELECT 1 FROM event_content_ceiling_log WHERE event_id=?
    ) THEN '{}' ELSE ? END, ?
WHERE NOT EXISTS (
    SELECT 1 FROM native_message_refs prior
    WHERE prior.adapter=? AND prior.native_channel_id IS ?
      AND prior.native_message_id=?
)
"""

_INSERT_RECEIPT = """
INSERT INTO delivery_receipts
    (receipt_id, event_id, delivery_plan_id, target_adapter,
     target_channel, route_id, status, receipt_kind, failure_kind,
     capability_level, capability_field, delivery_strategy,
     adapter_message_id, next_retry_at, attempt_number, parent_receipt_id, source,
     replay_run_id, retry_max_attempts, retry_backoff_base,
     retry_max_delay, retry_jitter, outbox_id,
     confirmation_level, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

# The frozen dispatch context is written inside the outbox-creation
# transaction, keyed by the exact outbox identity.  The row's committed
# creation generation is read from the just-inserted outbox row, so a replay
# allocation's rewritten attempt number is the one recorded.
_INSERT_OUTBOX_SNAPSHOT = """
INSERT INTO delivery_outbox_snapshots
    (outbox_id, attempt_number, snapshot_version, snapshot_kind, context_json,
     created_at)
SELECT outbox_id, attempt_number, ?, ?, ?, ?
FROM delivery_outbox WHERE outbox_id = ?
"""

# Admission-time route retry bounds for the just-inserted outbox generation.
# The SELECT form keys the fact to the committed row inside the same
# creation transaction; adopted existing rows never run this op.
_INSERT_OUTBOX_ROUTE_RETRY_BOUNDS = """
INSERT INTO delivery_route_retry_bounds
    (outbox_id, max_attempts, backoff_base, max_delay_seconds, jitter)
SELECT outbox_id, ?, ?, ?, ?
FROM delivery_outbox WHERE outbox_id = ?
"""

# Retirement of the frozen dispatch context happens ONLY inside guarded
# terminal transitions, in the same transaction as the status change; a
# rolled-back transition preserves the snapshot.
_RETIRE_OUTBOX_SNAPSHOT = """
DELETE FROM delivery_outbox_snapshots WHERE outbox_id = ?
"""

_FINALIZE_QUEUED_OUTBOX_SENT = """
UPDATE delivery_outbox
SET status = 'sent',
    attempt_number = COALESCE(active_attempt, attempt_number),
    active_attempt = NULL,
    failure_kind = NULL,
    next_attempt_at = NULL,
    updated_at = ?,
    last_attempt_at = ?,
    locked_at = NULL,
    lease_until = NULL,
    worker_id = NULL,
    receipt_id = ?
WHERE outbox_id = ?
  AND event_id = ?
  AND delivery_plan_id = ?
  AND target_adapter = ?
  AND target_channel IS ?
  AND ? = COALESCE(active_attempt, attempt_number)
  AND status IN ('queued', 'in_progress')
"""

_FINALIZE_OUTBOX_TERMINAL = """
UPDATE delivery_outbox
SET status = ?,
    attempt_number = COALESCE(active_attempt, attempt_number),
    active_attempt = NULL,
    failure_kind = ?,
    next_attempt_at = NULL,
    updated_at = ?,
    locked_at = NULL,
    lease_until = NULL,
    worker_id = NULL,
    receipt_id = ?
WHERE outbox_id = ?
  AND event_id = ?
  AND delivery_plan_id = ?
  AND target_adapter = ?
  AND target_channel IS ?
  AND ? = COALESCE(active_attempt, attempt_number)
  AND status IN ('queued', 'in_progress')
  AND (? IS NULL OR worker_id = ?)
  AND (NOT ? OR (
      status = 'in_progress' AND dispatch_source = 'replay'
      AND replay_run_id IS ? AND active_attempt IS NULL
      AND NOT EXISTS (
          SELECT 1 FROM delivery_receipts AS r
          WHERE r.outbox_id = delivery_outbox.outbox_id
            AND r.attempt_number = delivery_outbox.attempt_number)
      AND NOT EXISTS (
          SELECT 1 FROM delivery_observations AS o
          WHERE o.outbox_id = delivery_outbox.outbox_id
            AND o.attempt_number = delivery_outbox.attempt_number)
      AND NOT EXISTS (
          SELECT 1 FROM radio_attempt_progress AS p
          WHERE p.outbox_id = delivery_outbox.outbox_id
            AND p.attempt_number = delivery_outbox.attempt_number)
  ))
"""


# ---------------------------------------------------------------------------
# SELECT statements
# ---------------------------------------------------------------------------

_SELECT_RELATION_SOURCES = """
SELECT event_id
FROM event_relations
WHERE target_event_id = ?
GROUP BY event_id
ORDER BY MIN(id) ASC
"""

_SELECT_RELATION_SOURCES_FOR_NATIVE_REF = """
SELECT event_id
FROM event_relations
WHERE target_native_adapter = ?
  AND target_native_channel_id IS ?
  AND target_native_message_id = ?
GROUP BY event_id
ORDER BY MIN(id) ASC
"""

_RESOLVE_NATIVE_REF = """
SELECT event_id FROM native_message_refs
WHERE adapter = ? AND native_channel_id IS ? AND native_message_id = ?
"""

_DELIVERY_RECEIPT_LATEST_BY_EVENT_CHANNEL = """
WITH outbox_current AS (
    SELECT r.*
    FROM delivery_receipts r
    JOIN delivery_outbox o
      ON o.outbox_id = r.outbox_id
     AND o.receipt_id = r.receipt_id
    WHERE r.event_id = ? AND r.delivery_plan_id = ? AND r.target_adapter = ?
      AND r.target_channel IS ?
    ORDER BY o.attempt_number DESC, r.sequence DESC
    LIMIT 1
),
outboxless_current AS (
    SELECT r.*
    FROM delivery_receipts r
    WHERE r.event_id = ? AND r.delivery_plan_id = ? AND r.target_adapter = ?
      AND r.target_channel IS ? AND r.outbox_id IS NULL
      AND NOT EXISTS (
          SELECT 1 FROM delivery_outbox settled
          JOIN delivery_handoff_settlements handoff
            ON handoff.outbox_id = settled.outbox_id
           AND handoff.attempt_number = settled.attempt_number
          WHERE settled.event_id = r.event_id
            AND settled.delivery_plan_id = r.delivery_plan_id
            AND settled.target_adapter = r.target_adapter
            AND COALESCE(settled.target_channel, '') =
                COALESCE(r.target_channel, '')
            AND settled.status = 'sent' AND settled.receipt_id IS NULL
            AND handoff.receipt_watermark >= r.sequence
            AND NOT EXISTS (
                SELECT 1 FROM delivery_outbox newer
                JOIN delivery_receipts committed
                  ON committed.outbox_id = newer.outbox_id
                 AND committed.receipt_id = newer.receipt_id
                WHERE newer.event_id = settled.event_id
                  AND newer.delivery_plan_id = settled.delivery_plan_id
                  AND newer.target_adapter = settled.target_adapter
                  AND COALESCE(newer.target_channel, '') =
                      COALESCE(settled.target_channel, '')
                  AND newer.attempt_number > settled.attempt_number
            )
      )
    ORDER BY r.sequence DESC
    LIMIT 1
),
candidates AS (
    SELECT * FROM outbox_current
    UNION ALL
    SELECT * FROM outboxless_current
)
SELECT * FROM candidates
ORDER BY sequence DESC
LIMIT 1
"""

_SELECT_RECEIPTS_FOR_DELIVERY = """
SELECT * FROM delivery_receipts
WHERE event_id = ? AND delivery_plan_id = ? AND target_adapter = ?
  AND target_channel IS ?
ORDER BY attempt_number ASC, sequence ASC
"""

_SELECT_RECEIPTS_FOR_OUTBOX = """
SELECT * FROM delivery_receipts
WHERE outbox_id = ?
ORDER BY attempt_number ASC, sequence ASC
"""

_SELECT_RECEIPTS_BY_REPLAY_RUN = """
SELECT * FROM delivery_receipts
WHERE replay_run_id = ?
ORDER BY sequence ASC
"""

_SELECT_RECEIPTS_FOR_EVENT = """
SELECT * FROM delivery_receipts
WHERE event_id = ?
ORDER BY sequence ASC
"""

_SELECT_ALL_RECEIPTS = """
SELECT * FROM delivery_receipts
ORDER BY sequence ASC
"""

_NATIVE_REFERENCE_SAFE_FROM = f"""
FROM (SELECT
    n.id, n.event_id, n.adapter, n.native_channel_id,
    n.native_message_id, n.native_thread_id, n.native_relation_id,
    n.direction, n.created_at,
    CASE WHEN {event_content_denied_sql("n.event_id")}
    THEN '{{}}' ELSE n.metadata END AS metadata
FROM native_message_refs n) AS safe_refs
"""  # nosec B608 - static SQL fragments and bound values

_SELECT_NREFS_FOR_EVENT = (
    "SELECT * "
    + _NATIVE_REFERENCE_SAFE_FROM
    + "WHERE event_id = ? ORDER BY created_at ASC, id ASC"
)
_GET_NATIVE_REF = (
    "SELECT * "
    + _NATIVE_REFERENCE_SAFE_FROM
    + "WHERE adapter = ? AND native_channel_id IS ? AND native_message_id = ? LIMIT 1"
)

# Inspection variants carry no derived subquery: the metadata-only projection
# rewrites the base-table column list directly, so the stored metadata column
# is never referenced — not even inside an ELSE branch a column authorizer
# would deny. Ordinary reads above keep the enforcement-receipt masking.
_SELECT_NREFS_FOR_EVENT_PLAIN = (
    "SELECT * FROM native_message_refs "
    "WHERE event_id = ? ORDER BY created_at ASC, id ASC"
)
_GET_NATIVE_REF_PLAIN = (
    "SELECT * FROM native_message_refs "
    "WHERE adapter = ? AND native_channel_id IS ? AND native_message_id = ? LIMIT 1"
)
