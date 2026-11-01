"""Count query mixins for SQLiteStorage.

Authority surface: all methods are **list/get** (read-only).  Pure
aggregation queries that never mutate storage.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from medre.core.events.schema import VALID_RELATION_TYPES
from medre.core.storage.backend import StorageError
from medre.core.storage.classification import (
    CONTENT_POTENTIAL_CLASSES,
    STORAGE_COLUMN_CLASSES,
)
from medre.core.storage.content import CONTENT_SURFACES, inventory_sql

# The relation type is a closed core enum; a corrupted/free-form value is
# not assumed to be content-free structured identity.
_KNOWN_RELATION_TYPES_SQL = ", ".join(
    f"'{kind}'" for kind in sorted(VALID_RELATION_TYPES)
)

# Closed, event-correlated inventory. All probes return counts, never values.
# No guess that an arbitrary JSON/metadata field is free of message content.
_EVENT_COPY_PROBES: dict[str, str] = {
    "canonical_text": "SELECT COUNT(*) FROM event_body_content c WHERE c.event_id=e.event_id",
    # The advisory assessment must not call a broken admission identity
    # collectable.  Evaluate the same row-presence rules as the guarded
    # collector without reading content or acquiring any new authority.
    "inconsistent_admission_identity": (
        "SELECT CASE WHEN NOT EXISTS ("
        "SELECT 1 FROM event_body_identity i WHERE i.event_id=e.event_id "
        "AND i.present_at_admission IN (0,1)) "
        "OR EXISTS (SELECT 1 FROM event_body_identity i "
        "WHERE i.event_id=e.event_id AND ("
        "(i.present_at_admission=0 AND EXISTS ("
        "SELECT 1 FROM event_body_content b WHERE b.event_id=e.event_id)) "
        "OR (i.present_at_admission=1 AND NOT EXISTS ("
        "SELECT 1 FROM event_body_content b WHERE b.event_id=e.event_id) "
        "AND NOT EXISTS (SELECT 1 FROM event_content_collection_log l "
        "WHERE l.event_id=e.event_id)))) "
        "OR (EXISTS (SELECT 1 FROM event_content_collection_log l "
        "WHERE l.event_id=e.event_id) AND ("
        "EXISTS (SELECT 1 FROM event_body_content b WHERE b.event_id=e.event_id) "
        "OR EXISTS (SELECT 1 FROM event_relation_excerpts x "
        "JOIN event_relations r ON r.id=x.relation_id WHERE r.event_id=e.event_id))) "
        "THEN 1 ELSE 0 END"
    ),
    "relation_excerpts": "SELECT COUNT(*) FROM event_relation_excerpts x JOIN event_relations r ON r.id=x.relation_id WHERE r.event_id=e.event_id",
    "incoming_relation_excerpts": (
        "SELECT COUNT(*) FROM event_relation_excerpts x "
        "JOIN event_relations r ON r.id=x.relation_id "
        "WHERE r.event_id<>e.event_id AND (r.target_event_id=e.event_id "
        "OR EXISTS (SELECT 1 FROM native_message_refs n "
        "WHERE n.event_id=e.event_id "
        "AND n.adapter=r.target_native_adapter "
        "AND n.native_channel_id IS r.target_native_channel_id "
        "AND n.native_message_id=r.target_native_message_id))"
    ),
    "opaque_envelope": "SELECT COUNT(*) FROM canonical_events c WHERE c.event_id=e.event_id AND (c.payload<>'{}' OR c.metadata<>'{}')",
    "relation_records": "SELECT COUNT(*) FROM event_relations r WHERE r.event_id=e.event_id",
    "opaque_relation_records": "SELECT COUNT(*) FROM event_relations r WHERE r.event_id=e.event_id AND (r.key IS NOT NULL OR r.metadata<>'{}' OR r.relation_type NOT IN ("  # nosec B608 - closed core enum; quoted literals only, never user input
    + _KNOWN_RELATION_TYPES_SQL
    + "))",
    "native_references": "SELECT COUNT(*) FROM native_message_refs n WHERE n.event_id=e.event_id",
    "delivery_receipts": "SELECT COUNT(*) FROM delivery_receipts r WHERE r.event_id=e.event_id",
    "delivery_observations": "SELECT COUNT(*) FROM delivery_observations o WHERE o.event_id=e.event_id",
    "outbox_records": "SELECT COUNT(*) FROM delivery_outbox o WHERE o.event_id=e.event_id",
    "radio_plans": "SELECT COUNT(*) FROM radio_attempt_progress r JOIN delivery_outbox o ON o.outbox_id=r.outbox_id WHERE o.event_id=e.event_id",
    "attachment_associations": "SELECT COUNT(*) FROM event_attachment_associations a WHERE a.event_id=e.event_id",
    "shared_attachment_references": "SELECT COUNT(*) FROM event_attachment_associations a WHERE a.event_id=e.event_id AND EXISTS (SELECT 1 FROM event_attachment_associations other WHERE other.content_ref=a.content_ref AND other.event_id<>e.event_id)",
    "operator_decisions": "SELECT COUNT(*) FROM operator_delivery_decisions d WHERE d.event_id=e.event_id",
    "ingress_work": "SELECT COUNT(*) FROM durable_ingress_work w WHERE w.event_id=e.event_id",
    # Retained durable delivery evidence (docs/spec/content-collection-guard):
    # diagnostic rows are counted through their parent evidence row so the
    # release assessment reports them without ever selecting their text.
    "retained_diagnostics": (
        "SELECT COUNT(*) FROM delivery_evidence_diagnostics d WHERE "
        "(d.family='receipt' AND d.row_id IN (SELECT r.receipt_id "
        "FROM delivery_receipts r WHERE r.event_id=e.event_id)) "
        "OR (d.family='observation' AND d.row_id IN (SELECT o.observation_id "
        "FROM delivery_observations o WHERE o.event_id=e.event_id)) "
        "OR (d.family='outbox' AND d.row_id IN (SELECT b.outbox_id "
        "FROM delivery_outbox b WHERE b.event_id=e.event_id)) "
        "OR (d.family='ingress' AND d.row_id=e.event_id)"
    ),
    "active_ingress": "SELECT COUNT(*) FROM durable_ingress_work w WHERE w.event_id=e.event_id AND w.status NOT IN ('completed', 'failed')",
    "active_outbox": "SELECT COUNT(*) FROM delivery_outbox o WHERE o.event_id=e.event_id AND o.status NOT IN ('sent', 'cancelled', 'dead_lettered', 'abandoned')",
    "unsettled_mandatory_owners": "SELECT COUNT(*) FROM event_content_dependencies d WHERE d.event_id=e.event_id AND d.released_at IS NULL",
    # Frozen outbox work snapshots block by pure presence: a snapshot row
    # exists exactly while the guarded terminal settlement that retires it has
    # not committed. The join through delivery_outbox resolves each snapshot's
    # owner without reading any snapshot value.
    "unretired_outbox_snapshots": (
        "SELECT COUNT(*) FROM delivery_outbox_snapshots s "
        "JOIN delivery_outbox o ON o.outbox_id=s.outbox_id "
        "WHERE o.event_id=e.event_id"
    ),
    "unrevoked_optional_grants": "SELECT COUNT(*) FROM event_content_access_grants g WHERE g.event_id=e.event_id AND g.revoked_at IS NULL AND g.expired_at IS NULL",
}

# These row families block collection wholesale under an independent policy
# decision: opaque free-form values, incoming copies owned by other events,
# radio recovery plans, attachment copies, and the unreviewed operator
# decision copy family. Their lifetime requires a separate policy before any
# collector can act.
_WHOLESALE_BLOCKER_PROBES = frozenset(
    {
        "opaque_envelope",
        "opaque_relation_records",
        "incoming_relation_excerpts",
        "radio_plans",
        "attachment_associations",
        "operator_decisions",
    }
)

_INTEGRITY_BLOCKER_PROBES = ("inconsistent_admission_identity",)

_LIVE_BLOCKER_PROBES = (
    "active_ingress",
    "active_outbox",
    "unsettled_mandatory_owners",
    "unretired_outbox_snapshots",
    "unrevoked_optional_grants",
)

# Retained durable delivery/native/ingress evidence families. Their rows are
# evidence that survives collection, so they never block as rows; a family
# blocks only while a registry content-potential column still carries a value
# on the event's own rows (historical inline diagnostics, non-empty provider
# metadata). The column-aware probe is derived from the declared
# classification registry, never hand-written per column.
_EVIDENCE_FAMILY_TABLES: dict[str, str] = {
    "delivery_receipts": "delivery_receipts",
    "delivery_observations": "delivery_observations",
    "outbox_records": "delivery_outbox",
    "native_references": "native_message_refs",
    "ingress_work": "durable_ingress_work",
}

# Only reviewed JSON metadata columns use '{}' as their empty representation.
# Plain diagnostic text can literally be "{}" and must remain a blocker.
_EVIDENCE_JSON_COLUMNS = frozenset(
    {
        ("delivery_observations", "metadata"),
        ("delivery_outbox", "metadata"),
        ("native_message_refs", "metadata"),
    }
)


def _content_potential_columns(table: str) -> tuple[str, ...]:
    """Registry content-potential columns of *table* in declaration order."""
    return tuple(
        column
        for column, column_class in STORAGE_COLUMN_CLASSES[table].items()
        if column_class in CONTENT_POTENTIAL_CLASSES
    )


def _evidence_content_probe(table: str) -> str:
    """Count the event's rows in *table* that still carry content potential."""
    predicate = " OR ".join(
        (
            f"t.{column} IS NOT NULL AND t.{column}<>'{{}}'"
            if (table, column) in _EVIDENCE_JSON_COLUMNS
            else f"t.{column} IS NOT NULL"
        )
        for column in _content_potential_columns(table)
    )
    if not predicate:
        # A family the registry declares content-free never blocks on rows.
        return "SELECT 0"
    return (
        f"SELECT COUNT(*) FROM {table} t WHERE t.event_id=e.event_id "  # nosec B608 - probe SQL is built only from module constants; the event ID is a bound parameter
        f"AND ({predicate})"
    )


_BOUNDED_CANDIDATE_PROBES = frozenset({"canonical_text", "relation_excerpts"})
# Only these content-bearing rows are candidates for the first collector.
# Other surfaces are never rewritten as a side effect of collection.
COLLECTIBLE_CORE_SURFACES = ("canonical_text", "relation_excerpts")


def _classify_copy(name: str) -> str:
    if name in _BOUNDED_CANDIDATE_PROBES:
        return "bounded_candidate"
    if name == "shared_attachment_references":
        return "shared_reference"
    if name == "relation_records":
        return "identity_retained"
    if name in _LIVE_BLOCKER_PROBES or name in _INTEGRITY_BLOCKER_PROBES:
        return "authority_or_state"
    if name in _EVIDENCE_FAMILY_TABLES or name == "retained_diagnostics":
        return "retained_evidence"
    return "requires_independent_policy"


def _release_blockers(counts: dict[str, int]) -> list[str]:
    """Column-aware blocker decision shared by collector and assessment.

    Live-ownership gates and wholesale policy blockers decide on their own
    row counts. Each retained-evidence family blocks only when its
    registry-derived content probe (``<family>_content``) observes a
    content-bearing value on the event's rows inside the caller's snapshot.
    """
    blockers = [
        name
        for name in (*_LIVE_BLOCKER_PROBES, *_INTEGRITY_BLOCKER_PROBES)
        if counts[name]
    ]
    blockers.extend(sorted(name for name in _WHOLESALE_BLOCKER_PROBES if counts[name]))
    blockers.extend(
        sorted(
            family for family in _EVIDENCE_FAMILY_TABLES if counts[f"{family}_content"]
        )
    )
    return blockers


_UNMEASURED_CONTENT_SCOPES = (
    "provider_native_queues_histories_and_peers",
    "plugin_state_and_adapter_checkpoints",
    "sqlite_free_pages_and_wal",
    "backups_exports_and_snapshots",
    "application_and_provider_logs",
    "copies_embedded_in_other_events",
)

# One compound projection serves the collector and the read-only release
# assessment: the report probes plus each evidence family's registry-derived
# content probe, all in a single read snapshot.
_ALL_EVENT_COPY_PROBES: dict[str, str] = {
    **_EVENT_COPY_PROBES,
    **{
        f"{family}_content": _evidence_content_probe(table)
        for family, table in _EVIDENCE_FAMILY_TABLES.items()
    },
}

_EVENT_COPY_SQL = (
    "SELECT "  # nosec B608 - probe names and SQL are module constants; the event ID is a bound parameter
    + ", ".join(
        f"({probe}) AS {name}" for name, probe in _ALL_EVENT_COPY_PROBES.items()
    )
    + " FROM canonical_events e WHERE e.event_id=?"
)


class _CountMixin:
    """Count methods for SQLiteStorage.

    Accesses ``self._read_one`` from the base class via MRO.
    """

    if TYPE_CHECKING:

        async def _read_all(
            self, sql: str, params: tuple[Any, ...] = ()
        ) -> list[dict[str, Any]]: ...

        async def _read_one(
            self, sql: str, params: tuple[Any, ...] = ()
        ) -> dict[str, Any] | None: ...

    async def inspect_content_inventory(self) -> dict[str, Any]:
        """Count content surfaces without returning messages or granting deletion.

        A single compound SELECT keeps all aggregates in one read snapshot.
        This explicit inspection scans retained values; it is not a startup
        probe. Physical bytes, provider stores and existing backups are outside
        the measured core SQLite scope.
        """
        for surface in CONTENT_SURFACES:
            columns = {
                str(row["name"])
                for row in await self._read_all(
                    "SELECT name FROM pragma_table_info(?)", (surface.table,)
                )
            }
            if not columns or not set(surface.columns) <= columns:
                raise StorageError(
                    "Content inventory requires initialized content storage"
                )
        rows = await self._read_all(inventory_sql())
        by_surface = {str(row["surface"]): row for row in rows}
        return {
            "format": "medre-content-inventory",
            "version": 1,
            "scope": "core_sqlite",
            "retention_authorized": False,
            "surfaces": [
                {
                    "table": surface.table,
                    "columns": list(surface.columns),
                    "consumers": list(surface.consumers),
                    "rows": int(by_surface[surface.table]["rows"]),
                    "logical_bytes": int(by_surface[surface.table]["logical_bytes"]),
                }
                for surface in CONTENT_SURFACES
            ],
            "unmeasured": [
                "correlation_identifiers",
                "sqlite_free_pages_and_wal",
                "provider_profiles_and_histories",
                "snapshots_and_exports",
                "logs",
            ],
        }

    async def inspect_event_content_dependencies(self, event_id: str) -> dict[str, Any]:
        """Preview current core content consumers without reading retained bytes.

        This is an advisory snapshot, not a retention lease or permission to
        delete. Provider-owned queues, external copies and unknown consumers
        remain explicitly outside this observation.
        """
        if not event_id or len(event_id) > 512:
            raise ValueError("Content dependency preview requires a bounded event ID")
        # One statement observes the event and all counts in the same SQLite
        # snapshot, including incoming relations that may retain reply excerpts.
        rows = await self._read_all(
            "WITH selected(event_id) AS (VALUES (?)) "
            "SELECT 'event' AS kind, '' AS status, COUNT(*) AS count "
            "FROM canonical_events WHERE event_id=(SELECT event_id FROM selected) "
            "UNION ALL SELECT 'outbox', status, COUNT(*) FROM delivery_outbox "
            "WHERE event_id=(SELECT event_id FROM selected) GROUP BY status "
            "UNION ALL SELECT 'ingress', status, COUNT(*) FROM durable_ingress_work "
            "WHERE event_id=(SELECT event_id FROM selected) GROUP BY status "
            "UNION ALL SELECT 'relations', '', COUNT(*) FROM event_relations "
            "WHERE event_id=(SELECT event_id FROM selected) "
            "UNION ALL SELECT 'incoming_relations', '', COUNT(*) FROM event_relations "
            "WHERE target_event_id=(SELECT event_id FROM selected) "
            "UNION ALL SELECT 'receipts', '', COUNT(*) FROM delivery_receipts "
            "WHERE event_id=(SELECT event_id FROM selected) "
            "UNION ALL SELECT 'observations', '', COUNT(*) FROM delivery_observations "
            "WHERE event_id=(SELECT event_id FROM selected) "
            "UNION ALL SELECT 'attachments', '', COUNT(*) "
            "FROM event_attachment_associations "
            "WHERE event_id=(SELECT event_id FROM selected) "
            "UNION ALL SELECT 'radio_plans', '', COUNT(*) FROM radio_attempt_progress r "
            "JOIN delivery_outbox o ON o.outbox_id=r.outbox_id "
            "WHERE o.event_id=(SELECT event_id FROM selected) "
            "ORDER BY kind, status",
            (event_id,),
        )
        copies = {str(row["kind"]): int(row["count"]) for row in rows}
        if not copies.get("event"):
            raise StorageError("Content dependency event does not exist")
        outbox = [row for row in rows if row["kind"] == "outbox"]
        ingress = [row for row in rows if row["kind"] == "ingress"]
        return {
            "format": "medre-content-dependency-preview",
            "version": 1,
            "scope": "core_sqlite_event",
            "event_id": event_id,
            "retention_authorized": False,
            "deletion_authorized": False,
            "delivery_hold_candidate": any(
                row["status"] not in {"sent", "cancelled"} for row in outbox
            )
            or any(row["status"] != "completed" for row in ingress),
            "outbox": [
                {"status": row["status"], "count": int(row["count"])} for row in outbox
            ],
            "ingress": [
                {"status": row["status"], "count": int(row["count"])} for row in ingress
            ],
            "content_copies": {
                key: copies[key]
                for key in (
                    "relations",
                    "incoming_relations",
                    "receipts",
                    "observations",
                    "attachments",
                    "radio_plans",
                )
            },
            "unmeasured": [
                "provider_native_queues_and_histories",
                "snapshots_and_backups",
                "remote_peers",
                "logs",
                "optional_plugin_consumers",
                "canonical_payload_and_metadata",
                "native_reference_and_conversation_projections",
                "sqlite_free_pages_and_wal",
            ],
            "note": "Advisory counts only; no content read or retention decision made.",
        }

    async def assess_event_content_release(self, event_id: str) -> dict[str, Any]:
        """Classify one event's retained copies without content or write access.

        This is a conservative snapshot, **not** collection permission.
        Nonempty opaque surfaces remain blockers until explicitly classified;
        retained evidence families block only while their registry
        content-potential columns carry values; shared attachments and all
        external stores are not owned by this API. No SQL selects message
        content or provider metadata values.
        """
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("A bounded event ID is required")
        row = await self._read_one(_EVENT_COPY_SQL, (event_id,))
        if row is None:
            raise StorageError("Content release assessment requires an admitted event")
        counts = {name: int(row[name]) for name in _ALL_EVENT_COPY_PROBES}
        blockers = _release_blockers(counts)
        return {
            "format": "medre-event-content-release-assessment",
            "version": 1,
            "event_id": event_id,
            "scope": "core_sqlite_event",
            "status": "blocked" if blockers else "unmeasured",
            "core_subset_status": "blocked" if blockers else "eligible",
            "classified_core_candidate": not blockers,
            "deletion_authorized": False,
            "retention_authorized": False,
            "blockers": blockers,
            "copies": [
                {
                    "surface": name,
                    "rows": counts[name],
                    "classification": _classify_copy(name),
                }
                for name in _EVENT_COPY_PROBES
            ],
            "unmeasured": list(_UNMEASURED_CONTENT_SCOPES),
        }

    async def count_events(self) -> int:
        """Return the total number of persisted canonical events.

        Returns
        -------
        int
            Count of rows in ``canonical_events``.
        """
        row = await self._read_one("SELECT COUNT(*) AS cnt FROM canonical_events")
        if row is not None:
            return int(row["cnt"])
        return 0

    async def count_delivery_observations(self) -> int:
        """Return the total number of post-handoff delivery observations."""
        row = await self._read_one("SELECT COUNT(*) AS cnt FROM delivery_observations")
        return int(row["cnt"]) if row is not None else 0

    async def count_receipts(self) -> int:
        """Return the total number of delivery receipt rows.

        Returns
        -------
        int
            Count of rows in ``delivery_receipts``.
        """
        row = await self._read_one("SELECT COUNT(*) AS cnt FROM delivery_receipts")
        if row is not None:
            return int(row["cnt"])
        return 0

    async def count_native_refs(self) -> int:
        """Return the total number of native message ref records.

        Returns
        -------
        int
            Count of rows in ``native_message_refs``.
        """
        row = await self._read_one("SELECT COUNT(*) AS cnt FROM native_message_refs")
        return int(row["cnt"]) if row else 0

    async def count_receipts_by_source(self, source: str) -> int:
        """Return the number of delivery receipts matching *source*.

        Parameters
        ----------
        source:
            The ``source`` column value to match (e.g. ``"live"`` or
            ``"replay"``).

        Returns
        -------
        int
            Count of rows in ``delivery_receipts`` with the given source.
        """
        row = await self._read_one(
            "SELECT COUNT(*) AS cnt FROM delivery_receipts WHERE source = ?",
            (source,),
        )
        return int(row["cnt"]) if row else 0

    async def count_replay_runs(self) -> int:
        """Return the number of distinct durable replay run IDs.

        A named replay run becomes durable when either immutable receipt evidence
        exists or a dispatchable target has atomically claimed an outbox
        generation.  Counting the union keeps operator diagnostics truthful
        across the crash window between outbox admission and first receipt.
        """
        row = await self._read_one(
            "SELECT COUNT(*) AS cnt FROM ("
            "SELECT replay_run_id FROM delivery_receipts "
            "WHERE replay_run_id IS NOT NULL AND replay_run_id <> '' "
            "UNION "
            "SELECT replay_run_id FROM delivery_outbox "
            "WHERE replay_run_id IS NOT NULL AND replay_run_id <> ''"
            ")",
        )
        return int(row["cnt"]) if row else 0
