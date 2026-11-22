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

import asyncio
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import msgspec

from medre.core.delivery_authority import dispatch_source_matches_attempt
from medre.core.events.delivery import DeliveryAttemptProvenance
from medre.core.storage.backend import StorageError
from medre.core.storage.radio_handoff import RadioLocalHandoff
from medre.core.storage.sqlite._content_identity import _utc_microseconds
from medre.core.storage.sqlite._handoff_facts import observation_scope
from medre.core.storage.sqlite._radio import _closed_handoff, _decode

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import ParamSpec, TypeVar

    _P = ParamSpec("_P")
    _T = TypeVar("_T")

#: The sweep is bounded per invocation so it can ride a poll cycle without
#: holding the write lock unconditionally.
_CEILING_SWEEP_MAX_LIMIT = 500
_CEILING_SWEEP_CURSOR_KEY = "content_ceiling_enforcement_cursor"
_CEILING_RESIDUAL_CURSOR_KEY = "content_ceiling_residual_cursor"

#: Predicates for content-potential values attributable to one event on
#: stores that survive enforcement. Every purge target has exactly one
#: entry; the verification battery demands all of them false after the
#: purge, and the same battery detects post-enforcement residual values.
#: *event_ref* is either the ``:ev`` bind parameter (per-event battery) or
#: a correlated outer column reference (candidate discovery).
# Index-first relation lookup. Canonical targets win over native hints; an
# explicitly resolved relation to B can never purge B's quote during A's
# sweep merely because its native hint happens to name A. Each UNION arm
# uses a supported target index before touching excerpt content.
_INCOMING_EXCERPT_RELATION_IDS = (
    "SELECT r.id FROM event_relations r INDEXED BY idx_relations_target_event_id "
    "WHERE r.event_id<>{ev} AND r.target_event_id={ev} "
    "UNION "
    "SELECT r.id FROM canonical_events c "
    "JOIN event_relations r INDEXED BY idx_relations_target_native_ref "
    "ON r.target_native_adapter=c.source_native_adapter "
    "AND r.target_native_channel_id IS c.source_native_channel_id "
    "AND r.target_native_message_id=c.source_native_message_id "
    "WHERE c.event_id={ev} AND r.event_id<>{ev} "
    "AND NOT EXISTS (SELECT 1 FROM native_message_refs owner "
    "WHERE owner.adapter=c.source_native_adapter "
    "AND owner.native_channel_id IS c.source_native_channel_id "
    "AND owner.native_message_id=c.source_native_message_id "
    "AND owner.event_id<>c.event_id) "
    "AND r.target_event_id IS NULL AND r.target_native_adapter IS NOT NULL "
    "AND r.target_native_message_id IS NOT NULL "
    "UNION "
    "SELECT r.id FROM native_message_refs n "
    "JOIN event_relations r INDEXED BY idx_relations_target_native_ref "
    "ON r.target_native_adapter=n.adapter "
    "AND r.target_native_channel_id IS n.native_channel_id "
    "AND r.target_native_message_id=n.native_message_id "
    "WHERE n.event_id={ev} AND r.event_id<>{ev} "
    "AND r.target_event_id IS NULL AND r.target_native_adapter IS NOT NULL "
    "AND r.target_native_message_id IS NOT NULL"
)
_INCOMING_EXCERPTS_PREDICATE = (
    "x.relation_id IN (" + _INCOMING_EXCERPT_RELATION_IDS + ")"
)

#: Attribution of separated delivery diagnostic text to one event's evidence
#: rows. The same four-family predicate drives the residual battery, the byte
#: measurement, and the purge DELETE, so a future attribution change updates
#: the receipt counts, the measured bytes, and the erasure together. Every
#: consumer spells the event reference once via *ev* — a bind parameter or a
#: correlated outer column reference, matching the battery's convention.
_DIAGNOSTIC_ATTRIBUTION_SQL = (
    "(d.family='receipt' AND d.row_id IN (SELECT r.receipt_id "  # nosec B608 - closed module-owned fragment; event reference is a bind parameter or correlated column
    "FROM delivery_receipts r WHERE r.event_id={ev})) "
    "OR (d.family='observation' AND d.row_id IN (SELECT o.observation_id "
    "FROM delivery_observations o WHERE o.event_id={ev})) "
    "OR (d.family='outbox' AND d.row_id IN (SELECT b.outbox_id "
    "FROM delivery_outbox b WHERE b.event_id={ev})) "
    "OR (d.family='ingress' AND d.row_id={ev})"
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
        "EXISTS (SELECT 1 FROM event_relation_excerpts x WHERE "  # nosec B608 - closed module-owned fragment; event reference is a bind parameter or correlated column
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
        "AND (o.metadata<>'{}' OR o.payload_hash IS NOT NULL))",
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
        + _DIAGNOSTIC_ATTRIBUTION_SQL
        + ")",
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
    # Deleting this event's sole remaining attachment association also deletes
    # the corresponding blob, even though the blob has no event_id column.
    (
        "attachment_blobs:last_reference",
        "EXISTS (SELECT 1 FROM event_attachment_associations a "  # nosec B608 - closed module-owned fragment
        "JOIN attachment_blobs b ON b.content_ref=a.content_ref "
        "WHERE a.event_id={ev} "
        "AND NOT EXISTS (SELECT 1 FROM event_attachment_associations other "
        "WHERE other.content_ref=a.content_ref AND other.event_id<>{ev}))",
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

#: Bound *policy visits*, not just missing-receipt matches: a NOT EXISTS
#: filter before LIMIT scans arbitrarily many clean policies under BEGIN
#: IMMEDIATE. Every visited key advances the cursor, even with a receipt.
_CEILING_POLICY_PAGE_SQL = """
    SELECT p.event_id, p.hard_ceiling
      FROM event_content_access_policies AS p
     WHERE p.hard_ceiling <= ?
       AND (p.hard_ceiling, p.event_id) > (?, ?)
       AND (p.hard_ceiling, p.event_id) <= (?, ?)
     ORDER BY p.hard_ceiling ASC, p.event_id ASC
     LIMIT ?
"""

#: The read-only audit selects matching events without taking a writer lock.
_CEILING_AUDIT_PENDING_SQL = """
    SELECT p.event_id, p.hard_ceiling
      FROM event_content_access_policies AS p
     WHERE p.hard_ceiling <= ?
       AND NOT EXISTS (
           SELECT 1 FROM event_content_ceiling_log l WHERE l.event_id=p.event_id
       )
       AND p.event_id > ?
     ORDER BY p.event_id
     LIMIT ?
"""  # nosec B608 - static SQL fragments and parameter placeholders only

#: Read-only audit may filter for residual matches; no write lock is held.
#: The outer alias ``pol`` is deliberately unused by every battery fragment:
#: several fragments declare inner aliases of their own (``radio_attempt_progress
#: p`` among them), and a shared name would let SQLite resolve the correlation
#: to the innermost table the moment that table grows a same-named column.
_CEILING_RESIDUAL_CANDIDATES_SQL = (
    "SELECT pol.event_id, pol.hard_ceiling\n"  # nosec B608 - static SQL fragments and parameter placeholders only
    "  FROM event_content_access_policies AS pol\n"
    " WHERE pol.event_id > ?\n"
    "   AND EXISTS (\n"
    "       SELECT 1 FROM event_content_ceiling_log l WHERE l.event_id=pol.event_id\n"
    "   )\n"
    "   AND (\n" + " OR ".join(_residual_fragments("pol.event_id")) + "\n"
    "   )\n"
    " ORDER BY pol.event_id\n"
    " LIMIT ?\n"
)

#: The mutation path walks the *receipt* primary key, not policies with
#: a receipt EXISTS filter. Each keyset page visits at most the bound number
#: of receipt rows, even if millions of other policies have never expired.
_CEILING_RESIDUAL_PAGE_SQL = """
    SELECT event_id, ceiling AS hard_ceiling
      FROM event_content_ceiling_log
     WHERE event_id > ? AND event_id <= ?
     ORDER BY event_id
     LIMIT ?
"""


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
        "DELETE FROM delivery_evidence_diagnostics AS d WHERE "  # nosec B608 - closed module-owned attribution fragment; the event reference is a bound parameter
        + _DIAGNOSTIC_ATTRIBUTION_SQL.format(ev="?"),
        (event_id, event_id, event_id, event_id),
    )


def _purge_own_and_incoming_excerpts(db: sqlite3.Connection, event_id: str) -> None:
    """Delete same-event excerpts and excerpts other events quoted from it."""
    db.execute(
        "DELETE FROM event_relation_excerpts WHERE relation_id IN "  # nosec B608 - closed module-owned fragment; event reference is a bind parameter or correlated column
        "(SELECT r.id FROM event_relations r WHERE r.event_id=:ev) "
        "OR relation_id IN (" + _INCOMING_EXCERPT_RELATION_IDS.format(ev=":ev") + ")",
        {"ev": event_id},
    )


#: A valid summary row that records the existence of undecodable radio
#: evidence without retaining any wire plan. ``handoff_id="{}"`` can never
#: decode into a handoff reference, so settlement stays unresolved instead
#: of falling through to whole-message observations as if no checkpoint
#: existed. One part with an ``accepted`` state produces the revision-2
#: summary the typed evidence reader accepts.
_UNREADABLE_RADIO_PARTS = '[{"ordinal":1,"state":"accepted","handoff_id":"{}"}]'


def _checkpoint_source_coherent(
    row_source: str, attempt_source: str, attempt_number: int
) -> bool:
    """Validate a retained checkpoint's lane against its own reservation.

    The row's current ``dispatch_source`` describes the latest reservation,
    not this checkpoint's: ``reserve_outbox_attempt`` moves a live origin to
    ``retry`` whenever it reserves a later generation, so an older attempt's
    checkpoint legitimately keeps the lane it was bound under. The evidence
    resolver applies the same doctrine by skipping source coherence for
    older attempts. The reservation CASE constrains the reachable lanes: a
    first reservation from zero stamps ``live`` (dispatched either through
    the live path or by the retry worker), every later live-origin
    reservation stamps ``retry``, and replay rows never re-source. A
    ``live`` lane claim is therefore coherent only for attempt 1 on a
    live-descended row; every other disagreement stays fail-closed.
    """
    if dispatch_source_matches_attempt(row_source, attempt_source, attempt_number):
        return True
    return attempt_number == 1 and attempt_source == "live" and row_source == "retry"


def _radio_summary_provenance(
    db: sqlite3.Connection, outbox_id: str, attempt_number: int
) -> DeliveryAttemptProvenance:
    """Rebuild the closed attempt identity for an undecodable checkpoint.

    The outbox row is schema-owned identity; no plan, part, provider metadata,
    or diagnostic text from the unreadable checkpoint is copied.
    """
    outbox = db.execute(
        "SELECT event_id, delivery_plan_id, target_adapter, target_channel, "
        "dispatch_source, replay_run_id FROM delivery_outbox WHERE outbox_id=?",
        (outbox_id,),
    ).fetchone()
    if outbox is None:
        raise StorageError("Unreadable radio evidence lost its outbox identity")
    try:
        return DeliveryAttemptProvenance(
            event_id=str(outbox["event_id"]),
            delivery_plan_id=str(outbox["delivery_plan_id"]),
            target_adapter=str(outbox["target_adapter"]),
            target_channel=outbox["target_channel"],
            outbox_id=outbox_id,
            attempt_number=attempt_number,
            source=outbox["dispatch_source"],
            replay_run_id=outbox["replay_run_id"],
        )
    except (TypeError, ValueError) as exc:
        raise StorageError(
            "Unreadable radio evidence has invalid outbox identity"
        ) from exc


def _retain_unreadable_radio_checkpoint(
    db: sqlite3.Connection, outbox_id: str, attempt_number: int
) -> None:
    """Retain a non-authorizing blocker for a checkpoint we cannot trust."""
    prior = db.execute(
        "SELECT revision FROM radio_attempt_evidence_summaries "
        "WHERE outbox_id=? AND attempt_number=?",
        (outbox_id, attempt_number),
    ).fetchone()
    _retain_radio_evidence_summary(
        db,
        _radio_summary_provenance(db, outbox_id, attempt_number),
        json.loads(_UNREADABLE_RADIO_PARTS),
        max(2, int(prior["revision"]) + 1 if prior is not None else 2),
    )


def _retain_radio_evidence_summary(
    db: sqlite3.Connection,
    provenance: DeliveryAttemptProvenance,
    parts: list[dict[str, Any]],
    revision: int,
) -> None:
    """Advance closed evidence without rewriting the attempt's provenance.

    A stale checkpoint cannot regress a newer summary. Conflicting provenance
    or a different claim at the *same* revision is incoherent evidence, not
    permission to reattribute an accepted handoff: preserve the original
    identity, replace only the parts with a content-free unreadable marker,
    and advance the revision so stale settlements are rejected.
    """
    db.execute(
        "INSERT INTO radio_attempt_evidence_summaries "
        "(outbox_id, attempt_number, provenance_json, parts_json, revision) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(outbox_id, attempt_number) DO UPDATE SET "
        "parts_json=CASE "
        "WHEN excluded.provenance_json<>radio_attempt_evidence_summaries.provenance_json "
        "OR (excluded.revision=radio_attempt_evidence_summaries.revision "
        "AND excluded.parts_json<>radio_attempt_evidence_summaries.parts_json) "
        "THEN ? "
        "WHEN excluded.revision>=radio_attempt_evidence_summaries.revision "
        "THEN excluded.parts_json ELSE radio_attempt_evidence_summaries.parts_json END, "
        "revision=CASE "
        "WHEN (excluded.provenance_json<>radio_attempt_evidence_summaries.provenance_json "
        "OR (excluded.revision=radio_attempt_evidence_summaries.revision "
        "AND excluded.parts_json<>radio_attempt_evidence_summaries.parts_json)) "
        "THEN MAX(excluded.revision, radio_attempt_evidence_summaries.revision)+1 "
        "ELSE MAX(excluded.revision, radio_attempt_evidence_summaries.revision) END "
        "WHERE excluded.revision>=radio_attempt_evidence_summaries.revision "
        "OR excluded.provenance_json<>radio_attempt_evidence_summaries.provenance_json",
        (
            provenance.outbox_id,
            provenance.attempt_number,
            msgspec.json.encode(provenance).decode(),
            json.dumps(parts, separators=(",", ":")),
            revision,
            _UNREADABLE_RADIO_PARTS,
        ),
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
        "FROM event_relation_excerpts x WHERE "
        + _INCOMING_EXCERPTS_PREDICATE.format(ev=":ev"),
        {"ev": event_id},
    ).fetchone()
    rows_removed += int(own_excerpts[0]) + int(incoming_excerpts[0])
    logical_bytes += int(own_excerpts[1]) + int(incoming_excerpts[1])
    _purge_own_and_incoming_excerpts(db, event_id)

    # Opaque envelope values on the canonical row (identity row survives).
    envelope = db.execute(
        "SELECT CASE WHEN payload<>'{}' THEN length(CAST(payload AS BLOB)) "
        "ELSE 0 END, CASE WHEN metadata<>'{}' "
        "THEN length(CAST(metadata AS BLOB)) ELSE 0 END "
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
        "COALESCE(SUM(CASE WHEN metadata<>'{}' "
        "THEN length(CAST(metadata AS BLOB)) ELSE 0 END), 0) "
        "FROM event_relations "
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
    outbox_hashes = db.execute(
        "SELECT COALESCE(SUM(length(CAST(payload_hash AS BLOB))), 0) "
        "FROM delivery_outbox WHERE event_id=? AND payload_hash IS NOT NULL",
        (event_id,),
    ).fetchone()
    logical_bytes += int(outbox_hashes[0])
    db.execute(
        "UPDATE delivery_outbox SET metadata='{}', payload_hash=NULL WHERE event_id=?",
        (event_id,),
    )

    # Provider metadata on the event's delivery observations.
    observation_metadata = db.execute(
        "SELECT COALESCE(SUM(length(CAST(metadata AS BLOB))), 0) "
        "FROM delivery_observations WHERE event_id=? AND metadata<>'{}'",
        (event_id,),
    ).fetchone()
    logical_bytes += int(observation_metadata[0])
    # Preserve only packet identity, never provider metadata. A malformed
    # packet claim remains explicitly unreadable rather than being promoted
    # to whole-message acceptance by the redaction to '{}'.
    for observation in db.execute(
        "SELECT observation_id, outbox_id, attempt_number, metadata "
        "FROM delivery_observations WHERE event_id=? AND metadata<>'{}'",
        (event_id,),
    ):
        try:
            metadata = json.loads(observation["metadata"])
        except (TypeError, ValueError, RecursionError):
            # Legacy JSON can be syntactically valid but too deeply nested
            # to decode. It must not block enforcement of unrelated events.
            metadata = {"radio_packet": None}
        scope = observation_scope(
            metadata,
            outbox_id=observation["outbox_id"],
            attempt_number=observation["attempt_number"],
        )
        if scope is not None:
            # The upsert re-fences scopes derived from legacy pre-fence
            # claims whose retained row still carries a richer scope than
            # the validated packet scope computed here; scope_json is never
            # content-bearing, so rewriting it tightens, not leaks.
            db.execute(
                "INSERT INTO delivery_observation_scopes "
                "(observation_id, scope_json) VALUES (?, ?) "
                "ON CONFLICT(observation_id) DO UPDATE SET scope_json=excluded.scope_json",
                (observation["observation_id"], scope),
            )
    db.execute(
        "UPDATE delivery_observations SET metadata='{}' WHERE event_id=?",
        (event_id,),
    )

    # Separated delivery diagnostic text on the event's evidence rows.
    diagnostics = db.execute(
        "SELECT COUNT(*), COALESCE(SUM(length(CAST(d.diagnostic AS BLOB))), 0) "  # nosec B608 - closed module-owned attribution fragment; the event reference is a bound parameter
        "FROM delivery_evidence_diagnostics d WHERE "
        + _DIAGNOSTIC_ATTRIBUTION_SQL.format(ev="?"),
        (event_id, event_id, event_id, event_id),
    ).fetchone()
    rows_removed += int(diagnostics[0])
    logical_bytes += int(diagnostics[1])
    # The diagnostic string is content-potential, but its closed handoff state
    # is authority-neutral evidence admission for the original attempt.
    db.execute(
        """
        WITH latest AS (
            SELECT o.outbox_id, o.attempt_number, o.status, o.active_attempt,
                   (SELECT d.diagnostic FROM delivery_evidence_diagnostics d
                    WHERE d.family='outbox' AND d.row_id=o.outbox_id
                      AND d.attempt=o.attempt_number
                      AND d.field='failure_kind_detail'
                    ORDER BY d.diag_id DESC LIMIT 1) AS diagnostic
            FROM delivery_outbox o WHERE o.event_id=?
        )
        INSERT INTO delivery_handoff_uncertainty (outbox_id, attempt_number, state)
        SELECT outbox_id, attempt_number,
               CASE WHEN status IN ('retry_wait', 'in_progress')
                         AND diagnostic='uncertain_native_handoff'
                    THEN 'uncertain_native_handoff'
                    ELSE 'superseded_native_handoff' END
        FROM latest
        WHERE diagnostic IS NOT NULL AND attempt_number >= 1
        ON CONFLICT(outbox_id) DO UPDATE SET
            attempt_number=excluded.attempt_number,
            state=CASE
                WHEN delivery_handoff_uncertainty.attempt_number=excluded.attempt_number
                     AND delivery_handoff_uncertainty.state='superseded_native_handoff'
                    THEN 'superseded_native_handoff'
                ELSE excluded.state END
        """,
        (event_id,),
    )
    _purge_diagnostics(db, event_id)

    # Radio recovery plans riding the event's outbox generations.
    radio = db.execute(
        "SELECT COUNT(*), COALESCE(SUM("
        "length(CAST(p.plan_json AS BLOB)) + "
        "length(CAST(p.provenance_json AS BLOB)) + "
        "length(CAST(p.parts_json AS BLOB)) + "
        "COALESCE(length(CAST(p.plan_fingerprint AS BLOB)), 0)), 0) "
        "FROM radio_attempt_progress p JOIN delivery_outbox o "
        "ON o.outbox_id=p.outbox_id WHERE o.event_id=?",
        (event_id,),
    ).fetchone()
    rows_removed += int(radio[0])
    logical_bytes += int(radio[1])
    for checkpoint_row in db.execute(
        "SELECT p.* FROM radio_attempt_progress p JOIN delivery_outbox o "
        "ON o.outbox_id=p.outbox_id WHERE o.event_id=?",
        (event_id,),
    ):
        try:
            saved = _decode(checkpoint_row)
        except (StorageError, ValueError, TypeError, RecursionError):
            # Invalid wire plans must still be erased, but their existence
            # must survive. Without a retained blocker, a later whole-message
            # observation could settle through the no-checkpoint path as if
            # no native grant had ever been issued.
            _retain_unreadable_radio_checkpoint(
                db,
                str(checkpoint_row["outbox_id"]),
                int(checkpoint_row["attempt_number"]),
            )
            continue
        # The checkpoint must describe the parent outbox's exact delivery
        # identity. A decodable but mismatched provenance would otherwise
        # become an original-acceptance fact for the wrong event after the
        # wire plan is erased.
        parent = db.execute(
            "SELECT event_id, delivery_plan_id, target_adapter, target_channel, "
            "dispatch_source, replay_run_id FROM delivery_outbox WHERE outbox_id=?",
            (saved.provenance.outbox_id,),
        ).fetchone()
        if parent is None or (
            saved.provenance.outbox_id != checkpoint_row["outbox_id"]
            or saved.provenance.attempt_number != checkpoint_row["attempt_number"]
            or saved.provenance.event_id != parent["event_id"]
            or saved.provenance.delivery_plan_id != parent["delivery_plan_id"]
            or saved.provenance.target_adapter != parent["target_adapter"]
            or (saved.provenance.target_channel or "")
            != (parent["target_channel"] or "")
            or saved.provenance.replay_run_id != parent["replay_run_id"]
            or not _checkpoint_source_coherent(
                str(parent["dispatch_source"]),
                saved.provenance.source,
                saved.provenance.attempt_number,
            )
        ):
            _retain_unreadable_radio_checkpoint(
                db,
                str(checkpoint_row["outbox_id"]),
                int(checkpoint_row["attempt_number"]),
            )
            continue
        evidence_parts: list[dict[str, Any]] = []
        accepted_facts: list[tuple[str, int, int, str | None, str]] = []
        for part in saved.progress.parts:
            if part.state != "accepted":
                evidence_parts.append(
                    {"ordinal": part.ordinal, "state": part.state, "handoff_id": None}
                )
                continue
            try:
                fact = RadioLocalHandoff.from_part(part, saved.provenance)
            except (ValueError, TypeError, msgspec.DecodeError):
                # Never erase the *existence* of unreadable radio evidence:
                # otherwise later message observations could settle by the
                # no-checkpoint path. The sentinel contains no provider data.
                evidence_parts.append(
                    {"ordinal": part.ordinal, "state": "accepted", "handoff_id": "{}"}
                )
                continue
            # The compact projection strips payload fingerprints and a
            # redundant origin, preserving only closed native identity.
            original_attempt = fact.origin is not None and (
                fact.origin.outbox_id == saved.provenance.outbox_id
                and fact.origin.attempt_number == saved.provenance.attempt_number
            )
            evidence_parts.append(
                {
                    "ordinal": part.ordinal,
                    "state": part.state,
                    "handoff_id": _closed_handoff(part, saved.provenance),
                }
            )
            if not original_attempt or fact.origin is None:
                continue  # A continuation cannot create original acceptance.
            if fact.native_message_id:
                accepted_facts.append(
                    (
                        fact.origin.outbox_id,
                        fact.origin.attempt_number,
                        fact.ordinal,
                        fact.native_channel_id,
                        fact.native_message_id,
                    )
                )
        if evidence_parts:
            _retain_radio_evidence_summary(
                db, saved.provenance, evidence_parts, saved.revision
            )
            # A newer or conflicting retained summary may reject this old
            # checkpoint. Only the exact summary that actually won may mint
            # corresponding original-acceptance facts. Both actions share the
            # sweep transaction; an old checkpoint can never add authority
            # behind an unchanged newer summary.
            committed_summary = db.execute(
                "SELECT provenance_json, parts_json, revision "
                "FROM radio_attempt_evidence_summaries "
                "WHERE outbox_id=? AND attempt_number=?",
                (saved.provenance.outbox_id, saved.provenance.attempt_number),
            ).fetchone()
            if committed_summary is not None and (
                committed_summary["revision"] == saved.revision
                and committed_summary["provenance_json"]
                == msgspec.json.encode(saved.provenance).decode()
                and committed_summary["parts_json"]
                == json.dumps(evidence_parts, separators=(",", ":"))
            ):
                for fact_params in accepted_facts:
                    db.execute(
                        "INSERT INTO radio_accepted_part_facts "
                        "(outbox_id, attempt_number, ordinal, native_channel_id, native_message_id) "
                        "VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                        fact_params,
                    )
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
    blobs = db.execute(
        "SELECT COUNT(*), COALESCE(SUM(b.size_bytes), 0) "
        "FROM event_attachment_associations a JOIN attachment_blobs b "
        "ON b.content_ref=a.content_ref WHERE a.event_id=? "
        "AND NOT EXISTS (SELECT 1 FROM event_attachment_associations other "
        "WHERE other.content_ref=b.content_ref AND other.event_id<>?)",
        (event_id, event_id),
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

            # Residual discovery walks one bounded *receipt* page on every
            # invocation, including when that page is clean. Primary-key
            # traversal avoids touching an unbounded number of pending
            # policies. Progress is durable and wraps for late writes.
            residual_budget = max(25, limit)
            progress = db.execute(
                "SELECT value FROM _medre_schema_meta WHERE key=?",
                (_CEILING_RESIDUAL_CURSOR_KEY,),
            ).fetchone()
            saved_residual = json.loads(progress[0]) if progress is not None else ""
            if isinstance(saved_residual, str):
                residual_cursor, residual_upper = saved_residual, ""
            elif (
                isinstance(saved_residual, list)
                and len(saved_residual) == 2
                and all(isinstance(value, str) for value in saved_residual)
            ):
                residual_cursor, residual_upper = saved_residual
            else:
                raise StorageError("Invalid ceiling residual cursor")
            if not residual_upper:
                row = db.execute(
                    "SELECT MAX(event_id) FROM event_content_ceiling_log"
                ).fetchone()
                residual_upper = str(row[0]) if row[0] is not None else ""
            residual_rows = db.execute(
                _CEILING_RESIDUAL_PAGE_SQL,
                (residual_cursor, residual_upper, residual_budget),
            ).fetchall()
            for row in residual_rows:
                event_id = str(row["event_id"])
                residual_cursor = event_id
                if (
                    int(
                        db.execute(_CEILING_RESIDUAL_SQL, {"ev": event_id}).fetchone()[
                            0
                        ]
                    )
                    == 0
                ):
                    continue
                rows, logical, blobs, blob_bytes = _measure_and_purge(db, event_id)
                _release_authority(db, event_id, observed)
                if (
                    int(
                        db.execute(_CEILING_RESIDUAL_SQL, {"ev": event_id}).fetchone()[
                            0
                        ]
                    )
                    != 0
                ):
                    raise StorageError(
                        "Ceiling enforcement left residual content for an "
                        "enforced event"
                    )
                enforced.append(
                    CeilingEnforcement(
                        event_id=event_id,
                        ceiling=str(row["hard_ceiling"]),
                        enforced_at=observed,
                        rows_removed=rows,
                        logical_bytes_removed=logical,
                        blobs_removed=blobs,
                        blob_bytes_removed=blob_bytes,
                        residual_pass=True,
                    )
                )
            if (
                len(residual_rows) < residual_budget
                or residual_cursor >= residual_upper
            ):
                residual_cursor = residual_upper = ""
            db.execute(
                "INSERT INTO _medre_schema_meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (
                    _CEILING_RESIDUAL_CURSOR_KEY,
                    json.dumps([residual_cursor, residual_upper]),
                ),
            )

            # Record progress over every policy examined in keyset order.
            progress = db.execute(
                "SELECT value FROM _medre_schema_meta WHERE key=?",
                (_CEILING_SWEEP_CURSOR_KEY,),
            ).fetchone()
            cursor = json.loads(progress[0]) if progress is not None else ["", ""]
            if (
                not isinstance(cursor, list)
                or len(cursor) not in (2, 4)
                or not all(isinstance(value, str) for value in cursor)
            ):
                raise StorageError("Invalid ceiling enforcement cursor")
            last_ceiling, last_event_id = cursor[:2]
            upper_ceiling, upper_event_id = cursor[2:] if len(cursor) == 4 else ("", "")
            if not upper_ceiling:
                row = db.execute(
                    "SELECT hard_ceiling, event_id FROM event_content_access_policies "
                    "WHERE hard_ceiling<=? "
                    "ORDER BY hard_ceiling DESC, event_id DESC LIMIT 1",
                    (observed,),
                ).fetchone()
                if row is not None:
                    upper_ceiling, upper_event_id = map(str, row)
            budget = max(25, limit)
            examined = 0
            first_time = 0
            while first_time < limit and examined < budget:
                page_size = min(100, budget - examined)
                page = db.execute(
                    _CEILING_POLICY_PAGE_SQL,
                    (
                        observed,
                        last_ceiling,
                        last_event_id,
                        upper_ceiling,
                        upper_event_id,
                        page_size,
                    ),
                ).fetchall()
                if not page:
                    # Wrap: the next sweep rescans from the start so events
                    # admitted (or backdated) behind the cursor are still
                    # enforced.
                    last_ceiling = last_event_id = ""
                    upper_ceiling = upper_event_id = ""
                    break
                for row in page:
                    examined += 1
                    last_ceiling = str(row["hard_ceiling"])
                    last_event_id = str(row["event_id"])
                    event_id = str(row["event_id"])
                    ceiling = str(row["hard_ceiling"])
                    if (
                        db.execute(
                            "SELECT 1 FROM event_content_ceiling_log WHERE event_id=?",
                            (event_id,),
                        ).fetchone()
                        is not None
                    ):
                        continue
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
                    first_time += 1
                    if first_time >= limit:
                        break
                if (
                    len(page) < page_size and last_event_id == str(page[-1]["event_id"])
                ) or (last_ceiling, last_event_id) >= (upper_ceiling, upper_event_id):
                    last_ceiling = last_event_id = ""
                    upper_ceiling = upper_event_id = ""
                    break
            db.execute(
                "INSERT INTO _medre_schema_meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (
                    _CEILING_SWEEP_CURSOR_KEY,
                    json.dumps(
                        [last_ceiling, last_event_id, upper_ceiling, upper_event_id]
                    ),
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
    after_event: str | None = None,
) -> dict[str, Any]:
    """Read-only audit of the ceiling residual battery; it purges nothing.

    One deferred read transaction observes, for content whose ceiling has
    elapsed, exactly what the guarded enforcement would remove: the
    per-store residual counts from the same battery table enforcement
    verifies with. Elapsed ceilings without a receipt are reported as
    ``elapsed_pending`` (the sweep's next candidates); enforced events that
    regained content-potential values through writes authorized before
    their ceiling are reported as ``residual_enforced`` (the spec's
    documented late-write case, which a later sweep purges when it revisits the receipt). Event
    identifiers and census store labels are content-free identity; no
    message text, excerpt, blob bytes, or other content value is returned.
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
                _CEILING_AUDIT_PENDING_SQL, (observed, after_event or "", fetch)
            ).fetchall()
            pending = [
                _audit_finding(db, str(row["event_id"]), str(row["hard_ceiling"]))
                for row in pending_rows[:limit]
            ]
            residual_rows = db.execute(
                _CEILING_RESIDUAL_CANDIDATES_SQL, (after_event or "", fetch)
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
                "elapsed_pending_next_after_event": (
                    pending[-1]["event_id"] if len(pending_rows) > limit else None
                ),
                "residual_enforced_next_after_event": (
                    residual[-1]["event_id"] if len(residual_rows) > limit else None
                ),
            }
        finally:
            db.rollback()


def _audit_finding(
    db: sqlite3.Connection, event_id: str, ceiling: str
) -> dict[str, Any]:
    """Build one audit finding: per-store residual counts for one event.

    The counts describe this event in the caller's snapshot. They are not
    additive across events and do not forecast a sweep: a shared attachment
    blob is ``last_reference`` only for the event that currently owns its
    final association.
    """
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


#: Verify receipts in immutable primary-key order for continuation across windows.
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
    after_event: str | None = None,
) -> dict[str, Any]:
    """Read-only one-shot proof: receipts re-checked against the live store.

    Re-runs the residual battery — the same battery that gates the receipt
    commit — against the current database for every enforcement receipt in
    a bounded window, and reports pass/fail per receipt with the receipt's
    recorded content-free counters. A receipt fails when its event holds
    any residual content-potential value now (the documented late-write
    case until a sweep revisits its receipt); an explicitly requested event
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
                event_state = "receipt_present" if rows else "no_receipt"
            else:
                rows = db.execute(
                    _CEILING_RECEIPTS_SQL
                    + " WHERE event_id > ? ORDER BY event_id LIMIT ?",
                    (after_event or "", fetch),
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
                "receipts_next_after_event": (
                    receipts[-1]["event_id"] if len(rows) > limit else None
                ),
                "receipts": receipts,
            }
            if event_state is not None:
                if event_state == "no_receipt":
                    result["event_state"] = "no_receipt"
                elif verdict == "passed":
                    result["event_state"] = "receipt_verified"
                else:
                    result["event_state"] = "receipt_failed"
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

        async def _await_deferring_cancel(
            self, future: asyncio.Future[Any]
        ) -> tuple[BaseException | None, asyncio.CancelledError | None]: ...

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
        content-free receipt commits. Already-enforced events regaining
        residual values through writes authorized before the ceiling are
        re-purged without a second receipt. Delivery identity and evidence
        rows (receipts, observations, outbox generations, native
        references, settlements, operator decisions) are never deleted.
        A verification failure raises and rolls the entire sweep back.

        Production omits *now* so storage samples UTC after acquiring its
        SQLite transaction; an explicit *now* is available to trusted
        controlled-clock qualification and must be timezone-aware. Returns
        metadata-only summaries; each call re-enforces at most
        ``max(25, limit)`` receipt rows and enforces at most
        *limit* new events, examining at most ``max(25, limit)`` candidates
        per queue. Pending work has its own capacity, never consumed by a
        residual repair. A durable keyset cursor in internal metadata advances in the same
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
        # Do not let a forced RetryWorker cancellation outlive a committing
        # SQLite transaction while its stop() reports a clean teardown. The
        # storage executor's private connection remains owned until the
        # transaction finishes (or rolls back), even if the caller is cancelled.
        operation = asyncio.create_task(
            self._run_in_thread(
                sync_enforce_content_ceilings,
                self._require_db(),
                self._lock,
                ceiling_observation,
                limit,
            )
        )
        error, cancelled = await self._await_deferring_cancel(operation)
        if error is not None:
            if isinstance(error, sqlite3.Error):
                raise StorageError(
                    f"Ceiling enforcement sweep failed: {error}"
                ) from error
            raise error
        if cancelled is not None:
            raise cancelled
        return operation.result()

    async def audit_content_ceilings(
        self,
        *,
        now: datetime | None = None,
        limit: int = 25,
        event_id: str | None = None,
        after_event: str | None = None,
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
        (``residual_enforced``) and are flagged until the sweep
        revisits the receipt. Event identifiers and census store labels are
        content-free identity; no content value is returned. With *event_id*,
        reports exactly one event's state (``unknown``, ``not_elapsed``,
        ``elapsed_pending``, ``residual_enforced``, ``enforced_clean``)
        with its receipt counters when present. Production omits *now* so
        storage samples UTC; an explicit timezone-aware *now* is available
        to trusted controlled-clock qualification. The report window is
        bounded by *limit* and exposes a continuation for each list.
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
        if after_event is not None and (
            event_id is not None or not isinstance(after_event, str) or not after_event
        ):
            raise ValueError(
                "A nonempty continuation is valid only for a report window"
            )
        try:
            return await self._run_in_thread(
                sync_audit_content_ceilings,
                self._require_db(),
                self._lock,
                ceiling_observation,
                limit,
                event_id,
                after_event,
            )
        except sqlite3.Error as exc:
            raise StorageError(f"Ceiling audit failed: {exc}") from exc

    async def verify_content_ceiling_receipts(
        self,
        *,
        limit: int = 25,
        event_id: str | None = None,
        after_event: str | None = None,
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
        if after_event is not None and (
            event_id is not None or not isinstance(after_event, str) or not after_event
        ):
            raise ValueError(
                "A nonempty continuation is valid only for a report window"
            )
        try:
            return await self._run_in_thread(
                sync_verify_content_ceiling_receipts,
                self._require_db(),
                self._lock,
                limit,
                event_id,
                after_event,
            )
        except sqlite3.Error as exc:
            raise StorageError(f"Ceiling receipt verification failed: {exc}") from exc
