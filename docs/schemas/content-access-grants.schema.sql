-- Executable prerelease SQLite schema fragment.
-- Authoritative DDL: src/medre/core/storage/sqlite/schema.py.

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
