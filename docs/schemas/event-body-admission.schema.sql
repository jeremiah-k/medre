-- Executable prerelease SQLite schema fragment.
-- Authoritative DDL: src/medre/core/storage/sqlite/schema.py.

CREATE TABLE IF NOT EXISTS event_body_identity (
    event_id TEXT PRIMARY KEY REFERENCES canonical_events(event_id),
    version INTEGER NOT NULL CHECK (version = 1),
    present_at_admission INTEGER NOT NULL CHECK (present_at_admission IN (0, 1)),
    recorded_at TEXT NOT NULL
);
