"""Route retry bounds are admission facts of the freshly created generation.

An enqueue that adopts an existing outbox generation must not run
``_INSERT_OUTBOX_ROUTE_RETRY_BOUNDS`` with the new submission's settings:
the adopted row keeps the bounds recorded when its own generation was
created.
"""

from __future__ import annotations

from medre.core.storage.backend import (
    DeliveryOutboxItem,
    OutboxRouteRetryBoundsWrite,
)
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage_outbox import admit_event


def _item(outbox_id: str, event_id: str) -> DeliveryOutboxItem:
    return DeliveryOutboxItem(
        outbox_id=outbox_id,
        event_id=event_id,
        route_id="route-gen",
        delivery_plan_id="plan-gen",
        target_adapter="fake",
        target_channel="ch-0",
        attempt_number=1,
        status="pending",
    )


def _bounds(max_attempts: int) -> OutboxRouteRetryBoundsWrite:
    return OutboxRouteRetryBoundsWrite(
        max_attempts=max_attempts,
        backoff_base=2.0,
        max_delay_seconds=60.0,
        jitter=False,
    )


async def test_adopted_generation_keeps_its_original_bounds(
    temp_storage: SQLiteStorage,
) -> None:
    await admit_event(temp_storage, "evt-bounds-gen")
    await temp_storage.create_outbox_item(
        _item("obox-gen", "evt-bounds-gen"),
        route_retry_bounds=_bounds(3),
    )
    await temp_storage.create_outbox_item(
        _item("obox-gen", "evt-bounds-gen"),
        route_retry_bounds=_bounds(5),
    )
    bounds = await temp_storage.get_outbox_route_retry_bounds("obox-gen")
    assert bounds is not None
    assert bounds.max_attempts == 3
    assert bounds.jitter is False
