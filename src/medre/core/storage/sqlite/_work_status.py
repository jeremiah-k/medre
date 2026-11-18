"""Work-status aggregation mixin for SQLiteStorage.

Authority surface:
  - query_work_status_summary: **list/get** (read-only). Bounded
    aggregation of the operator work-status projection over one
    consistent read snapshot.

The current-generation selection implements the same shared rule as
:mod:`medre.core.delivery_authority`: per ``(event_id, delivery_plan_id,
target_adapter, COALESCE(target_channel, ''))`` identity, the newest
operational generation wins, ranked by effective generation
(``active_attempt`` when present, else the finalized ``attempt_number``,
with stored zeros never counting as a generation), then ``updated_at``,
``created_at``, and ``outbox_id``. Retained non-current generations stay
in the retained-history dimension and never enter the category
partition.

All aggregation happens in SQL inside one deferred read transaction; no
Python-level row load or per-identity detail query exists. The category,
anchor, and origin ``CASE`` expressions replicate
:func:`medre.core.observability.work_status.work_status_category` and
:func:`medre.core.observability.work_status.outstanding_age_origin`
branch for branch and are checked against those pure functions by
conformance tests.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from medre.core.storage.sqlite.constants import STALE_QUEUED_GRACE_SECONDS

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable
    from typing import ParamSpec, TypeVar

    _P = ParamSpec("_P")
    _T = TypeVar("_T")

#: Effective generation with the shared ``or`` semantics: a stored zero
#: finalized counter (never-dispatched work) is not a generation rank.
_EFFECTIVE_GENERATION = (
    "COALESCE(NULLIF(o.active_attempt, 0), NULLIF(o.attempt_number, 0), 1)"
)

_IDENTITY_PARTITION = (
    "PARTITION BY o.event_id, o.delivery_plan_id, o.target_adapter,"
    " COALESCE(o.target_channel, '')"
)

_ADAPTER_FILTER = "WHERE o.target_adapter = :adapter"

_CURRENT_GENERATIONS = f"""
WITH ranked AS (
    SELECT o.outbox_id, o.event_id, o.delivery_plan_id, o.target_adapter,
           o.target_channel, o.status, o.attempt_number, o.active_attempt,
           o.next_attempt_at, o.created_at, o.updated_at, o.locked_at,
           o.lease_until,
           {_EFFECTIVE_GENERATION} AS effective_generation,
           ROW_NUMBER() OVER (
               {_IDENTITY_PARTITION}
               ORDER BY {_EFFECTIVE_GENERATION} DESC,
                        o.updated_at DESC, o.created_at DESC, o.outbox_id DESC
           ) AS identity_rank
    FROM delivery_outbox o
    {{filter}}
),
current_gen AS (
    SELECT * FROM ranked WHERE identity_rank = 1
)
"""

_CATEGORY_CASE = """
CASE
    WHEN status = 'in_progress' THEN 'reserved_active'
    WHEN status = 'queued' THEN 'native_queue'
    WHEN status = 'abandoned' THEN 'uncertain_outcome'
    WHEN status IN ('sent', 'dead_lettered', 'cancelled')
        THEN 'terminal_outcome'
    WHEN status = 'retry_wait' AND next_attempt_at IS NOT NULL
         AND next_attempt_at > :sampled_at
        THEN 'retry_scheduled'
    WHEN status IN ('pending', 'retry_wait') THEN 'unstarted_eligible'
    ELSE 'unrecognized'
END
"""

_ANCHOR_CASE = """
CASE
    WHEN status = 'retry_wait' AND next_attempt_at IS NOT NULL
        THEN next_attempt_at
    WHEN status = 'queued' THEN COALESCE(updated_at, created_at)
    WHEN status = 'in_progress' AND locked_at IS NOT NULL THEN locked_at
    ELSE COALESCE(created_at, updated_at)
END
"""

_ORIGIN_CASE = """
CASE
    WHEN status = 'retry_wait' AND next_attempt_at IS NOT NULL
        THEN 'retry_due'
    WHEN status = 'queued' THEN 'queue_admission'
    WHEN status = 'in_progress' AND locked_at IS NOT NULL THEN 'claim'
    ELSE 'admission'
END
"""

_OUTSTANDING_PREDICATE = "status IN ('pending', 'retry_wait', 'in_progress', 'queued')"

_SELECT_ADAPTER_SUMMARY = (
    _CURRENT_GENERATIONS
    + f"""
SELECT target_adapter,
       COUNT(*) AS current_generations,
       SUM(CASE WHEN ({_CATEGORY_CASE}) = 'unstarted_eligible'
                THEN 1 ELSE 0 END) AS unstarted_eligible,
       SUM(CASE WHEN ({_CATEGORY_CASE}) = 'retry_scheduled'
                THEN 1 ELSE 0 END) AS retry_scheduled,
       SUM(CASE WHEN ({_CATEGORY_CASE}) = 'reserved_active'
                THEN 1 ELSE 0 END) AS reserved_active,
       SUM(CASE WHEN ({_CATEGORY_CASE}) = 'native_queue'
                THEN 1 ELSE 0 END) AS native_queue,
       SUM(CASE WHEN ({_CATEGORY_CASE}) = 'uncertain_outcome'
                THEN 1 ELSE 0 END) AS uncertain_outcome,
       SUM(CASE WHEN ({_CATEGORY_CASE}) = 'terminal_outcome'
                THEN 1 ELSE 0 END) AS terminal_outcome,
       SUM(CASE WHEN ({_CATEGORY_CASE}) = 'unrecognized'
                THEN 1 ELSE 0 END) AS unrecognized,
       SUM(CASE WHEN status = 'in_progress' AND active_attempt IS NOT NULL
                AND NOT EXISTS (
                    SELECT 1 FROM delivery_receipts r
                    WHERE r.outbox_id = current_gen.outbox_id
                      AND r.attempt_number = current_gen.active_attempt
                )
                THEN 1 ELSE 0 END) AS reserved_without_generation_receipt,
       SUM(CASE WHEN status = 'in_progress'
                AND (lease_until IS NULL OR lease_until <= :sampled_at)
                THEN 1 ELSE 0 END) AS lease_missing_or_expired,
       SUM(CASE WHEN status = 'queued' AND updated_at <= :stale_cutoff
                THEN 1 ELSE 0 END) AS stale_queue_reclaimable
FROM current_gen
GROUP BY target_adapter
ORDER BY target_adapter
"""
)

_SELECT_OLDEST_OUTSTANDING = (
    _CURRENT_GENERATIONS
    + f""",
classified AS (
    SELECT outbox_id, event_id, delivery_plan_id, target_adapter,
           target_channel, status, attempt_number, active_attempt,
           ({_CATEGORY_CASE}) AS category,
           ({_ANCHOR_CASE}) AS age_anchor,
           ({_ORIGIN_CASE}) AS age_origin
    FROM current_gen
),
outstanding AS (
    SELECT * FROM classified
    WHERE ({_OUTSTANDING_PREDICATE})
),
oldest AS (
    SELECT *, ROW_NUMBER() OVER (
        PARTITION BY target_adapter
        ORDER BY age_anchor ASC, outbox_id ASC
    ) AS oldest_rank
    FROM outstanding
)
SELECT target_adapter, outbox_id, event_id, delivery_plan_id,
       target_channel, status, attempt_number, active_attempt,
       category, age_anchor, age_origin
FROM oldest
WHERE oldest_rank = 1
ORDER BY target_adapter
"""
)


def _select_raw_status(adapter: str | None) -> str:
    """All retained outbox rows grouped by status, optionally scoped."""
    filter_sql = "WHERE target_adapter = :adapter" if adapter else ""
    return f"""
SELECT status, COUNT(*) AS rows FROM delivery_outbox {filter_sql}
GROUP BY status ORDER BY status
""".strip()


def _select_current_total(adapter: str | None) -> str:
    """Count current generations, optionally scoped to one adapter."""
    filter_sql = _ADAPTER_FILTER if adapter else ""
    return (
        _CURRENT_GENERATIONS.format(filter=filter_sql)
        + """
SELECT COUNT(*) AS current_generations FROM current_gen
""".strip()
    )


def _select_receipts_by_status(adapter: str | None) -> str:
    """Receipt counts by status over retained receipts, optionally scoped."""
    filter_sql = "WHERE target_adapter = :adapter" if adapter else ""
    return f"""
SELECT status, COUNT(*) AS receipts FROM delivery_receipts {filter_sql}
GROUP BY status ORDER BY status
""".strip()


class _WorkStatusMixin:
    """Work-status aggregation for SQLiteStorage.

    Accesses ``self._read_batch`` from the base class via MRO.
    """

    if TYPE_CHECKING:

        _lock: threading.Lock

        async def _run_in_thread(
            self, func: Callable[_P, _T], *args: _P.args, **kwargs: _P.kwargs
        ) -> _T: ...

        async def _read_batch(
            self, statements: list[tuple[str, tuple[Any, ...] | dict[str, Any]]]
        ) -> list[list[dict[str, Any]]]: ...

    async def query_work_status_summary(
        self,
        *,
        sampled_at: str,
        adapter: str | None = None,
    ) -> dict[str, Any]:
        """Aggregate the work-status projection over one read snapshot.

        Read-only: SELECTs only, inside one deferred read transaction.
        ``adapter`` scopes every aggregate to one target adapter; the
        report states that scope rather than zero-filling the world. The
        returned mapping carries raw aggregate rows for
        :func:`medre.core.observability.work_status.
        assemble_work_status_report`.

        ``sampled_at`` is the caller's ISO-8601 UTC instant; all due-time
        and lease comparisons compare stored ISO strings against it, so
        the report's sampled time is exactly the comparison time.
        """
        parsed = datetime.fromisoformat(sampled_at)
        stale_cutoff = (
            parsed - timedelta(seconds=STALE_QUEUED_GRACE_SECONDS)
        ).isoformat()

        cte_filter = _ADAPTER_FILTER if adapter else ""
        base_params: dict[str, Any] = {
            "sampled_at": sampled_at,
            "stale_cutoff": stale_cutoff,
        }
        if adapter is not None:
            base_params["adapter"] = adapter

        statements: list[tuple[str, tuple[Any, ...] | dict[str, Any]]] = [
            (_SELECT_ADAPTER_SUMMARY.replace("{filter}", cte_filter), base_params),
            (_SELECT_OLDEST_OUTSTANDING.replace("{filter}", cte_filter), base_params),
            (_select_raw_status(adapter), base_params),
            (_select_current_total(adapter), base_params),
            (_select_receipts_by_status(adapter), base_params),
        ]
        (
            adapter_rows,
            oldest_rows,
            raw_status_rows,
            current_total_rows,
            receipt_rows,
        ) = await self._read_batch(statements)

        return {
            "adapter_rows": adapter_rows,
            "oldest_rows": oldest_rows,
            "raw_status_rows": raw_status_rows,
            "current_generations_total": (
                int(current_total_rows[0]["current_generations"])
                if current_total_rows
                else 0
            ),
            "receipts_by_status": {
                str(row["status"]): int(row["receipts"]) for row in receipt_rows
            },
        }
