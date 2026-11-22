"""Mechanical alignment between the column registry and ceiling enforcement.

The column registry (``STORAGE_COLUMN_CLASSES``) is the column-level
authority for "content-potential"; the enforcement purge and the residual
battery must cover every content-potential, event-attributable registry
column. These tests tie the three together mechanically:

- the reviewed exception maps partition the registry's content-potential
  columns into covered, non-event-scoped, and expressly retained sets;
- a per-column plan map (battery labels + seed + cleared probe) must cover
  exactly the covered set, and the residual battery's label set must equal
  the union of the planned labels plus the association load-authority store;
- each covered column is seeded in isolation on one admitted event: the
  battery audit must detect the sentinel before enforcement and report zero
  residuals after, and the column must be observed cleared or its rows
  deleted;
- a full-seed event enforces once end to end and commits a passing receipt.

What these tests catch: a registry content-potential column added without a
plan (plan-map key equality), a column mapped but missing from the battery
(label equality and the truncation self-checks), a column mapped but missed
by the purge (enforcement raises; the post-purge audit and the cleared probe
fail), and a battery entry that predicates on no planned column (reverse
label equality, except the documented association label).

What these tests do not catch: a persistent table missing from the registry
itself (the schema-walk guard in ``test_storage_column_classification``),
battery predicates' value-shape blind spots (sentinels are distinctive
non-placeholder values), and physical SQLite remnants outside the logical
stores (spec §5 operator erasure).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from medre.core.storage.backend import OutboxDispatchSnapshotWrite
from medre.core.storage.classification import (
    ColumnClass,
    content_potential_entries,
    entries_of_class,
)
from medre.core.storage.sqlite._ceiling_enforcement import _RESIDUAL_BATTERY
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.ceiling_audit import (
    _admit_plain,
    _admit_with_ceiling,
    _associate_blob,
    _count,
    _insert_blob,
    _read_ceiling,
    _snapshot,
)

#: Content-potential registry columns outside event scope, with the census
#: reason each stays unmeasured (spec §3 census rows for the two tables).
_NON_EVENT_SCOPED: tuple[tuple[tuple[str, str], str], ...] = (
    (
        ("adapter_checkpoints", "cursor"),
        "adapter-scoped, no event attribution exists; documented unmeasured scope",
    ),
    (
        ("adapter_checkpoints", "metadata"),
        "adapter-scoped, no event attribution exists; documented unmeasured scope",
    ),
    (
        ("plugin_state", "value"),
        "plugin-scoped, no event attribution exists; documented unmeasured scope",
    ),
)

#: The one express exception, with the specification's reason: the copy
#: family is conservatively blocked as unreviewed, and the route-target text
#: is an expressly retained operator-decision identity field, not part of
#: the residual battery (spec §3 census row and registry-contract paragraph).
_EXPRESS_EXCEPTIONS: tuple[tuple[tuple[str, str], str], ...] = (
    (
        ("operator_delivery_decisions", "target_channel"),
        "expressly retained operator-decision identity field, not part of the residual battery",
    ),
)

_COLUMN_KEY = tuple[str, str]
_Seed = Callable[[SQLiteStorage, str, str], Awaitable[None]]
_Cleared = Callable[[SQLiteStorage, str], Awaitable[bool]]

_UNSET = "{}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True, slots=True)
class _ColumnPlan:
    """How one covered column is detected, purged, and observed cleared."""

    labels: tuple[str, ...]
    seed: _Seed
    cleared: _Cleared


async def _new_outbox(
    store: SQLiteStorage,
    event_id: str,
    suffix: str,
    snapshot: OutboxDispatchSnapshotWrite | None = None,
) -> str:
    from tests.helpers.storage_outbox import make_outbox_item

    item = make_outbox_item(
        delivery_plan_id=f"plan-{event_id}-{suffix}",
        event_id=event_id,
        target_adapter="radio",
    )
    created = await store.create_outbox_item(item, dispatch_snapshot=snapshot)
    return created.outbox_id


async def _seed_body(store: SQLiteStorage, event_id: str, suffix: str) -> None:
    await store._write(
        "INSERT INTO event_body_content (event_id, field, body, created_at) "
        "VALUES (?, 'body', ?, ?) "
        "ON CONFLICT(event_id, field) DO UPDATE SET body=excluded.body",
        (event_id, f"alignment body {suffix}", _now()),
    )


async def _seed_canonical_payload(
    store: SQLiteStorage, event_id: str, suffix: str
) -> None:
    await store._write(
        "UPDATE canonical_events SET payload=? WHERE event_id=?",
        (f'{{"opaque":"alignment payload {suffix}"}}', event_id),
    )


async def _seed_canonical_metadata(
    store: SQLiteStorage, event_id: str, suffix: str
) -> None:
    await store._write(
        "UPDATE canonical_events SET metadata=? WHERE event_id=?",
        (f'{{"opaque":"alignment metadata {suffix}"}}', event_id),
    )


async def _insert_relation(
    store: SQLiteStorage, event_id: str, *, key: str | None, metadata: str
) -> int:
    await store._write(
        "INSERT INTO event_relations (event_id, relation_type, target_event_id, "
        "key, metadata, created_at) VALUES (?, 'reply', NULL, ?, ?, ?)",
        (event_id, key, metadata, _now()),
    )
    row = await store._read_one(
        "SELECT id FROM event_relations WHERE event_id=? ORDER BY id DESC LIMIT 1",
        (event_id,),
    )
    assert row is not None
    return int(row["id"])


async def _seed_relation_key(store: SQLiteStorage, event_id: str, suffix: str) -> None:
    await _insert_relation(
        store, event_id, key=f"alignment key {suffix}", metadata=_UNSET
    )


async def _seed_relation_metadata(
    store: SQLiteStorage, event_id: str, suffix: str
) -> None:
    await _insert_relation(
        store,
        event_id,
        key=None,
        metadata=f'{{"opaque":"alignment relation {suffix}"}}',
    )


async def _insert_excerpt(store: SQLiteStorage, relation_id: int, text: str) -> None:
    await store._write(
        "INSERT INTO event_relation_excerpts (relation_id, excerpt) VALUES (?, ?)",
        (relation_id, text),
    )


async def _seed_excerpt(store: SQLiteStorage, event_id: str, suffix: str) -> None:
    """Seed the event's own excerpt and an incoming neighbor excerpt."""
    own_relation = await _insert_relation(store, event_id, key=None, metadata=_UNSET)
    await _insert_excerpt(store, own_relation, f"alignment excerpt {suffix}")
    neighbor = f"neighbor-{event_id}-{suffix}"
    await _admit_plain(store, neighbor)
    neighbor_relation = await _insert_relation_for_target(store, neighbor, event_id)
    await _insert_excerpt(store, neighbor_relation, f"alignment incoming {suffix}")


async def _insert_relation_for_target(
    store: SQLiteStorage, event_id: str, target_event_id: str
) -> int:
    await store._write(
        "INSERT INTO event_relations (event_id, relation_type, target_event_id, "
        "key, metadata, created_at) VALUES (?, 'reply', ?, NULL, '{}', ?)",
        (event_id, target_event_id, _now()),
    )
    row = await store._read_one(
        "SELECT id FROM event_relations WHERE event_id=? ORDER BY id DESC LIMIT 1",
        (event_id,),
    )
    assert row is not None
    return int(row["id"])


async def _seed_native_metadata(
    store: SQLiteStorage, event_id: str, suffix: str
) -> None:
    await store._write(
        "INSERT INTO native_message_refs (id, event_id, adapter, "
        "native_message_id, direction, metadata, created_at) "
        "VALUES (?, ?, 'source', ?, 'inbound', ?, ?)",
        (
            f"nref-{event_id}-{suffix}",
            event_id,
            f"nm-{event_id}-{suffix}",
            f'{{"opaque":"alignment native {suffix}"}}',
            _now(),
        ),
    )


async def _seed_outbox_metadata(
    store: SQLiteStorage, event_id: str, suffix: str
) -> None:
    outbox_id = await _new_outbox(store, event_id, suffix)
    await store._write(
        "UPDATE delivery_outbox SET metadata=? WHERE outbox_id=?",
        (f'{{"opaque":"alignment dispatch {suffix}"}}', outbox_id),
    )


async def _seed_outbox_payload_hash(
    store: SQLiteStorage, event_id: str, suffix: str
) -> None:
    outbox_id = await _new_outbox(store, event_id, suffix)
    await store._write(
        "UPDATE delivery_outbox SET payload_hash=? WHERE outbox_id=?",
        ("cd" * 32, outbox_id),
    )


async def _seed_observation_metadata(
    store: SQLiteStorage, event_id: str, suffix: str
) -> None:
    outbox_id = await _new_outbox(store, event_id, suffix)
    await store._write(
        "INSERT INTO delivery_observations (observation_id, event_id, "
        "delivery_plan_id, target_adapter, outbox_id, attempt_number, state, "
        "metadata, observed_at) VALUES (?, ?, ?, 'radio', ?, 1, 'delivered', "
        "?, ?)",
        (
            f"obs-{event_id}-{suffix}",
            event_id,
            f"plan-{event_id}-{suffix}",
            outbox_id,
            f'{{"opaque":"alignment observation {suffix}"}}',
            _now(),
        ),
    )


async def _seed_diagnostic(store: SQLiteStorage, event_id: str, suffix: str) -> None:
    await store._write(
        "INSERT INTO delivery_evidence_diagnostics (family, row_id, attempt, "
        "field, diagnostic, created_at) VALUES ('ingress', ?, 1, 'error', "
        "?, ?)",
        (event_id, f"alignment diagnostic {suffix}", _now()),
    )


async def _seed_radio_progress(
    store: SQLiteStorage, event_id: str, suffix: str
) -> None:
    """Seed one radio recovery row; the row is a unit (NOT NULL columns)."""
    outbox_id = await _new_outbox(store, event_id, suffix)
    await store._write(
        "INSERT INTO radio_attempt_progress (outbox_id, attempt_number, "
        "plan_fingerprint, plan_json, provenance_json, parts_json, revision, "
        "created_at, updated_at) VALUES (?, 1, ?, ?, ?, ?, 0, ?, ?)",
        (
            outbox_id,
            "ab" * 32,
            f'{{"opaque":"alignment plan {suffix}"}}',
            f'{{"opaque":"alignment provenance {suffix}"}}',
            f'{{"parts":["alignment parts {suffix}"]}}',
            _now(),
            _now(),
        ),
    )


async def _seed_snapshot_context(
    store: SQLiteStorage, event_id: str, suffix: str
) -> None:
    await _new_outbox(
        store,
        event_id,
        suffix,
        snapshot=_snapshot({"opaque": f"alignment snapshot {suffix}"}),
    )


async def _seed_blob_data(store: SQLiteStorage, event_id: str, suffix: str) -> None:
    content_ref = f"alignment-blob-{suffix}"
    await _insert_blob(store, content_ref)
    await _associate_blob(store, event_id, content_ref)


async def _cleared_body(store: SQLiteStorage, event_id: str) -> bool:
    return (
        await _count(
            store,
            "SELECT COUNT(*) FROM event_body_content WHERE event_id=?",
            (event_id,),
        )
        == 0
    )


async def _cleared_canonical_values(store: SQLiteStorage, event_id: str) -> bool:
    row = await store._read_one(
        "SELECT payload, metadata FROM canonical_events WHERE event_id=?", (event_id,)
    )
    return (
        row is not None
        and str(row["payload"]) == _UNSET
        and str(row["metadata"]) == _UNSET
    )


async def _cleared_relation_values(store: SQLiteStorage, event_id: str) -> bool:
    return (
        await _count(
            store,
            "SELECT COUNT(*) FROM event_relations WHERE event_id=? "
            "AND (key IS NOT NULL OR metadata<>'{}')",
            (event_id,),
        )
        == 0
    )


async def _cleared_excerpts(store: SQLiteStorage, event_id: str) -> bool:
    own = await _count(
        store,
        "SELECT COUNT(*) FROM event_relation_excerpts x WHERE x.relation_id IN "
        "(SELECT r.id FROM event_relations r WHERE r.event_id=?)",
        (event_id,),
    )
    incoming = await _count(
        store,
        "SELECT COUNT(*) FROM event_relation_excerpts x WHERE x.relation_id IN "
        "(SELECT r.id FROM event_relations r WHERE r.event_id<>? "
        "AND r.target_event_id=?)",
        (event_id, event_id),
    )
    return own == 0 and incoming == 0


async def _cleared_native_metadata(store: SQLiteStorage, event_id: str) -> bool:
    return (
        await _count(
            store,
            "SELECT COUNT(*) FROM native_message_refs WHERE event_id=? "
            "AND metadata<>'{}'",
            (event_id,),
        )
        == 0
    )


async def _cleared_outbox_values(store: SQLiteStorage, event_id: str) -> bool:
    return (
        await _count(
            store,
            "SELECT COUNT(*) FROM delivery_outbox WHERE event_id=? "
            "AND (metadata<>'{}' OR payload_hash IS NOT NULL)",
            (event_id,),
        )
        == 0
    )


async def _cleared_observation_metadata(store: SQLiteStorage, event_id: str) -> bool:
    return (
        await _count(
            store,
            "SELECT COUNT(*) FROM delivery_observations WHERE event_id=? "
            "AND metadata<>'{}'",
            (event_id,),
        )
        == 0
    )


async def _cleared_diagnostics(store: SQLiteStorage, event_id: str) -> bool:
    return (
        await _count(
            store,
            "SELECT COUNT(*) FROM delivery_evidence_diagnostics "
            "WHERE family='ingress' AND row_id=?",
            (event_id,),
        )
        == 0
    )


async def _cleared_radio_rows(store: SQLiteStorage, event_id: str) -> bool:
    return (
        await _count(
            store,
            "SELECT COUNT(*) FROM radio_attempt_progress p "
            "JOIN delivery_outbox o ON o.outbox_id=p.outbox_id WHERE o.event_id=?",
            (event_id,),
        )
        == 0
    )


async def _cleared_snapshot_rows(store: SQLiteStorage, event_id: str) -> bool:
    return (
        await _count(
            store,
            "SELECT COUNT(*) FROM delivery_outbox_snapshots s "
            "JOIN delivery_outbox o ON o.outbox_id=s.outbox_id WHERE o.event_id=?",
            (event_id,),
        )
        == 0
    )


async def _cleared_blob_data(store: SQLiteStorage, event_id: str) -> bool:
    return (
        await _count(
            store,
            "SELECT COUNT(*) FROM attachment_blobs b "
            "JOIN event_attachment_associations a ON a.content_ref=b.content_ref "
            "WHERE a.event_id=?",
            (event_id,),
        )
        == 0
    )


#: One plan per covered registry column, in seeding order. The body comes
#: first because admission already created its row. The three radio columns
#: share a row unit (the schema requires all three JSON columns NOT NULL),
#: so each seeds its own row with its own sentinel.
_COLUMN_PLANS: dict[_COLUMN_KEY, _ColumnPlan] = {
    ("event_body_content", "body"): _ColumnPlan(
        ("event_body_content",), _seed_body, _cleared_body
    ),
    ("canonical_events", "payload"): _ColumnPlan(
        ("canonical_events",), _seed_canonical_payload, _cleared_canonical_values
    ),
    ("canonical_events", "metadata"): _ColumnPlan(
        ("canonical_events",), _seed_canonical_metadata, _cleared_canonical_values
    ),
    ("event_relations", "key"): _ColumnPlan(
        ("event_relations",), _seed_relation_key, _cleared_relation_values
    ),
    ("event_relations", "metadata"): _ColumnPlan(
        ("event_relations",), _seed_relation_metadata, _cleared_relation_values
    ),
    ("event_relation_excerpts", "excerpt"): _ColumnPlan(
        ("event_relation_excerpts", "event_relation_excerpts:incoming"),
        _seed_excerpt,
        _cleared_excerpts,
    ),
    ("native_message_refs", "metadata"): _ColumnPlan(
        ("native_message_refs",), _seed_native_metadata, _cleared_native_metadata
    ),
    ("delivery_outbox", "payload_hash"): _ColumnPlan(
        ("delivery_outbox",), _seed_outbox_payload_hash, _cleared_outbox_values
    ),
    ("delivery_outbox", "metadata"): _ColumnPlan(
        ("delivery_outbox",), _seed_outbox_metadata, _cleared_outbox_values
    ),
    ("delivery_observations", "metadata"): _ColumnPlan(
        ("delivery_observations",),
        _seed_observation_metadata,
        _cleared_observation_metadata,
    ),
    ("delivery_evidence_diagnostics", "diagnostic"): _ColumnPlan(
        ("delivery_evidence_diagnostics",), _seed_diagnostic, _cleared_diagnostics
    ),
    ("radio_attempt_progress", "plan_json"): _ColumnPlan(
        ("radio_attempt_progress",), _seed_radio_progress, _cleared_radio_rows
    ),
    ("radio_attempt_progress", "provenance_json"): _ColumnPlan(
        ("radio_attempt_progress",), _seed_radio_progress, _cleared_radio_rows
    ),
    ("radio_attempt_progress", "parts_json"): _ColumnPlan(
        ("radio_attempt_progress",), _seed_radio_progress, _cleared_radio_rows
    ),
    ("delivery_outbox_snapshots", "context_json"): _ColumnPlan(
        ("delivery_outbox_snapshots",), _seed_snapshot_context, _cleared_snapshot_rows
    ),
    ("attachment_blobs", "data"): _ColumnPlan(
        ("attachment_blobs:last_reference",), _seed_blob_data, _cleared_blob_data
    ),
}


def _covered_columns() -> set[_COLUMN_KEY]:
    potential = {(entry.table, entry.column) for entry in content_potential_entries()}
    return (
        potential
        - {key for key, _ in _NON_EVENT_SCOPED}
        - {key for key, _ in _EXPRESS_EXCEPTIONS}
    )


def _uncovered_columns(
    plans: dict[_COLUMN_KEY, _ColumnPlan],
    battery: tuple[tuple[str, str], ...],
) -> set[tuple[str, str, str]]:
    """Report every (column, battery label) pair the battery omits.

    The battery table is a parameter so a truncated copy can prove the
    detection without editing production code.
    """
    battery_labels = {label for label, _ in battery}
    return {
        (table, column, label)
        for (table, column), plan in plans.items()
        for label in plan.labels
        if label not in battery_labels
    }


def test_reviewed_maps_partition_the_content_potential_registry() -> None:
    potential = {(entry.table, entry.column) for entry in content_potential_entries()}
    non_scoped = {key for key, _ in _NON_EVENT_SCOPED}
    exceptions = {key for key, _ in _EXPRESS_EXCEPTIONS}
    covered = potential - non_scoped - exceptions
    assert not non_scoped & exceptions
    assert non_scoped | exceptions | covered == potential
    assert len(non_scoped) + len(exceptions) + len(covered) == len(potential)
    non_scoped_tables = {table for table, _ in non_scoped}
    assert non_scoped_tables == {"adapter_checkpoints", "plugin_state"}
    assert non_scoped == {
        (entry.table, entry.column)
        for entry in content_potential_entries()
        if entry.table in non_scoped_tables
    }
    unclassified = {
        (entry.table, entry.column)
        for entry in entries_of_class(ColumnClass.UNCLASSIFIED)
    }
    assert exceptions == unclassified
    for _, reason in (*_NON_EVENT_SCOPED, *_EXPRESS_EXCEPTIONS):
        assert reason


def test_plan_map_covers_exactly_the_covered_registry_columns() -> None:
    assert set(_COLUMN_PLANS) == _covered_columns()


def test_battery_labels_equal_the_planned_labels() -> None:
    assert _uncovered_columns(_COLUMN_PLANS, _RESIDUAL_BATTERY) == set()
    planned = {label for plan in _COLUMN_PLANS.values() for label in plan.labels}
    assert {label for label, _ in _RESIDUAL_BATTERY} == (
        planned | {"event_attachment_associations"}
    )


def test_self_check_reports_a_truncated_battery() -> None:
    without_canonical = tuple(
        entry for entry in _RESIDUAL_BATTERY if entry[0] != "canonical_events"
    )
    assert _uncovered_columns(_COLUMN_PLANS, without_canonical) == {
        ("canonical_events", "payload", "canonical_events"),
        ("canonical_events", "metadata", "canonical_events"),
    }
    without_incoming = tuple(
        entry
        for entry in _RESIDUAL_BATTERY
        if entry[0] != "event_relation_excerpts:incoming"
    )
    assert _uncovered_columns(_COLUMN_PLANS, without_incoming) == {
        ("event_relation_excerpts", "excerpt", "event_relation_excerpts:incoming"),
    }


async def test_purge_and_battery_cover_each_covered_column_in_isolation(
    temp_storage: SQLiteStorage,
) -> None:
    event_id = "alignment-isolation"
    await _admit_with_ceiling(temp_storage, event_id)
    ceiling = await _read_ceiling(temp_storage, event_id)
    elapsed = ceiling + timedelta(seconds=1)
    for (table, column), plan in _COLUMN_PLANS.items():
        suffix = f"{table}-{column}".replace("_", "-")
        await plan.seed(temp_storage, event_id, suffix)
        audit = await temp_storage.audit_content_ceilings(
            now=elapsed, event_id=event_id
        )
        missing = sorted(set(plan.labels) - set(audit["residual_stores"]))
        assert not missing, f"battery missed {table}.{column}: {missing}"
        await temp_storage.enforce_content_ceilings(now=elapsed)
        post = await temp_storage.audit_content_ceilings(now=elapsed, event_id=event_id)
        assert (
            post["residual_stores"] == {}
        ), f"residual after purging {table}.{column}: {post['residual_stores']}"
        assert await plan.cleared(
            temp_storage, event_id
        ), f"{table}.{column} not cleared after enforcement"


async def test_full_seed_enforcement_commits_a_passing_receipt(
    temp_storage: SQLiteStorage,
) -> None:
    event_id = "alignment-full-seed"
    await _admit_with_ceiling(temp_storage, event_id)
    ceiling = await _read_ceiling(temp_storage, event_id)
    elapsed = ceiling + timedelta(seconds=1)
    for (table, column), plan in _COLUMN_PLANS.items():
        await plan.seed(
            temp_storage, event_id, f"full-{table}-{column}".replace("_", "-")
        )
    summaries = await temp_storage.enforce_content_ceilings(now=elapsed)
    summary = next(s for s in summaries if s.event_id == event_id)
    assert summary.residual_pass is False
    assert summary.rows_removed > 0
    audit = await temp_storage.audit_content_ceilings(now=elapsed, event_id=event_id)
    assert audit["residual_stores"] == {}
    verified = await temp_storage.verify_content_ceiling_receipts(
        limit=10, event_id=event_id
    )
    assert verified["verdict"] == "passed"
