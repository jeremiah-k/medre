"""Fenced, auditable operator decisions over one exact delivery leg.

These are storage authorities, not replay execution. Three siblings live
here: ``cancel_unstarted_delivery`` changes one currently unreserved
``pending``/``retry_wait`` leg to ``cancelled``,
``record_exact_target_decision`` records an audited resolve/resume
acknowledgement over one observed exact target without touching the
outbox row, and ``resolve_confirmation_pending`` resolves one observed
``queued`` row — the durable confirmation-pending disposition — to an
explicit operator outcome. None of them can redispatch a send, cancel or
decide active attempts, stage a send, or affect sibling destinations.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from medre.core.delivery_authority import DeliveryIdentity
from medre.core.storage.backend import StorageError
from medre.core.storage.sqlite._evidence_diagnostics import outbox_diagnostic_ops
from medre.core.storage.sqlite.connection import _settle_outbox_content_dependency
from medre.core.storage.sqlite.statements import _RETIRE_OUTBOX_SNAPSHOT

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import ParamSpec, TypeVar

    _P = ParamSpec("_P")
    _T = TypeVar("_T")


class OperatorDecisionRefused(ValueError):
    """Persisted delivery authority differs; no mutation was committed."""


#: Frozen refusal vocabulary. Identical semantics reuse the cancellation
#: wording; decision-specific messages state why the observed state is not
#: decidable. Every refusal leaves storage exactly as observed.
DECISION_ID_REBOUND: str = "Decision ID already binds another request"
EXACT_TARGET_GONE: str = "Exact destination no longer exists"
OBSERVATION_STALE: str = "Stale observed state; nothing decided"
COMPETING_GENERATION: str = "A competing delivery generation exists"
CONCURRENT_DECISION_CHANGE: str = "Concurrent attempt changed; nothing decided"
ACTIVE_NATIVE_UNDECIDABLE: str = (
    "In-progress, queued or uncertain native work is not decidable by this" " authority"
)
TERMINAL_REOPEN_REFUSED: str = (
    "Reopening terminal failed or dead-lettered work requires a separate"
    " send-capable authority"
)
SETTLED_TARGET_UNDECIDABLE: str = (
    "Completed or cancelled delivery work is not decidable by this authority"
)
CONFIRMATION_PENDING_ONLY: str = (
    "Only a queued confirmation-pending row is resolvable by this authority"
)
SETTLED_RECEIPT_CONTRADICTION: str = (
    "A committed sent receipt already settles this delivery"
)
RECEIPT_PROVENANCE_CONTRADICTION: str = (
    "Committed receipt provenance contradicts the delivery identity"
)

#: What an exact-target decision decides. Both actions are audit-only
#: acknowledgements over one unstarted scheduled row; resumption itself
#: always goes through the normal outbox claim/attempt fences.
RESUME_SCHEDULED_ACTION: str = "resume_scheduled"
RESOLVE_UNCERTAIN_ACTION: str = "resolve_uncertain"
EXACT_TARGET_DECISION_ACTIONS: frozenset[str] = frozenset(
    {RESUME_SCHEDULED_ACTION, RESOLVE_UNCERTAIN_ACTION}
)

#: What a confirmation-pending resolution decides. Each resolution is the
#: explicit operator outcome for one observed ``queued`` row — the durable
#: confirmation-pending disposition. ``delivered`` rules the delivery
#: complete (outbox ``sent``), ``failed`` rules it a terminal failure
#: (outbox ``dead_lettered`` with the ``operator_ruling`` failure kind),
#: and ``cancelled`` rules it withdrawn (outbox ``cancelled``). No
#: resolution dispatches a send.
RESOLVE_CONFIRMATION_DELIVERED: str = "delivered"
RESOLVE_CONFIRMATION_FAILED: str = "failed"
RESOLVE_CONFIRMATION_CANCELLED: str = "cancelled"
CONFIRMATION_PENDING_RESOLUTIONS: frozenset[str] = frozenset(
    {
        RESOLVE_CONFIRMATION_DELIVERED,
        RESOLVE_CONFIRMATION_FAILED,
        RESOLVE_CONFIRMATION_CANCELLED,
    }
)

#: The typed, content-free failure fact recorded when the operator rules
#: the delivery failed. It is not a transport classification: the ruling
#: itself is the durable evidence.
OPERATOR_RULING_FAILURE_KIND: str = "operator_ruling"

#: The observed status a confirmation-pending resolution binds: the row
#: must still be in the durable confirmation-pending disposition.
CONFIRMATION_PENDING_STATUS: str = "queued"


def _validate_decision_fields(
    decision_id: str,
    actor_id: str,
    outbox_id: str,
    expected_updated_at: str,
    identity: DeliveryIdentity,
) -> None:
    """Apply the same bounded exact-identity contract to both decision types."""
    for name, value in (
        ("decision_id", decision_id),
        ("actor_id", actor_id),
        ("outbox_id", outbox_id),
        ("expected_updated_at", expected_updated_at),
    ):
        if not isinstance(value, str) or not value.strip() or len(value) > 512:
            raise ValueError(f"{name} must be a non-empty bounded string")
    if not isinstance(identity, DeliveryIdentity) or not identity.complete:
        raise ValueError("A complete event/plan/adapter identity is required")
    for value in (
        identity.event_id,
        identity.delivery_plan_id,
        identity.target_adapter,
    ):
        if not isinstance(value, str) or not value.strip() or len(value) > 512:
            raise ValueError("Delivery identity must contain bounded non-empty strings")
    if identity.target_channel is not None and (
        not isinstance(identity.target_channel, str)
        or len(identity.target_channel) > 512
    ):
        raise ValueError("Delivery channel must be bounded")


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
        _validate_decision_fields(
            self.decision_id,
            self.actor_id,
            self.outbox_id,
            self.expected_updated_at,
            self.identity,
        )
        if self.expected_status not in {"pending", "retry_wait"}:
            raise ValueError("Only pending or retry_wait work can be cancelled")
        if type(self.expected_generation) is not int or self.expected_generation < 0:
            raise ValueError("Expected generation must be nonnegative")


@dataclass(frozen=True, slots=True)
class ExactTargetDecision:
    """An audited operator decision over one observed exact delivery target.

    ``action`` states what is decided (the frozen
    :data:`EXACT_TARGET_DECISION_ACTIONS` vocabulary); the ``expected_*``
    fields carry the observation the write transaction revalidates. The
    decision never stages a send, never creates a delivery generation and
    never mutates the observed outbox row: any resumption goes through the
    normal outbox claim/attempt fences the retry worker already owns. An
    observation or its fingerprint is evidence only; this model is the
    caller's statement of what was observed, and the transaction decides
    whether that statement still holds.
    """

    decision_id: str
    actor_id: str
    action: str
    identity: DeliveryIdentity
    outbox_id: str
    expected_generation: int
    expected_status: str
    expected_updated_at: str

    def __post_init__(self) -> None:
        _validate_decision_fields(
            self.decision_id,
            self.actor_id,
            self.outbox_id,
            self.expected_updated_at,
            self.identity,
        )
        if self.action not in EXACT_TARGET_DECISION_ACTIONS:
            raise ValueError(
                "action must be one of the frozen exact-target decision"
                f" actions: {sorted(EXACT_TARGET_DECISION_ACTIONS)}"
            )
        # The expected status is the operator's observation, not a request:
        # undecidable observed states are refused inside the write
        # transaction with the typed refusal vocabulary, not here.
        if (
            not isinstance(self.expected_status, str)
            or not self.expected_status.strip()
            or len(self.expected_status) > 512
        ):
            raise ValueError("Expected status must be a non-empty bounded string")
        if type(self.expected_generation) is not int or self.expected_generation < 0:
            raise ValueError("Expected generation must be nonnegative")


@dataclass(frozen=True, slots=True)
class ConfirmationPendingResolution:
    """An explicit operator outcome for one confirmation-pending row.

    The observed row is a ``queued`` outbox row — the durable,
    content-free confirmation-pending disposition. The ``resolution``
    names the ruled outcome (the frozen
    :data:`CONFIRMATION_PENDING_RESOLUTIONS` vocabulary); the
    ``expected_*`` fields carry the observation the write transaction
    revalidates. The resolution never dispatches a send and never
    fabricates a receipt: it commits the ruled terminal transition on the
    row itself, and every refusal leaves storage exactly as observed.
    """

    decision_id: str
    actor_id: str
    identity: DeliveryIdentity
    outbox_id: str
    expected_generation: int
    expected_status: str
    expected_updated_at: str
    resolution: str

    def __post_init__(self) -> None:
        _validate_decision_fields(
            self.decision_id,
            self.actor_id,
            self.outbox_id,
            self.expected_updated_at,
            self.identity,
        )
        if self.resolution not in CONFIRMATION_PENDING_RESOLUTIONS:
            raise ValueError(
                "resolution must be one of the frozen confirmation-pending"
                f" resolutions: {sorted(CONFIRMATION_PENDING_RESOLUTIONS)}"
            )
        if self.expected_status != CONFIRMATION_PENDING_STATUS:
            raise ValueError(
                "Only a queued confirmation-pending row can be resolved;"
                f" expected_status must be {CONFIRMATION_PENDING_STATUS!r}"
            )
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
            # The guarded cancellation is a terminal transition: retire the
            # exact target owner here, in the same transaction, like every
            # other terminal path. Siblings and evidence stay untouched.
            _settle_outbox_content_dependency(
                db, decision.outbox_id, identity.event_id, now
            )
            # Operator cancellation is terminal too. Retire private retry
            # context inside this exact guarded transaction, not afterward.
            db.execute(_RETIRE_OUTBOX_SNAPSHOT, (decision.outbox_id,))
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


def _sync_exact_target_decision(
    db: sqlite3.Connection, lock: threading.Lock, decision: ExactTargetDecision
) -> bool:
    """Return True on first commit, False on an identical repeated decision.

    The BEGIN IMMEDIATE lock serializes this acknowledgement with attempt
    reservation, retries and newer-generation creation, even across separate
    SQLite handles. The outbox row is never modified: the acknowledgement is
    the audit row alone, and every refusal leaves storage exactly as
    observed.
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
                decision.action,
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
                    raise OperatorDecisionRefused(DECISION_ID_REBOUND)
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
                raise OperatorDecisionRefused(EXACT_TARGET_GONE)
            status = row["status"]
            # State-family refusals come before the CAS comparison so the
            # refusal names the state, not a stale expectation: terminal
            # failed/dead-lettered work is not reopenable here, completed or
            # cancelled outcomes are settled, and in-progress, queued or
            # uncertain native work is never decided from an observation.
            if status in {"failed", "dead_lettered"}:
                raise OperatorDecisionRefused(TERMINAL_REOPEN_REFUSED)
            if status in {"sent", "cancelled"}:
                raise OperatorDecisionRefused(SETTLED_TARGET_UNDECIDABLE)
            if (
                status not in {"pending", "retry_wait"}
                or row["active_attempt"] is not None
                or any(
                    row[key] is not None
                    for key in ("worker_id", "locked_at", "lease_until")
                )
            ):
                raise OperatorDecisionRefused(ACTIVE_NATIVE_UNDECIDABLE)
            if (
                row["attempt_number"] != decision.expected_generation
                or row["status"] != decision.expected_status
                or row["updated_at"] != decision.expected_updated_at
            ):
                raise OperatorDecisionRefused(OBSERVATION_STALE)
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
                raise OperatorDecisionRefused(COMPETING_GENERATION)
            now = datetime.now(timezone.utc).isoformat()
            # The acknowledgement mutates nothing, so the guarded conditional
            # insert rechecks the full observed state: the audit row can only
            # commit while the outbox row still matches the decision.
            cursor = db.execute(
                "INSERT INTO operator_delivery_decisions "
                "(decision_id,actor_id,event_id,delivery_plan_id,target_adapter,"
                "target_channel,outbox_id,observed_generation,observed_updated_at,"
                "observed_status,action,recorded_at) "
                "SELECT ?,?,?,?,?,?,?,?,?,?,?,? WHERE EXISTS ("
                "SELECT 1 FROM delivery_outbox WHERE outbox_id=? "
                "AND attempt_number=? AND status=? AND updated_at=? "
                "AND active_attempt IS NULL "
                "AND worker_id IS NULL AND locked_at IS NULL AND lease_until IS NULL)",
                (
                    decision.decision_id,
                    *fields,
                    now,
                    decision.outbox_id,
                    decision.expected_generation,
                    decision.expected_status,
                    decision.expected_updated_at,
                ),
            )
            if cursor.rowcount != 1:
                raise OperatorDecisionRefused(CONCURRENT_DECISION_CHANGE)
            db.commit()
            return True
        except BaseException:
            db.rollback()
            raise


def _resolve_target_state(resolution: str) -> tuple[str, str | None]:
    """Map one confirmation-pending resolution to its terminal write.

    ``delivered`` rules the delivery complete (``sent``): the failure fact
    is cleared and the attempt time is stamped, mirroring the guarded
    ``sent`` transition. ``failed`` rules a terminal failure
    (``dead_lettered``) with the typed content-free ``operator_ruling``
    failure kind. ``cancelled`` rules the delivery withdrawn
    (``cancelled``). No resolution stages a send or fabricates a receipt.
    """
    if resolution == RESOLVE_CONFIRMATION_DELIVERED:
        return "sent", None
    if resolution == RESOLVE_CONFIRMATION_FAILED:
        return "dead_lettered", OPERATOR_RULING_FAILURE_KIND
    return "cancelled", None


def _sync_resolve_confirmation_pending(
    db: sqlite3.Connection,
    lock: threading.Lock,
    decision: ConfirmationPendingResolution,
) -> bool:
    """Commit one explicit operator resolution; every refusal writes nothing.

    The BEGIN IMMEDIATE lock serializes this resolution with attempt
    reservation, terminal evidence settlement and newer-generation
    creation, even across separate SQLite handles. The observed row must
    still be in the durable confirmation-pending disposition (``queued``,
    no reservation, no claim ownership) at the observed generation and
    version. A committed ``sent`` receipt at the effective attempt, or a
    receipt whose identity contradicts the claimed delivery, refuses the
    resolution: evidence settlement outranks a ruling, and contradictory
    provenance never grounds one. A resolution is not repeatable — the
    ruled terminal row is immutable.
    """
    identity = decision.identity
    target_status, target_failure_kind = _resolve_target_state(decision.resolution)
    with lock:
        try:
            db.execute("BEGIN IMMEDIATE")
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
                raise OperatorDecisionRefused(EXACT_TARGET_GONE)
            status = row["status"]
            if status in {"sent", "dead_lettered", "cancelled", "abandoned"}:
                raise OperatorDecisionRefused(SETTLED_TARGET_UNDECIDABLE)
            if status != CONFIRMATION_PENDING_STATUS:
                raise OperatorDecisionRefused(CONFIRMATION_PENDING_ONLY)
            if (
                row["attempt_number"] != decision.expected_generation
                or row["status"] != decision.expected_status
                or row["updated_at"] != decision.expected_updated_at
                or row["active_attempt"] is not None
                or any(
                    row[key] is not None
                    for key in ("worker_id", "locked_at", "lease_until")
                )
            ):
                raise OperatorDecisionRefused(OBSERVATION_STALE)
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
                raise OperatorDecisionRefused(COMPETING_GENERATION)
            # Receipt-evidence contradiction refusals. A committed ``sent``
            # receipt at the effective attempt settles the delivery already;
            # a receipt bound to another delivery identity can never ground
            # a ruling. Both leave storage exactly as observed.
            effective_attempt = decision.expected_generation
            receipts = db.execute(
                "SELECT event_id, delivery_plan_id, target_adapter, target_channel, "
                "attempt_number, status FROM delivery_receipts "
                "WHERE outbox_id=? AND attempt_number=?",
                (decision.outbox_id, effective_attempt),
            ).fetchall()
            if any(receipt["status"] == "sent" for receipt in receipts):
                raise OperatorDecisionRefused(SETTLED_RECEIPT_CONTRADICTION)
            if any(
                receipt["event_id"] != identity.event_id
                or receipt["delivery_plan_id"] != identity.delivery_plan_id
                or receipt["target_adapter"] != identity.target_adapter
                or (receipt["target_channel"] or None)
                != (identity.target_channel or None)
                for receipt in receipts
            ):
                raise OperatorDecisionRefused(RECEIPT_PROVENANCE_CONTRADICTION)
            now = datetime.now(timezone.utc).isoformat()
            sets = [
                "status = ?",
                "updated_at = ?",
                "next_attempt_at = NULL",
                "worker_id = NULL",
                "locked_at = NULL",
                "lease_until = NULL",
                "failure_kind = ?",
            ]
            params: list[Any] = [
                target_status,
                now,
                target_failure_kind,
            ]
            if target_status == "sent":
                sets.append("last_attempt_at = ?")
                params.append(now)
            cursor = db.execute(
                f"UPDATE delivery_outbox SET {', '.join(sets)} "
                "WHERE outbox_id=? AND status=? AND attempt_number=? "
                "AND updated_at=? AND active_attempt IS NULL "
                "AND worker_id IS NULL AND locked_at IS NULL "
                "AND lease_until IS NULL",  # nosec B608 - static SQL, ? placeholders only
                (
                    *params,
                    decision.outbox_id,
                    decision.expected_status,
                    decision.expected_generation,
                    decision.expected_updated_at,
                ),
            )
            if cursor.rowcount != 1:
                raise OperatorDecisionRefused(CONCURRENT_DECISION_CHANGE)
            # Terminal settlement duties, in the same transaction, like
            # every other terminal path: release the row's own target
            # dependency and retire the frozen dispatch context. The
            # failure-detail clear keeps a later cancellation from
            # resurrecting cleared text through the read projection.
            _settle_outbox_content_dependency(
                db, decision.outbox_id, identity.event_id, now
            )
            db.execute(_RETIRE_OUTBOX_SNAPSHOT, (decision.outbox_id,))
            for sql, diag_params in outbox_diagnostic_ops(
                {"failure_kind_detail": ""}, now, decision.outbox_id
            ):
                db.execute(sql, diag_params)
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

    async def record_exact_target_decision(self, decision: ExactTargetDecision) -> bool:
        """Record one audited exact-target decision; never dispatch or mutate.

        The observed outbox row is never modified and nothing is staged:
        resumption goes through the normal claim/attempt fences. Returns
        True on first commit and False on an identical repeated decision.
        Refusals raise :class:`OperatorDecisionRefused` with the frozen
        refusal vocabulary, no audit row and no state change.
        """
        try:
            return await self._run_in_thread(
                _sync_exact_target_decision,
                self._require_db(),
                self._lock,
                decision,
            )
        except sqlite3.Error as exc:
            raise StorageError(f"Operator decision write failed: {exc}") from exc

    async def resolve_confirmation_pending(
        self, decision: ConfirmationPendingResolution
    ) -> bool:
        """Resolve one confirmation-pending row to the ruled outcome.

        The explicit operator recovery path for the durable
        confirmation-pending disposition. Commits the ruled terminal
        transition (``sent``/``dead_lettered``/``cancelled``) on one
        observed ``queued`` row; never dispatches a send and never
        fabricates a receipt. Returns True on first commit. Refusals raise
        :class:`OperatorDecisionRefused` with the frozen refusal
        vocabulary, no state change, and no audit row: the ruled terminal
        row is the durable record, so a resolution is not repeatable.
        """
        try:
            return await self._run_in_thread(
                _sync_resolve_confirmation_pending,
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
