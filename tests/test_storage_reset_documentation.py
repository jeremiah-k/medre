"""Reset inspection must reveal queued work beyond terminal-row sample limits."""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from pathlib import Path

from medre.core.storage.sqlite.schema import _SCHEMA


def test_reset_inspection_counts_and_samples_pending_work_separately() -> None:
    """The documented SQL cannot conceal a queued row behind a full terminal sample."""
    document = (
        Path(__file__).resolve().parents[1] / "docs/ops/operator-workflows.md"
    ).read_text(encoding="utf-8")
    match = re.search(
        r"sqlite3 -readonly /private/medre-copy.sqlite <<'SQL'\n(.*?)\nSQL",
        document,
        re.DOTALL,
    )
    assert match is not None
    statements = [sql for sql in match.group(1).split(";") if sql.strip()]
    with closing(sqlite3.connect(":memory:")) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(_SCHEMA)
        db.execute(
            "INSERT INTO canonical_events (event_id, event_kind, schema_version, "
            "timestamp, source_adapter, source_transport_id, created_at) "
            "VALUES ('event', 'message.created', 1, 'time', 'source', 'sender', 'time')"
        )
        db.executemany(
            "INSERT INTO delivery_outbox "
            "(outbox_id, event_id, delivery_plan_id, target_adapter, status, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (f"a-{i:03}", "event", "plan", "radio", "dead_lettered", "time", "time")
                for i in range(101)
            ]
            + [("z-queued", "event", "plan", "radio", "queued", "time", "time")],
        )
        results = [db.execute(sql).fetchall() for sql in statements]
    assert dict(results[1]) == {"dead_lettered": 101, "queued": 1}
    assert results[2] == [("z-queued", "event", "radio", "queued")]
    assert len(results[3]) == 100
    assert all(row[-1] == "dead_lettered" for row in results[3])


def test_reset_inspection_includes_admitted_work_without_an_outbox_row() -> None:
    """An empty outbox does not conceal an event waiting for ingress processing."""
    document = (
        Path(__file__).resolve().parents[1] / "docs/ops/operator-workflows.md"
    ).read_text(encoding="utf-8")
    match = re.search(
        r"sqlite3 -readonly /private/medre-copy.sqlite <<'SQL'\n(.*?)\nSQL",
        document,
        re.DOTALL,
    )
    assert match is not None
    statements = [sql for sql in match.group(1).split(";") if sql.strip()]
    with closing(sqlite3.connect(":memory:")) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(_SCHEMA)
        db.execute(
            "INSERT INTO canonical_events (event_id, event_kind, schema_version, "
            "timestamp, source_adapter, source_transport_id, created_at) "
            "VALUES ('admitted', 'message.created', 1, 'time', 'source', 'sender', 'time')"
        )
        db.execute(
            "INSERT INTO durable_ingress_work "
            "(event_id, provenance, status, created_at, updated_at) "
            "VALUES ('admitted', '{}', 'pending', 'time', 'time')"
        )
        results = [db.execute(sql).fetchall() for sql in statements]
    assert results[1] == []
    assert dict(results[4]) == {"pending": 1}
    assert results[5] == [("admitted", "pending", 0)]
