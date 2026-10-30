"""Atomic radio checkpoints in the existing SQLiteStorage connection.

The table owns immutable wire plans and guarded operational part progress, not
receipt/outbox lifecycle states. Nothing here calls a transport or authorizes
replay of an unknown handoff. Every writer checks the full current outbox claim.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

import msgspec

from medre.core.events.delivery import DeliveryAttemptProvenance
from medre.core.rendering.radio_progress import RadioAttemptProgress, RadioPartProgress
from medre.core.rendering.radio_text import RadioTextPlan
from medre.core.storage.backend import StorageError
from medre.core.storage.radio import RadioAttemptMetadata, StoredRadioAttempt
from medre.core.storage.radio_handoff import RadioLocalHandoff
from medre.core.storage.sqlite.serde import _now_iso

if TYPE_CHECKING:
    import threading

_P = ParamSpec("_P")
_T = TypeVar("_T")


def _json(value: object) -> str:
    """Encode deterministic private state without depending on mapping order."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _provenance(value: DeliveryAttemptProvenance) -> str:
    """Retain the complete immutable claim, including replay/source lineage."""
    return _json(msgspec.to_builtins(value))


def _delivery_identity(claim: DeliveryAttemptProvenance) -> tuple[str, str, str, str]:
    """Compare delivery scope across retries while preserving attempt-local lineage."""
    return (
        claim.event_id,
        claim.delivery_plan_id,
        claim.target_adapter,
        claim.target_channel or "",
    )


def _decode(row: Mapping[str, Any]) -> StoredRadioAttempt:
    """Validate stored evidence without treating an active read as a restart.

    Only the immutable initial plan uses the public recovery loader. Current
    part records are decoded separately; explicit recovery owns the transition
    from a persisted submission to unknown.
    """
    try:
        claim = msgspec.json.decode(
            row["provenance_json"], type=DeliveryAttemptProvenance
        )
        baseline = RadioAttemptProgress.from_data(
            json.loads(row["plan_json"]),
            expected_outbox_id=row["outbox_id"],
            expected_attempt_number=row["attempt_number"],
            expected_plan_fingerprint=row["plan_fingerprint"],
        )
        if any(part.state != "unsent" for part in baseline.parts):
            raise ValueError("plan binding is not an initial snapshot")
        raw_parts = json.loads(row["parts_json"])
        if not isinstance(raw_parts, list) or len(raw_parts) != len(baseline.parts):
            raise ValueError("progress does not cover the immutable plan")
        if any(
            not isinstance(part, dict)
            or set(part) != {"ordinal", "state", "handoff_id"}
            for part in raw_parts
        ):
            raise ValueError("progress part fields are incomplete or unknown")
        parts = tuple(RadioPartProgress(**part) for part in raw_parts)
        progress = RadioAttemptProgress(
            baseline.outbox_id, baseline.attempt_number, baseline.plan, parts
        )
        return StoredRadioAttempt(claim, progress, row["revision"])
    except (ValueError, TypeError, KeyError, msgspec.DecodeError) as exc:
        raise StorageError("Retained radio attempt evidence is invalid") from exc


def _accepted_prefix(
    saved: StoredRadioAttempt,
    history: Mapping[tuple[str, int], StoredRadioAttempt],
) -> tuple[RadioPartProgress, ...]:
    """Resolve inherited facts to their original accepted rows; refuse uncertainty.

    Origins are flattened on transfer. Each must identify an actual original
    acceptance in this delivery's history, with the same ordinal and native
    fact. A serialized origin alone cannot manufacture prior acceptance.
    """
    prefix = []
    for part in saved.progress.parts:
        if part.state in {"submitting", "unknown"}:
            raise ValueError("uncertain radio handoffs cannot be continued")
        if part.state == "unsent":
            continue
        evidence = RadioLocalHandoff.from_part(part, saved.provenance)
        if evidence.origin is None or evidence.payload_fingerprint is None:
            raise ValueError("radio handoff lacks continuation payload evidence")
        origin = history.get(
            (evidence.origin.outbox_id, evidence.origin.attempt_number)
        )
        if (
            origin is None
            or origin.provenance.attempt_number > saved.provenance.attempt_number
        ):
            raise ValueError("radio handoff origin is absent or newer than its owner")
        original = RadioLocalHandoff.from_part(
            origin.progress.parts[part.ordinal - 1], origin.provenance
        )
        if original != evidence or (
            original.origin is None
            or original.origin.outbox_id != origin.provenance.outbox_id
            or original.origin.attempt_number != origin.provenance.attempt_number
        ):
            raise ValueError("radio handoff origin contradicts original acceptance")
        prefix.append(RadioPartProgress(part.ordinal, "accepted", evidence.encode()))
    return tuple(prefix)


def _owns(
    db: sqlite3.Connection,
    claim: DeliveryAttemptProvenance,
    worker_id: str | None,
) -> bool:
    """Require an active, unexpired dispatch claim and fence newer generations.

    Once the parent enters queued it has relinquished its worker reservation;
    a checkpoint must never resurrect native submission authority from that row.
    """
    row = db.execute(
        """SELECT * FROM delivery_outbox AS current
           WHERE outbox_id = ? AND worker_id IS ?
             AND status = 'in_progress'
             AND (lease_until IS NULL OR lease_until > ?)
             AND COALESCE(active_attempt, attempt_number) = ?
             AND NOT EXISTS (
                 SELECT 1 FROM delivery_outbox AS sibling
                  WHERE sibling.outbox_id <> current.outbox_id
                    AND sibling.event_id = current.event_id
                    AND sibling.delivery_plan_id = current.delivery_plan_id
                    AND sibling.target_adapter = current.target_adapter
                    AND COALESCE(sibling.target_channel, '') = COALESCE(current.target_channel, '')
                    AND COALESCE(sibling.active_attempt, sibling.attempt_number) >= ?
             )""",
        (
            claim.outbox_id,
            worker_id,
            _now_iso(),
            claim.attempt_number,
            claim.attempt_number,
        ),
    ).fetchone()
    return row is not None and (
        row["event_id"],
        row["delivery_plan_id"],
        row["target_adapter"],
        row["target_channel"] or "",
        row["dispatch_source"],
        row["replay_run_id"],
    ) == (
        claim.event_id,
        claim.delivery_plan_id,
        claim.target_adapter,
        claim.target_channel or "",
        claim.source,
        claim.replay_run_id,
    )


class _RadioMixin:
    """Persist attempt-local facts using the storage owner's executor and lock."""

    if TYPE_CHECKING:
        _lock: threading.Lock

        def _require_db(self) -> sqlite3.Connection: ...

        async def _run_in_thread(
            self, func: Callable[_P, _T], *args: _P.args, **kwargs: _P.kwargs
        ) -> _T: ...

        async def _read_one(
            self, sql: str, params: tuple[Any, ...] = ()
        ) -> dict[str, Any] | None: ...

    async def _radio_transaction(
        self, operation: Callable[[sqlite3.Connection], _T]
    ) -> _T:
        """Keep claim validation and its checkpoint change in one owned transaction."""
        db = self._require_db()

        def write() -> _T:
            with self._lock:
                db.execute("BEGIN IMMEDIATE")
                try:
                    result = operation(db)
                    db.commit()
                    return result
                except BaseException:
                    db.rollback()
                    raise

        try:
            return await self._run_in_thread(write)
        except sqlite3.Error as exc:
            raise StorageError("Radio checkpoint transaction failed") from exc

    async def bind_radio_attempt(
        self,
        provenance: DeliveryAttemptProvenance,
        worker_id: str | None,
        plan: RadioTextPlan,
    ) -> StoredRadioAttempt | None:
        """Bind a wire plan to the current reserved attempt before native work.

        A duplicate binding grants no new submission ownership. Another attempt
        cannot erase prior native progress or replace an already bound wire plan.
        No receipt, generation, lease or parent outbox status is changed.
        """
        return await self._bind_radio_attempt(provenance, worker_id, plan)

    async def resume_radio_attempt(
        self,
        provenance: DeliveryAttemptProvenance,
        worker_id: str | None,
        plan: RadioTextPlan,
        predecessor: StoredRadioAttempt,
    ) -> StoredRadioAttempt | None:
        """Explicitly transfer a stable accepted prefix into a newer owned claim.

        Normal binding never selects this operation. The exact predecessor must
        be the latest stored attempt for this delivery, with an accepted prefix
        and a wholly unsent tail. Old facts and parent lifecycle remain unchanged.
        """
        if not isinstance(predecessor, StoredRadioAttempt):
            raise ValueError("radio continuation requires a typed predecessor")
        return await self._bind_radio_attempt(
            provenance, worker_id, plan, predecessor=predecessor
        )

    async def _bind_radio_attempt(
        self,
        provenance: DeliveryAttemptProvenance,
        worker_id: str | None,
        plan: RadioTextPlan,
        *,
        predecessor: StoredRadioAttempt | None = None,
    ) -> StoredRadioAttempt | None:
        """Bind a fresh claim, optionally inheriting independently retained facts."""
        if provenance.outbox_id is None:
            raise ValueError("radio plans require a durable outbox claim")
        progress = RadioAttemptProgress.bind(
            outbox_id=provenance.outbox_id,
            attempt_number=provenance.attempt_number,
            plan=plan,
        )

        def bind(db: sqlite3.Connection) -> StoredRadioAttempt | None:
            if not _owns(db, provenance, worker_id):
                return None
            prior = db.execute(
                """SELECT progress.* FROM radio_attempt_progress AS progress
                   JOIN delivery_outbox AS parent ON parent.outbox_id=progress.outbox_id
                   WHERE parent.event_id=? AND parent.delivery_plan_id=?
                     AND parent.target_adapter=?
                     AND COALESCE(parent.target_channel, '')=?""",
                _delivery_identity(provenance),
            ).fetchall()
            history = tuple(_decode(row) for row in prior)
            for saved in history:
                if _delivery_identity(saved.provenance) != _delivery_identity(
                    provenance
                ):
                    raise StorageError(
                        "Retained radio attempt delivery identity contradicts its parent"
                    )
                if saved.progress.plan != plan:
                    raise StorageError(
                        "Radio wire plan contradicts its existing binding"
                    )
                if saved.provenance == provenance:
                    return None
                if saved.provenance.attempt_number >= provenance.attempt_number:
                    return None
            bound = progress
            if predecessor is None:
                if any(
                    part.state != "unsent"
                    for saved in history
                    for part in saved.progress.parts
                ):
                    return None
            else:
                if (
                    not history
                    or max(history, key=lambda saved: saved.provenance.attempt_number)
                    != predecessor
                ):
                    return None
                records = {
                    (saved.provenance.outbox_id, saved.provenance.attempt_number): saved
                    for saved in history
                }
                try:
                    prefix = _accepted_prefix(predecessor, records)
                    if not 0 < len(prefix) < len(plan.parts):
                        return None
                    for saved in history:
                        older = _accepted_prefix(saved, records)
                        if older != prefix[: len(older)]:
                            return None
                    bound = RadioAttemptProgress(
                        progress.outbox_id,
                        progress.attempt_number,
                        plan,
                        prefix + progress.parts[len(prefix) :],
                    )
                except (ValueError, TypeError, msgspec.DecodeError) as exc:
                    raise StorageError(
                        "Radio continuation evidence is invalid"
                    ) from exc
            revision = 2 * sum(part.state == "accepted" for part in bound.parts)
            data = progress.to_data()
            now = _now_iso()
            db.execute(
                """INSERT INTO radio_attempt_progress
                   (outbox_id, attempt_number, plan_fingerprint, plan_json,
                    provenance_json, parts_json, revision, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    provenance.outbox_id,
                    provenance.attempt_number,
                    plan.fingerprint,
                    _json(data),
                    _provenance(provenance),
                    _json(bound.to_data()["parts"]),
                    revision,
                    now,
                    now,
                ),
            )
            return StoredRadioAttempt(provenance, bound, revision)

        return await self._radio_transaction(bind)

    async def _checkpoint_radio_attempt(
        self,
        expected: StoredRadioAttempt,
        worker_id: str | None,
        updated: RadioAttemptProgress,
    ) -> StoredRadioAttempt | None:
        """CAS only an exact active checkpoint; accepted native facts never regress."""
        claim = expected.provenance

        def checkpoint(db: sqlite3.Connection) -> StoredRadioAttempt | None:
            if not _owns(db, claim, worker_id):
                return None
            row = db.execute(
                "SELECT * FROM radio_attempt_progress WHERE outbox_id=? AND attempt_number=?",
                (claim.outbox_id, claim.attempt_number),
            ).fetchone()
            if row is None or _decode(row) != expected:
                return None
            result = StoredRadioAttempt(claim, updated, expected.revision + 1)
            count = db.execute(
                """UPDATE radio_attempt_progress SET parts_json=?, revision=?, updated_at=?
                   WHERE outbox_id=? AND attempt_number=? AND revision=?""",
                (
                    _json(updated.to_data()["parts"]),
                    result.revision,
                    _now_iso(),
                    claim.outbox_id,
                    claim.attempt_number,
                    expected.revision,
                ),
            ).rowcount
            return result if count == 1 else None

        return await self._radio_transaction(checkpoint)

    async def begin_radio_part(
        self,
        expected: StoredRadioAttempt,
        worker_id: str | None,
        ordinal: int,
    ) -> StoredRadioAttempt | None:
        """Commit the next native-call boundary; only a returned grant permits send."""
        return await self._checkpoint_radio_attempt(
            expected, worker_id, expected.progress.begin(ordinal)
        )

    async def observe_radio_part(
        self,
        expected: StoredRadioAttempt,
        worker_id: str | None,
        ordinal: int,
        *,
        handoff_id: str | None = None,
    ) -> StoredRadioAttempt | None:
        """Retain local acceptance or explicit uncertainty under the same claim/CAS."""
        return await self._checkpoint_radio_attempt(
            expected,
            worker_id,
            expected.progress.observe(ordinal, handoff_id=handoff_id),
        )

    async def recover_radio_attempt(
        self,
        provenance: DeliveryAttemptProvenance,
        worker_id: str | None,
    ) -> StoredRadioAttempt | None:
        """Persist unknown before exposing any interrupted submission after recovery."""

        def recover(db: sqlite3.Connection) -> StoredRadioAttempt | None:
            if not _owns(db, provenance, worker_id):
                return None
            row = db.execute(
                "SELECT * FROM radio_attempt_progress WHERE outbox_id=? AND attempt_number=?",
                (provenance.outbox_id, provenance.attempt_number),
            ).fetchone()
            if row is None:
                return None
            saved = _decode(row)
            if saved.provenance != provenance:
                return None
            recovered = saved.progress.after_restart()
            if recovered == saved.progress:
                return saved
            result = StoredRadioAttempt(provenance, recovered, saved.revision + 1)
            count = db.execute(
                """UPDATE radio_attempt_progress SET parts_json=?, revision=?, updated_at=?
                   WHERE outbox_id=? AND attempt_number=? AND revision=?""",
                (
                    _json(recovered.to_data()["parts"]),
                    result.revision,
                    _now_iso(),
                    provenance.outbox_id,
                    provenance.attempt_number,
                    saved.revision,
                ),
            ).rowcount
            return result if count == 1 else None

        return await self._radio_transaction(recover)

    async def inspect_radio_attempt(
        self, outbox_id: str, attempt_number: int
    ) -> RadioAttemptMetadata | None:
        """Read progress/provenance without selecting retained wire-plan bytes."""
        row = await self._read_one(
            "SELECT outbox_id, attempt_number, plan_fingerprint, parts_json, provenance_json, revision "
            "FROM radio_attempt_progress WHERE outbox_id=? AND attempt_number=?",
            (outbox_id, attempt_number),
        )
        if row is None:
            return None
        try:
            provenance = msgspec.json.decode(
                row["provenance_json"], type=DeliveryAttemptProvenance
            )
            raw_parts = json.loads(row["parts_json"])
            if not isinstance(raw_parts, list) or any(
                not isinstance(part, dict)
                or set(part) != {"ordinal", "state", "handoff_id"}
                for part in raw_parts
            ):
                raise ValueError("invalid radio progress metadata")
            if (
                provenance.outbox_id != outbox_id
                or provenance.attempt_number != attempt_number
            ):
                raise ValueError("radio metadata identity mismatch")
            return RadioAttemptMetadata(
                provenance,
                tuple(RadioPartProgress(**part) for part in raw_parts),
                row["revision"],
                row["plan_fingerprint"],
            )
        except (ValueError, TypeError, msgspec.DecodeError) as exc:
            raise StorageError(f"Invalid radio metadata: {exc}") from exc

    async def get_radio_attempt_snapshot(
        self,
        outbox_id: str,
        attempt_number: int,
    ) -> dict[str, object] | None:
        """Inspect validated stored facts without recovery, mutation or a send grant."""
        saved = await self.get_radio_attempt(outbox_id, attempt_number)
        return None if saved is None else saved.progress.to_data()

    async def get_radio_attempt(
        self, outbox_id: str, attempt_number: int
    ) -> StoredRadioAttempt | None:
        """Read typed provenance and progress without mutation or send authority."""
        row = await self._read_one(
            "SELECT * FROM radio_attempt_progress WHERE outbox_id=? AND attempt_number=?",
            (outbox_id, attempt_number),
        )
        return None if row is None else _decode(row)

    async def get_latest_radio_attempt(
        self, outbox_id: str, *, before_attempt: int
    ) -> StoredRadioAttempt | None:
        """Select retained evidence across consumed generations, never a native grant.

        The continuation transaction independently checks the complete delivery
        history, exact predecessor and fresh worker claim before inheriting any
        fact. An intervening reservation need not have submitted a radio packet.
        """
        if type(before_attempt) is not int or before_attempt < 1:
            raise ValueError("radio lookup requires a positive attempt boundary")
        row = await self._read_one(
            """SELECT * FROM radio_attempt_progress
               WHERE outbox_id=? AND attempt_number<?
               ORDER BY attempt_number DESC LIMIT 1""",
            (outbox_id, before_attempt),
        )
        return None if row is None else _decode(row)
