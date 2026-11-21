"""Post-handoff delivery observation mixin for SQLiteStorage.

Authority surface:
  - append_delivery_observation: **append/idempotent**.  Observations are
    immutable transport evidence and never mutate delivery receipts or outbox
    lifecycle state.  The append atomically revalidates the authoritative
    outbox attempt and allowed handoff state so a concurrent retry/dead-letter
    transition cannot admit stale callback evidence.
  - list_delivery_observations_for_event: **list/get** (read-only).
  - list_delivery_observations_for_outbox: **list/get** (read-only).
  - append_radio_packet_observation: **append/idempotent** against retained
    original acceptance, independent of the parent's effective generation.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, TypeVar

import msgspec

from medre.core.events import (
    DELIVERY_CONFIRMATION_LEVEL_VALUES,
    DELIVERY_OBSERVATION_STATE_VALUES,
    DeliveryObservation,
)
from medre.core.storage.backend import StorageError
from medre.core.storage.radio_handoff import RadioLocalHandoff
from medre.core.storage.sqlite._handoff_facts import observation_scope
from medre.core.storage.sqlite._evidence_diagnostics import (
    _EvidenceDiagnosticsReadMixin,
    diagnostic_write_ops,
)
from medre.core.storage.sqlite._projections import inspection_sql
from medre.core.storage.sqlite._radio import _decode
from medre.core.storage.sqlite.serde import _encode_json, _row_to_delivery_observation

_T = TypeVar("_T")

_INSERT_OBSERVATION = """
INSERT INTO delivery_observations (
    observation_id, event_id, delivery_plan_id, target_adapter,
    target_channel, native_channel_id, outbox_id, attempt_number, adapter_message_id,
    state, confirmation_level, metadata, observed_at
)
SELECT
    ?, o.event_id, o.delivery_plan_id, o.target_adapter,
    o.target_channel, ?, o.outbox_id,
    COALESCE(o.active_attempt, o.attempt_number), ?,
    ?, ?, ?, ?
FROM delivery_outbox AS o
WHERE o.outbox_id = ?
  AND o.event_id = ?
  AND o.delivery_plan_id = ?
  AND o.target_adapter = ?
  AND ? = COALESCE(o.active_attempt, o.attempt_number)
  AND (
      o.status IN ('in_progress', 'queued', 'sent')
      OR (
          o.status = 'retry_wait' AND o.active_attempt IS NULL
          AND (
              SELECT d.diagnostic FROM delivery_evidence_diagnostics d
              WHERE d.family = 'outbox' AND d.row_id = o.outbox_id
                AND d.attempt = o.attempt_number
                AND d.field = 'failure_kind_detail'
              ORDER BY d.diag_id DESC LIMIT 1
          ) = 'uncertain_native_handoff'
          OR EXISTS (
              SELECT 1 FROM delivery_handoff_uncertainty u
              WHERE u.outbox_id=o.outbox_id AND u.attempt_number=o.attempt_number
                AND u.state='uncertain_native_handoff'
          )
      )
  )
ON CONFLICT(observation_id) DO NOTHING
"""

#: Keep the typed observation fields and exact attempt identity; mask only
#: content-potential metadata before values cross the SQLite read boundary.
#: SELECT * is intentionally the outer projection so inspection_sql can
#: apply its separate metadata_only field allowlist without changing callers.
_OBSERVATION_CONTENT_SAFE_FROM = """
FROM (SELECT
    CASE WHEN EXISTS (
        SELECT 1 FROM event_content_ceiling_log l WHERE l.event_id=o.event_id
    ) THEN COALESCE((
        SELECT s.scope_json FROM delivery_observation_scopes s
        WHERE s.observation_id=o.observation_id
    ), '{}') ELSE o.metadata END AS metadata,
    o.sequence, o.observation_id, o.event_id, o.delivery_plan_id,
    o.target_adapter, o.target_channel, o.native_channel_id, o.outbox_id,
    o.attempt_number, o.adapter_message_id, o.state, o.confirmation_level,
    o.observed_at
FROM delivery_observations o) AS observed
"""

_SELECT_OBSERVATIONS_FOR_EVENT = (
    "SELECT * "
    + _OBSERVATION_CONTENT_SAFE_FROM
    + "WHERE event_id = ? ORDER BY sequence ASC"
)

_SELECT_OBSERVATIONS_FOR_OUTBOX = (
    "SELECT * "
    + _OBSERVATION_CONTENT_SAFE_FROM
    + "WHERE outbox_id = ? ORDER BY sequence ASC"
)


def _observation_insert_params(observation: DeliveryObservation) -> tuple[object, ...]:
    """Validate and serialize one immutable delivery observation.

    The free-form ``error`` text is separated at write time: it lands only in
    ``delivery_evidence_diagnostics`` in the same transaction.
    """
    if observation.state not in DELIVERY_OBSERVATION_STATE_VALUES:
        raise ValueError(
            f"Unknown observation state {observation.state!r}; "
            f"expected one of {sorted(DELIVERY_OBSERVATION_STATE_VALUES)}"
        )
    if observation.confirmation_level not in DELIVERY_CONFIRMATION_LEVEL_VALUES:
        raise ValueError(
            f"Unknown confirmation level {observation.confirmation_level!r}; "
            f"expected one of {sorted(DELIVERY_CONFIRMATION_LEVEL_VALUES)}"
        )
    if observation.attempt_number < 1:
        raise ValueError("observation attempt_number must be >= 1")
    return (
        observation.observation_id,
        observation.native_channel_id or None,
        observation.adapter_message_id,
        observation.state,
        observation.confirmation_level,
        _encode_json(dict(observation.metadata)),
        observation.observed_at.isoformat(),
        observation.outbox_id,
        observation.event_id,
        observation.delivery_plan_id,
        observation.target_adapter,
        observation.attempt_number,
    )


class _ObservationMixin(_EvidenceDiagnosticsReadMixin):
    """Post-handoff delivery observation methods for SQLiteStorage."""

    if TYPE_CHECKING:

        async def _write_rowcount(
            self, sql: str, params: tuple[Any, ...] = ()
        ) -> int: ...

        async def _write_rowcount_with_followups(
            self,
            sql: str,
            params: tuple[Any, ...],
            follow_ups: list[tuple[str, tuple[Any, ...]]],
        ) -> int: ...

        async def _read_all(
            self, sql: str, params: tuple[Any, ...] = ()
        ) -> list[dict[str, Any]]: ...

        async def _radio_transaction(
            self, operation: Callable[[sqlite3.Connection], _T]
        ) -> _T: ...

    async def append_delivery_observation(
        self, observation: DeliveryObservation
    ) -> bool:
        """Append one observation only while its exact outbox attempt is valid.

        The ``INSERT ... SELECT`` revalidates correlation and handoff state in
        the same SQLite statement that persists the observation. A completed
        attempt in ``retry_wait`` remains observable only while its explicit
        ``uncertain_native_handoff`` diagnostic is current; this lets an
        authenticated late acknowledgement arrive during backoff without
        reopening native send authority.  This closes
        the read-then-insert race with retry/dead-letter/cancel transitions.
        Returns ``True`` when a new row was inserted and ``False`` for either a
        duplicate observation ID or an attempt that is no longer admissible.

        The observation's free-form ``error`` text is separated at write time:
        the row keeps the column ``NULL`` and the text lands in
        ``delivery_evidence_diagnostics`` in the same transaction.
        """
        rowcount = await self._write_rowcount_with_followups(
            _INSERT_OBSERVATION,
            _observation_insert_params(observation),
            (
                [
                    (
                        "INSERT INTO delivery_observation_scopes "
                        "(observation_id, scope_json) VALUES (?, ?)",
                        (
                            observation.observation_id,
                            observation_scope(observation.metadata),
                        ),
                    )
                ]
                if observation_scope(observation.metadata) is not None
                else []
            )
            + diagnostic_write_ops(
                "observation",
                observation.observation_id,
                observation.attempt_number,
                {"error": observation.error},
                observation.observed_at.isoformat(),
            ),
        )
        return rowcount == 1

    async def append_radio_packet_observation(
        self, observation: DeliveryObservation
    ) -> bool:
        """Admit a delayed packet observation against its *original* accepted fact.

        Ordinary observations require the current outbox attempt. Radio packet
        ACKs can arrive after a newer attempt inherited the original packet.
        Validate the retained original checkpoint and parent delivery identity
        inside one SQLite write transaction, without reopening an old attempt.
        This is an append-only evidence interface, never lifecycle authority.
        The caller still owns authentication of the native observation source.
        """
        packet = observation.metadata.get("radio_packet")
        if not isinstance(packet, Mapping):
            return False
        ordinal = packet.get("ordinal")
        if (
            type(ordinal) is not int
            or type(observation.attempt_number) is not int
            or type(packet.get("origin_attempt_number")) is not int
            or packet.get("origin_attempt_number") != observation.attempt_number
            or packet.get("origin_outbox_id") != observation.outbox_id
            or observation.adapter_message_id is None
            or observation.state not in {"delivered", "failed", "rejected", "cancelled"}
            or (
                observation.state == "delivered"
                and observation.confirmation_level
                not in {"remote_service", "end_to_end"}
            )
        ):
            return False
        _observation_insert_params(observation)

        def insert(db: sqlite3.Connection) -> bool:
            # Hold the write lock across identity verification and insertion.
            row = db.execute(
                """SELECT progress.* FROM radio_attempt_progress AS progress
                   WHERE outbox_id=? AND attempt_number=?""",
                (observation.outbox_id, observation.attempt_number),
            ).fetchone()
            if row is None:
                provenance = None
                accepted_fact = db.execute(
                    "SELECT native_channel_id, native_message_id "
                    "FROM radio_accepted_part_facts WHERE "
                    "outbox_id=? AND attempt_number=? AND ordinal=?",
                    (observation.outbox_id, observation.attempt_number, ordinal),
                ).fetchone()
                if accepted_fact is None or (
                    accepted_fact["native_channel_id"] != observation.native_channel_id
                    or accepted_fact["native_message_id"]
                    != observation.adapter_message_id
                ):
                    return False
            else:
                checkpoint = _decode(row)
                provenance = checkpoint.provenance
            parent = db.execute(
                "SELECT * FROM delivery_outbox WHERE outbox_id=?",
                (observation.outbox_id,),
            ).fetchone()
            if parent is None or (
                observation.event_id != parent["event_id"]
                or observation.delivery_plan_id != parent["delivery_plan_id"]
                or observation.target_adapter != parent["target_adapter"]
                or (observation.target_channel or "")
                != (parent["target_channel"] or "")
                or (
                    provenance is not None
                    and (
                        provenance.event_id != parent["event_id"]
                        or provenance.delivery_plan_id != parent["delivery_plan_id"]
                        or provenance.target_adapter != parent["target_adapter"]
                        or (provenance.target_channel or "")
                        != (parent["target_channel"] or "")
                        or not 1 <= ordinal <= len(checkpoint.progress.parts)
                    )
                )
            ):
                return False
            if provenance is not None:
                part = checkpoint.progress.parts[ordinal - 1]
                if part.state != "accepted":
                    return False
                # An inherited acceptance is not a fact about this attempt.
                try:
                    handoff = RadioLocalHandoff.from_part(part, provenance)
                except (ValueError, TypeError, msgspec.DecodeError) as exc:
                    raise StorageError(
                        "Retained radio attempt evidence is invalid"
                    ) from exc
                if handoff.origin is None or (
                    handoff.origin.outbox_id != observation.outbox_id
                    or handoff.origin.attempt_number != observation.attempt_number
                    or handoff.native_channel_id != observation.native_channel_id
                    or handoff.native_message_id != observation.adapter_message_id
                ):
                    return False
            # Both the original checkpoint and its content-free post-ceiling
            # acceptance fact are append-only evidence, never send grants.
            db.execute(
                """INSERT INTO delivery_observations (
                    observation_id, event_id, delivery_plan_id, target_adapter,
                    target_channel, native_channel_id, outbox_id, attempt_number,
                    adapter_message_id, state, confirmation_level,
                    metadata, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(observation_id) DO NOTHING""",
                (
                    observation.observation_id,
                    parent["event_id"],
                    parent["delivery_plan_id"],
                    parent["target_adapter"],
                    parent["target_channel"],
                    observation.native_channel_id,
                    observation.outbox_id,
                    observation.attempt_number,
                    observation.adapter_message_id,
                    observation.state,
                    observation.confirmation_level,
                    _encode_json(dict(observation.metadata)),
                    observation.observed_at.isoformat(),
                ),
            )
            inserted = int(db.execute("SELECT changes()").fetchone()[0])
            if inserted == 1:
                # Same transaction: the separated diagnostic text lands only
                # with its committed parent observation row.
                scope = observation_scope(observation.metadata)
                if scope is not None:
                    db.execute(
                        "INSERT INTO delivery_observation_scopes "
                        "(observation_id, scope_json) VALUES (?, ?)",
                        (observation.observation_id, scope),
                    )
                for diagnostic_sql, diagnostic_params in diagnostic_write_ops(
                    "observation",
                    observation.observation_id,
                    observation.attempt_number,
                    {"error": observation.error},
                    observation.observed_at.isoformat(),
                ):
                    db.execute(diagnostic_sql, diagnostic_params)
            return inserted == 1

        return await self._radio_transaction(insert)

    async def list_delivery_observations_for_event(
        self, event_id: str, *, metadata_only: bool = False
    ) -> list[DeliveryObservation]:
        """Return observations for an event in append order."""
        rows = await self._read_all(
            inspection_sql(
                _SELECT_OBSERVATIONS_FOR_EVENT, "delivery_observations", metadata_only
            ),
            (event_id,),
        )
        if not metadata_only:
            rows = await self._resolve_evidence_diagnostics(
                "observation", rows, identity_column="observation_id"
            )
        return [_row_to_delivery_observation(row) for row in rows]

    async def list_delivery_observations_for_outbox(
        self, outbox_id: str, *, metadata_only: bool = False
    ) -> list[DeliveryObservation]:
        """Return observations for one exact outbox item in append order."""
        rows = await self._read_all(
            inspection_sql(
                _SELECT_OBSERVATIONS_FOR_OUTBOX, "delivery_observations", metadata_only
            ),
            (outbox_id,),
        )
        if not metadata_only:
            rows = await self._resolve_evidence_diagnostics(
                "observation", rows, identity_column="observation_id"
            )
        return [_row_to_delivery_observation(row) for row in rows]

    async def list_delivery_observations_for_attempt(
        self, outbox_id: str, attempt_number: int
    ) -> list[DeliveryObservation]:
        """Read one attempt before decoding, independently of older evidence."""
        rows = await self._read_all(
            "SELECT * "
            + _OBSERVATION_CONTENT_SAFE_FROM
            + "WHERE outbox_id = ? AND attempt_number = ? ORDER BY sequence ASC",
            (outbox_id, attempt_number),
        )
        return [_row_to_delivery_observation(row) for row in rows]
