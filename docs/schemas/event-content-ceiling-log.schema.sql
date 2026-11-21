-- Immutable hard-ceiling enforcement receipt. Written by the guarded
-- ceiling-enforcement transition when one event's immutable hard ceiling
-- elapsed and the ceiling purge of every enumerated content store committed
-- and verified. Content-free counters only; presence is the durable fact
-- that enforcement completed for this event and no content may be
-- resurrected for it. It does not claim physical erasure of SQLite free
-- pages, WAL, provider copies, or backups.
CREATE TABLE IF NOT EXISTS event_content_ceiling_log (
    event_id TEXT NOT NULL PRIMARY KEY REFERENCES canonical_events(event_id),
    enforced_at TEXT NOT NULL,
    ceiling TEXT NOT NULL,
    rows_removed INTEGER NOT NULL CHECK (rows_removed >= 0),
    logical_bytes_removed INTEGER NOT NULL CHECK (logical_bytes_removed >= 0),
    blobs_removed INTEGER NOT NULL CHECK (blobs_removed >= 0),
    blob_bytes_removed INTEGER NOT NULL CHECK (blob_bytes_removed >= 0)
);
