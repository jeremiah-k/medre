"""Prepared SQL statement strings for the SQLite storage backend.

Every statement is a module-level constant.  Parameter placeholders use
``?`` (qmark style) for use with the Python ``sqlite3`` / ``aiosqlite``
parameterised execution APIs.
"""

from __future__ import annotations

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
     key, fallback_text,
     metadata, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_NATIVE_REF = """
INSERT OR IGNORE INTO native_message_refs
    (id, event_id, adapter, native_channel_id,
     native_message_id, native_thread_id, native_relation_id,
     direction, metadata, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_RECEIPT = """
INSERT INTO delivery_receipts
    (receipt_id, event_id, delivery_plan_id, target_adapter,
     target_channel, route_id, status, error, failure_kind, adapter_message_id,
     next_retry_at, attempt_number, parent_receipt_id, source,
     replay_run_id, retry_max_attempts, retry_backoff_base,
     retry_max_delay, retry_jitter, rendering_evidence, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

# ---------------------------------------------------------------------------
# SELECT statements
# ---------------------------------------------------------------------------

_SELECT_EVENT = "SELECT * FROM canonical_events WHERE event_id = ?"

_SELECT_RELATIONS = "SELECT * FROM event_relations WHERE event_id = ? ORDER BY id ASC"

_RESOLVE_NATIVE_REF = """
SELECT event_id FROM native_message_refs
WHERE adapter = ? AND native_channel_id IS ? AND native_message_id = ?
"""

_DELIVERY_RECEIPT_LATEST_BY_CHANNEL = """
SELECT * FROM delivery_receipts
WHERE delivery_plan_id = ? AND target_adapter = ?
  AND target_channel IS ?
ORDER BY attempt_number DESC, sequence DESC
LIMIT 1
"""

_SELECT_RECEIPTS_FOR_PLAN = """
SELECT * FROM delivery_receipts
WHERE delivery_plan_id = ? AND target_adapter = ?
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

_SELECT_NREFS_FOR_EVENT = """
SELECT * FROM native_message_refs
WHERE event_id = ?
ORDER BY created_at ASC, id ASC
"""
