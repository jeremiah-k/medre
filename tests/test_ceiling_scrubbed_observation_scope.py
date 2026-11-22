"""A ceiling sweep restores legacy packet scopes and resolution stays closed.

Two behaviors around a legacy observation whose inline metadata survives
only until the sweep:

- The sweep-derived scope revision must be observable: an observation
  seeded with inline packet metadata and no retained scope gains its
  scope row from the sweep itself, the observation count stays fixed,
  and the terminal-write fences see the advanced revision.
- The resolver must keep treating the scrubbed observation as unreadable
  packet evidence: a purged attempt resolves to an explicitly
  ``unresolved`` verdict, never to a delivered observation with no
  packet scope.
"""

from __future__ import annotations

import json

from medre.core.evidence.handoff_resolution import EvidenceBackedHandoffResolver
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.ceiling_audit import _install_elapsed_policy
from tests.helpers.handoff_resolution import (
    _observation,
    _seed_reserved_claim,
)


async def test_sweep_derived_scope_revision_is_visible_to_the_fences(
    temp_storage: SQLiteStorage,
) -> None:
    stored, provenance = await _seed_reserved_claim(
        temp_storage,
        outbox_id="obox-sweep-scope",
        event_id="ceiling-sweep-scope",
    )
    observation = _observation("legacy-inline-packet", provenance)
    assert await temp_storage.append_delivery_observation(observation)
    item = await temp_storage.get_outbox_item(stored.outbox_id)
    assert item is not None
    before = await EvidenceBackedHandoffResolver(temp_storage).resolve(
        item, provenance.attempt_number
    )
    assert before.observation_count == 1
    assert before.observation_scope_revision == 0

    # Legacy inline packet metadata with no retained scope row: the sweep
    # is the only writer that can restore the scope.
    await temp_storage._write(
        "UPDATE delivery_observations SET metadata=? WHERE observation_id=?",
        (
            json.dumps(
                {
                    "radio_packet": {
                        "ordinal": 1,
                        "origin_attempt_number": provenance.attempt_number,
                        "origin_outbox_id": stored.outbox_id,
                    }
                }
            ),
            observation.observation_id,
        ),
    )
    now = await _install_elapsed_policy(temp_storage, provenance.event_id)
    enforced = await temp_storage.enforce_content_ceilings(now=now)
    assert [outcome.event_id for outcome in enforced] == [provenance.event_id]

    row = await temp_storage._read_one(
        "SELECT metadata FROM delivery_observations WHERE observation_id=?",
        (observation.observation_id,),
    )
    assert row is not None and row["metadata"] == "{}"
    scope = await temp_storage._read_one(
        "SELECT scope_json FROM delivery_observation_scopes WHERE observation_id=?",
        (observation.observation_id,),
    )
    assert scope is not None
    assert json.loads(scope["scope_json"]) == {
        "radio_packet": {
            "ordinal": 1,
            "origin_attempt_number": provenance.attempt_number,
            "origin_outbox_id": stored.outbox_id,
        }
    }

    after = await EvidenceBackedHandoffResolver(temp_storage).resolve(
        item, provenance.attempt_number
    )
    # The observation count is fixed; only the sweep changed the revision.
    assert after.observation_count == 1
    assert after.observation_scope_revision == 1

    # The terminal-write fences now demand exactly the sweep-produced
    # revision: the pre-sweep revision 0 must be rejected, revision 1
    # accepted.
    assert not await temp_storage.mark_outbox_abandoned(
        stored.outbox_id,
        attempt_number=provenance.attempt_number,
        expected_observation_count=1,
        expected_observation_scope_revision=0,
    )
    assert await temp_storage.mark_outbox_abandoned(
        stored.outbox_id,
        attempt_number=provenance.attempt_number,
        expected_observation_count=1,
        expected_observation_scope_revision=1,
    )


async def test_scrubbed_legacy_observation_resolves_unresolved(
    temp_storage: SQLiteStorage,
) -> None:
    stored, provenance = await _seed_reserved_claim(
        temp_storage,
        outbox_id="obox-scrub-unresolved",
        event_id="ceiling-scrub-unresolved",
    )
    observation = _observation("legacy-scrub-unresolved", provenance)
    assert await temp_storage.append_delivery_observation(observation)
    await temp_storage._write(
        "UPDATE delivery_observations SET metadata='[]' WHERE observation_id=?",
        (observation.observation_id,),
    )
    now = await _install_elapsed_policy(temp_storage, provenance.event_id)
    assert await temp_storage.enforce_content_ceilings(now=now)

    # Storage-level scrub: metadata redacted, unreadable marker retained.
    row = await temp_storage._read_one(
        "SELECT metadata FROM delivery_observations WHERE observation_id=?",
        (observation.observation_id,),
    )
    assert row is not None and row["metadata"] == "{}"
    scope = await temp_storage._read_one(
        "SELECT scope_json FROM delivery_observation_scopes WHERE observation_id=?",
        (observation.observation_id,),
    )
    assert scope is not None
    assert json.loads(scope["scope_json"]) == {"radio_packet": None}

    item = await temp_storage.get_outbox_item(stored.outbox_id)
    assert item is not None
    resolution = await EvidenceBackedHandoffResolver(temp_storage).resolve(
        item, provenance.attempt_number
    )
    # The delivered-shaped observation must not settle the purged attempt.
    assert resolution.verdict == "unresolved"
    assert resolution.observation_count == 1
