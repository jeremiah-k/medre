"""Published SQLite fragments retain every declared content constraint."""

import sqlite3
from pathlib import Path

import pytest

from medre.core.storage.sqlite.schema import _SCHEMA


@pytest.mark.parametrize(
    "filename,tables",
    [
        ("event-body-admission.schema.sql", {"event_body_identity"}),
    ],
)
def test_content_schema_fragment_matches_authoritative_ddl(filename, tables):
    reference = sqlite3.connect(":memory:")
    documented = sqlite3.connect(":memory:")
    try:
        reference.executescript(_SCHEMA)
        documented.execute("CREATE TABLE canonical_events (event_id TEXT PRIMARY KEY)")
        path = Path(__file__).resolve().parents[1] / "docs" / "schemas" / filename
        documented.executescript(path.read_text(encoding="utf-8"))
        actual = dict(
            documented.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table' AND name != 'canonical_events'"
            )
        )
        assert actual.keys() == tables
        for table in tables:
            expected = reference.execute(
                "SELECT sql FROM sqlite_master WHERE name=?", (table,)
            ).fetchone()[0]
            assert actual[table] == expected
    finally:
        documented.close()
        reference.close()
