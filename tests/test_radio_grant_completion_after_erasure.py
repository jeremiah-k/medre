"""In-flight radio completion recorded with the grant the caller received.

After ceiling enforcement erases the wire plan, part completion is
recordable with the in-memory grant the worker actually received from
``begin_radio_part``: a retained unreadable acceptance is a
non-authorizing existence sentinel and never contradicts a decodable
closed handoff, and the retained summary keeps that sentinel unchanged.
"""

from __future__ import annotations

import json
from dataclasses import replace as dataclass_replace

import pytest

from medre.core.storage.radio import StoredRadioAttempt
from medre.core.storage.radio_handoff import RadioLocalHandoff
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.ceiling_audit import _install_elapsed_policy
from tests.helpers.handoff_resolution import (
    _WORKER,
    _plan,
    _seed_reserved_claim,
)


async def _seed_corrupted_checkpoint(
    temp_storage: SQLiteStorage,
) -> tuple[StoredRadioAttempt, StoredRadioAttempt, str]:
    """Grant part 2, then corrupt part 1's stored handoff behind the grant."""
    stored, provenance = await _seed_reserved_claim(temp_storage)
    bound = await temp_storage.bind_radio_attempt(provenance, _WORKER, _plan())
    assert bound is not None
    first = await temp_storage.begin_radio_part(bound, _WORKER, 1)
    assert first is not None
    accepted = await temp_storage.observe_radio_part(
        first,
        _WORKER,
        1,
        handoff_id=RadioLocalHandoff(1, "native-1", "ch-0", "local_transport").encode(),
    )
    assert accepted is not None
    started = await temp_storage.begin_radio_part(accepted, _WORKER, 2)
    assert started is not None

    corrupted_parts = list(started.progress.parts)
    corrupted_parts[0] = dataclass_replace(
        corrupted_parts[0], handoff_id="{bad-handoff}"
    )
    await temp_storage._write(
        "UPDATE radio_attempt_progress SET parts_json=? WHERE outbox_id=?",
        (
            json.dumps(
                [
                    {
                        "ordinal": part.ordinal,
                        "state": part.state,
                        "handoff_id": part.handoff_id,
                    }
                    for part in corrupted_parts
                ]
            ),
            stored.outbox_id,
        ),
    )
    return (
        started,
        StoredRadioAttempt(
            provenance,
            dataclass_replace(started.progress, parts=tuple(corrupted_parts)),
            started.revision,
        ),
        stored.outbox_id,
    )


@pytest.mark.parametrize("completion", ["accepted", "unknown"])
async def test_original_grant_completes_after_erasure(
    temp_storage: SQLiteStorage, completion: str
) -> None:
    """The sweep must not invalidate the grant a live caller still holds."""
    started, _corrupted, outbox_id = await _seed_corrupted_checkpoint(temp_storage)
    now = await _install_elapsed_policy(temp_storage, started.provenance.event_id)
    await temp_storage.enforce_content_ceilings(now=now)

    handoff = (
        RadioLocalHandoff(2, "native-2", "ch-0", "local_transport").encode()
        if completion == "accepted"
        else None
    )
    finished = await temp_storage.observe_radio_part(
        started, _WORKER, 2, handoff_id=handoff
    )
    assert finished is not None
    summary = await temp_storage.get_radio_attempt_evidence(outbox_id, 1)
    assert summary is not None
    # The earlier unreadable acceptance stays the retained existence
    # sentinel; the completion never replaces it with a caller value.
    assert summary.parts[0].handoff_id == "{}"
    assert summary.parts[0].state == "accepted"
    assert summary.parts[1].state == completion
