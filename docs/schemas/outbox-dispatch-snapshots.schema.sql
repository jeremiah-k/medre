-- Executable prerelease SQLite schema fragment.
-- Authoritative DDL: src/medre/core/storage/sqlite/schema.py.

CREATE TABLE IF NOT EXISTS delivery_outbox_snapshots (
    outbox_id TEXT NOT NULL REFERENCES delivery_outbox(outbox_id),
    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 0),
    snapshot_version INTEGER NOT NULL CHECK (snapshot_version = 1),
    snapshot_kind TEXT NOT NULL CHECK (snapshot_kind IN ('staged', 'direct')),
    context_json TEXT NOT NULL CHECK (json_valid(context_json)),
    created_at TEXT NOT NULL,
    PRIMARY KEY (outbox_id, attempt_number)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_outbox_snapshot_owner_unique
    ON delivery_outbox_snapshots(outbox_id);
