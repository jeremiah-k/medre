"""Fenced, auditable cancellation of one currently unreserved delivery leg.

This is a storage authority, not replay execution. It cannot discharge an
ambiguous native send, cancel active attempts, or affect sibling destinations.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from medre.core.delivery_authority import DeliveryIdentity
from medre.core.storage.backend import StorageError

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import ParamSpec, TypeVar

    _P = ParamSpec("_P")
    _T = TypeVar("_T")


class OperatorDecisionRefused(ValueError):
    """Persisted delivery authority differs; no mutation was committed."""


@dataclass(frozen=True, slots=True)
class CancelUnstartedDecision:
    decision_id: str
    actor_id: str
    identity: DeliveryIdentity
    outbox_id: str
    expected_generation: int
    expected_status: str
    expected_updated_at: str

    def __post_init__(self) -> None:
        for name in ("decision_id", "actor_id", "outbox_id", "expected_updated_at"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or len(value) > 512:
                raise ValueError(f"{name} must be a non-empty bounded string")
        if (
            not isinstance(self.identity, DeliveryIdentity)
            or not self.identity.complete
        ):
            raise ValueError("A complete event/plan/adapter identity is required")
        for value in (
            self.identity.event_id,
            self.identity.delivery_plan_id,
            self.identity.target_adapter,
        ):
            if not isinstance(value, str) or not value.strip() or len(value) > 512:
                raise ValueError(
                    "Delivery identity must contain bounded non-empty strings"
                )
        if (
            self.identity.target_channel is not None
            and len(self.identity.target_channel) > 512
        ):
            raise ValueError("Delivery channel must be bounded")
        if self.expected_status not in {"pending", "retry_wait"}:
            raise ValueError("Only pending or retry_wait work can be cancelled")
        if type(self.expected_generation) is not int or self.expected_generation < 0:
            raise ValueError("Expected generation must be nonnegative")


def _sync_cancel_unstarted(
    db: sqlite3.Connection, lock: threading.Lock, decision: CancelUnstartedDecision
) -> bool:
    """Return True on first commit, False on an identical repeated decision.

    The BEGIN IMMEDIATE lock serializes this selection with attempt reservation,
    retries and newer-generation creation, even across separate SQLite handles.
    An identical idempotent retry must not re-open or modify the finished row.
    """
    identity = decision.identity
    with lock:
        try:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT * FROM operator_delivery_decisions WHERE decision_id=?",
                (decision.decision_id,),
            ).fetchone()
            fields = (
                decision.actor_id,
                identity.event_id,
                identity.delivery_plan_id,
                identity.target_adapter,
                identity.target_channel,
                decision.outbox_id,
                decision.expected_generation,
                decision.expected_updated_at,
                decision.expected_status,
                "cancel_unstarted",
            )
            if old is not None:
                actual = tuple(
                    old[key]
                    for key in (
                        "actor_id",
                        "event_id",
                        "delivery_plan_id",
                        "target_adapter",
                        "target_channel",
                        "outbox_id",
                        "observed_generation",
                        "observed_updated_at",
                        "observed_status",
                        "action",
                    )
                )
                if actual != fields:
                    raise OperatorDecisionRefused(
                        "Decision ID already binds another request"
                    )
                db.commit()
                return False
            row = db.execute(
                "SELECT event_id, delivery_plan_id, target_adapter, target_channel, "
                "attempt_number, active_attempt, status, updated_at, "
                "worker_id, locked_at, lease_until "
                "FROM delivery_outbox WHERE outbox_id=?",
                (decision.outbox_id,),
            ).fetchone()
            if (
                row is None
                or DeliveryIdentity(
                    row["event_id"],
                    row["delivery_plan_id"],
                    row["target_adapter"],
                    row["target_channel"],
                )
                != identity
            ):
                raise OperatorDecisionRefused("Exact destination no longer exists")
            if (
                row["attempt_number"] != decision.expected_generation
                or row["active_attempt"] is not None
                or any(
                    row[key] is not None
                    for key in ("worker_id", "locked_at", "lease_until")
                )
                or row["status"] != decision.expected_status
                or row["updated_at"] != decision.expected_updated_at
            ):
                raise OperatorDecisionRefused(
                    "Stale generation or active attempt; nothing cancelled"
                )
            # A newly-created generation for the same logical delivery supersedes
            # this snapshot, even if its attempt was never reserved.
            newer = db.execute(
                "SELECT 1 FROM delivery_outbox WHERE event_id=? "
                "AND delivery_plan_id=? AND target_adapter=? "
                "AND COALESCE(target_channel,'')=COALESCE(?,'') "
                "AND outbox_id != ? "
                "AND COALESCE(active_attempt, attempt_number) >= ? LIMIT 1",
                (
                    identity.event_id,
                    identity.delivery_plan_id,
                    identity.target_adapter,
                    identity.target_channel,
                    decision.outbox_id,
                    decision.expected_generation,
                ),
            ).fetchone()
            if newer is not None:
                raise OperatorDecisionRefused("A competing delivery generation exists")
            now = datetime.now(timezone.utc).isoformat()
            cursor = db.execute(
                "UPDATE delivery_outbox SET status='cancelled', updated_at=?, "
                "next_attempt_at=NULL WHERE outbox_id=? AND active_attempt IS NULL "
                "AND attempt_number=? AND status=? AND updated_at=?",
                (
                    now,
                    decision.outbox_id,
                    decision.expected_generation,
                    decision.expected_status,
                    decision.expected_updated_at,
                ),
            )
            if cursor.rowcount != 1:
                raise OperatorDecisionRefused(
                    "Concurrent attempt changed; nothing cancelled"
                )
            db.execute(
                "INSERT INTO operator_delivery_decisions "
                "(decision_id,actor_id,event_id,delivery_plan_id,target_adapter,"
                "target_channel,outbox_id,observed_generation,observed_updated_at,"
                "observed_status,action,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (decision.decision_id, *fields, now),
            )
            db.commit()
            return True
        except BaseException:
            db.rollback()
            raise


class _OperatorDecisionMixin:
    if TYPE_CHECKING:
        _lock: threading.Lock

        def _require_db(self) -> sqlite3.Connection: ...

        async def _run_in_thread(
            self, func: Callable[_P, _T], *args: _P.args, **kwargs: _P.kwargs
        ) -> _T: ...

        async def _read_all(
            self, sql: str, params: tuple[Any, ...] = ()
        ) -> list[dict[str, Any]]: ...

    async def cancel_unstarted_delivery(
        self, decision: CancelUnstartedDecision
    ) -> bool:
        """Atomically cancel one currently unreserved leg; no native I/O."""
        try:
            return await self._run_in_thread(
                _sync_cancel_unstarted,
                self._require_db(),
                self._lock,
                decision,
            )
        except sqlite3.Error as exc:
            raise StorageError(f"Operator decision write failed: {exc}") from exc

    async def list_operator_decisions(self, event_id: str) -> list[dict[str, Any]]:
        """Read immutable operator evidence without message content."""
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("A non-empty event identifier is required")
        return await self._read_all(
            "SELECT * FROM operator_delivery_decisions WHERE event_id=? "
            "ORDER BY recorded_at, decision_id",
            (event_id,),
        )
