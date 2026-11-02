-- Executable prerelease SQLite schema fragment.
-- Authoritative DDL: src/medre/core/storage/sqlite/schema.py.

CREATE TABLE IF NOT EXISTS content_retention_epochs (
    epoch INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL CHECK (mode = 'relay-only'),
    activated_at TEXT NOT NULL,
    admission_watermark TEXT,
    last_observed_active_at TEXT,
    activated_by TEXT NOT NULL CHECK (activated_by IN ('runtime-first-start','operator'))
);
