"""Shared test helper for constructing DeliveryOutboxItem rows.

Centralises the ``_make_outbox_item`` factory that was previously duplicated
across the split storage outbox test modules.  Defaults match the original
in-test factories byte-for-byte.

This helper is a **pure factory** — it does NOT validate ``status``.  The
production ``create_outbox_item()`` enforces that only ``pending`` and
``in_progress`` are accepted as initial statuses.  Tests that need a row
in another status (queued, sent, retry_wait, dead_lettered, cancelled,
abandoned) may construct the item freely via this helper and then call the
appropriate ``mark_outbox_*`` transition method.

**When bypassing ``create_outbox_item()`` is acceptable:**

- Pure property / unit tests that check read-only predicates
  (``is_claimable``, ``is_terminal``, etc.) without exercising storage.

**When bypassing ``create_outbox_item()`` is NOT acceptable:**

- Behaviour tests that verify the storage lifecycle — reclaim,
  transition, or finalization flows.  These must go through
  ``create_outbox_item()`` so the production validation gate is
  exercised end-to-end.
"""

from __future__ import annotations

import uuid

from medre.core.storage.backend import DeliveryOutboxItem


def make_outbox_item(
    delivery_plan_id: str = "plan-1",
    target_adapter: str = "fake_presentation",
    target_channel: str | None = "ch-0",
    attempt_number: int = 1,
    status: str = "pending",
    next_attempt_at: str | None = None,
    event_id: str = "__outbox_default__",
) -> DeliveryOutboxItem:
    """Build a minimal DeliveryOutboxItem for tests.

    No validation is performed here; ``create_outbox_item()`` enforces
    the production lifecycle policy.  See module docstring.
    """
    return DeliveryOutboxItem(
        outbox_id=f"obox-{uuid.uuid4()}",
        event_id=event_id,
        route_id="route-1",
        delivery_plan_id=delivery_plan_id,
        target_adapter=target_adapter,
        target_channel=target_channel,
         attempt_number=attempt_number,
         status=status,
         next_attempt_at=next_attempt_at,
     )


SENTINEL_EVENT_ID = "__outbox_default__"


async def admit_default_event(storage) -> None:
    """Append the sentinel canonical event used by ``make_outbox_item()``.

    Tests that write ``delivery_outbox`` / ``native_message_refs`` /
    ``delivery_receipts`` rows directly (bypassing runtime admission)
    must satisfy ``PRAGMA foreign_keys=ON`` against
    ``canonical_events(event_id)``.  Call once per freshly-initialised
    storage, before the first direct outbox/native-ref/receipt write.
    """
    from medre.core.events import CanonicalEvent, EventMetadata
    from datetime import datetime, timezone

    await storage.append(
        CanonicalEvent(
            event_id=SENTINEL_EVENT_ID,
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="test-fixture",
            source_transport_id="t1",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "outbox fixture default"},
            metadata=EventMetadata(),
        )
    )
