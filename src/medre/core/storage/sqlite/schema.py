"""Schema DDL, indexes, and schema-version metadata.

This module owns all database-shape definitions used by the SQLite storage
backend.  During pre-release development the schema version remains frozen at
``1`` while the required shape evolves; incompatible old shapes are rejected.
Only explicitly allowlisted exact additive predecessor layouts can be upgraded.  Once a release compatibility boundary is declared, DDL changes must
advance :data:`_EXPECTED_SCHEMA_VERSION`.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

_SCHEMA: str = """
CREATE TABLE IF NOT EXISTS canonical_events (
    event_id TEXT PRIMARY KEY,
    event_kind TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    source_adapter TEXT NOT NULL,
    source_transport_id TEXT NOT NULL,
    source_channel_id TEXT,
    parent_event_id TEXT,
    lineage TEXT NOT NULL DEFAULT '[]',
    payload TEXT NOT NULL DEFAULT '{}',
    metadata TEXT NOT NULL DEFAULT '{}',
    depth INTEGER NOT NULL DEFAULT 0,
    trace_id TEXT,
    root_event_id TEXT,
    conversation_id TEXT,
    source_native_adapter TEXT,
    source_native_channel_id TEXT,
    source_native_message_id TEXT,
    source_native_thread_id TEXT,
    created_at TEXT NOT NULL
);

-- One current, immutable admission fact per canonical body.  Absence of a
-- body is not an empty string.  These facts do NOT authorize retention, reads,
-- or collection: those require separately implemented consumer authority.
CREATE TABLE IF NOT EXISTS event_body_identity (
    event_id TEXT PRIMARY KEY REFERENCES canonical_events(event_id),
    version INTEGER NOT NULL CHECK (version = 1),
    present_at_admission INTEGER NOT NULL CHECK (present_at_admission IN (0, 1)),
    recorded_at TEXT NOT NULL
);

-- Canonical body text is stored separately from identity and envelope JSON.
-- No row means no text; an empty string is an explicitly present body.
-- This table is not a grant. It must not be exposed by ordinary inspection.
CREATE TABLE IF NOT EXISTS event_body_content (
    event_id TEXT NOT NULL REFERENCES canonical_events(event_id),
    field TEXT NOT NULL CHECK (field IN ('body', 'text')),
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (event_id, field)
);

-- One durable dependency per content owner. The ingress owner is admitted
-- atomically with the event and durable work; each outbox owner is created
-- atomically with its outbox generation. No collector runs in this slice.
CREATE TABLE IF NOT EXISTS event_content_dependencies (
    event_id TEXT NOT NULL REFERENCES event_body_identity(event_id),
    owner_kind TEXT NOT NULL CHECK (owner_kind IN ('ingress', 'append', 'outbox')),
    owner_id TEXT NOT NULL CHECK (length(owner_id) > 0),
    acquired_at TEXT NOT NULL,
    deadline TEXT,
    released_at TEXT,
    PRIMARY KEY (event_id, owner_kind, owner_id),
    CHECK (deadline IS NULL OR acquired_at < deadline)
);

-- Explicit opt-in content read policy. No implicit grant is created by
-- admission. The immutable event-level hard ceiling is set once, on first
-- authorized policy installation; future grants cannot extend it.
CREATE TABLE IF NOT EXISTS event_content_access_policies (
    event_id TEXT PRIMARY KEY REFERENCES canonical_events(event_id),
    version INTEGER NOT NULL CHECK (version = 1),
    hard_ceiling TEXT NOT NULL,
    allowed_consumers TEXT NOT NULL CHECK (json_valid(allowed_consumers)),
    max_optional_hold_microseconds INTEGER NOT NULL CHECK (max_optional_hold_microseconds > 0),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_content_access_grants (
    event_id TEXT NOT NULL REFERENCES event_content_access_policies(event_id),
    scope TEXT NOT NULL CHECK (scope = 'body'),
    consumer TEXT NOT NULL CHECK (length(consumer) BETWEEN 1 AND 128),
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    expired_at TEXT,
    PRIMARY KEY(event_id, scope, consumer),
    CHECK (issued_at < expires_at)
);

-- Immutable logical-collection receipt. Does not claim physical erasure or
-- removal of provider/native/backup copies. Retains dedup and admission facts.
CREATE TABLE IF NOT EXISTS event_content_collection_log (
    event_id TEXT PRIMARY KEY REFERENCES canonical_events(event_id),
    collected_at TEXT NOT NULL,
    policy TEXT NOT NULL CHECK (policy = 'relay-only'),
    rows_removed INTEGER NOT NULL CHECK (rows_removed >= 0),
    logical_bytes_removed INTEGER NOT NULL CHECK (logical_bytes_removed >= 0)
);

CREATE TABLE IF NOT EXISTS event_relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL REFERENCES canonical_events(event_id),
    relation_type TEXT NOT NULL,
    target_event_id TEXT,
    target_native_adapter TEXT,
    target_native_channel_id TEXT,
    target_native_message_id TEXT,
    target_native_thread_id TEXT,
    key TEXT,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

-- Relation excerpts are content, not relation identity. Native target refs
-- stay in event_relations even when the text excerpt becomes unavailable.
CREATE TABLE IF NOT EXISTS event_relation_excerpts (
    relation_id INTEGER NOT NULL REFERENCES event_relations(id),
    excerpt TEXT NOT NULL,
    UNIQUE (relation_id)
);

CREATE TABLE IF NOT EXISTS conversation_membership (
    event_id TEXT PRIMARY KEY REFERENCES canonical_events(event_id),
    root_event_id TEXT NOT NULL REFERENCES canonical_events(event_id),
    conversation_id TEXT NOT NULL,
    resolved_target_event_id TEXT REFERENCES canonical_events(event_id),
    relation_type TEXT,
    depth INTEGER NOT NULL,
    resolution_state TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (depth >= 0),
    CHECK (conversation_id = root_event_id),
    CHECK (resolution_state IN ('root', 'resolved', 'unresolved', 'cycle'))
);

CREATE TABLE IF NOT EXISTS conversation_projection_state (
    singleton_id INTEGER PRIMARY KEY,
    projection_revision INTEGER NOT NULL,
    status TEXT NOT NULL,
    last_event_id TEXT,
    updated_at TEXT NOT NULL,
    CHECK (singleton_id = 1),
    CHECK (projection_revision >= 1),
    CHECK (status IN ('clean', 'dirty', 'rebuilding')),
    CHECK (status = 'rebuilding' OR last_event_id IS NULL)
);

CREATE TABLE IF NOT EXISTS native_message_refs (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES canonical_events(event_id),
    adapter TEXT NOT NULL,
    native_channel_id TEXT,
    native_message_id TEXT NOT NULL,
    native_thread_id TEXT,
    native_relation_id TEXT,
    direction TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(adapter, native_channel_id, native_message_id)
);

CREATE TABLE IF NOT EXISTS delivery_receipts (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_id TEXT UNIQUE NOT NULL,
    event_id TEXT NOT NULL REFERENCES canonical_events(event_id),
    delivery_plan_id TEXT NOT NULL,
    target_adapter TEXT NOT NULL,
    target_channel TEXT,
    route_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    receipt_kind TEXT NOT NULL,
    error TEXT,
    failure_kind TEXT,
    capability_level TEXT,
    capability_field TEXT,
    capability_reason TEXT,
    delivery_strategy TEXT,
    adapter_message_id TEXT,
    next_retry_at TEXT,
    attempt_number INTEGER NOT NULL DEFAULT 1,
    parent_receipt_id TEXT,
    source TEXT NOT NULL DEFAULT 'live',
    replay_run_id TEXT,
    retry_max_attempts INTEGER,
    retry_backoff_base REAL,
    retry_max_delay REAL,
    retry_jitter INTEGER,
    rendering_evidence TEXT,
    outbox_id TEXT,
    confirmation_level TEXT NOT NULL DEFAULT 'unknown',
    created_at TEXT NOT NULL,
    CHECK (attempt_number >= 1),
    CHECK (receipt_kind IN ('attempt', 'lifecycle')),
    CHECK ((receipt_kind = 'attempt' AND status IN ('queued', 'sent', 'failed')) OR (receipt_kind = 'lifecycle' AND status IN ('dead_lettered', 'cancelled', 'abandoned', 'suppressed'))),
    CHECK (source IN ('live', 'retry', 'replay')),
    CHECK (source != 'live' OR replay_run_id IS NULL),
    CHECK (replay_run_id IS NULL OR (length(replay_run_id) > 0 AND replay_run_id = trim(replay_run_id))),
    CHECK (capability_level IS NULL OR capability_level IN ('native', 'fallback', 'unsupported')),
    CHECK (delivery_strategy IS NULL OR delivery_strategy IN ('direct', 'fallback_text', 'skip', 'propagated', 'opportunistic', 'paper')),
    CHECK (confirmation_level IN ('unknown', 'local_queue', 'local_transport', 'remote_service', 'end_to_end'))
);

-- delivery_status view: one row per unique (event_id, delivery_plan_id,
-- target_adapter, target_channel) tuple.  Outbox-backed receipts are current only when the
-- outbox row points at that receipt_id; receipts that lost a guarded outbox
-- transition remain immutable historical evidence but do not become current
-- status merely because they were appended later.
-- COALESCE(target_channel, '') in GROUP BY ensures that NULL and '' channels
-- are treated as the same group, avoiding duplicate rows when some receipts
-- have NULL and others have '' for target_channel.
-- Drop and recreate to ensure column shape stays current (e.g. when
-- rendering_evidence is added).
DROP VIEW IF EXISTS delivery_status;
CREATE VIEW delivery_status AS
WITH authoritative_receipts AS (
    SELECT dr.*, NULL AS committed_attempt
    FROM delivery_receipts dr
    WHERE dr.outbox_id IS NULL
    UNION ALL
    SELECT dr.*, o.attempt_number AS committed_attempt
    FROM delivery_receipts dr
    JOIN delivery_outbox o
      ON o.outbox_id = dr.outbox_id
     AND o.receipt_id = dr.receipt_id
),
ranked AS (
    SELECT dr.*,
           CASE
               WHEN outbox_id IS NULL THEN
                   ROW_NUMBER() OVER (
                       PARTITION BY event_id, delivery_plan_id, target_adapter,
                                    COALESCE(target_channel, ''), (outbox_id IS NULL)
                       ORDER BY sequence DESC
                   )
               ELSE
                   ROW_NUMBER() OVER (
                       PARTITION BY event_id, delivery_plan_id, target_adapter,
                                    COALESCE(target_channel, ''), (outbox_id IS NULL)
                       ORDER BY committed_attempt DESC, sequence DESC
                   )
           END AS class_rank
    FROM authoritative_receipts dr
),
candidates AS (
    SELECT * FROM ranked WHERE class_rank = 1
),
current_rows AS (
    SELECT c.*,
           ROW_NUMBER() OVER (
               PARTITION BY event_id, delivery_plan_id, target_adapter,
                            COALESCE(target_channel, '')
               ORDER BY sequence DESC
           ) AS authority_rank
    FROM candidates c
)
SELECT sequence, receipt_id, event_id, delivery_plan_id,
       target_adapter, target_channel, route_id, status,
       receipt_kind, error, failure_kind,
       capability_level, capability_field, capability_reason, delivery_strategy,
       adapter_message_id, next_retry_at, attempt_number,
       parent_receipt_id, source, replay_run_id,
       retry_max_attempts, retry_backoff_base,
       retry_max_delay, retry_jitter, rendering_evidence,
       outbox_id, confirmation_level, created_at
FROM current_rows
WHERE authority_rank = 1;

CREATE TABLE IF NOT EXISTS delivery_outbox (
    outbox_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES canonical_events(event_id),
    route_id TEXT NOT NULL DEFAULT '',
    delivery_plan_id TEXT NOT NULL,
    target_adapter TEXT NOT NULL,
    target_channel TEXT,
    target_address TEXT,
    attempt_number INTEGER NOT NULL DEFAULT 1,
    active_attempt INTEGER,
    status TEXT NOT NULL DEFAULT 'pending',
    failure_kind TEXT,
    failure_kind_detail TEXT,
    next_attempt_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_attempt_at TEXT,
    locked_at TEXT,
    lease_until TEXT,
    worker_id TEXT,
    payload_hash TEXT,
    receipt_id TEXT,
    parent_receipt_id TEXT,
    error_summary TEXT,
    dispatch_source TEXT NOT NULL DEFAULT 'live',
    replay_run_id TEXT,
    metadata TEXT NOT NULL DEFAULT '{}',
    UNIQUE(event_id, delivery_plan_id, target_adapter, target_channel, attempt_number),
    CHECK (attempt_number >= 0),
    CHECK (active_attempt IS NULL OR active_attempt = attempt_number + 1),
    CHECK (active_attempt IS NULL OR status = 'in_progress'),
    CHECK (status IN ('pending', 'in_progress', 'queued', 'sent', 'retry_wait', 'dead_lettered', 'cancelled', 'abandoned')),
    CHECK (dispatch_source IN ('live', 'replay', 'retry')),
    CHECK (dispatch_source != 'live' OR replay_run_id IS NULL),
    CHECK (replay_run_id IS NULL OR (length(replay_run_id) > 0 AND replay_run_id = trim(replay_run_id)))
);

CREATE TABLE IF NOT EXISTS delivery_observations (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_id TEXT UNIQUE NOT NULL,
    event_id TEXT NOT NULL REFERENCES canonical_events(event_id),
    delivery_plan_id TEXT NOT NULL,
    target_adapter TEXT NOT NULL,
    target_channel TEXT,
    native_channel_id TEXT,
    outbox_id TEXT NOT NULL REFERENCES delivery_outbox(outbox_id),
    attempt_number INTEGER NOT NULL,
    adapter_message_id TEXT,
    state TEXT NOT NULL,
    confirmation_level TEXT NOT NULL DEFAULT 'unknown',
    error TEXT,
    metadata TEXT NOT NULL DEFAULT '{}',
    observed_at TEXT NOT NULL,
    CHECK (attempt_number >= 1),
    CHECK (state IN ('delivered', 'failed', 'rejected', 'cancelled')),
    CHECK (confirmation_level IN ('unknown', 'local_queue', 'local_transport', 'remote_service', 'end_to_end'))
);

CREATE TABLE IF NOT EXISTS radio_attempt_progress (
    outbox_id TEXT NOT NULL REFERENCES delivery_outbox(outbox_id),
    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
    plan_fingerprint TEXT NOT NULL CHECK (length(plan_fingerprint) = 64),
    plan_json TEXT NOT NULL CHECK (json_valid(plan_json)),
    provenance_json TEXT NOT NULL CHECK (json_valid(provenance_json)),
    parts_json TEXT NOT NULL CHECK (json_valid(parts_json)),
    revision INTEGER NOT NULL CHECK (revision >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (outbox_id, attempt_number)
);

CREATE TABLE IF NOT EXISTS durable_ingress_work (
    event_id TEXT PRIMARY KEY REFERENCES canonical_events(event_id),
    provenance TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    locked_at TEXT,
    lease_until TEXT,
    worker_id TEXT
);

CREATE TABLE IF NOT EXISTS adapter_checkpoints (
    adapter_id TEXT NOT NULL,
    stream TEXT NOT NULL,
    cursor TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    PRIMARY KEY(adapter_id, stream)
);

-- Durable attachment content.  Content-addressed: one row per unique
-- retained byte string; identical content admitted twice stores once and
-- consumes quota once.  ``data`` is the plaintext payload (see
-- docs/spec/security-privacy.md); ordinary event queries never select it.
CREATE TABLE IF NOT EXISTS attachment_blobs (
    content_ref TEXT PRIMARY KEY,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    media_kind TEXT,
    mime_type TEXT,
    created_at TEXT NOT NULL,
    data BLOB NOT NULL
);

-- Event → content association.  One primary attachment per canonical
-- event; the association is the only authority for loading retained bytes
-- for an event (a content_ref named in a render payload without this
-- association is forged and must fail).
CREATE TABLE IF NOT EXISTS event_attachment_associations (
    event_id TEXT PRIMARY KEY REFERENCES canonical_events(event_id),
    content_ref TEXT NOT NULL REFERENCES attachment_blobs(content_ref),
    created_at TEXT NOT NULL
);

-- Pre-admission inbound pressure aggregates.  One row per (window,
-- source, outcome); counters only — a rejected or deferred arrival never
-- persists payload, sender, or transport-native content here.  Written
-- before durable admission, so it is the only durable trace of arrivals
-- the admission gate turned away. Required in the strict prerelease shape,
-- alongside the attachment tables; incompatible state is never repaired.
CREATE TABLE IF NOT EXISTS inbound_pressure_observations (
    window_start INTEGER NOT NULL,
    source TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('rejected', 'timed_out', 'deferred')),
    count INTEGER NOT NULL CHECK (count > 0),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY(window_start, source, outcome)
);

CREATE TABLE IF NOT EXISTS plugin_state (
    plugin_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(plugin_id, key)
);

-- Operator decisions are immutable audit evidence, not native-send grants.
CREATE TABLE IF NOT EXISTS operator_delivery_decisions (
    decision_id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL,
    event_id TEXT NOT NULL REFERENCES canonical_events(event_id),
    delivery_plan_id TEXT NOT NULL,
    target_adapter TEXT NOT NULL,
    target_channel TEXT,
    outbox_id TEXT NOT NULL REFERENCES delivery_outbox(outbox_id),
    observed_generation INTEGER NOT NULL,
    observed_updated_at TEXT NOT NULL,
    observed_status TEXT NOT NULL,
    action TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    CHECK (action = 'cancel_unstarted'),
    CHECK (observed_status IN ('pending', 'retry_wait')),
    CHECK (observed_generation >= 0)
);

CREATE TABLE IF NOT EXISTS _medre_schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Targeted indexes matching actual query patterns.
# Run AFTER shape validation so incompatible databases fail with a clear
# StorageInitializationError before index creation is attempted.
# native_message_refs(adapter, native_channel_id, native_message_id) is already
# covered by the UNIQUE constraint autoindex; no manual duplicate is needed.
_INDEXES: str = """
CREATE INDEX IF NOT EXISTS idx_events_timestamp
    ON canonical_events(timestamp, event_id);
CREATE INDEX IF NOT EXISTS idx_relations_event_id
    ON event_relations(event_id, id);
CREATE INDEX IF NOT EXISTS idx_relations_target_event_id
    ON event_relations(target_event_id);
CREATE INDEX IF NOT EXISTS idx_relations_target_native_ref
    ON event_relations(target_native_adapter, target_native_channel_id, target_native_message_id);
CREATE INDEX IF NOT EXISTS idx_nrefs_event_created
    ON native_message_refs(event_id, created_at);
CREATE INDEX IF NOT EXISTS idx_receipts_event
    ON delivery_receipts(event_id, sequence);
CREATE INDEX IF NOT EXISTS idx_receipts_outbox_attempt
    ON delivery_receipts(outbox_id, attempt_number, sequence);
CREATE INDEX IF NOT EXISTS idx_receipts_replay_run
    ON delivery_receipts(replay_run_id);
CREATE INDEX IF NOT EXISTS idx_receipts_source
    ON delivery_receipts(source, replay_run_id);
-- Lineage index for unresolved-delivery recovery scans: matches the
-- correlated current-outcome predicate for (event, plan, adapter, channel),
-- including the COALESCE normalization of NULL/'' channel values.
-- replay_run_id is receipt provenance and is intentionally not part of the
-- lineage key.
CREATE INDEX IF NOT EXISTS idx_receipts_lineage
    ON delivery_receipts(event_id, delivery_plan_id, target_adapter,
                         COALESCE(target_channel, ''), sequence);
CREATE INDEX IF NOT EXISTS idx_receipts_retry_due
    ON delivery_receipts(status, failure_kind, next_retry_at);
CREATE INDEX IF NOT EXISTS idx_receipts_parent_retry
    ON delivery_receipts(parent_receipt_id, source);
CREATE INDEX IF NOT EXISTS idx_operator_decisions_event
    ON operator_delivery_decisions(event_id, outbox_id, recorded_at);
CREATE INDEX IF NOT EXISTS idx_outbox_due
    ON delivery_outbox(status, next_attempt_at);
-- One-per-adapter claims probe active owners by adapter before lease expiry.
CREATE INDEX IF NOT EXISTS idx_outbox_active_adapter
    ON delivery_outbox(target_adapter, status, lease_until);
CREATE INDEX IF NOT EXISTS idx_outbox_plan_target
    ON delivery_outbox(delivery_plan_id, target_adapter, target_channel);
CREATE INDEX IF NOT EXISTS idx_outbox_event
    ON delivery_outbox(event_id);
CREATE INDEX IF NOT EXISTS idx_outbox_event_created
    ON delivery_outbox(event_id, created_at, outbox_id);
CREATE INDEX IF NOT EXISTS idx_outbox_lineage
    ON delivery_outbox(
        event_id, delivery_plan_id, target_adapter,
        COALESCE(target_channel, ''), attempt_number
    );
-- A non-empty replay run ID is a durable idempotency key for one logical
-- delivery target.  The expression normalizes NULL/'' target channels exactly
-- like DeliveryIdentity so concurrent executions of the same replay run
-- cannot allocate sibling outbox generations.
CREATE UNIQUE INDEX IF NOT EXISTS idx_outbox_replay_run_identity_unique
    ON delivery_outbox(
        event_id, delivery_plan_id, target_adapter,
        COALESCE(target_channel, ''), replay_run_id
    )
    WHERE replay_run_id IS NOT NULL AND replay_run_id <> '';
CREATE INDEX IF NOT EXISTS idx_observations_event
    ON delivery_observations(event_id, sequence);
CREATE INDEX IF NOT EXISTS idx_observations_outbox
    ON delivery_observations(outbox_id, attempt_number, sequence);
CREATE INDEX IF NOT EXISTS idx_ingress_work_claim
    ON durable_ingress_work(status, lease_until, created_at);
-- SQLite treats NULL != NULL in UNIQUE constraints.  This partial unique
-- index closes the gap: no two outbox items with NULL target_channel can
-- share the same (event_id, delivery_plan_id, target_adapter, attempt_number) tuple.
CREATE UNIQUE INDEX IF NOT EXISTS idx_outbox_null_channel_unique
    ON delivery_outbox(event_id, delivery_plan_id, target_adapter, attempt_number)
    WHERE target_channel IS NULL;
"""

# ---------------------------------------------------------------------------
# Schema versioning
# ---------------------------------------------------------------------------

_EXPECTED_SCHEMA_VERSION: int = 1
"""Frozen schema version — stays at **1** until the project curator declares a
release compatibility boundary.  DDL shape changes during pre-release do **not**
require a version bump; the expected version is only incremented once the
storage contract is formally release-tracked.

That said, the version **is** checked on every
:meth:`SQLiteStorage.initialize` call and a mismatch will raise an error.
This strictness is intentional: it catches databases whose shape differs from
the current pre-release contract. MEDRE rejects that mismatch rather than
transforming it automatically.
"""

# ---------------------------------------------------------------------------
# Required column inventory  (derived from _SCHEMA DDL above)
# ---------------------------------------------------------------------------

_REQUIRED_COLUMNS: dict[str, frozenset[str]] = {
    "canonical_events": frozenset(
        {
            "event_id",
            "event_kind",
            "schema_version",
            "timestamp",
            "source_adapter",
            "source_transport_id",
            "source_channel_id",
            "parent_event_id",
            "lineage",
            "payload",
            "metadata",
            "depth",
            "trace_id",
            "root_event_id",
            "conversation_id",
            "source_native_adapter",
            "source_native_channel_id",
            "source_native_message_id",
            "source_native_thread_id",
            "created_at",
        }
    ),
    "event_body_identity": frozenset(
        {"event_id", "version", "present_at_admission", "recorded_at"}
    ),
    "event_body_content": frozenset({"event_id", "body", "field", "created_at"}),
    "event_content_dependencies": frozenset(
        {"event_id", "owner_kind", "owner_id", "acquired_at", "deadline", "released_at"}
    ),
    "event_content_collection_log": frozenset(
        {"event_id", "collected_at", "policy", "rows_removed", "logical_bytes_removed"}
    ),
    "event_relations": frozenset(
        {
            "id",
            "event_id",
            "relation_type",
            "target_event_id",
            "target_native_adapter",
            "target_native_channel_id",
            "target_native_message_id",
            "target_native_thread_id",
            "key",
            "metadata",
            "created_at",
        }
    ),
    "event_relation_excerpts": frozenset({"relation_id", "excerpt"}),
    "event_content_access_policies": frozenset(
        {
            "event_id",
            "version",
            "hard_ceiling",
            "allowed_consumers",
            "max_optional_hold_microseconds",
            "created_at",
        }
    ),
    "event_content_access_grants": frozenset(
        {
            "event_id",
            "scope",
            "consumer",
            "issued_at",
            "expires_at",
            "revoked_at",
            "expired_at",
        }
    ),
    "conversation_membership": frozenset(
        {
            "event_id",
            "root_event_id",
            "conversation_id",
            "resolved_target_event_id",
            "relation_type",
            "depth",
            "resolution_state",
            "updated_at",
        }
    ),
    "conversation_projection_state": frozenset(
        {
            "singleton_id",
            "projection_revision",
            "status",
            "last_event_id",
            "updated_at",
        }
    ),
    "native_message_refs": frozenset(
        {
            "id",
            "event_id",
            "adapter",
            "native_channel_id",
            "native_message_id",
            "native_thread_id",
            "native_relation_id",
            "direction",
            "metadata",
            "created_at",
        }
    ),
    "delivery_receipts": frozenset(
        {
            "sequence",
            "receipt_id",
            "event_id",
            "delivery_plan_id",
            "target_adapter",
            "target_channel",
            "route_id",
            "status",
            "receipt_kind",
            "error",
            "failure_kind",
            "capability_level",
            "capability_field",
            "capability_reason",
            "delivery_strategy",
            "adapter_message_id",
            "next_retry_at",
            "attempt_number",
            "parent_receipt_id",
            "source",
            "replay_run_id",
            "retry_max_attempts",
            "retry_backoff_base",
            "retry_max_delay",
            "retry_jitter",
            "rendering_evidence",
            "outbox_id",
            "confirmation_level",
            "created_at",
        }
    ),
    "delivery_observations": frozenset(
        {
            "sequence",
            "observation_id",
            "event_id",
            "delivery_plan_id",
            "target_adapter",
            "target_channel",
            "native_channel_id",
            "outbox_id",
            "attempt_number",
            "adapter_message_id",
            "state",
            "confirmation_level",
            "error",
            "metadata",
            "observed_at",
        }
    ),
    "delivery_outbox": frozenset(
        {
            "outbox_id",
            "event_id",
            "route_id",
            "delivery_plan_id",
            "target_adapter",
            "target_channel",
            "target_address",
            "attempt_number",
            "active_attempt",
            "status",
            "failure_kind",
            "failure_kind_detail",
            "next_attempt_at",
            "created_at",
            "updated_at",
            "last_attempt_at",
            "locked_at",
            "lease_until",
            "worker_id",
            "payload_hash",
            "receipt_id",
            "parent_receipt_id",
            "error_summary",
            "dispatch_source",
            "replay_run_id",
            "metadata",
        }
    ),
    "durable_ingress_work": frozenset(
        {
            "event_id",
            "provenance",
            "status",
            "attempts",
            "last_error",
            "created_at",
            "updated_at",
            "locked_at",
            "lease_until",
            "worker_id",
        }
    ),
    "adapter_checkpoints": frozenset(
        {"adapter_id", "stream", "cursor", "metadata", "updated_at"}
    ),
    "operator_delivery_decisions": frozenset(
        {
            "decision_id",
            "actor_id",
            "event_id",
            "delivery_plan_id",
            "target_adapter",
            "target_channel",
            "outbox_id",
            "observed_generation",
            "observed_updated_at",
            "observed_status",
            "action",
            "recorded_at",
        }
    ),
    "plugin_state": frozenset(
        {
            "plugin_id",
            "key",
            "value",
            "updated_at",
        }
    ),
    "attachment_blobs": frozenset(
        {"content_ref", "size_bytes", "media_kind", "mime_type", "created_at", "data"}
    ),
    "event_attachment_associations": frozenset(
        {"event_id", "content_ref", "created_at"}
    ),
    "inbound_pressure_observations": frozenset(
        {"window_start", "source", "outcome", "count", "first_seen_at", "last_seen_at"}
    ),
    "_medre_schema_meta": frozenset(
        {
            "key",
            "value",
        }
    ),
    "radio_attempt_progress": frozenset(
        {
            "outbox_id",
            "attempt_number",
            "plan_fingerprint",
            "plan_json",
            "provenance_json",
            "parts_json",
            "revision",
            "created_at",
            "updated_at",
        }
    ),
}

# Structural constraints that cannot be inferred from column presence alone.
# Existing pre-release databases must already carry these relationships; MEDRE
# rejects older shapes rather than rebuilding them automatically.
# Required table-level UNIQUE constraints.  These are part of logical identity,
# so column-presence validation alone is insufficient: an older pre-release
# table can have every current column while still enforcing a stale key.
_REQUIRED_UNIQUE_CONSTRAINTS: dict[str, frozenset[tuple[str, ...]]] = {
    "event_body_identity": frozenset({("event_id",)}),
    "event_body_content": frozenset({("event_id", "field")}),
    "event_content_dependencies": frozenset({("event_id", "owner_kind", "owner_id")}),
    "event_relation_excerpts": frozenset({("relation_id",)}),
    "event_content_access_policies": frozenset({("event_id",)}),
    "event_content_access_grants": frozenset({("event_id", "scope", "consumer")}),
    "operator_delivery_decisions": frozenset({("decision_id",)}),
    "radio_attempt_progress": frozenset({("outbox_id", "attempt_number")}),
    "delivery_outbox": frozenset(
        {
            (
                "event_id",
                "delivery_plan_id",
                "target_adapter",
                "target_channel",
                "attempt_number",
            )
        }
    ),
}

_REQUIRED_FOREIGN_KEYS: dict[str, frozenset[tuple[str, str, str]]] = {
    "radio_attempt_progress": frozenset(
        {("outbox_id", "delivery_outbox", "outbox_id")}
    ),
    "event_body_identity": frozenset({("event_id", "canonical_events", "event_id")}),
    "event_body_content": frozenset({("event_id", "canonical_events", "event_id")}),
    "event_content_dependencies": frozenset(
        {("event_id", "event_body_identity", "event_id")}
    ),
    "event_relation_excerpts": frozenset({("relation_id", "event_relations", "id")}),
    "event_content_access_policies": frozenset(
        {("event_id", "canonical_events", "event_id")}
    ),
    "event_content_access_grants": frozenset(
        {("event_id", "event_content_access_policies", "event_id")}
    ),
    "operator_delivery_decisions": frozenset(
        {
            ("event_id", "canonical_events", "event_id"),
            ("outbox_id", "delivery_outbox", "outbox_id"),
        }
    ),
    "conversation_membership": frozenset(
        {
            ("event_id", "canonical_events", "event_id"),
            ("root_event_id", "canonical_events", "event_id"),
            ("resolved_target_event_id", "canonical_events", "event_id"),
        }
    ),
    "delivery_outbox": frozenset({("event_id", "canonical_events", "event_id")}),
    "delivery_observations": frozenset(
        {
            ("event_id", "canonical_events", "event_id"),
            ("outbox_id", "delivery_outbox", "outbox_id"),
        }
    ),
}

# Required table-level CHECK clauses.  Column/FK validation cannot detect an
# existing pre-release table created without these invariants because
# ``CREATE TABLE IF NOT EXISTS`` leaves that older definition untouched.
_REQUIRED_CHECK_CONSTRAINTS: dict[str, tuple[tuple[str, str], ...]] = {
    "event_body_identity": (
        (
            "CHECK (version = 1)",
            r"CHECK\s*\(\s*version\s*=\s*1\s*\)",
        ),
        (
            "CHECK (present_at_admission IN (0, 1))",
            r"CHECK\s*\(\s*present_at_admission\s+IN\s*\(\s*0\s*,\s*1\s*\)\s*\)",
        ),
    ),
    "event_content_access_policies": (
        ("CHECK (version = 1)", r"CHECK\s*\(\s*version\s*=\s*1\s*\)"),
        (
            "CHECK (json_valid(allowed_consumers))",
            r"CHECK\s*\(\s*json_valid\s*\(\s*allowed_consumers\s*\)\s*\)",
        ),
        (
            "CHECK (max_optional_hold_microseconds > 0)",
            r"CHECK\s*\(\s*max_optional_hold_microseconds\s*>\s*0\s*\)",
        ),
    ),
    "event_content_access_grants": (
        ("CHECK (scope = 'body')", r"CHECK\s*\(\s*scope\s*=\s*'body'\s*\)"),
        (
            "CHECK (length(consumer) BETWEEN 1 AND 128)",
            r"CHECK\s*\(\s*length\s*\(\s*consumer\s*\)\s+BETWEEN\s+1\s+AND\s+128\s*\)",
        ),
        (
            "CHECK (issued_at < expires_at)",
            r"CHECK\s*\(\s*issued_at\s*<\s*expires_at\s*\)",
        ),
    ),
    "operator_delivery_decisions": (
        (
            "CHECK (observed_status IN ('pending', 'retry_wait'))",
            r"CHECK\s*\(\s*observed_status\s+IN\s*\(\s*'pending'\s*,\s*'retry_wait'\s*\)\s*\)",
        ),
        (
            "CHECK (action = 'cancel_unstarted')",
            r"CHECK\s*\(\s*action\s*=\s*'cancel_unstarted'\s*\)",
        ),
        (
            "CHECK (observed_generation >= 0)",
            r"CHECK\s*\(\s*observed_generation\s*>=\s*0\s*\)",
        ),
    ),
    "radio_attempt_progress": (
        ("CHECK (attempt_number >= 1)", r"CHECK\s*\(\s*attempt_number\s*>=\s*1\s*\)"),
        ("CHECK (revision >= 0)", r"CHECK\s*\(\s*revision\s*>=\s*0\s*\)"),
        (
            "CHECK (length(plan_fingerprint) = 64)",
            r"CHECK\s*\(\s*length\s*\(\s*plan_fingerprint\s*\)\s*=\s*64\s*\)",
        ),
        *tuple(
            (
                f"CHECK (json_valid({column}))",
                rf"CHECK\s*\(\s*json_valid\s*\(\s*{column}\s*\)\s*\)",
            )
            for column in ("plan_json", "provenance_json", "parts_json")
        ),
    ),
    "delivery_receipts": (
        (
            "CHECK (attempt_number >= 1)",
            r"CHECK\s*\(\s*attempt_number\s*>=\s*1\s*\)",
        ),
        (
            "CHECK (receipt_kind IN ('attempt', 'lifecycle'))",
            (
                r"CHECK\s*\(\s*receipt_kind\s+IN\s*\(\s*'attempt'\s*,\s*"
                r"'lifecycle'\s*\)\s*\)"
            ),
        ),
        (
            "CHECK (receipt_kind/status pairing)",
            (
                r"CHECK\s*\(\s*\(\s*receipt_kind\s*=\s*'attempt'\s+AND\s+"
                r"status\s+IN\s*\(\s*'queued'\s*,\s*'sent'\s*,\s*'failed'\s*\)\s*\)"
                r"\s+OR\s+\(\s*receipt_kind\s*=\s*'lifecycle'\s+AND\s+status\s+IN\s*\("
                r"\s*'dead_lettered'\s*,\s*'cancelled'\s*,\s*'abandoned'\s*,\s*'suppressed'\s*"
                r"\)\s*\)\s*\)"
            ),
        ),
        (
            "CHECK (source IN ('live', 'retry', 'replay'))",
            (
                r"CHECK\s*\(\s*source\s+IN\s*\(\s*'live'\s*,\s*"
                r"'retry'\s*,\s*'replay'\s*\)\s*\)"
            ),
        ),
        (
            "CHECK (source != 'live' OR replay_run_id IS NULL)",
            (
                r"CHECK\s*\(\s*source\s*!=\s*'live'\s+OR\s+"
                r"replay_run_id\s+IS\s+NULL\s*\)"
            ),
        ),
        (
            "CHECK (replay_run_id canonical)",
            (
                r"CHECK\s*\(\s*replay_run_id\s+IS\s+NULL\s+OR\s+\("
                r"\s*length\s*\(\s*replay_run_id\s*\)\s*>\s*0\s+AND\s+"
                r"replay_run_id\s*=\s*trim\s*\(\s*replay_run_id\s*\)\s*\)\s*\)"
            ),
        ),
    ),
    "delivery_outbox": (
        (
            "CHECK (attempt_number >= 0)",
            r"CHECK\s*\(\s*attempt_number\s*>=\s*0\s*\)",
        ),
        (
            "CHECK (active_attempt IS NULL OR active_attempt = attempt_number + 1)",
            (
                r"CHECK\s*\(\s*active_attempt\s+IS\s+NULL\s+OR\s+active_attempt\s*=\s*"
                r"attempt_number\s*\+\s*1\s*\)"
            ),
        ),
        (
            "CHECK (active_attempt IS NULL OR status = 'in_progress')",
            (
                r"CHECK\s*\(\s*active_attempt\s+IS\s+NULL\s+OR\s+status\s*=\s*"
                r"'in_progress'\s*\)"
            ),
        ),
        (
            "CHECK (delivery_outbox.status vocabulary)",
            (
                r"CHECK\s*\(\s*status\s+IN\s*\(\s*'pending'\s*,\s*'in_progress'\s*,\s*"
                r"'queued'\s*,\s*'sent'\s*,\s*'retry_wait'\s*,\s*'dead_lettered'\s*,\s*"
                r"'cancelled'\s*,\s*'abandoned'\s*\)\s*\)"
            ),
        ),
        (
            "CHECK (dispatch_source IN ('live', 'replay', 'retry'))",
            (
                r"CHECK\s*\(\s*dispatch_source\s+IN\s*\(\s*'live'\s*,\s*"
                r"'replay'\s*,\s*'retry'\s*\)\s*\)"
            ),
        ),
        (
            "CHECK (dispatch_source != 'live' OR replay_run_id IS NULL)",
            (
                r"CHECK\s*\(\s*dispatch_source\s*!=\s*'live'\s+OR\s+"
                r"replay_run_id\s+IS\s+NULL\s*\)"
            ),
        ),
        (
            "CHECK (replay_run_id canonical)",
            (
                r"CHECK\s*\(\s*replay_run_id\s+IS\s+NULL\s+OR\s+\("
                r"\s*length\s*\(\s*replay_run_id\s*\)\s*>\s*0\s+AND\s+"
                r"replay_run_id\s*=\s*trim\s*\(\s*replay_run_id\s*\)\s*\)\s*\)"
            ),
        ),
    ),
    "delivery_observations": (
        (
            "CHECK (attempt_number >= 1)",
            r"CHECK\s*\(\s*attempt_number\s*>=\s*1\s*\)",
        ),
        (
            "CHECK (state IN ('delivered', 'failed', 'rejected', 'cancelled'))",
            (
                r"CHECK\s*\(\s*state\s+IN\s*\(\s*'delivered'\s*,\s*"
                r"'failed'\s*,\s*'rejected'\s*,\s*'cancelled'\s*\)\s*\)"
            ),
        ),
        (
            "CHECK (confirmation_level IN ('unknown', 'local_queue', 'local_transport', 'remote_service', 'end_to_end'))",
            (
                r"CHECK\s*\(\s*confirmation_level\s+IN\s*\(\s*'unknown'\s*,\s*"
                r"'local_queue'\s*,\s*'local_transport'\s*,\s*'remote_service'\s*,\s*"
                r"'end_to_end'\s*\)\s*\)"
            ),
        ),
    ),
    "conversation_projection_state": (
        ("CHECK (singleton_id = 1)", r"CHECK\s*\(\s*singleton_id\s*=\s*1\s*\)"),
        (
            "CHECK (projection_revision >= 1)",
            r"CHECK\s*\(\s*projection_revision\s*>=\s*1\s*\)",
        ),
        (
            "CHECK (status IN ('clean', 'dirty', 'rebuilding'))",
            (
                r"CHECK\s*\(\s*status\s+IN\s*\(\s*'clean'\s*,\s*'dirty'\s*,\s*"
                r"'rebuilding'\s*\)\s*\)"
            ),
        ),
        (
            "CHECK (status = 'rebuilding' OR last_event_id IS NULL)",
            (
                r"CHECK\s*\(\s*status\s*=\s*'rebuilding'\s+OR\s+"
                r"last_event_id\s+IS\s+NULL\s*\)"
            ),
        ),
    ),
    "conversation_membership": (
        ("CHECK (depth >= 0)", r"CHECK\s*\(\s*depth\s*>=\s*0\s*\)"),
        (
            "CHECK (conversation_id = root_event_id)",
            r"CHECK\s*\(\s*conversation_id\s*=\s*root_event_id\s*\)",
        ),
        (
            "CHECK (resolution_state IN ('root', 'resolved', 'unresolved', 'cycle'))",
            (
                r"CHECK\s*\(\s*resolution_state\s+IN\s*\(\s*'root'\s*,\s*"
                r"'resolved'\s*,\s*'unresolved'\s*,\s*'cycle'\s*\)\s*\)"
            ),
        ),
    ),
}

# The only approved prerelease additive collection table. Its DDL is derived
# from the same authoritative schema used by strict initialization.
_COLLECTION_LOG_DDL = """
-- Immutable logical-collection receipt. Does not claim physical erasure or
-- removal of provider/native/backup copies. Retains dedup and admission facts.
CREATE TABLE IF NOT EXISTS event_content_collection_log (
    event_id TEXT PRIMARY KEY REFERENCES canonical_events(event_id),
    collected_at TEXT NOT NULL,
    policy TEXT NOT NULL CHECK (policy = 'relay-only'),
    rows_removed INTEGER NOT NULL CHECK (rows_removed >= 0),
    logical_bytes_removed INTEGER NOT NULL CHECK (logical_bytes_removed >= 0)
);

"""
