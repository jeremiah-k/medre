"""Pin envelope guarantees separately from payload and lifecycle authority."""

from datetime import datetime, timedelta, timezone

import msgspec
import pytest

from medre.core.storage.backend import EventFilter
from tests.helpers.pipeline import make_event


@pytest.mark.parametrize(
    "event_id", ["opaque-event", "550e8400-e29b-41d4-a716-446655440000"]
)
def test_event_identifiers_are_opaque_nonempty_strings(event_id):
    event = make_event(event_id=event_id)
    assert event.event_id == event_id
    assert msgspec.json.decode(msgspec.json.encode(event))["event_id"] == event_id


def test_envelope_does_not_promise_per_kind_payload_validation():
    event = make_event(
        event_kind="message.text", payload={"body": 42, "extension": {"values": [1, 2]}}
    )
    assert event.payload["body"] == 42
    with pytest.raises(TypeError):
        event.payload["extension"]["values"] = []


async def test_event_query_order_uses_timestamp_before_opaque_id(temp_storage):
    early = datetime(2026, 1, 1, tzinfo=timezone.utc)
    first = msgspec.structs.replace(make_event(event_id="z-first"), timestamp=early)
    second = msgspec.structs.replace(
        make_event(event_id="a-second"), timestamp=early + timedelta(seconds=1)
    )
    await temp_storage.append(second)
    await temp_storage.append(first)
    events = [event async for event in temp_storage.query(EventFilter())]
    assert [event.event_id for event in events] == ["z-first", "a-second"]
