"""Published SQLite fragments retain every declared content constraint."""

import sqlite3
from pathlib import Path

import pytest

from medre.core.storage.sqlite.schema import _INDEXES, _SCHEMA


@pytest.mark.parametrize(
    "filename,tables",
    [
        ("event-body-admission.schema.sql", {"event_body_identity"}),
        ("event-routing-facts.schema.sql", {"event_routing_facts"}),
        ("outbox-dispatch-snapshots.schema.sql", {"delivery_outbox_snapshots"}),
        (
            "content-access-grants.schema.sql",
            {"event_content_access_policies", "event_content_access_grants"},
        ),
        (
            "delivery-evidence-diagnostics.schema.sql",
            {"delivery_evidence_diagnostics"},
        ),
    ],
)
def test_content_schema_fragment_matches_authoritative_ddl(filename, tables):
    reference = sqlite3.connect(":memory:")
    documented = sqlite3.connect(":memory:")
    try:
        reference.executescript(_SCHEMA + _INDEXES)
        documented.execute("CREATE TABLE canonical_events (event_id TEXT PRIMARY KEY)")
        path = Path(__file__).resolve().parents[1] / "docs" / "schemas" / filename
        documented.executescript(path.read_text(encoding="utf-8"))
        actual = dict(
            documented.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table' "
                "AND name != 'canonical_events' AND name NOT GLOB 'sqlite_*'"
            )
        )
        assert actual.keys() == tables
        for table in tables:
            expected = reference.execute(
                "SELECT sql FROM sqlite_master WHERE name=?", (table,)
            ).fetchone()[0]
            assert actual[table] == expected
        if "delivery_outbox_snapshots" in tables:
            assert (
                documented.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='index' "
                    "AND name='idx_outbox_snapshot_owner_unique'"
                ).fetchone()
                is not None
            )
        for name, sql in documented.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
        ):
            expected = reference.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)
            ).fetchone()
            assert expected is not None and sql == expected[0]
    finally:
        documented.close()
        reference.close()
