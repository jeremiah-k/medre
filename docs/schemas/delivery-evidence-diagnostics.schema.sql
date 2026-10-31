-- Executable prerelease SQLite schema fragment.
-- Authoritative DDL: src/medre/core/storage/sqlite/schema.py.

CREATE TABLE IF NOT EXISTS delivery_evidence_diagnostics (
    diag_id INTEGER PRIMARY KEY AUTOINCREMENT,
    family TEXT NOT NULL CHECK (family IN ('receipt', 'observation', 'outbox', 'ingress')),
    row_id TEXT NOT NULL CHECK (length(row_id) > 0),
    attempt INTEGER NOT NULL CHECK (attempt >= 0),
    field TEXT NOT NULL CHECK (field IN ('error', 'capability_reason', 'rendering_evidence', 'failure_kind_detail', 'error_summary', 'last_error')),
    diagnostic TEXT NOT NULL,
    created_at TEXT NOT NULL
);
