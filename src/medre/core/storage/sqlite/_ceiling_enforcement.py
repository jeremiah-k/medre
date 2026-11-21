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
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

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


def _residual_fragments(event_ref: str) -> tuple[str, ...]:
    """Build the residual battery for one event-reference spelling."""
    ev = event_ref
    return (
        # Separated canonical body rows.
        f"EXISTS (SELECT 1 FROM event_body_content b WHERE b.event_id={ev})",  # nosec B608 - closed module-owned fragment; event reference is a bind parameter or correlated column
        # Same-event reply-quote excerpts.
        "EXISTS (SELECT 1 FROM event_relation_excerpts x WHERE x.relation_id IN "  # nosec B608 - closed module-owned fragment; event reference is a bind parameter or correlated column
        f"(SELECT r2.id FROM event_relations r2 WHERE r2.event_id={ev}))",
        # Excerpts other events quoted from this event's content.
        "EXISTS (SELECT 1 FROM event_relation_excerpts x "  # nosec B608 - closed module-owned fragment; event reference is a bind parameter or correlated column
        "JOIN event_relations r ON r.id=x.relation_id WHERE "
        + _INCOMING_EXCERPTS_PREDICATE.format(ev=ev)
        + ")",
        # Opaque envelope values on the canonical row.
        f"EXISTS (SELECT 1 FROM canonical_events c WHERE c.event_id={ev} "  # nosec B608 - closed module-owned fragment; event reference is a bind parameter or correlated column
        "AND (c.payload<>'{}' OR c.metadata<>'{}'))",
        # Content-potential values on the event's own relation rows.
        f"EXISTS (SELECT 1 FROM event_relations r WHERE r.event_id={ev} "  # nosec B608 - closed module-owned fragment; event reference is a bind parameter or correlated column
        "AND (r.key IS NOT NULL OR r.metadata<>'{}'))",
        # Provider metadata on the event's native references.
        f"EXISTS (SELECT 1 FROM native_message_refs n WHERE n.event_id={ev} "  # nosec B608 - closed module-owned fragment; event reference is a bind parameter or correlated column
        "AND n.metadata<>'{}')",
        # Inline dispatch context on the event's outbox generations.
        f"EXISTS (SELECT 1 FROM delivery_outbox o WHERE o.event_id={ev} "  # nosec B608 - closed module-owned fragment; event reference is a bind parameter or correlated column
        "AND o.metadata<>'{}')",
        # Provider metadata on the event's delivery observations.
        f"EXISTS (SELECT 1 FROM delivery_observations o WHERE o.event_id={ev} "  # nosec B608 - closed module-owned fragment; event reference is a bind parameter or correlated column
        "AND o.metadata<>'{}')",
        # Separated delivery diagnostic text on the event's evidence rows.
        "EXISTS (SELECT 1 FROM delivery_evidence_diagnostics d WHERE "  # nosec B608 - closed module-owned fragment; event reference is a bind parameter or correlated column
        "(d.family='receipt' AND d.row_id IN (SELECT r3.receipt_id "
        f"FROM delivery_receipts r3 WHERE r3.event_id={ev})) "
        "OR (d.family='observation' AND d.row_id IN (SELECT o2.observation_id "
        f"FROM delivery_observations o2 WHERE o2.event_id={ev})) "
        "OR (d.family='outbox' AND d.row_id IN (SELECT b2.outbox_id "
        f"FROM delivery_outbox b2 WHERE b2.event_id={ev})) "
        f"OR (d.family='ingress' AND d.row_id={ev}))",
        # Radio recovery plans riding the event's outbox generations.
        "EXISTS (SELECT 1 FROM radio_attempt_progress p "  # nosec B608 - closed module-owned fragment; event reference is a bind parameter or correlated column
        f"JOIN delivery_outbox o ON o.outbox_id=p.outbox_id WHERE o.event_id={ev})",
        # Frozen dispatch snapshots riding the event's outbox generations.
        "EXISTS (SELECT 1 FROM delivery_outbox_snapshots s "  # nosec B608 - closed module-owned fragment; event reference is a bind parameter or correlated column
        f"JOIN delivery_outbox o ON o.outbox_id=s.outbox_id WHERE o.event_id={ev})",
        # Attachment associations: the only authority for loading retained bytes.
        f"EXISTS (SELECT 1 FROM event_attachment_associations a WHERE a.event_id={ev})",  # nosec B608 - closed module-owned fragment; event reference is a bind parameter or correlated column
    )


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
