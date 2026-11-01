-- Executable prerelease SQLite schema fragment.
-- Authoritative DDL: src/medre/core/storage/sqlite/schema.py.

CREATE TABLE IF NOT EXISTS event_routing_facts (
    event_id TEXT PRIMARY KEY REFERENCES canonical_events(event_id),
    matched_routes TEXT NOT NULL CHECK (json_valid(matched_routes) AND json_type(matched_routes) = 'array'),
    fanout_group TEXT,
    route_trace TEXT NOT NULL CHECK (json_valid(route_trace) AND json_type(route_trace) = 'array' AND json_array_length(route_trace) <= 16),
    recorded_at TEXT NOT NULL
);
