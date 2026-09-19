"""Recovery query mixin for SQLiteStorage.

Authority surface:
  - query_unresolved_deliveries: **list/get** (read-only).  Keyset-paginated
    scan over *currently-unresolved* delivery outcomes — the latest receipt
    of each logical delivery whose status is ``failed`` or ``dead_lettered``.

The grouping here must stay identical to the pure Python grouping in
:func:`medre.core.storage.backend.resolve_delivery_outcomes` (used by the
per-event recovery runbook): ``(event_id, delivery_plan_id, target_adapter,
COALESCE(target_channel, ''))``.  ``event_id`` is part of the SQL key
because plan IDs are not guaranteed unique across events; the runbook
scopes by event first, so its key omits it.  Retry and executed-replay
receipts continue the same delivery (the replay lifecycle appends attempts
with ``attempt_number = max(existing) + 1`` and the ``delivery_status``
authority takes the latest receipt without a source filter), so they share
the group; ``replay_run_id`` is selected as per-receipt provenance only.
Dry-run replays append no receipts and can therefore fabricate nothing.

No OFFSET is used and no global COUNT is executed: the page is probed with
``limit + 1`` rows and the last emitted row's ``receipt_sequence`` becomes
the next keyset position (strictly ascending — ``sequence`` is the
AUTOINCREMENT primary key, so positions are unique and stable).
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

#: Latest receipt per logical delivery.  COALESCE folds NULL and ''
#: channel values into one group so a logical delivery never splits in
#: two; replay/retry receipts deliberately share the group (see module
#: docstring).
_LINEAGE_GROUP_BY = """
    GROUP BY event_id,
             delivery_plan_id,
             target_adapter,
             COALESCE(target_channel, '')
"""

_LATEST_RECEIPT_PER_DELIVERY = f"SELECT MAX(sequence) AS max_seq FROM delivery_receipts{_LINEAGE_GROUP_BY}"  # nosec B608 - interpolates only the module-level _LINEAGE_GROUP_BY constant

#: Since-filtered variant.  ``event_id`` is part of the lineage key, so
#: restricting canonical events before aggregation cannot change which
#: receipt is latest inside an included lineage; it only avoids scanning
#: receipt groups that are outside the operator-requested event-time scope.
_LATEST_RECEIPT_PER_DELIVERY_SINCE = """
    SELECT MAX(dr.sequence) AS max_seq
    FROM delivery_receipts dr
    JOIN canonical_events ce_scope ON ce_scope.event_id = dr.event_id
    WHERE ce_scope.timestamp >= ?
    GROUP BY dr.event_id,
             dr.delivery_plan_id,
             dr.target_adapter,
             COALESCE(dr.target_channel, '')
"""


def _select_unresolved_deliveries(latest_receipts_sql: str) -> str:
    """Build the fixed unresolved-delivery SELECT around a lineage subquery."""
    status_placeholders = ",".join("?" for _ in UNRESOLVED_RECEIPT_STATUSES)
    lineage_join = f"JOIN ({latest_receipts_sql}) latest ON dr.sequence = latest.max_seq"  # nosec B608 - latest_receipts_sql is a module-level constant, values parameterized
    return (
        "SELECT dr.sequence AS receipt_sequence,"  # nosec B608 - concatenation of module-level constants and ? placeholder f-strings only; values bound separately
        " dr.receipt_id, dr.event_id, dr.delivery_plan_id,"
        " dr.target_adapter, dr.target_channel, dr.route_id,"
        " dr.status, dr.error, dr.failure_kind,"
        " dr.attempt_number, dr.next_retry_at, dr.outbox_id,"
        " dr.created_at AS receipt_created_at,"
        " dr.source, dr.replay_run_id,"
        " ce.event_kind, ce.source_adapter, ce.timestamp AS event_timestamp"
        " FROM delivery_receipts dr"
        + lineage_join
        + " JOIN canonical_events ce ON ce.event_id = dr.event_id"
        + f" WHERE dr.status IN ({status_placeholders})"  # nosec B608 - status_placeholders is only ? markers, values parameterized
    )


_SELECT_UNRESOLVED_DELIVERIES = _select_unresolved_deliveries(
    _LATEST_RECEIPT_PER_DELIVERY
)
_SELECT_UNRESOLVED_DELIVERIES_SINCE = _select_unresolved_deliveries(
    _LATEST_RECEIPT_PER_DELIVERY_SINCE
)

#: Set-oriented outbox enrichment for one page of rows.  Outbox state may
#: only enrich disposition/retryability — it is never acceptance evidence.
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
        query_unresolved_deliveries` for the lineage/current-outcome
        contract.  Read-only: SELECTs only.
        """
        page_limit = _validate_page_limit(limit)
        after_sequence = decode_page_cursor(cursor) if cursor else None

        # Parameter order follows SQL appearance.  When the event-time
        # scope is present it lives inside the lineage subquery and therefore
        # binds before the outer status placeholders.  The cursor predicate
        # cannot be pushed into that subquery because it is a page position,
        # not a lineage-scope predicate.
        params: list[Any] = []
        if since_event_time is not None:
            select_sql = _SELECT_UNRESOLVED_DELIVERIES_SINCE
            params.append(since_event_time)
        else:
            select_sql = _SELECT_UNRESOLVED_DELIVERIES
        params.extend(sorted(UNRESOLVED_RECEIPT_STATUSES))

        clauses: list[str] = []
        if after_sequence is not None:
            clauses.append("dr.sequence > ?")
            params.append(after_sequence)

        where = (" AND " + " AND ".join(clauses)) if clauses else ""
        sql = (
            f"{select_sql}{where} "
            "ORDER BY dr.sequence ASC LIMIT ?"  # nosec: structure constant
        )
        params.append(page_limit + 1)  # limit+1 probe for has_more

        rows = await self._read_all(sql, tuple(params))

        has_more = len(rows) > page_limit
        page_rows = rows[:page_limit]

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
        # never per-item lookups.  Outbox state only describes disposition
        # and retryability; it is never acceptance evidence.
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
