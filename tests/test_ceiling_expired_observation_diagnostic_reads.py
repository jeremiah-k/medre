"""Expired observation diagnostics stay denied by their own read query.

The observation row query and the side-table diagnostic query are separate
statements. The row query can finish before an event's ceiling elapses; the
diagnostic query must then deny the stored error text on its own ceiling
evaluation, without any enforcement sweep having committed.
"""

from __future__ import annotations

import msgspec

from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.ceiling_audit import _install_elapsed_policy
from tests.helpers.handoff_resolution import _observation, _seed_reserved_claim

_EXPIRED_ERROR = "expired-gate synthetic failure text"


async def test_expired_observation_error_is_denied_by_the_diagnostic_query(
    temp_storage: SQLiteStorage,
) -> None:
    """A ceiling elapsed between the two statements still hides the text."""
    stored, provenance = await _seed_reserved_claim(temp_storage)
    observation = msgspec.structs.replace(
        _observation(
            "obs-expired-diag-gate",
            provenance,
            state="failed",
            confirmation_level="unknown",
            native_channel_id=None,
            adapter_message_id=None,
        ),
        error=_EXPIRED_ERROR,
    )
    assert await temp_storage.append_delivery_observation(observation)

    # Before the ceiling the separated error resolves from the side table.
    before = await temp_storage.list_delivery_observations_for_event(
        provenance.event_id
    )
    assert [row.error for row in before] == [_EXPIRED_ERROR]

    # After the ceiling, with no sweep committed, the diagnostic query
    # re-evaluates the policy itself and returns no error text.
    await _install_elapsed_policy(temp_storage, provenance.event_id)
    after = await temp_storage.list_delivery_observations_for_event(provenance.event_id)
    assert [row.error for row in after] == [None]
