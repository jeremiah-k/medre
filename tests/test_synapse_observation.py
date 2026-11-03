"""Provider observation waits for replay identity rather than an earlier body."""

from unittest.mock import Mock

import pytest

from tests.helpers.synapse_observation import wait_for_body_events


def _event(identity: str) -> dict:
    return {"type": "m.room.message", "event_id": identity, "content": {"body": "body"}}


async def test_observation_waits_for_the_distinct_replay_event() -> None:
    original, replay = _event("$original"), _event("$replay")
    fetch = Mock(side_effect=[[original], [replay, original]])
    observed = await wait_for_body_events(
        fetch, "body", expected_event_ids={"$original", "$replay"}, interval=0
    )
    assert observed == [replay, original]
    assert fetch.call_count == 2


async def test_idempotent_replay_accepts_the_same_provider_identity() -> None:
    original = _event("$original")
    fetch = Mock(return_value=[original])
    assert await wait_for_body_events(
        fetch, "body", expected_event_ids={"$original"}, interval=0
    ) == [original]
    fetch.assert_called_once()


async def test_observation_retries_a_transient_provider_error() -> None:
    replay = _event("$replay")
    fetch = Mock(side_effect=[OSError("not ready"), [replay]])
    assert await wait_for_body_events(
        fetch, "body", expected_event_ids={"$replay"}, interval=0
    ) == [replay]


async def test_an_original_event_cannot_confirm_a_missing_replay() -> None:
    fetch = Mock(return_value=[_event("$original")])
    with pytest.raises(AssertionError, match="never showed expected events"):
        await wait_for_body_events(
            fetch, "body", expected_event_ids={"$replay"}, timeout=0, interval=0
        )
