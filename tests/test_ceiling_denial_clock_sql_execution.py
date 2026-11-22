"""The radio denial predicate samples the clock inside SQLite at execution.

A queued read binds a timestamp before it joins the storage executor queue.
The guard under test is the registered ``medre_ceiling_now`` function: the
denial predicate in ``event_content_denied_sql`` must ignore that stale
bound instant and sample UTC while the statement itself evaluates the
policy row, so a read that waits behind the executor lock cannot expose a
wire plan whose ceiling elapsed while the read was queued.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

import medre.core.storage.sqlite._radio as radio_module
import medre.core.storage.sqlite.connection as connection_module
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.ceiling_audit import _admit_with_ceiling, _read_ceiling
from tests.helpers.handoff_resolution import (
    _bind_and_progress,
    _seed_reserved_claim,
)


async def test_radio_denial_ignores_the_stale_bound_instant(
    temp_storage: SQLiteStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_id = "ceiling-denial-clock"
    await _admit_with_ceiling(temp_storage, event_id)
    stored, provenance = await _seed_reserved_claim(
        temp_storage,
        outbox_id="obox-denial-clock",
        event_id=event_id,
    )
    await _bind_and_progress(temp_storage, provenance, ["accepted", "unsent", "unsent"])
    ceiling = await _read_ceiling(temp_storage, event_id)

    # Pre-ceiling, the checkpoint is readable through the ordinary path.
    saved = await temp_storage.get_radio_attempt(stored.outbox_id, 1)
    assert saved is not None

    stale = (ceiling - timedelta(seconds=60)).isoformat(timespec="microseconds")
    elapsed = (ceiling + timedelta(seconds=60)).isoformat(timespec="microseconds")
    # The value bound before the read joins the executor queue is stale, but
    # the registered in-statement clock already observes an elapsed ceiling.
    monkeypatch.setattr(radio_module, "ceiling_read_time", lambda: stale)
    monkeypatch.setattr(connection_module, "ceiling_read_time", lambda: elapsed)

    denied = await temp_storage.get_radio_attempt(stored.outbox_id, 1)
    assert denied is None

    # The combined evidence projection still settles the attempt as closed
    # evidence without exposing the wire plan.
    evidence = await temp_storage.get_radio_attempt_evidence(stored.outbox_id, 1)
    assert evidence is not None
    assert evidence.provenance.event_id == event_id


async def test_radio_checkpoint_is_readable_without_a_policy(
    temp_storage: SQLiteStorage,
) -> None:
    """Without a policy row the same read path returns the typed checkpoint."""
    stored, provenance = await _seed_reserved_claim(
        temp_storage,
        outbox_id="obox-denial-clock-unpolicy",
        event_id="ceiling-denial-clock-unpolicy",
    )
    await _bind_and_progress(temp_storage, provenance, ["accepted", "unsent", "unsent"])
    saved = await temp_storage.get_radio_attempt(stored.outbox_id, 1)
    assert saved is not None
    assert saved.provenance.outbox_id == stored.outbox_id
