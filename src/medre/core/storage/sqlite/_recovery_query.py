"""Recovery query mixin for SQLiteStorage.

Authority surface:
  - query_unresolved_deliveries: **list/get** (read-only). Keyset-paginated
    scan over *currently-unresolved* delivery outcomes — the
    lifecycle-authoritative receipt of each logical delivery whose status is
    ``failed`` or ``dead_lettered``.

The lineage predicate here must stay identical to the pure Python grouping in
:func:`medre.core.storage.backend.resolve_delivery_outcomes` (used by the
per-event recovery runbook): ``(event_id, delivery_plan_id, target_adapter,
COALESCE(target_channel, ''))``. ``event_id`` is part of the SQL key because
plan IDs are not guaranteed unique across events; the runbook scopes by event
first, so its key omits it. Retry and executed-replay receipts continue the
same delivery, so ``source`` and ``replay_run_id`` are receipt provenance only.

The scan starts from unresolved authoritative receipt candidates and rejects a
candidate when any later authoritative receipt exists in the same lineage.
Outbox-backed receipts are authoritative only when the outbox row points at
their ``receipt_id``; stale-worker appends remain history. This avoids
re-aggregating every receipt lineage for every page. No OFFSET or global COUNT is used: the page is
probed with ``limit + 1`` rows and the last emitted row's ``receipt_sequence``
becomes the next keyset position (strictly ascending — ``sequence`` is the
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

#: Correlated latest-receipt check. NULL and empty target channels are one
#: lineage; replay provenance deliberately is not part of the key.
_AUTHORITATIVE_RECEIPT = """
(
    {alias}.outbox_id IS NULL
    OR EXISTS (
        SELECT 1
        FROM delivery_outbox authoritative_outbox
        WHERE authoritative_outbox.outbox_id = {alias}.outbox_id
          AND authoritative_outbox.receipt_id = {alias}.receipt_id
    )
)
"""

_NO_LATER_RECEIPT = """
NOT EXISTS (
    SELECT 1
    FROM delivery_receipts newer
    WHERE newer.event_id = dr.event_id
      AND newer.delivery_plan_id = dr.delivery_plan_id
      AND newer.target_adapter = dr.target_adapter
      AND COALESCE(newer.target_channel, '') = COALESCE(dr.target_channel, '')
      AND newer.sequence > dr.sequence
      AND {newer_authoritative}
)
""".format(  # nosec B608 - interpolates only the module-local fragment above; values stay bound parameters
    newer_authoritative=_AUTHORITATIVE_RECEIPT.format(alias="newer")
)


def _select_unresolved_deliveries(*, since_scoped: bool) -> str:
    """Build the fixed unresolved-delivery SELECT.

    Only ``?`` placeholder text and module-local SQL fragments are interpolated;
    all values remain bound parameters.
    """
    status_placeholders = ",".join("?" for _ in UNRESOLVED_RECEIPT_STATUSES)
    since_clause = " AND ce.timestamp >= ?" if since_scoped else ""
    return (
        "SELECT dr.sequence AS receipt_sequence,"  # nosec B608 - values bound
        " dr.receipt_id, dr.event_id, dr.delivery_plan_id,"
        " dr.target_adapter, dr.target_channel, dr.route_id,"
        " dr.status, dr.error, dr.failure_kind,"
        " dr.attempt_number, dr.next_retry_at, dr.outbox_id,"
        " dr.created_at AS receipt_created_at,"
        " dr.source, dr.replay_run_id,"
        " ce.event_kind, ce.source_adapter, ce.timestamp AS event_timestamp"
        " FROM delivery_receipts dr"
        " JOIN canonical_events ce ON ce.event_id = dr.event_id"
        f" WHERE dr.status IN ({status_placeholders})"  # nosec B608 - ? only
        + since_clause
        + " AND "
        + _AUTHORITATIVE_RECEIPT.format(alias="dr")
        + " AND "
        + _NO_LATER_RECEIPT
    )


_SELECT_UNRESOLVED_DELIVERIES = _select_unresolved_deliveries(since_scoped=False)
_SELECT_UNRESOLVED_DELIVERIES_SINCE = _select_unresolved_deliveries(since_scoped=True)

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
        after_sequence = decode_page_cursor(cursor) if cursor else None

        # Parameter order follows SQL appearance: unresolved statuses, optional
        # canonical event-time scope, optional keyset position, then limit+1.
        params: list[Any] = sorted(UNRESOLVED_RECEIPT_STATUSES)
        if since_event_time is not None:
            select_sql = _SELECT_UNRESOLVED_DELIVERIES_SINCE
            params.append(since_event_time)
        else:
            select_sql = _SELECT_UNRESOLVED_DELIVERIES

        if after_sequence is not None:
            select_sql += " AND dr.sequence > ?"
            params.append(after_sequence)

        sql = select_sql + " ORDER BY dr.sequence ASC LIMIT ?"
        params.append(page_limit + 1)

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
