"""Recovery query mixin for SQLiteStorage.

Authority surface:
  - query_unresolved_deliveries: **list/get** (read-only). Keyset-paginated
    scan over *currently-unresolved* delivery outcomes — the
    lifecycle-authoritative receipt of each logical delivery whose status is
    ``failed`` or ``dead_lettered``.

The lineage predicate here must match the shared event-scoped delivery identity
used by :mod:`medre.core.delivery_authority`: ``(event_id, delivery_plan_id,
target_adapter, COALESCE(target_channel, ''))``. Plan IDs are not globally
unique, and ``source`` / ``replay_run_id`` are receipt provenance only.

Recovery deliberately starts from the append-only receipt keyset rather than
materialising the full ``delivery_status`` view for every page. Each unresolved
receipt candidate is checked against the same two authority classes as
:mod:`medre.core.delivery_authority`: exact committed outbox pointers ranked by
outbox generation, and outbox-less evidence ranked by append sequence. The
winning class is then selected by append sequence. No OFFSET or global COUNT is
used: the page is probed with ``limit + 1`` authoritative rows and the last
emitted row's ``receipt_sequence`` becomes the next keyset position (strictly
ascending — ``sequence`` is the AUTOINCREMENT primary key, so positions are
unique and stable).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from medre.core.storage.backend import (
    DEFAULT_RECOVERY_PAGE_LIMIT,
    MAX_RECOVERY_PAGE_LIMIT,
    UNRESOLVED_RECEIPT_STATUSES,
    UnresolvedDeliveriesPage,
    UnresolvedDelivery,
    attempt_source_label,
    decode_page_cursor,
    encode_page_cursor,
)


def _same_identity(alias: str) -> str:
    """Return SQL matching *alias* to the current ``dr`` delivery identity."""
    return (
        f"{alias}.event_id = dr.event_id"
        f" AND {alias}.delivery_plan_id = dr.delivery_plan_id"
        f" AND {alias}.target_adapter = dr.target_adapter"
        f" AND COALESCE({alias}.target_channel, '') = COALESCE(dr.target_channel, '')"
    )


_OUTBOXLESS_NEWER = (
    "NOT EXISTS ("  # nosec B608 - composes only module-local fragments; values stay bound parameters
    " SELECT 1 FROM delivery_receipts newer_outboxless"
    f" WHERE {_same_identity('newer_outboxless')}"
    " AND newer_outboxless.outbox_id IS NULL"
    " AND newer_outboxless.sequence > dr.sequence"
    ")"
)

_HIGHER_COMMITTED_THAN_CURRENT = (
    "NOT EXISTS ("  # nosec B608 - composes only module-local fragments; values stay bound parameters
    " SELECT 1"
    " FROM delivery_receipts higher_receipt"
    " JOIN delivery_outbox higher_outbox"
    "   ON higher_outbox.outbox_id = higher_receipt.outbox_id"
    "  AND higher_outbox.receipt_id = higher_receipt.receipt_id"
    f" WHERE {_same_identity('higher_receipt')}"
    " AND (higher_outbox.attempt_number > committed_outbox.attempt_number"
    "      OR (higher_outbox.attempt_number = committed_outbox.attempt_number"
    "          AND higher_receipt.sequence > dr.sequence))"
    ")"
)

# For an outbox-less candidate, reject it only when the *winning* committed
# outbox candidate has a later append sequence. A late append from an older
# outbox generation must not hide newer-generation authority.
_LATER_WINNING_COMMITTED = (
    "NOT EXISTS ("  # nosec B608 - composes only module-local fragments; values stay bound parameters
    " SELECT 1"
    " FROM delivery_receipts committed_receipt"
    " JOIN delivery_outbox candidate_outbox"
    "   ON candidate_outbox.outbox_id = committed_receipt.outbox_id"
    "  AND candidate_outbox.receipt_id = committed_receipt.receipt_id"
    f" WHERE {_same_identity('committed_receipt')}"
    " AND committed_receipt.sequence > dr.sequence"
    " AND NOT EXISTS ("
    "   SELECT 1"
    "   FROM delivery_receipts higher_receipt"
    "   JOIN delivery_outbox higher_outbox"
    "     ON higher_outbox.outbox_id = higher_receipt.outbox_id"
    "    AND higher_outbox.receipt_id = higher_receipt.receipt_id"
    f"   WHERE {_same_identity('higher_receipt')}"
    "   AND (higher_outbox.attempt_number > candidate_outbox.attempt_number"
    "        OR (higher_outbox.attempt_number = candidate_outbox.attempt_number"
    "            AND higher_receipt.sequence > committed_receipt.sequence))"
    " )"
    ")"
)

_AUTHORITY_PREDICATE = (
    " AND ((dr.outbox_id IS NULL"
    f" AND {_OUTBOXLESS_NEWER}"
    f" AND {_LATER_WINNING_COMMITTED})"
    " OR (dr.outbox_id IS NOT NULL"
    " AND committed_outbox.outbox_id IS NOT NULL"
    f" AND {_HIGHER_COMMITTED_THAN_CURRENT}"
    f" AND {_OUTBOXLESS_NEWER}))"
)


def _select_unresolved_deliveries(*, since_scoped: bool) -> str:
    """Build one bounded candidate-window authority SELECT.

    The inner CTE applies the unresolved-status, keyset, event-time, ordering,
    and candidate limit predicates *before* any correlated authority checks.
    The outer query then evaluates authority only for that bounded window.
    ``scan_end_sequence`` and ``candidate_count`` let the caller advance across
    a window even when every candidate in it has been superseded.
    """
    status_placeholders = ",".join("?" for _ in UNRESOLVED_RECEIPT_STATUSES)
    since_clause = " AND ce.timestamp >= ?" if since_scoped else ""
    return (
        "WITH candidate_receipts AS ("
        " SELECT dr.sequence, dr.receipt_id, dr.event_id, dr.delivery_plan_id,"
        " dr.target_adapter, dr.target_channel, dr.route_id,"
        " dr.status, dr.error, dr.failure_kind,"
        " dr.attempt_number, dr.next_retry_at, dr.outbox_id,"
        " dr.created_at AS receipt_created_at,"
        " dr.source, dr.replay_run_id,"
        " ce.event_kind, ce.source_adapter, ce.timestamp AS event_timestamp"
        " FROM delivery_receipts dr NOT INDEXED"
        " JOIN canonical_events ce ON ce.event_id = dr.event_id"
        f" WHERE dr.status IN ({status_placeholders})"  # nosec B608 - ? only
        + since_clause
        + " AND dr.sequence > ?"
        " ORDER BY dr.sequence ASC LIMIT ?"
        "), candidate_window AS ("
        " SELECT MAX(sequence) AS scan_end_sequence,"
        " COUNT(*) AS candidate_count"
        " FROM candidate_receipts"
        "), authoritative AS ("
        " SELECT dr.*"
        " FROM candidate_receipts dr"
        " LEFT JOIN delivery_outbox committed_outbox"
        "   ON committed_outbox.outbox_id = dr.outbox_id"
        "  AND committed_outbox.receipt_id = dr.receipt_id"
        " WHERE 1 = 1" + _AUTHORITY_PREDICATE + ")"
        " SELECT dr.sequence AS receipt_sequence,"
        " dr.receipt_id, dr.event_id, dr.delivery_plan_id,"
        " dr.target_adapter, dr.target_channel, dr.route_id,"
        " dr.status, dr.error, dr.failure_kind,"
        " dr.attempt_number, dr.next_retry_at, dr.outbox_id,"
        " dr.receipt_created_at, dr.source, dr.replay_run_id,"
        " dr.event_kind, dr.source_adapter, dr.event_timestamp,"
        " candidate_window.scan_end_sequence, candidate_window.candidate_count"
        " FROM candidate_window"
        " LEFT JOIN authoritative dr ON 1 = 1"
        " ORDER BY dr.sequence ASC"
    )


_SELECT_UNRESOLVED_DELIVERIES = _select_unresolved_deliveries(since_scoped=False)
_SELECT_UNRESOLVED_DELIVERIES_SINCE = _select_unresolved_deliveries(since_scoped=True)


_RECOVERY_CANDIDATE_BATCH_MIN = 64
_RECOVERY_CANDIDATE_BATCH_MULTIPLIER = 4


def _candidate_batch_limit(page_limit: int) -> int:
    """Return a bounded raw-candidate window for one recovery DB read."""
    return max(
        _RECOVERY_CANDIDATE_BATCH_MIN,
        page_limit * _RECOVERY_CANDIDATE_BATCH_MULTIPLIER,
    )


#: Set-oriented outbox enrichment for one page of rows. Outbox state may only
#: enrich disposition/retryability — it is never acceptance evidence.
_SELECT_OUTBOX_FOR_PAGE = """
SELECT outbox_id, status, next_attempt_at
FROM delivery_outbox
WHERE outbox_id IN ({placeholders})
"""


def _validate_page_limit(limit: int) -> int:
    """Validate and normalise a page-size request."""
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise ValueError(f"limit must be an integer, got {limit!r}")
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")
    if limit > MAX_RECOVERY_PAGE_LIMIT:
        raise ValueError(f"limit must be <= {MAX_RECOVERY_PAGE_LIMIT}, got {limit}")
    return limit


class _RecoveryQueryMixin:
    """Unresolved-delivery paging for SQLiteStorage.

    Accesses ``self._read_all`` from the base class via MRO.
    """

    async def query_unresolved_deliveries(
        self,
        *,
        cursor: str | None = None,
        since_event_time: str | None = None,
        limit: int = DEFAULT_RECOVERY_PAGE_LIMIT,
    ) -> UnresolvedDeliveriesPage:
        """Return one bounded page of currently-unresolved deliveries.

        See :meth:`medre.core.storage.backend.StorageBackend.
        query_unresolved_deliveries` for the lineage/current-outcome contract.
        Read-only: SELECTs only.
        """
        page_limit = _validate_page_limit(limit)
        external_after_sequence = decode_page_cursor(cursor) if cursor else 0
        scan_after_sequence = external_after_sequence
        candidate_limit = _candidate_batch_limit(page_limit)
        authoritative_rows: list[dict[str, Any]] = []

        if since_event_time is not None:
            select_sql = _SELECT_UNRESOLVED_DELIVERIES_SINCE
        else:
            select_sql = _SELECT_UNRESOLVED_DELIVERIES

        # One public page may cross several raw-candidate windows when stale
        # failures have been superseded. Each DB read is independently bounded
        # before authority checks, and the loop stops as soon as one look-ahead
        # authoritative row proves ``has_more``.
        while len(authoritative_rows) <= page_limit:
            params: list[Any] = sorted(UNRESOLVED_RECEIPT_STATUSES)
            if since_event_time is not None:
                params.append(since_event_time)
            params.extend((scan_after_sequence, candidate_limit))

            rows = await self._read_all(select_sql, tuple(params))
            if not rows:
                break

            candidate_count = int(rows[0]["candidate_count"] or 0)
            scan_end = rows[0]["scan_end_sequence"]
            authoritative_rows.extend(
                row for row in rows if row["receipt_id"] is not None
            )

            if len(authoritative_rows) > page_limit:
                break
            if candidate_count < candidate_limit or scan_end is None:
                break
            next_scan_after = int(scan_end)
            if next_scan_after <= scan_after_sequence:
                raise RuntimeError(
                    "recovery candidate scan did not advance its sequence keyset"
                )
            scan_after_sequence = next_scan_after

        has_more = len(authoritative_rows) > page_limit
        page_rows = authoritative_rows[:page_limit]

        items = [
            UnresolvedDelivery(
                event_id=row["event_id"],
                event_kind=row["event_kind"],
                source_adapter=row["source_adapter"],
                event_timestamp=row["event_timestamp"],
                delivery_plan_id=row["delivery_plan_id"],
                target_adapter=row["target_adapter"],
                target_channel=row["target_channel"],
                route_id=row["route_id"] or "",
                attempt_source=attempt_source_label(
                    row["source"], row["replay_run_id"]
                ),
                replay_run_id=row["replay_run_id"],
                receipt_id=row["receipt_id"],
                receipt_sequence=int(row["receipt_sequence"]),
                status=row["status"],
                failure_kind=row["failure_kind"],
                error=row["error"],
                attempt_number=int(row["attempt_number"] or 1),
                next_retry_at=row["next_retry_at"],
                receipt_created_at=row["receipt_created_at"],
                outbox_id=row["outbox_id"],
            )
            for row in page_rows
        ]

        # Set-oriented outbox enrichment — one bounded IN () query per page,
        # never per-item lookups. Outbox state only describes disposition and
        # retryability; it is never acceptance evidence.
        outbox_ids = [item.outbox_id for item in items if item.outbox_id]
        if outbox_ids:
            placeholders = ",".join("?" for _ in outbox_ids)
            outbox_rows = await self._read_all(
                _SELECT_OUTBOX_FOR_PAGE.format(placeholders=placeholders),
                tuple(outbox_ids),
            )
            outbox_by_id: dict[str, tuple[str, str | None]] = {
                str(orow["outbox_id"]): (
                    str(orow["status"]),
                    orow["next_attempt_at"],
                )
                for orow in outbox_rows
            }
            items = [
                (
                    replace(
                        item,
                        outbox_status=outbox_by_id[item.outbox_id][0],
                        outbox_next_attempt_at=outbox_by_id[item.outbox_id][1],
                    )
                    if item.outbox_id in outbox_by_id
                    else item
                )
                for item in items
            ]

        next_cursor = (
            encode_page_cursor(items[-1].receipt_sequence)
            if has_more and items
            else None
        )
        return UnresolvedDeliveriesPage(
            items=items,
            limit=page_limit,
            has_more=has_more,
            next_cursor=next_cursor,
        )
