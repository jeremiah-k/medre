"""The guarded hard content-privacy ceiling enforcement transition.

When one event's immutable hard ceiling elapses, every store that can hold
that event's content or bytes derivable from it must be purged and the
purge must be verified before enforcement is recorded. This module owns
that single mutation boundary: a bounded, keyset-cursored sweep that, per
elapsed-ceiling event, inside one ``BEGIN IMMEDIATE`` transaction

1. measures the content rows and values it is about to remove,
2. deletes separated content rows (bodies, excerpts including excerpts
   other events quoted from this event, radio recovery plans, frozen
   dispatch snapshots, separated delivery diagnostics, attachment
   associations and now-unreferenced attachment blobs),
3. clears every registry-declared content-potential value on rows that
   survive as identity or evidence linkage (opaque envelope, relation
   keys/metadata, native reference metadata, inline dispatch context,
   observation metadata),
4. tombstones every live optional grant and releases every unsettled
   content dependency — the ceiling supersedes both,
5. verifies with the same probe battery that no content-potential value
   remains for the event, and
6. records the immutable content-free enforcement receipt.

The transition never deletes delivery identity or evidence rows (receipts,
observations, outbox generations, native references, handoff settlements,
operator decisions, conversation projections): those rows carry no message
content after the purge and remain the delivery history. A sweep failure
rolls back completely; a partial purge is never committed. Like the
deadline cleanup, the sweep never deletes outbox rows.

Recurring writes authorized before the ceiling (for example a delivery
observation completing an in-flight send) can append content-potential
values to surviving evidence rows after enforcement. The sweep therefore
re-purges residual values on already-enforced events: the receipt stays
immutable, only residual content values are removed. Read mediation keeps
such content unreachable in the interim.

Physical SQLite pages, WAL frames, provider copies and backups are outside
logical enforcement; the content-retention ceiling spec documents the
explicit operator erasure step.

The same module also owns the strictly read-only operator audit surface
(``audit_content_ceilings`` / ``verify_content_ceiling_receipts``): the
audit walks the residual battery for elapsed ceilings and reports what the
enforcement would remove, per census store, and flags residual writes that
landed after a prior sweep; the verification re-runs the battery over the
recorded enforcement receipts and reports pass/fail per receipt with the
receipt's counters. Both derive their probes from the same battery table
as the purge verification, so the audit cannot drift from enforcement.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from medre.core.storage.backend import StorageError
from medre.core.storage.sqlite._content_identity import _utc_microseconds

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import ParamSpec, TypeVar

    _P = ParamSpec("_P")
    _T = TypeVar("_T")

#: The sweep is bounded per invocation so it can ride a poll cycle without
#: holding the write lock unconditionally.
_CEILING_SWEEP_MAX_LIMIT = 500
_CEILING_SWEEP_CURSOR_KEY = "content_ceiling_enforcement_cursor"

#: Predicates for content-potential values attributable to one event on
#: stores that survive enforcement. Every purge target has exactly one
#: entry; the verification battery demands all of them false after the
#: purge, and the same battery detects post-enforcement residual values.
#: *event_ref* is either the ``:ev`` bind parameter (per-event battery) or
#: a correlated outer column reference (candidate discovery).
_INCOMING_EXCERPTS_PREDICATE = (
    "r.event_id<>{ev} AND (r.target_event_id={ev} OR EXISTS ("
    "SELECT 1 FROM native_message_refs n WHERE n.event_id={ev} "
    "AND n.adapter=r.target_native_adapter "
    "AND n.native_channel_id IS r.target_native_channel_id "
    "AND n.native_message_id=r.target_native_message_id))"
)


#: The residual battery as one ordered ``(census store label, predicate
#: template)`` table. This is the single source for three consumers: the
#: compound per-event battery (``_CEILING_RESIDUAL_SQL``), the residual
#: candidate discovery (``_CEILING_RESIDUAL_CANDIDATES_SQL``), and the
#: read-only operator audit's per-store residual counts (which derive a
#: row-countable ``SELECT COUNT(*)`` from each ``EXISTS`` fragment). One
#: table means the audit can never drift from what enforcement verifies.
#: Every predicate carries exactly one ``{ev}`` placeholder spelling: the
#: ``:ev`` bind parameter (per-event battery and audit counts) or a
#: correlated outer column reference (candidate discovery).
_RESIDUAL_BATTERY: tuple[tuple[str, str], ...] = (
    # Separated canonical body rows.
    (
        "event_body_content",
        "EXISTS (SELECT 1 FROM event_body_content b WHERE b.event_id={ev})",  # nosec B608 - closed module-owned fragment; event reference is a bind parameter or correlated column
    ),
    # Same-event reply-quote excerpts.
    (
        "event_relation_excerpts",
        "EXISTS (SELECT 1 FROM event_relation_excerpts x WHERE x.relation_id IN "  # nosec B608 - closed module-owned fragment; event reference is a bind parameter or correlated column
        "(SELECT r2.id FROM event_relations r2 WHERE r2.event_id={ev}))",
    ),
    # Excerpts other events quoted from this event's content.
    (
        "event_relation_excerpts:incoming",
        "EXISTS (SELECT 1 FROM event_relation_excerpts x "  # nosec B608 - closed module-owned fragment; event reference is a bind parameter or correlated column
        "JOIN event_relations r ON r.id=x.relation_id WHERE "
        + _INCOMING_EXCERPTS_PREDICATE
        + ")",
    ),
    # Opaque envelope values on the canonical row.
    (
        "canonical_events",
        "EXISTS (SELECT 1 FROM canonical_events c WHERE c.event_id={ev} "  # nosec B608 - closed module-owned fragment; event reference is a bind parameter or correlated column
        "AND (c.payload<>'{}' OR c.metadata<>'{}'))",
    ),
    # Content-potential values on the event's own relation rows.
    (
        "event_relations",
        "EXISTS (SELECT 1 FROM event_relations r WHERE r.event_id={ev} "  # nosec B608 - closed module-owned fragment; event reference is a bind parameter or correlated column
        "AND (r.key IS NOT NULL OR r.metadata<>'{}'))",
    ),
    # Provider metadata on the event's native references.
    (
        "native_message_refs",
        "EXISTS (SELECT 1 FROM native_message_refs n WHERE n.event_id={ev} "  # nosec B608 - closed module-owned fragment; event reference is a bind parameter or correlated column
        "AND n.metadata<>'{}')",
    ),
    # Inline dispatch context on the event's outbox generations.
    (
        "delivery_outbox",
        "EXISTS (SELECT 1 FROM delivery_outbox o WHERE o.event_id={ev} "  # nosec B608 - closed module-owned fragment; event reference is a bind parameter or correlated column
        "AND o.metadata<>'{}')",
    ),
    # Provider metadata on the event's delivery observations.
    (
        "delivery_observations",
        "EXISTS (SELECT 1 FROM delivery_observations o WHERE o.event_id={ev} "  # nosec B608 - closed module-owned fragment; event reference is a bind parameter or correlated column
        "AND o.metadata<>'{}')",
    ),
    # Separated delivery diagnostic text on the event's evidence rows.
    (
        "delivery_evidence_diagnostics",
        "EXISTS (SELECT 1 FROM delivery_evidence_diagnostics d WHERE "  # nosec B608 - closed module-owned fragment; event reference is a bind parameter or correlated column
        "(d.family='receipt' AND d.row_id IN (SELECT r3.receipt_id "
        "FROM delivery_receipts r3 WHERE r3.event_id={ev})) "
        "OR (d.family='observation' AND d.row_id IN (SELECT o2.observation_id "
        "FROM delivery_observations o2 WHERE o2.event_id={ev})) "
        "OR (d.family='outbox' AND d.row_id IN (SELECT b2.outbox_id "
        "FROM delivery_outbox b2 WHERE b2.event_id={ev})) "
        "OR (d.family='ingress' AND d.row_id={ev}))",
    ),
    # Radio recovery plans riding the event's outbox generations.
    (
        "radio_attempt_progress",
        "EXISTS (SELECT 1 FROM radio_attempt_progress p "  # nosec B608 - closed module-owned fragment; event reference is a bind parameter or correlated column
        "JOIN delivery_outbox o ON o.outbox_id=p.outbox_id WHERE o.event_id={ev})",
    ),
    # Frozen dispatch snapshots riding the event's outbox generations.
    (
        "delivery_outbox_snapshots",
        "EXISTS (SELECT 1 FROM delivery_outbox_snapshots s "  # nosec B608 - closed module-owned fragment; event reference is a bind parameter or correlated column
        "JOIN delivery_outbox o ON o.outbox_id=s.outbox_id WHERE o.event_id={ev})",
    ),
    # Attachment associations: the only authority for loading retained bytes.
    (
        "event_attachment_associations",
        "EXISTS (SELECT 1 FROM event_attachment_associations a WHERE a.event_id={ev})",  # nosec B608 - closed module-owned fragment; event reference is a bind parameter or correlated column
    ),
)

#: Count-query transformation over the battery table: every fragment has the
#: exact ``EXISTS (SELECT 1 FROM <inner>)`` shape, so stripping the wrapper
#: yields the row-countable inner select for the operator audit. A fragment
#: that does not match is a programming error in this closed table.
_BATTERY_FRAGMENT_PREFIX = "EXISTS (SELECT 1 FROM "


def _residual_fragments(event_ref: str) -> tuple[str, ...]:
    """Build the residual battery for one event-reference spelling.

    Substitution is a plain token replace, never ``str.format``: the
    predicates contain literal ``'{}'`` JSON-value comparisons that a
    format pass would misread as positional fields.
    """
    return tuple(
        predicate.replace("{ev}", event_ref) for _, predicate in _RESIDUAL_BATTERY
    )


def _residual_store_counts(db: sqlite3.Connection, event_id: str) -> dict[str, int]:
    """Count residual content-potential rows per census store for one event.

    Runs one ``SELECT COUNT(*)`` per battery store from the same predicate
    table the verification battery uses, bound to the event ID as a
    parameter exactly like the per-event battery. Only stores holding at
    least one residual content-potential row are reported; the census store
    label is the identity, and no content value is ever read.
    """
    counts: dict[str, int] = {}
    for label, template in _RESIDUAL_BATTERY:
        if not (
            template.startswith(_BATTERY_FRAGMENT_PREFIX) and template.endswith(")")
        ):
            raise StorageError(
                "Residual battery fragment is not row-countable: " + label
            )
        inner = template[len(_BATTERY_FRAGMENT_PREFIX) : -1].replace("{ev}", ":ev")
        sql = f"SELECT COUNT(*) FROM {inner}"  # nosec B608 - closed module-owned fragment; event reference is a bind parameter
        rows = int(db.execute(sql, {"ev": event_id}).fetchone()[0])
        if rows:
            counts[label] = rows
    return counts


#: One compound SELECT summing the battery for a bound event; zero means
#: no content-potential value remains attributable to the event anywhere
#: it was enumerated.
_CEILING_RESIDUAL_SQL = "SELECT " + " + ".join(
    f"CASE WHEN {predicate} THEN 1 ELSE 0 END"
    for predicate in _residual_fragments(":ev")
)

#: Candidate discovery: elapsed ceilings without an enforcement receipt, in
#: a stable keyset ordering backed by the additive partial-order index.
#: Already-enforced events are re-discovered separately by the residual
#: battery, not here: the main pass is bounded to unenforced events.
_CEILING_CANDIDATES_SQL = """
    SELECT p.event_id, p.hard_ceiling
      FROM event_content_access_policies AS p
     WHERE p.hard_ceiling <= ?
       AND NOT EXISTS (
           SELECT 1 FROM event_content_ceiling_log l WHERE l.event_id=p.event_id
       )
       AND (p.hard_ceiling, p.event_id) > (?, ?)
     ORDER BY p.hard_ceiling ASC, p.event_id ASC
     LIMIT ?
"""  # nosec B608 - static SQL fragments and parameter placeholders only

#: Residual re-enforcement candidates: enforced events that regained
#: content-potential values through writes authorized before the ceiling.
#: The residual battery is OR-joined, correlated to the policy row, so
#: only events that still carry content-potential values are returned,
#: bounded by the caller's limit.
_CEILING_RESIDUAL_CANDIDATES_SQL = (
    "SELECT p.event_id, p.hard_ceiling\n"  # nosec B608 - static SQL fragments and parameter placeholders only
    "  FROM event_content_access_policies AS p\n"
    " WHERE p.hard_ceiling <= ?\n"
    "   AND EXISTS (\n"
    "       SELECT 1 FROM event_content_ceiling_log l WHERE l.event_id=p.event_id\n"
    "   )\n"
    "   AND (\n" + " OR ".join(_residual_fragments("p.event_id")) + "\n"
    "   )\n"
    " ORDER BY p.hard_ceiling ASC, p.event_id ASC\n"
    " LIMIT ?\n"
)


@dataclass(frozen=True, slots=True)
class CeilingEnforcement:
    """Metadata-only summary of one event's ceiling enforcement."""

    event_id: str
    ceiling: str
    enforced_at: str
    rows_removed: int
    logical_bytes_removed: int
    blobs_removed: int
    blob_bytes_removed: int
    #: ``True`` when the event already carried an enforcement receipt and
    #: this pass removed only residual content values (no new receipt).
    residual_pass: bool


def _purge_diagnostics(db: sqlite3.Connection, event_id: str) -> None:
    """Delete the event's separated delivery diagnostic rows."""
    db.execute(
        "DELETE FROM delivery_evidence_diagnostics WHERE "
        "(family='receipt' AND row_id IN (SELECT r.receipt_id "
        "FROM delivery_receipts r WHERE r.event_id=?)) "
        "OR (family='observation' AND row_id IN (SELECT o.observation_id "
        "FROM delivery_observations o WHERE o.event_id=?)) "
        "OR (family='outbox' AND row_id IN (SELECT b.outbox_id "
        "FROM delivery_outbox b WHERE b.event_id=?)) "
        "OR (family='ingress' AND row_id=?)",
        (event_id, event_id, event_id, event_id),
    )


def _purge_own_and_incoming_excerpts(db: sqlite3.Connection, event_id: str) -> None:
    """Delete same-event excerpts and excerpts other events quoted from it."""
    db.execute(
        "DELETE FROM event_relation_excerpts WHERE relation_id IN "  # nosec B608 - closed module-owned fragment; event reference is a bind parameter or correlated column
        "(SELECT r.id FROM event_relations r WHERE r.event_id=:ev) "
        "OR relation_id IN (SELECT r.id FROM event_relations r WHERE "
        + _INCOMING_EXCERPTS_PREDICATE.format(ev=":ev")
        + ")",
        {"ev": event_id},
    )


def _measure_and_purge(
    db: sqlite3.Connection, event_id: str
) -> tuple[int, int, int, int]:
    """Measure, then purge, every enumerated content store for one event.

    Returns ``(rows_removed, logical_bytes_removed, blobs_removed,
    blob_bytes_removed)``. Measurement and mutation share one ``BEGIN
    IMMEDIATE`` snapshot, so the counters describe exactly what this
    transition removed.
    """
    rows_removed = 0
    logical_bytes = 0

    # Separated canonical body rows.
    body = db.execute(
        "SELECT COUNT(*), COALESCE(SUM(length(CAST(body AS BLOB))), 0) "
        "FROM event_body_content WHERE event_id=?",
        (event_id,),
    ).fetchone()
    rows_removed += int(body[0])
    logical_bytes += int(body[1])
    db.execute("DELETE FROM event_body_content WHERE event_id=?", (event_id,))

    # Same-event excerpts and excerpts quoted from this event by others.
    own_excerpts = db.execute(
        "SELECT COUNT(*), COALESCE(SUM(length(CAST(x.excerpt AS BLOB))), 0) "
        "FROM event_relation_excerpts x JOIN event_relations r ON r.id=x.relation_id "
        "WHERE r.event_id=?",
        (event_id,),
    ).fetchone()
    incoming_excerpts = db.execute(
        "SELECT COUNT(*), COALESCE(SUM(length(CAST(x.excerpt AS BLOB))), 0) "  # nosec B608 - closed module-owned fragment; event reference is a bind parameter or correlated column
        "FROM event_relation_excerpts x JOIN event_relations r ON r.id=x.relation_id "
        "WHERE " + _INCOMING_EXCERPTS_PREDICATE.format(ev=":ev"),
        {"ev": event_id},
    ).fetchone()
    rows_removed += int(own_excerpts[0]) + int(incoming_excerpts[0])
    logical_bytes += int(own_excerpts[1]) + int(incoming_excerpts[1])
    _purge_own_and_incoming_excerpts(db, event_id)

    # Opaque envelope values on the canonical row (identity row survives).
    envelope = db.execute(
        "SELECT length(CAST(payload AS BLOB)), length(CAST(metadata AS BLOB)) "
        "FROM canonical_events WHERE event_id=? "
        "AND (payload<>'{}' OR metadata<>'{}')",
        (event_id,),
    ).fetchone()
    if envelope is not None:
        logical_bytes += int(envelope[0] or 0) + int(envelope[1] or 0)
        db.execute(
            "UPDATE canonical_events SET payload='{}', metadata='{}' "
            "WHERE event_id=?",
            (event_id,),
        )

    # Content-potential values on the event's own relation rows.
    relation_values = db.execute(
        "SELECT COALESCE(SUM(length(CAST(key AS BLOB))), 0), "
        "COALESCE(SUM(length(CAST(metadata AS BLOB))), 0) FROM event_relations "
        "WHERE event_id=? AND (key IS NOT NULL OR metadata<>'{}')",
        (event_id,),
    ).fetchone()
    logical_bytes += int(relation_values[0]) + int(relation_values[1])
    db.execute(
        "UPDATE event_relations SET key=NULL, metadata='{}' WHERE event_id=?",
        (event_id,),
    )

    # Provider metadata on the event's native references.
    native_metadata = db.execute(
        "SELECT COALESCE(SUM(length(CAST(metadata AS BLOB))), 0) "
        "FROM native_message_refs WHERE event_id=? AND metadata<>'{}'",
        (event_id,),
    ).fetchone()
    logical_bytes += int(native_metadata[0])
    db.execute(
        "UPDATE native_message_refs SET metadata='{}' WHERE event_id=?",
        (event_id,),
    )

    # Inline dispatch context on the event's outbox generations.
    outbox_metadata = db.execute(
        "SELECT COALESCE(SUM(length(CAST(metadata AS BLOB))), 0) "
        "FROM delivery_outbox WHERE event_id=? AND metadata<>'{}'",
        (event_id,),
    ).fetchone()
    logical_bytes += int(outbox_metadata[0])
    db.execute("UPDATE delivery_outbox SET metadata='{}' WHERE event_id=?", (event_id,))

    # Provider metadata on the event's delivery observations.
    observation_metadata = db.execute(
        "SELECT COALESCE(SUM(length(CAST(metadata AS BLOB))), 0) "
        "FROM delivery_observations WHERE event_id=? AND metadata<>'{}'",
        (event_id,),
    ).fetchone()
    logical_bytes += int(observation_metadata[0])
    db.execute(
        "UPDATE delivery_observations SET metadata='{}' WHERE event_id=?",
        (event_id,),
    )

    # Separated delivery diagnostic text on the event's evidence rows.
    diagnostics = db.execute(
        "SELECT COUNT(*), COALESCE(SUM(length(CAST(diagnostic AS BLOB))), 0) "
        "FROM delivery_evidence_diagnostics WHERE "
        "(family='receipt' AND row_id IN (SELECT r.receipt_id "
        "FROM delivery_receipts r WHERE r.event_id=?)) "
        "OR (family='observation' AND row_id IN (SELECT o.observation_id "
        "FROM delivery_observations o WHERE o.event_id=?)) "
        "OR (family='outbox' AND row_id IN (SELECT b.outbox_id "
        "FROM delivery_outbox b WHERE b.event_id=?)) "
        "OR (family='ingress' AND row_id=?)",
        (event_id, event_id, event_id, event_id),
    ).fetchone()
    rows_removed += int(diagnostics[0])
    logical_bytes += int(diagnostics[1])
    _purge_diagnostics(db, event_id)

    # Radio recovery plans riding the event's outbox generations.
    radio = db.execute(
        "SELECT COUNT(*), COALESCE(SUM("
        "length(CAST(p.plan_json AS BLOB)) + "
        "length(CAST(p.provenance_json AS BLOB)) + "
        "length(CAST(p.parts_json AS BLOB))), 0) "
        "FROM radio_attempt_progress p JOIN delivery_outbox o "
        "ON o.outbox_id=p.outbox_id WHERE o.event_id=?",
        (event_id,),
    ).fetchone()
    rows_removed += int(radio[0])
    logical_bytes += int(radio[1])
    db.execute(
        "DELETE FROM radio_attempt_progress WHERE outbox_id IN "
        "(SELECT outbox_id FROM delivery_outbox WHERE event_id=?)",
        (event_id,),
    )

    # Frozen dispatch snapshots riding the event's outbox generations.
    snapshots = db.execute(
        "SELECT COUNT(*), COALESCE(SUM(length(CAST(s.context_json AS BLOB))), 0) "
        "FROM delivery_outbox_snapshots s JOIN delivery_outbox o "
        "ON o.outbox_id=s.outbox_id WHERE o.event_id=?",
        (event_id,),
    ).fetchone()
    rows_removed += int(snapshots[0])
    logical_bytes += int(snapshots[1])
    db.execute(
        "DELETE FROM delivery_outbox_snapshots WHERE outbox_id IN "
        "(SELECT outbox_id FROM delivery_outbox WHERE event_id=?)",
        (event_id,),
    )

    # Attachment associations and now-unreferenced attachment blobs. The
    # blob measurement and ref capture run before the association delete so
    # the last reference is still visible; deletion removes this event's
    # associations first, then exactly the captured blobs that no other
    # event still refers to. Pre-existing orphans are never touched here.
    associations = db.execute(
        "SELECT COUNT(*) FROM event_attachment_associations WHERE event_id=?",
        (event_id,),
    ).fetchone()
    refs = [
        str(row[0])
        for row in db.execute(
            "SELECT content_ref FROM event_attachment_associations WHERE event_id=?",
            (event_id,),
        ).fetchall()
    ]
    blobs_removed, blob_bytes = 0, 0
    if refs:
        placeholders = ",".join("?" for _ in refs)
        blobs = db.execute(
            "SELECT COUNT(*), COALESCE(SUM(b.size_bytes), 0) FROM attachment_blobs b "
            f"WHERE b.content_ref IN ({placeholders}) AND NOT EXISTS ("  # nosec B608 - placeholders only; refs are bound values
            "SELECT 1 FROM event_attachment_associations a2 "
            "WHERE a2.content_ref=b.content_ref AND a2.event_id<>?)",
            (*refs, event_id),
        ).fetchone()
        blobs_removed, blob_bytes = int(blobs[0]), int(blobs[1])
    rows_removed += int(associations[0])
    db.execute(
        "DELETE FROM event_attachment_associations WHERE event_id=?", (event_id,)
    )
    for ref in refs:
        db.execute(
            "DELETE FROM attachment_blobs WHERE content_ref=? AND NOT EXISTS ("
            "SELECT 1 FROM event_attachment_associations a "
            "WHERE a.content_ref=attachment_blobs.content_ref)",
            (ref,),
        )

    return rows_removed, logical_bytes, blobs_removed, blob_bytes


def _release_authority(db: sqlite3.Connection, event_id: str, observed: str) -> None:
    """Tombstone live optional grants and release unsettled dependencies.

    The elapsed ceiling supersedes both: a grant cannot outlive the ceiling
    and no owner may keep content authority past it. Revocation tombstones
    already present are preserved.
    """
    db.execute(
        "UPDATE event_content_access_grants SET expired_at=? "
        "WHERE event_id=? AND expired_at IS NULL AND revoked_at IS NULL",
        (observed, event_id),
    )
    db.execute(
        "UPDATE event_content_dependencies SET released_at=? "
        "WHERE event_id=? AND released_at IS NULL",
        (observed, event_id),
    )


def sync_enforce_content_ceilings(
    db: sqlite3.Connection,
    lock: threading.Lock,
    observed: str | None,
    limit: int,
) -> list[CeilingEnforcement]:
    """Enforce every elapsed hard ceiling the store can prove and purge.

    One ``BEGIN IMMEDIATE`` transaction serializes the sweep with every
    other writer. Each elapsed-ceiling event without a receipt is purged
    across the enumerated stores, verified content-free with the residual
    battery, and recorded; each enforced event still carrying residual
    content-potential values is re-purged without rewriting its receipt.
    Any verification failure raises and rolls the whole sweep back.
    """
    with lock:
        try:
            db.execute("BEGIN IMMEDIATE")
            if observed is None:
                observed = _utc_microseconds(
                    datetime.now(timezone.utc).isoformat(), "Ceiling clock"
                )
            enforced: list[CeilingEnforcement] = []

            # Residual re-enforcement first: enforced events that regained
            # content-potential values through writes authorized before
            # their ceiling. The candidate query itself carries the residual
            # battery and the caller's bound, so only events that still
            # hold content-potential values are re-purged.
            residual_rows = db.execute(
                _CEILING_RESIDUAL_CANDIDATES_SQL, (observed, max(25, limit))
            ).fetchall()
            for row in residual_rows:
                event_id = str(row["event_id"])
                ceiling = str(row["hard_ceiling"])
                _measure_and_purge(db, event_id)
                _release_authority(db, event_id, observed)
                residual = db.execute(_CEILING_RESIDUAL_SQL, {"ev": event_id})
                if int(residual.fetchone()[0]) != 0:
                    raise StorageError(
                        "Ceiling enforcement left residual content for an "
                        "enforced event"
                    )
                enforced.append(
                    CeilingEnforcement(
                        event_id=event_id,
                        ceiling=ceiling,
                        enforced_at=observed,
                        rows_removed=0,
                        logical_bytes_removed=0,
                        blobs_removed=0,
                        blob_bytes_removed=0,
                        residual_pass=True,
                    )
                )

            # Main pass: unenforced elapsed ceilings in keyset order, with
            # durable progress persisted in the same transaction.
            progress = db.execute(
                "SELECT value FROM _medre_schema_meta WHERE key=?",
                (_CEILING_SWEEP_CURSOR_KEY,),
            ).fetchone()
            cursor = json.loads(progress[0]) if progress is not None else ["", ""]
            if (
                not isinstance(cursor, list)
                or len(cursor) != 2
                or not all(isinstance(value, str) for value in cursor)
            ):
                raise StorageError("Invalid ceiling enforcement cursor")
            last_ceiling, last_event_id = cursor
            budget = max(25, limit)
            examined = 0
            while len(enforced) < limit and examined < budget:
                page_size = min(100, budget - examined)
                page = db.execute(
                    _CEILING_CANDIDATES_SQL,
                    (observed, last_ceiling, last_event_id, page_size),
                ).fetchall()
                if not page:
                    # Wrap: the next sweep rescans from the start so events
                    # admitted (or backdated) behind the cursor are still
                    # enforced. Receipt presence makes re-scan cheap.
                    last_ceiling = last_event_id = ""
                    break
                for row in page:
                    examined += 1
                    last_ceiling = str(row["hard_ceiling"])
                    last_event_id = str(row["event_id"])
                    event_id = str(row["event_id"])
                    ceiling = str(row["hard_ceiling"])
                    (
                        rows_removed,
                        logical_bytes,
                        blobs_removed,
                        blob_bytes,
                    ) = _measure_and_purge(db, event_id)
                    _release_authority(db, event_id, observed)
                    residual = db.execute(_CEILING_RESIDUAL_SQL, {"ev": event_id})
                    if int(residual.fetchone()[0]) != 0:
                        raise StorageError(
                            "Ceiling enforcement left residual content for "
                            f"event {event_id}"
                        )
                    db.execute(
                        "INSERT INTO event_content_ceiling_log "
                        "(event_id, enforced_at, ceiling, rows_removed, "
                        "logical_bytes_removed, blobs_removed, "
                        "blob_bytes_removed) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            event_id,
                            observed,
                            ceiling,
                            rows_removed,
                            logical_bytes,
                            blobs_removed,
                            blob_bytes,
                        ),
                    )
                    enforced.append(
                        CeilingEnforcement(
                            event_id=event_id,
                            ceiling=ceiling,
                            enforced_at=observed,
                            rows_removed=rows_removed,
                            logical_bytes_removed=logical_bytes,
                            blobs_removed=blobs_removed,
                            blob_bytes_removed=blob_bytes,
                            residual_pass=False,
                        )
                    )
                    if len(enforced) >= limit:
                        break
                if len(page) < page_size and last_event_id == str(page[-1]["event_id"]):
                    last_ceiling = last_event_id = ""
                    break
            db.execute(
                "INSERT INTO _medre_schema_meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (
                    _CEILING_SWEEP_CURSOR_KEY,
                    json.dumps([last_ceiling, last_event_id]),
                ),
            )
            db.commit()
            return enforced
        except BaseException:
            db.rollback()
            raise


#: Single-event audit projection: the event's immutable ceiling, whether it
#: has elapsed against the caller's observation, and whether an enforcement
#: receipt exists. The elapsed comparison is the same stored-UTC text
#: comparison the candidate discovery uses, evaluated inside the read
#: snapshot.
_CEILING_EVENT_STATE_SQL = """
    SELECT p.event_id, p.hard_ceiling,
           (p.hard_ceiling <= :observed) AS elapsed,
           EXISTS (
               SELECT 1 FROM event_content_ceiling_log l
                WHERE l.event_id=p.event_id
           ) AS enforced
      FROM event_content_access_policies AS p
     WHERE p.event_id=:ev
"""  # nosec B608 - static SQL fragments and parameter placeholders only


def sync_audit_content_ceilings(
    db: sqlite3.Connection,
    lock: threading.Lock,
    observed: str | None,
    limit: int,
    event_id: str | None,
) -> dict[str, Any]:
    """Read-only audit of the ceiling residual battery; it purges nothing.

    One deferred read transaction observes, for content whose ceiling has
    elapsed, exactly what the guarded enforcement would remove: the
    per-store residual counts from the same battery table enforcement
    verifies with. Elapsed ceilings without a receipt are reported as
    ``elapsed_pending`` (the sweep's next candidates); enforced events that
    regained content-potential values through writes authorized before
    their ceiling are reported as ``residual_enforced`` (the spec's
    documented late-write case, which the next sweep re-purges). Event
    identifiers and census store labels are content-free identity; no
    message text, excerpt, blob bytes, or other content value is read.
    """
    with lock:
        try:
            db.execute("BEGIN")
            if observed is None:
                observed = _utc_microseconds(
                    datetime.now(timezone.utc).isoformat(), "Ceiling clock"
                )
            if event_id is not None:
                return _audit_single_event(db, event_id, observed)

            fetch = limit + 1
            pending_rows = db.execute(
                _CEILING_CANDIDATES_SQL, (observed, "", "", fetch)
            ).fetchall()
            pending = [
                _audit_finding(db, str(row["event_id"]), str(row["hard_ceiling"]))
                for row in pending_rows[:limit]
            ]
            residual_rows = db.execute(
                _CEILING_RESIDUAL_CANDIDATES_SQL, (observed, fetch)
            ).fetchall()
            residual = [
                _audit_finding(db, str(row["event_id"]), str(row["hard_ceiling"]))
                for row in residual_rows[:limit]
            ]
            receipts = int(
                db.execute("SELECT COUNT(*) FROM event_content_ceiling_log").fetchone()[
                    0
                ]
            )
            return {
                "observed": observed,
                "elapsed_pending": pending,
                "residual_enforced": residual,
                "receipts": receipts,
                "elapsed_pending_has_more": len(pending_rows) > limit,
                "residual_enforced_has_more": len(residual_rows) > limit,
            }
        finally:
            db.rollback()


def _audit_finding(
    db: sqlite3.Connection, event_id: str, ceiling: str
) -> dict[str, Any]:
    """Build one audit finding: per-store residual counts for one event."""
    stores = _residual_store_counts(db, event_id)
    return {
        "event_id": event_id,
        "ceiling": ceiling,
        "residual_stores": stores,
        "residual_stores_total": len(stores),
    }


def _audit_single_event(
    db: sqlite3.Connection, event_id: str, observed: str
) -> dict[str, Any]:
    """Audit exactly one event; every outcome is reported, fail-closed."""
    row = db.execute(_CEILING_EVENT_STATE_SQL, {"ev": event_id, "observed": observed})
    policy = row.fetchone()
    if policy is None:
        return {"event_id": event_id, "state": "unknown", "observed": observed}
    ceiling = str(policy["hard_ceiling"])
    receipt = db.execute(
        "SELECT enforced_at, ceiling, rows_removed, logical_bytes_removed, "
        "blobs_removed, blob_bytes_removed FROM event_content_ceiling_log "
        "WHERE event_id=:ev",
        {"ev": event_id},
    ).fetchone()
    elapsed = bool(policy["elapsed"])
    enforced = bool(policy["enforced"])
    finding = _audit_finding(db, event_id, ceiling)
    if enforced:
        state = "residual_enforced" if finding["residual_stores"] else "enforced_clean"
    elif elapsed:
        state = "elapsed_pending"
    else:
        state = "not_elapsed"
    return {
        "event_id": event_id,
        "state": state,
        "ceiling": ceiling,
        "observed": observed,
        "residual_stores": finding["residual_stores"],
        "residual_stores_total": finding["residual_stores_total"],
        "receipt": dict(receipt) if receipt is not None else None,
    }


#: Bounded receipt-verification window: most recent receipts first, so the
#: operator's one-shot post-sweep proof reads the freshest enforcement
#: facts. The battery re-runs against the current database, not the
#: enforcement-time snapshot.
_CEILING_RECEIPTS_SQL = """
    SELECT event_id, enforced_at, ceiling, rows_removed, logical_bytes_removed,
           blobs_removed, blob_bytes_removed
      FROM event_content_ceiling_log
"""  # nosec B608 - static SQL fragments and parameter placeholders only


def sync_verify_content_ceiling_receipts(
    db: sqlite3.Connection,
    lock: threading.Lock,
    limit: int,
    event_id: str | None,
) -> dict[str, Any]:
    """Read-only one-shot proof: receipts re-checked against the live store.

    Re-runs the residual battery — the same battery that gates the receipt
    commit — against the current database for every enforcement receipt in
    a bounded window, and reports pass/fail per receipt with the receipt's
    recorded content-free counters. A receipt fails when its event holds
    any residual content-potential value now (the documented late-write
    case until the next sweep re-purges it); an explicitly requested event
    without a receipt fails closed: absence of proof is not proof.
    """
    with lock:
        try:
            db.execute("BEGIN")
            fetch = limit + 1
            if event_id is not None:
                rows = db.execute(
                    _CEILING_RECEIPTS_SQL + " WHERE event_id=:ev",
                    {"ev": event_id},
                ).fetchall()
                event_state = "receipt_verified" if rows else "no_receipt"
            else:
                rows = db.execute(
                    _CEILING_RECEIPTS_SQL
                    + " ORDER BY enforced_at DESC, event_id ASC LIMIT ?",
                    (fetch,),
                ).fetchall()
                event_state = None
            receipts: list[dict[str, Any]] = []
            passed = 0
            for row in rows[:limit]:
                record = dict(row)
                stores = _residual_store_counts(db, str(row["event_id"]))
                record["residual_stores"] = stores
                record["residual_stores_total"] = len(stores)
                record["status"] = "failed" if stores else "passed"
                passed += record["status"] == "passed"
                receipts.append(record)
            failed = len(receipts) - passed
            verdict = "passed"
            if failed or event_state == "no_receipt":
                verdict = "failed"
            result: dict[str, Any] = {
                "verdict": verdict,
                "receipts_examined": len(receipts),
                "receipts_passed": passed,
                "receipts_failed": failed,
                "receipts_has_more": len(rows) > limit,
                "receipts": receipts,
            }
            if event_state is not None:
                result["event_state"] = event_state
            return result
        finally:
            db.rollback()


class _CeilingEnforcementMixin:
    """The guarded hard content-privacy ceiling enforcement for SQLiteStorage."""

    if TYPE_CHECKING:
        _lock: threading.Lock

        def _require_db(self) -> sqlite3.Connection: ...

        async def _run_in_thread(
            self, func: Callable[_P, _T], *args: _P.args, **kwargs: _P.kwargs
        ) -> _T: ...

    async def enforce_content_ceilings(
        self,
        *,
        now: datetime | None = None,
        limit: int = 50,
    ) -> list[CeilingEnforcement]:
        """Purge and verify every elapsed hard content ceiling, per store.

        Authority: **purge** (the operator's own declared hard ceiling).
        Candidates are events whose immutable ``hard_ceiling`` has elapsed
        and carry no enforcement receipt. For each candidate, inside one
        ``BEGIN IMMEDIATE`` transaction, every enumerated content store is
        purged for that event (separated bodies, same-event and incoming
        excerpts, opaque envelope values, relation keys/metadata, native
        reference metadata, inline dispatch context, observation metadata,
        separated delivery diagnostics, radio recovery plans, frozen
        dispatch snapshots, attachment associations and now-unreferenced
        blobs), every live optional grant is tombstoned, every unsettled
        content dependency is released, and the residual battery must
        observe zero content-potential values before the immutable
        content-free receipt commits. Already-enforced events regain-ing
        residual values through writes authorized before the ceiling are
        re-purged without a second receipt. Delivery identity and evidence
        rows (receipts, observations, outbox generations, native
        references, settlements, operator decisions) are never deleted.
        A verification failure raises and rolls the entire sweep back.

        Production omits *now* so storage samples UTC after acquiring its
        SQLite transaction; an explicit *now* is available to trusted
        controlled-clock qualification and must be timezone-aware. Returns
        metadata-only summaries; each call re-enforces at most
        ``max(25, limit)`` residual events and enforces at most *limit*
        new events, examining at most ``max(25, limit)`` candidates. A
        durable keyset cursor in internal metadata advances in the same
        transaction and wraps at queue end.
        """
        ceiling_observation = (
            None if now is None else _utc_microseconds(now.isoformat(), "Ceiling clock")
        )
        if type(limit) is not int or not 1 <= limit <= _CEILING_SWEEP_MAX_LIMIT:
            raise ValueError(
                "The ceiling enforcement limit must be an integer between 1 and "
                f"{_CEILING_SWEEP_MAX_LIMIT}"
            )
        try:
            return await self._run_in_thread(
                sync_enforce_content_ceilings,
                self._require_db(),
                self._lock,
                ceiling_observation,
                limit,
            )
        except sqlite3.Error as exc:
            raise StorageError(f"Ceiling enforcement sweep failed: {exc}") from exc

    async def audit_content_ceilings(
        self,
        *,
        now: datetime | None = None,
        limit: int = 25,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """Read-only audit: what the ceiling enforcement would remove, per store.

        Authority: none — strictly a read snapshot; it purges nothing,
        tombstones nothing, records nothing. Walks the residual battery
        for content whose immutable ceiling has elapsed and reports
        per-census-store residual row counts from the same battery table
        enforcement verifies with. Elapsed ceilings without a receipt are
        the sweep's next candidates (``elapsed_pending``); enforced events
        that regained residual content-potential values through writes
        authorized before their ceiling are the documented late-write case
        (``residual_enforced``) and are flagged until the next sweep
        re-purges them. Event identifiers and census store labels are
        content-free identity; no content value is read. With *event_id*,
        reports exactly one event's state (``unknown``, ``not_elapsed``,
        ``elapsed_pending``, ``residual_enforced``, ``enforced_clean``)
        with its receipt counters when present. Production omits *now* so
        storage samples UTC; an explicit timezone-aware *now* is available
        to trusted controlled-clock qualification. The report window is
        bounded by *limit* (1–``max(25, limit)`` semantics of the sweep)
        with explicit ``*_has_more`` truncation flags.
        """
        ceiling_observation = (
            None if now is None else _utc_microseconds(now.isoformat(), "Ceiling clock")
        )
        if type(limit) is not int or not 1 <= limit <= _CEILING_SWEEP_MAX_LIMIT:
            raise ValueError(
                "The ceiling audit limit must be an integer between 1 and "
                f"{_CEILING_SWEEP_MAX_LIMIT}"
            )
        if event_id is not None and (not isinstance(event_id, str) or not event_id):
            raise ValueError("The ceiling audit event filter must be a nonempty string")
        try:
            return await self._run_in_thread(
                sync_audit_content_ceilings,
                self._require_db(),
                self._lock,
                ceiling_observation,
                limit,
                event_id,
            )
        except sqlite3.Error as exc:
            raise StorageError(f"Ceiling audit failed: {exc}") from exc

    async def verify_content_ceiling_receipts(
        self,
        *,
        limit: int = 25,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """Read-only one-shot proof: re-run the battery over the receipts.

        Reads the immutable enforcement receipts and re-runs the residual
        battery — the same battery that gated each receipt's commit —
        against the current database, reporting pass/fail per receipt with
        the receipt's recorded content-free counters (rows, logical bytes,
        blobs, blob bytes removed). A receipt fails when its event holds
        residual content-potential values now; an explicitly requested
        event without a receipt fails closed. Nothing is purged or
        rewritten here: remediation belongs to the guarded sweep.
        """
        if type(limit) is not int or not 1 <= limit <= _CEILING_SWEEP_MAX_LIMIT:
            raise ValueError(
                "The ceiling receipt verification limit must be an integer "
                f"between 1 and {_CEILING_SWEEP_MAX_LIMIT}"
            )
        if event_id is not None and (not isinstance(event_id, str) or not event_id):
            raise ValueError(
                "The ceiling verification event filter must be a nonempty string"
            )
        try:
            return await self._run_in_thread(
                sync_verify_content_ceiling_receipts,
                self._require_db(),
                self._lock,
                limit,
                event_id,
            )
        except sqlite3.Error as exc:
            raise StorageError(f"Ceiling receipt verification failed: {exc}") from exc
