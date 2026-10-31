"""Storage column classification contract guards.

Three guarantees over the declared registry in
``medre.core.storage.classification``:

1. **Schema walk**: a fresh storage instance's real SQLite schema (via
   ``sqlite_master`` and ``pragma_table_info``) is covered by exactly one
   registry entry per persistent table and column, and the DDL source
   agrees.
2. **Content inventory parity**: the registry's independently-owned-content
   set equals the enforced ``CONTENT_SURFACES`` inventory exactly, so no
   column becomes collector-safe merely because the registry says so.
3. **Collector equivalence**: the registry reproduces the collector's
   conservative decisions exactly — wholesale and column-aware blocker
   families, the collectible candidate set, and the per-probe assessment
   labels emitted by ``_classify_copy``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from medre.core.storage.classification import (
    CONTENT_POTENTIAL_CLASSES,
    STORAGE_COLUMN_CLASSES,
    ColumnClass,
    column_class,
    columns_for_table,
    entries,
    entries_of_class,
    export_registry,
    known_tables,
    validate_registry,
)
from medre.core.storage.content import CONTENT_SURFACES
from medre.core.storage.sqlite._count import (
    _ALL_EVENT_COPY_PROBES,
    _EVENT_COPY_PROBES,
    _EVIDENCE_FAMILY_TABLES,
    _INTEGRITY_BLOCKER_PROBES,
    _LIVE_BLOCKER_PROBES,
    _WHOLESALE_BLOCKER_PROBES,
    COLLECTIBLE_CORE_SURFACES,
    _classify_copy,
    _content_potential_columns,
    _release_blockers,
)
from medre.core.storage.sqlite.schema import _SCHEMA
from medre.core.storage.sqlite.storage import SQLiteStorage

# ---------------------------------------------------------------------------
# Declared collector decision inputs (read off sqlite/_count.py probe SQL)
# ---------------------------------------------------------------------------

#: The collector-owned candidate tables: the only row families whose removal
#: the collection guard authorizes (docs/spec/content-collection-guard.md and
#: the storage module's DELETE allowlist).
_COLLECTOR_OWNED_TABLES = frozenset({"event_body_content", "event_relation_excerpts"})

#: Columns whose content potential each wholesale blocker probe's decision
#: accounts for.  Blocker probes count whole row families, so the accounting
#: covers the family's classified content columns, not only the columns the
#: probe SQL names.
_PROBE_CONTENT_ACCOUNTING: dict[str, frozenset[tuple[str, str]]] = {
    "canonical_text": frozenset({("event_body_content", "body")}),
    "relation_excerpts": frozenset({("event_relation_excerpts", "excerpt")}),
    "incoming_relation_excerpts": frozenset({("event_relation_excerpts", "excerpt")}),
    "opaque_envelope": frozenset(
        {("canonical_events", "payload"), ("canonical_events", "metadata")}
    ),
    "opaque_relation_records": frozenset(
        {("event_relations", "key"), ("event_relations", "metadata")}
    ),
    "radio_plans": frozenset(
        {
            ("radio_attempt_progress", "plan_json"),
            ("radio_attempt_progress", "provenance_json"),
            ("radio_attempt_progress", "parts_json"),
        }
    ),
    # Attachment bytes are reached through the association's content_ref
    # load authority; the association family blocks because of them.
    "attachment_associations": frozenset({("attachment_blobs", "data")}),
    "operator_decisions": frozenset(
        {("operator_delivery_decisions", "target_channel")}
    ),
}

#: Live-ownership gate probes: they block on operational-authority columns
#: whose nonterminal state means a live lifecycle still holds content.
_PROBE_AUTHORITY_GATES: dict[str, frozenset[tuple[str, str]]] = {
    "active_ingress": frozenset({("durable_ingress_work", "status")}),
    "inconsistent_admission_identity": frozenset(
        {("event_body_identity", "present_at_admission")}
    ),
    "active_outbox": frozenset({("delivery_outbox", "status")}),
    "unsettled_mandatory_owners": frozenset(
        {("event_content_dependencies", "released_at")}
    ),
    "unrevoked_optional_grants": frozenset(
        {
            ("event_content_access_grants", "revoked_at"),
            ("event_content_access_grants", "expired_at"),
        }
    ),
}

#: Retained durable delivery/native/ingress evidence probes.  Their row
#: families survive collection; a family blocks only while its mapped
#: table's registry content-potential columns still carry a value on the
#: event's rows (historical inline diagnostics, non-empty provider
#: metadata).  The write-time separated ``delivery_evidence_diagnostics``
#: side table is retained durable evidence too: its rows exist only under a
#: parent evidence row, survive collection, and never block.
_PROBE_RETAINED_EVIDENCE: dict[str, frozenset[tuple[str, str]]] = {
    "delivery_receipts": frozenset(
        {
            ("delivery_receipts", "error"),
            ("delivery_receipts", "capability_reason"),
            ("delivery_receipts", "rendering_evidence"),
        }
    ),
    "delivery_observations": frozenset(
        {
            ("delivery_observations", "error"),
            ("delivery_observations", "metadata"),
        }
    ),
    "outbox_records": frozenset(
        {
            ("delivery_outbox", "failure_kind_detail"),
            ("delivery_outbox", "error_summary"),
            ("delivery_outbox", "metadata"),
        }
    ),
    "native_references": frozenset({("native_message_refs", "metadata")}),
    "ingress_work": frozenset({("durable_ingress_work", "last_error")}),
    "retained_diagnostics": frozenset(
        {("delivery_evidence_diagnostics", "diagnostic")}
    ),
}

#: Probes whose read set is pure correlation without content accounting or a
#: live-ownership gate.
_IDENTITY_READ_PROBES: dict[str, frozenset[tuple[str, str]]] = {
    "relation_records": frozenset({("event_relations", "event_id")}),
    "shared_attachment_references": frozenset(
        {
            ("event_attachment_associations", "event_id"),
            ("event_attachment_associations", "content_ref"),
        }
    ),
}

#: Probes that count rows of collector-owned candidate tables for the
#: assessed event itself; any other probe over content-bearing columns
#: requires independent policy.
_OWN_SCOPE_CANDIDATE_PROBES = frozenset({"canonical_text", "relation_excerpts"})


def _resolve_class(table: str, column: str) -> ColumnClass:
    """Resolve a declared column in the registry or fail the assertion."""
    resolved = column_class(table, column)
    assert resolved is not None, f"Registry does not classify {table}.{column}"
    return resolved


def _is_own_scope_candidate(probe: str) -> bool:
    """Whether *probe* counts collector-owned content rows of this event."""
    accounted = _PROBE_CONTENT_ACCOUNTING.get(probe, frozenset())
    return (
        bool(accounted)
        and probe in _OWN_SCOPE_CANDIDATE_PROBES
        and all(table in _COLLECTOR_OWNED_TABLES for table, _ in accounted)
    )


def _derived_category(probe: str) -> str:
    """Derive the structural category for *probe* from the declared tables."""
    if probe in _PROBE_AUTHORITY_GATES:
        return "authority_or_state"
    if probe in _PROBE_RETAINED_EVIDENCE:
        if probe in _EVIDENCE_FAMILY_TABLES:
            return "retained_evidence_family"
        return "retained_evidence"
    if _PROBE_CONTENT_ACCOUNTING.get(probe):
        if _is_own_scope_candidate(probe):
            return "bounded_candidate"
        return "requires_independent_policy"
    reads = _IDENTITY_READ_PROBES.get(probe, frozenset())
    if any(
        _resolve_class(table, column) is ColumnClass.OPERATIONAL_AUTHORITY
        for table, column in reads
    ):
        return "shared_reference"
    return "identity_retained"


#: Structural category → emitted assessment label.  Both retained-evidence
#: categories report the same ``retained_evidence`` label; only the family
#: variant can appear in the blocker list (column-aware).
_DERIVED_LABELS: dict[str, str] = {
    "authority_or_state": "authority_or_state",
    "retained_evidence_family": "retained_evidence",
    "retained_evidence": "retained_evidence",
    "bounded_candidate": "bounded_candidate",
    "requires_independent_policy": "requires_independent_policy",
    "shared_reference": "shared_reference",
    "identity_retained": "identity_retained",
}


def _derived_label(probe: str) -> str:
    """Derive the assessment label for *probe* from the registry."""
    return _DERIVED_LABELS[_derived_category(probe)]


# ---------------------------------------------------------------------------
# 1. Registry declarations
# ---------------------------------------------------------------------------


def test_registry_invariants_hold() -> None:
    validate_registry()


def test_every_entry_has_valid_class() -> None:
    for entry in entries():
        assert isinstance(entry.column_class, ColumnClass)


def test_registry_entries_are_unique_and_complete() -> None:
    declared = sum(len(columns) for columns in STORAGE_COLUMN_CLASSES.values())
    all_entries = entries()
    keys = {(entry.table, entry.column) for entry in all_entries}
    assert len(keys) == len(all_entries) == declared


#: The registry's operational-authority timestamps, pinned so the declared
#: set and the spec contract's timestamp rule
#: (docs/spec/storage-column-classification.md) cannot drift apart.  The
#: allowlist entries are exactly the rule's mutable-row categories:
#: `updated_at`-style transition timestamps that guarded transitions
#: advance, lease and freshness bounds (`locked_at`, `next_attempt_at`,
#: `last_seen_at`), settlement and expiry bounds (`released_at`,
#: `revoked_at`, `expired_at`, `expires_at`), and the MIN-bound aggregation
#: anchor `first_seen_at`.  Recorded CAS guard snapshots (for example
#: `operator_delivery_decisions.observed_updated_at`) are written once with
#: their row and classified immutable identity, so they never belong here.
#: A new `*_at` column classified operational_authority MUST consciously
#: extend this allowlist and keep the rule text consistent in the same
#: change.
_AUTHORITY_TIMESTAMP_COLUMNS: frozenset[tuple[str, str]] = frozenset(
    {
        ("adapter_checkpoints", "updated_at"),
        ("conversation_membership", "updated_at"),
        ("conversation_projection_state", "updated_at"),
        ("delivery_outbox", "last_attempt_at"),
        ("delivery_outbox", "locked_at"),
        ("delivery_outbox", "next_attempt_at"),
        ("delivery_outbox", "updated_at"),
        ("durable_ingress_work", "locked_at"),
        ("durable_ingress_work", "updated_at"),
        ("event_content_access_grants", "expired_at"),
        ("event_content_access_grants", "expires_at"),
        ("event_content_access_grants", "revoked_at"),
        ("event_content_dependencies", "released_at"),
        ("inbound_pressure_observations", "first_seen_at"),
        ("inbound_pressure_observations", "last_seen_at"),
        ("plugin_state", "updated_at"),
        ("radio_attempt_progress", "updated_at"),
    }
)


def test_authority_timestamps_match_the_rule_allowlist() -> None:
    """The timestamp rule's authority set is exactly the pinned allowlist."""
    derived = {
        (entry.table, entry.column)
        for entry in entries_of_class(ColumnClass.OPERATIONAL_AUTHORITY)
        if entry.column.endswith("_at")
    }
    assert derived == _AUTHORITY_TIMESTAMP_COLUMNS, (
        "Authority-timestamp registry set and allowlist disagree: "
        f"registry-only={sorted(derived - _AUTHORITY_TIMESTAMP_COLUMNS)} "
        f"allowlist-only={sorted(_AUTHORITY_TIMESTAMP_COLUMNS - derived)}"
    )


# ---------------------------------------------------------------------------
# 2. Schema walk over a fresh storage instance
# ---------------------------------------------------------------------------


async def _introspect(store: SQLiteStorage) -> tuple[set[str], set[str]]:
    objects = await store._read_all(
        "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view')"
    )
    tables = {
        str(row["name"])
        for row in objects
        if row["type"] == "table" and not str(row["name"]).startswith("sqlite_")
    }
    internal = {
        str(row["name"])
        for row in objects
        if row["type"] == "table" and str(row["name"]).startswith("sqlite_")
    }
    return tables, internal


@pytest.mark.parametrize(
    ("table", "column", "expected"),
    [
        ("canonical_events", "event_id", ColumnClass.IMMUTABLE_IDENTITY),
        ("canonical_events", "payload", ColumnClass.INDEPENDENTLY_OWNED_CONTENT),
        ("delivery_outbox", "status", ColumnClass.OPERATIONAL_AUTHORITY),
        # Route-target text is structured correlation, not free-form
        # content: the outbox mirrors the receipt family's standing.
        ("delivery_outbox", "target_channel", ColumnClass.IMMUTABLE_IDENTITY),
        ("delivery_outbox", "target_address", ColumnClass.IMMUTABLE_IDENTITY),
        (
            "delivery_evidence_diagnostics",
            "diagnostic",
            ColumnClass.INDEPENDENTLY_OWNED_CONTENT,
        ),
        (
            "delivery_evidence_diagnostics",
            "row_id",
            ColumnClass.IMMUTABLE_IDENTITY,
        ),
    ],
)
def test_lookup_examples(table: str, column: str, expected: ColumnClass) -> None:
    """Spot-check the typed lookup API against canonical tables."""
    assert column_class(table, column) is expected
    assert column_class("missing_table", column) is None
    assert column_class(table, "missing_column") is None


async def test_every_real_table_and_column_is_classified(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "walk.db"))
    await store.initialize()
    try:
        tables, internal = await _introspect(store)
        # Pinned so a new internal or view object forces a decision here.
        assert internal == {"sqlite_sequence"}
        objects = await store._read_all(
            "SELECT name FROM sqlite_master WHERE type = 'view'"
        )
        assert {str(row["name"]) for row in objects} == {"delivery_status"}
        assert tables == set(known_tables()), (
            "Persistent tables and classification registry disagree: "
            f"schema-only={sorted(tables - set(known_tables()))} "
            f"registry-only={sorted(set(known_tables()) - tables)}"
        )
        for table in sorted(tables):
            # pragma_table_info validates columns, not just tables; the
            # table name is a bound parameter, never a quoted literal.
            live_columns = {
                str(row["name"])
                for row in await store._read_all(
                    "SELECT name FROM pragma_table_info(?)", (table,)
                )
            }
            assert live_columns, f"Table {table} has no columns"
            assert live_columns == set(columns_for_table(table)), (
                f"Table {table}: live columns and registry disagree: "
                f"live-only={sorted(live_columns - set(columns_for_table(table)))} "
                f"registry-only={sorted(set(columns_for_table(table)) - live_columns)}"
            )
            for column in sorted(live_columns):
                assert isinstance(column_class(table, column), ColumnClass)
    finally:
        await store.close()


def test_ddl_tables_match_registry() -> None:
    """Every CREATE TABLE in the DDL source has a registry entry."""
    ddl_tables = set(
        re.findall(
            r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)",
            _SCHEMA,
            re.IGNORECASE,
        )
    )
    assert ddl_tables == set(known_tables()), (
        "DDL tables and classification registry disagree: "
        f"ddl-only={sorted(ddl_tables - set(known_tables()))} "
        f"registry-only={sorted(set(known_tables()) - ddl_tables)}"
    )


# ---------------------------------------------------------------------------
# 3. Content inventory parity
# ---------------------------------------------------------------------------


def _inventory_columns() -> set[tuple[str, str]]:
    return {
        (surface.table, column)
        for surface in CONTENT_SURFACES
        for column in surface.columns
    }


def test_content_class_equals_inventory_exactly() -> None:
    classified = {
        (entry.table, entry.column)
        for entry in entries_of_class(ColumnClass.INDEPENDENTLY_OWNED_CONTENT)
    }
    assert classified == _inventory_columns(), (
        "Registry content columns and CONTENT_SURFACES disagree: "
        f"registry-only={sorted(classified - _inventory_columns())} "
        f"inventory-only={sorted(_inventory_columns() - classified)}"
    )


def test_no_inventory_column_is_identity_or_authority() -> None:
    for table, column in _inventory_columns():
        resolved = _resolve_class(table, column)
        assert resolved not in (
            ColumnClass.IMMUTABLE_IDENTITY,
            ColumnClass.OPERATIONAL_AUTHORITY,
        ), f"Inventory column {table}.{column} classified as {resolved}"


def test_unclassified_columns_are_reviewed_remainder_only() -> None:
    """The conservative remainder is exactly the audited unreviewed set.

    ``operator_delivery_decisions.target_channel`` is the only column
    whose copy family blocks collection without any reviewed
    column-level classification of its free-form route-target text.
    Growing this set requires the spec contract's review step.
    """
    unclassified = {
        (entry.table, entry.column)
        for entry in entries_of_class(ColumnClass.UNCLASSIFIED)
    }
    assert unclassified == {("operator_delivery_decisions", "target_channel")}
    assert unclassified.isdisjoint(_inventory_columns())


# ---------------------------------------------------------------------------
# 4. Collector equivalence
# ---------------------------------------------------------------------------


def _all_declared_probes() -> set[str]:
    return (
        set(_PROBE_CONTENT_ACCOUNTING)
        | set(_PROBE_AUTHORITY_GATES)
        | set(_PROBE_RETAINED_EVIDENCE)
        | set(_IDENTITY_READ_PROBES)
    )


def test_every_probe_is_declared() -> None:
    """A new collector probe forces an explicit registry declaration."""
    assert set(_EVENT_COPY_PROBES) == _all_declared_probes()


def test_decision_probes_are_report_probes_plus_family_content() -> None:
    """The compound decision SQL extends the report probes exactly once."""
    expected = set(_EVENT_COPY_PROBES) | {
        f"{family}_content" for family in _EVIDENCE_FAMILY_TABLES
    }
    assert set(_ALL_EVENT_COPY_PROBES) == expected


def test_accounted_columns_have_content_potential() -> None:
    for accounting in (
        _PROBE_CONTENT_ACCOUNTING,
        _PROBE_RETAINED_EVIDENCE,
    ):
        for probe, accounted in accounting.items():
            for table, column in sorted(accounted):
                resolved = _resolve_class(table, column)
                assert resolved in CONTENT_POTENTIAL_CLASSES, (
                    f"{probe} accounts {table}.{column} but the registry "
                    f"classifies it {resolved.value}"
                )


def test_gate_columns_are_operational_authority() -> None:
    for probe, gates in _PROBE_AUTHORITY_GATES.items():
        for table, column in sorted(gates):
            resolved = _resolve_class(table, column)
            assert resolved is ColumnClass.OPERATIONAL_AUTHORITY, (
                f"{probe} gates on {table}.{column} but the registry "
                f"classifies it {resolved.value}"
            )


def test_evidence_families_derive_from_registry_content_columns() -> None:
    """Each family's content probe predicates on its registry columns."""
    for family, table in _EVIDENCE_FAMILY_TABLES.items():
        accounted = _PROBE_RETAINED_EVIDENCE[family]
        accounted_tables = {t for t, _ in accounted}
        assert accounted_tables == {table}, (
            f"{family} declares accounting over {sorted(accounted_tables)} "
            f"but the collector maps it to {table}"
        )
        registry_columns = frozenset(_content_potential_columns(table))
        assert registry_columns == {c for _, c in accounted}, (
            f"{family}: registry content columns {sorted(registry_columns)} "
            f"disagree with the declared accounting {sorted(accounted)}"
        )
        probe_sql = _ALL_EVENT_COPY_PROBES[f"{family}_content"]
        predicate_columns = set(re.findall(r"t\.(\w+) IS NOT NULL", probe_sql))
        assert predicate_columns == set(
            registry_columns
        ), f"{family} probe predicates on {sorted(predicate_columns)}"
        for column in sorted(registry_columns):
            assert f"t.{column} IS NOT NULL" in probe_sql
            if column == "metadata":
                assert f"t.{column}<>" + "'{}'" in probe_sql
            else:
                assert f"t.{column}<>" + "'{}'" not in probe_sql


def test_derived_blocker_set_matches_collector_exactly() -> None:
    derived_blockers = {
        probe
        for probe in _all_declared_probes()
        if _derived_category(probe)
        in {
            "authority_or_state",
            "requires_independent_policy",
            "retained_evidence_family",
        }
    }
    actual_blockers = (
        set(_LIVE_BLOCKER_PROBES)
        | set(_INTEGRITY_BLOCKER_PROBES)
        | set(_WHOLESALE_BLOCKER_PROBES)
        | set(_EVIDENCE_FAMILY_TABLES)
    )
    assert derived_blockers == actual_blockers, (
        "Registry-derived blockers and collector blockers disagree: "
        f"registry-only={sorted(derived_blockers - actual_blockers)} "
        f"collector-only={sorted(actual_blockers - derived_blockers)}"
    )


def test_derived_candidates_match_collectible_core_exactly() -> None:
    derived_candidates = {
        probe
        for probe in _all_declared_probes()
        if _derived_category(probe) == "bounded_candidate"
    }
    assert derived_candidates == set(COLLECTIBLE_CORE_SURFACES)


def test_derived_policy_blockers_match_wholesale_blockers_exactly() -> None:
    derived = {
        probe
        for probe in _all_declared_probes()
        if _derived_category(probe) == "requires_independent_policy"
    }
    assert derived == set(_WHOLESALE_BLOCKER_PROBES)


def test_derived_gates_match_live_blockers_exactly() -> None:
    derived = {
        probe
        for probe in _all_declared_probes()
        if _derived_category(probe) == "authority_or_state"
    }
    assert derived == set(_LIVE_BLOCKER_PROBES) | {"inconsistent_admission_identity"}


def test_derived_retained_evidence_split_matches_collector_exactly() -> None:
    """The five column-aware families and the never-blocking side table."""
    families = {
        probe
        for probe in _all_declared_probes()
        if _derived_category(probe) == "retained_evidence_family"
    }
    assert families == set(_EVIDENCE_FAMILY_TABLES)
    never_blocking = {
        probe
        for probe in _all_declared_probes()
        if _derived_category(probe) == "retained_evidence"
    }
    assert never_blocking == {"retained_diagnostics"}


def test_release_blocker_decision_is_column_aware() -> None:
    """The shared decision blocks on content values, never family rows."""
    empty = dict.fromkeys(_ALL_EVENT_COPY_PROBES, 0)
    assert _release_blockers(empty) == []
    live = {**empty, "active_outbox": 1, "unsettled_mandatory_owners": 2}
    assert _release_blockers(live) == [
        "active_outbox",
        "unsettled_mandatory_owners",
    ]
    wholesale = {**empty, "opaque_envelope": 1, "radio_plans": 3}
    assert _release_blockers(wholesale) == ["opaque_envelope", "radio_plans"]
    # Family rows without content values never block.
    settled = {
        **empty,
        "delivery_receipts": 2,
        "outbox_records": 1,
        "ingress_work": 1,
        "retained_diagnostics": 1,
        "delivery_receipts_content": 0,
        "outbox_records_content": 0,
        "ingress_work_content": 0,
    }
    assert _release_blockers(settled) == []
    # Any content-bearing value in one family blocks exactly that family.
    holding = {**settled, "outbox_records_content": 1}
    assert _release_blockers(holding) == ["outbox_records"]


def test_derived_identity_probes_are_exactly_the_remainder() -> None:
    derived = {
        probe
        for probe in _all_declared_probes()
        if _derived_category(probe) in {"identity_retained", "shared_reference"}
    }
    assert derived == {"relation_records", "shared_attachment_references"}


def test_derived_labels_match_assessment_classification_exactly() -> None:
    """Every probe's registry-derived label equals ``_classify_copy``."""
    mismatches = {
        probe: (_derived_label(probe), _classify_copy(probe))
        for probe in _EVENT_COPY_PROBES
        if _derived_label(probe) != _classify_copy(probe)
    }
    assert mismatches == {}


def test_content_columns_are_accounted_or_outside_event_scope() -> None:
    """Bridge completeness for event-scoped content-potential columns."""
    accounted: set[tuple[str, str]] = set()
    for columns in _PROBE_CONTENT_ACCOUNTING.values():
        accounted |= columns
    for columns in _PROBE_RETAINED_EVIDENCE.values():
        accounted |= columns
    unaccounted = []
    for entry in entries():
        if not entry.has_content_potential:
            continue
        if (entry.table, entry.column) in accounted:
            continue
        if "event_id" not in STORAGE_COLUMN_CLASSES[entry.table]:
            continue
        unaccounted.append(f"{entry.table}.{entry.column}")
    assert unaccounted == []


def test_registry_export_matches_declared_schema() -> None:
    schema_path = (
        Path(__file__).resolve().parents[1]
        / "docs/schemas/storage-column-classification.schema.json"
    )
    schema = json.loads(schema_path.read_text())
    export = export_registry()
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(export)
    declared = {member.value for member in ColumnClass}
    assert set(schema["properties"]["classes"]["items"]["enum"]) == declared
    assert (
        set(
            schema["properties"]["tables"]["items"]["properties"]["columns"]["items"][
                "properties"
            ]["column_class"]["enum"]
        )
        == declared
    )
    assert {item["table"] for item in export["tables"]} == set(STORAGE_COLUMN_CLASSES)
