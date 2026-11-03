"""Strictly fenced retirement of deadline-expired, provably unstarted work.

This is the delivery-deadline cleanup transition: a dedicated guarded
terminal transition, deliberately not the general ``mark_outbox_abandoned``
path, because that path accepts claimed ``in_progress`` rows. A claimed or
in-flight pipeline send may sit between preflight authorization and the
transport call without an ``active_attempt`` reservation, so only the exact
``pending``/``retry_wait`` status with no claim, no reservation and no lease
proves a row unsent. The transition runs under ``BEGIN IMMEDIATE`` with
compare-and-set revalidation like the operator decision authority, settles
only the retired row's own target dependency in the same transaction, and
records its refusal summary through the existing outbox diagnostics side
table. It never deletes outbox rows, receipts, observations or native
references.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from medre.core.planning.delivery_plan import DeliveryFailureKind
from medre.core.storage.backend import StorageError
from medre.core.storage.sqlite._content_identity import (
    _utc_microseconds,
    frozen_plan_deadline,
)
from medre.core.storage.sqlite._evidence_diagnostics import outbox_diagnostic_ops
from medre.core.storage.sqlite.connection import _settle_outbox_content_dependency
from medre.core.storage.sqlite.statements import _RETIRE_OUTBOX_SNAPSHOT

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import ParamSpec, TypeVar

    _P = ParamSpec("_P")
    _T = TypeVar("_T")

#: Terminal vocabulary is reused, never invented: retirement is the guarded
#: ``abandoned`` terminal status carrying the existing deadline failure kind.
DEADLINE_RETIREMENT_STATUS: str = "abandoned"
DEADLINE_RETIREMENT_FAILURE_KIND: str = DeliveryFailureKind.DEADLINE_EXCEEDED.value
#: The deadline refusal summary persisted with the terminal transition. The
#: text lands in the append-only diagnostics side table keyed to the row's
#: committed generation, exactly like every other outbox failure summary.
DEADLINE_RETIREMENT_SUMMARY: str = (
    "Effective delivery deadline elapsed before dispatch; unstarted work retired"
)

#: The sweep is bounded per invocation so it can ride a poll cycle without
#: holding the write lock unconditionally.
_DEADLINE_SWEEP_MAX_LIMIT = 500

# No newer authoritative generation may exist for the same event-scoped
# delivery identity: an older generation superseded by replay or a fresh
# allocation stays durable history and is never re-decided here, mirroring
# the claim and operator-decision sibling exclusion.
_NO_NEWER_GENERATION = """NOT EXISTS (
    SELECT 1
      FROM delivery_outbox AS sibling
     WHERE sibling.outbox_id <> delivery_outbox.outbox_id
       AND sibling.event_id = delivery_outbox.event_id
       AND sibling.delivery_plan_id = delivery_outbox.delivery_plan_id
       AND sibling.target_adapter = delivery_outbox.target_adapter
       AND COALESCE(sibling.target_channel, '') =
           COALESCE(delivery_outbox.target_channel, '')
       AND COALESCE(sibling.active_attempt, sibling.attempt_number) >=
           COALESCE(delivery_outbox.active_attempt, delivery_outbox.attempt_number)
)"""

_CANDIDATES_SQL = f"""
    SELECT outbox_id, event_id, attempt_number, status, updated_at, created_at
      FROM delivery_outbox
     WHERE status IN ('pending', 'retry_wait')
       AND active_attempt IS NULL
       AND worker_id IS NULL AND locked_at IS NULL AND lease_until IS NULL
       AND {_NO_NEWER_GENERATION}
       AND (created_at > ? OR (created_at = ? AND outbox_id > ?))
     ORDER BY created_at ASC, outbox_id ASC
     LIMIT ?
"""  # nosec B608 - static SQL fragments and parameter placeholders only

# The compare-and-set revalidates everything the candidate observation saw:
# the exact status, generation and row timestamp, plus the full unclaimed
# proof and sibling exclusion. A mismatch skips the row; nothing is retired
# on stale evidence.
_RETIRE_CAS_SQL = f"""
    UPDATE delivery_outbox
       SET status = '{DEADLINE_RETIREMENT_STATUS}',
           failure_kind = ?,
           failure_kind_detail = NULL,
           next_attempt_at = NULL,
           updated_at = ?,
           locked_at = NULL,
           lease_until = NULL,
           worker_id = NULL,
           error_summary = NULL
     WHERE outbox_id = ?
       AND status = ?
       AND attempt_number = ?
       AND updated_at = ?
       AND active_attempt IS NULL
       AND worker_id IS NULL AND locked_at IS NULL AND lease_until IS NULL
       AND {_NO_NEWER_GENERATION}
"""  # nosec B608 - static SQL fragments and parameter placeholders only


@dataclass(frozen=True, slots=True)
class DeadlineRetirement:
    """Metadata-only summary of one retired outbox generation."""

    outbox_id: str
    event_id: str
    attempt_number: int
    prior_status: str
    effective_deadline: str
    retired_at: str


def _deadline_bounds(
    db: sqlite3.Connection, event_id: str, outbox_id: str
) -> list[str] | None:
    """Return the normalized effective-deadline bounds, or ``None`` to skip.

    The effective deadline is the earlier of the immutable event ceiling and
    the frozen delivery-plan deadline, exactly as every other enforcement
    site computes it. The row's own adopted dependency deadline joins the
    bounds as a defensive floor: it can only be the same instant or earlier
    (adoption takes the minimum of all three), and a row whose own content
    authority already expired is undispatchable and must not churn. A bound
    that cannot be parsed proves nothing, so the row is skipped: retirement
    without proof is refused, never guessed.
    """
    bounds: list[str] = []
    ceiling = db.execute(
        "SELECT hard_ceiling FROM event_content_access_policies WHERE event_id=?",
        (event_id,),
    ).fetchone()
    if ceiling is not None and ceiling[0] is not None:
        try:
            bounds.append(_utc_microseconds(str(ceiling[0]), "Event ceiling"))
        except ValueError:
            return None
    dependency = db.execute(
        "SELECT deadline FROM event_content_dependencies "
        "WHERE event_id=? AND owner_kind='outbox' AND owner_id=?",
        (event_id, outbox_id),
    ).fetchone()
    if dependency is not None and dependency[0] is not None:
        try:
            bounds.append(_utc_microseconds(str(dependency[0]), "Target deadline"))
        except ValueError:
            return None
    snapshot = db.execute(
        "SELECT context_json FROM delivery_outbox_snapshots WHERE outbox_id=?",
        (outbox_id,),
    ).fetchone()
    try:
        if snapshot is not None:
            context = json.loads(str(snapshot[0]))
        else:
            # Rows predating snapshots retain their creation-time dispatch
            # context in the outbox metadata column. Never infer a later plan.
            inline = db.execute(
                "SELECT metadata FROM delivery_outbox WHERE outbox_id=?",
                (outbox_id,),
            ).fetchone()
            context = json.loads(str(inline[0])) if inline is not None else {}
        frozen = frozen_plan_deadline(context)
        if frozen is not None:
            bounds.append(_utc_microseconds(frozen, "Frozen plan deadline"))
    except (ValueError, TypeError):
        return None
    return bounds


def sync_retire_expired_unstarted_outbox(
    db: sqlite3.Connection,
    lock: threading.Lock,
    observed: str | None,
    limit: int,
) -> list[DeadlineRetirement]:
    """Retire provably unstarted work past its effective deadline.

    One ``BEGIN IMMEDIATE`` transaction serializes the sweep with attempt
    reservation, claims, retries and newer-generation creation across every
    SQLite handle. Only rows that are exactly ``pending`` or ``retry_wait``,
    carry no claim, lease or reservation, have no newer authoritative
    generation, and whose effective deadline has elapsed at *observed* are
    retired. The terminal settlement releases the row's own target content
    dependency and retires its frozen dispatch context in the same
    transaction. Already-terminal rows never requalify, so repeated or
    restarted sweeps are idempotent no-ops.
    """
    with lock:
        try:
            db.execute("BEGIN IMMEDIATE")
            if observed is None:
                observed = _utc_microseconds(
                    datetime.now(timezone.utc).isoformat(), "Cleanup clock"
                )
            retired: list[DeadlineRetirement] = []
            # Page in stable admission order, not just the first *limit*
            # pending rows: a durable no-deadline row must never starve a
            # later expired obligation. The public limit bounds transitions;
            # each query also bounds materialized candidate rows.
            last_created = ""
            last_outbox_id = ""
            page_size = min(100, max(25, limit))
            while len(retired) < limit:
                page = db.execute(
                    _CANDIDATES_SQL,
                    (last_created, last_created, last_outbox_id, page_size),
                ).fetchall()
                if not page:
                    break
                for row in page:
                    last_created = str(row["created_at"])
                    last_outbox_id = str(row["outbox_id"])
                    outbox_id = str(row["outbox_id"])
                    event_id = str(row["event_id"])
                    bounds = _deadline_bounds(db, event_id, outbox_id)
                    if not bounds:
                        continue
                    effective = min(bounds)
                    if observed < effective:
                        continue
                    cursor = db.execute(
                        _RETIRE_CAS_SQL,
                        (
                            DEADLINE_RETIREMENT_FAILURE_KIND,
                            observed,
                            outbox_id,
                            str(row["status"]),
                            int(row["attempt_number"]),
                            str(row["updated_at"]),
                        ),
                    )
                    if cursor.rowcount != 1:
                        continue
                    _settle_outbox_content_dependency(db, outbox_id, event_id, observed)
                    db.execute(_RETIRE_OUTBOX_SNAPSHOT, (outbox_id,))
                    for op_sql, op_params in outbox_diagnostic_ops(
                        {
                            "failure_kind_detail": "",
                            "error_summary": DEADLINE_RETIREMENT_SUMMARY,
                        },
                        observed,
                        outbox_id,
                    ):
                        db.execute(op_sql, op_params)
                    retired.append(
                        DeadlineRetirement(
                            outbox_id=outbox_id,
                            event_id=event_id,
                            attempt_number=int(row["attempt_number"]),
                            prior_status=str(row["status"]),
                            effective_deadline=effective,
                            retired_at=observed,
                        )
                    )
                    if len(retired) >= limit:
                        break
            db.commit()
            return retired
        except BaseException:
            db.rollback()
            raise


class _DeadlineCleanupMixin:
    """The strictly fenced deadline cleanup transition for SQLiteStorage."""

    if TYPE_CHECKING:
        _lock: threading.Lock

        def _require_db(self) -> sqlite3.Connection: ...

        async def _run_in_thread(
            self, func: Callable[_P, _T], *args: _P.args, **kwargs: _P.kwargs
        ) -> _T: ...

    async def retire_expired_unstarted_outbox(
        self,
        *,
        now: datetime | None = None,
        limit: int = 50,
    ) -> list[DeadlineRetirement]:
        """Retire deadline-expired, provably unstarted and unclaimed work.

        Authority: **mark** (strictly fenced terminal cleanup transition).
        Candidates are only ``pending``/``retry_wait`` rows with no claim,
        lease or ``active_attempt`` reservation and no newer authoritative
        generation, whose effective earlier-of deadline (event ceiling,
        frozen plan deadline, adopted target deadline) has elapsed. Every
        guard is revalidated by compare-and-set inside one ``BEGIN
        IMMEDIATE`` transaction; the guarded terminal settlement releases
        the row's own target dependency and retires its frozen dispatch
        context in the same commit. Claimed, reserved, ``in_progress`` and
        ``queued`` rows are never retired — a claimed pipeline send may be
        authorized and in flight without a reservation — and no outbox row,
        receipt, observation or native reference is ever deleted.

        Production omits *now* so storage samples UTC after acquiring its
        SQLite transaction; an explicit *now* is available to trusted
        controlled-clock qualification and must be timezone-aware. Returns
        a metadata-only summary of the generations retired by this call;
        an identical repeat sweep retires nothing.
        """
        deadline_observation = (
            None if now is None else _utc_microseconds(now.isoformat(), "Cleanup clock")
        )
        if type(limit) is not int or not 1 <= limit <= _DEADLINE_SWEEP_MAX_LIMIT:
            raise ValueError(
                "The deadline cleanup limit must be an integer between 1 and "
                f"{_DEADLINE_SWEEP_MAX_LIMIT}"
            )
        try:
            return await self._run_in_thread(
                sync_retire_expired_unstarted_outbox,
                self._require_db(),
                self._lock,
                deadline_observation,
                limit,
            )
        except sqlite3.Error as exc:
            raise StorageError(f"Deadline cleanup sweep failed: {exc}") from exc
