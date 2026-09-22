"""Post-handoff delivery observation mixin for SQLiteStorage.

Authority surface:
  - append_delivery_observation: **append/idempotent**.  Observations are
    immutable transport evidence and never mutate delivery receipts or outbox
    lifecycle state.  The append atomically revalidates the authoritative
    outbox attempt and allowed handoff state so a concurrent retry/dead-letter
    transition cannot admit stale callback evidence.
  - list_delivery_observations_for_event: **list/get** (read-only).
  - list_delivery_observations_for_outbox: **list/get** (read-only).
"""

from __future__ import annotations

from medre.core.events import (
    DELIVERY_CONFIRMATION_LEVEL_VALUES,
    DELIVERY_OBSERVATION_STATE_VALUES,
    DeliveryObservation,
)
from medre.core.storage.sqlite.serde import _encode_json, _row_to_delivery_observation

_INSERT_OBSERVATION = """
INSERT INTO delivery_observations (
    observation_id, event_id, delivery_plan_id, target_adapter,
    target_channel, native_channel_id, outbox_id, attempt_number, adapter_message_id,
    state, confirmation_level, error, metadata, observed_at
)
SELECT
    ?, o.event_id, o.delivery_plan_id, o.target_adapter,
    o.target_channel, ?, o.outbox_id, o.attempt_number, ?,
    ?, ?, ?, ?, ?
FROM delivery_outbox AS o
WHERE o.outbox_id = ?
  AND o.event_id = ?
  AND o.delivery_plan_id = ?
  AND o.target_adapter = ?
  AND o.attempt_number = ?
  AND o.status IN ('in_progress', 'queued', 'sent')
ON CONFLICT(observation_id) DO NOTHING
"""

_SELECT_OBSERVATIONS_FOR_EVENT = """
SELECT * FROM delivery_observations
WHERE event_id = ?
ORDER BY sequence ASC
"""

_SELECT_OBSERVATIONS_FOR_OUTBOX = """
SELECT * FROM delivery_observations
WHERE outbox_id = ?
ORDER BY sequence ASC
"""


def _observation_insert_params(observation: DeliveryObservation) -> tuple[object, ...]:
    """Validate and serialize one immutable delivery observation."""
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
        observation.error,
        _encode_json(dict(observation.metadata)),
        observation.observed_at.isoformat(),
        observation.outbox_id,
        observation.event_id,
        observation.delivery_plan_id,
        observation.target_adapter,
        observation.attempt_number,
    )


class _ObservationMixin:
    """Post-handoff delivery observation methods for SQLiteStorage."""

    async def append_delivery_observation(
        self, observation: DeliveryObservation
    ) -> bool:
        """Append one observation only while its exact outbox attempt is valid.

        The ``INSERT ... SELECT`` revalidates correlation and handoff state in
        the same SQLite statement that persists the observation.  This closes
        the read-then-insert race with retry/dead-letter/cancel transitions.
        Returns ``True`` when a new row was inserted and ``False`` for either a
        duplicate observation ID or an attempt that is no longer admissible.
        """
        rowcount = await self._write_rowcount(
            _INSERT_OBSERVATION,
            _observation_insert_params(observation),
        )
        return rowcount == 1

    async def list_delivery_observations_for_event(
        self, event_id: str
    ) -> list[DeliveryObservation]:
        """Return observations for an event in append order."""
        rows = await self._read_all(_SELECT_OBSERVATIONS_FOR_EVENT, (event_id,))
        return [_row_to_delivery_observation(row) for row in rows]

    async def list_delivery_observations_for_outbox(
        self, outbox_id: str
    ) -> list[DeliveryObservation]:
        """Return observations for one exact outbox item in append order."""
        rows = await self._read_all(_SELECT_OBSERVATIONS_FOR_OUTBOX, (outbox_id,))
        return [_row_to_delivery_observation(row) for row in rows]
