"""Defensive ordering, outbox datetime edge cases, and missing-backend-method tests.

Split from ``test_evidence_bundle.py`` so that each module stays under 1500 lines.
Helpers are intentionally duplicated (see NOTE in ``test_evidence_bundle.py``)
so each file remains independently runnable.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from medre.core.events import (
    CanonicalEvent,
    DeliveryReceipt,
    NativeMessageRef,
)
from medre.core.evidence.collector import EvidenceCollector
from medre.core.storage.backend import DeliveryOutboxItem
from tests.helpers.storage import make_storage_event

# ---------------------------------------------------------------------------
# Helpers (duplicated for independent runnability)
# ---------------------------------------------------------------------------

_FIXED_NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)


def _fixed_now() -> datetime:
    return _FIXED_NOW


def _make_receipt(
    receipt_id: str = "rcpt-1",
    event_id: str = "evt-1",
    sequence: int = 1,
    delivery_plan_id: str = "plan-1",
    target_adapter: str = "adapter_a",
    target_channel: str | None = None,
    status: str = "sent",
    attempt_number: int = 1,
    source: str = "live",
    replay_run_id: str | None = None,
    rendering_evidence: str | None = None,
    created_at: datetime | None = None,
) -> DeliveryReceipt:
    return DeliveryReceipt(
        sequence=sequence,
        receipt_id=receipt_id,
        event_id=event_id,
        delivery_plan_id=delivery_plan_id,
        target_adapter=target_adapter,
        target_channel=target_channel,
        route_id="route-1",
        status=status,  # type: ignore[arg-type]
        attempt_number=attempt_number,
        source=source,
        replay_run_id=replay_run_id,
        rendering_evidence=rendering_evidence,
        created_at=created_at or _FIXED_NOW,
    )


def _make_native_ref(
    event_id: str = "evt-1",
    ref_id: str = "nref-1",
    adapter: str = "adapter_a",
    created_at: datetime | None = None,
) -> NativeMessageRef:
    return NativeMessageRef(
        id=ref_id,
        event_id=event_id,
        adapter=adapter,
        native_channel_id="ch-0",
        native_message_id=f"msg-{ref_id}",
        native_thread_id=None,
        native_relation_id=None,
        direction="outbound",
        created_at=created_at or _FIXED_NOW,
    )


def _make_outbox_item(
    event_id: str = "evt-1",
    outbox_id: str = "ob-1",
    target_adapter: str = "adapter_a",
    status: str = "sent",
    created_at: str | None = None,
) -> DeliveryOutboxItem:
    return DeliveryOutboxItem(
        outbox_id=outbox_id,
        event_id=event_id,
        route_id="route-1",
        delivery_plan_id="plan-1",
        target_adapter=target_adapter,
        status=status,
        created_at=created_at or "2026-01-15T12:00:00+00:00",
        updated_at="2026-01-15T12:00:01+00:00",
    )


class FakeStorage:
    """Minimal fake storage for unit tests."""

    def __init__(self) -> None:
        self._events: dict[str, CanonicalEvent] = {}
        self._receipts: dict[str, list[DeliveryReceipt]] = {}
        self._native_refs: dict[str, list[NativeMessageRef]] = {}
        self._outbox: dict[str, list[DeliveryOutboxItem]] = {}

    async def get(self, event_id: str) -> CanonicalEvent | None:
        return self._events.get(event_id)

    async def list_receipts_for_event(self, event_id: str) -> list[DeliveryReceipt]:
        return sorted(self._receipts.get(event_id, []), key=lambda r: r.sequence)

    async def list_native_refs_for_event(self, event_id: str) -> list[NativeMessageRef]:
        return sorted(
            self._native_refs.get(event_id, []),
            key=lambda r: (r.created_at, r.id),
        )

    async def list_outbox_items_for_event(
        self, event_id: str
    ) -> list[DeliveryOutboxItem]:
        return self._outbox.get(event_id, [])


def _populated_fake(
    *,
    event_id: str = "evt-1",
    include_event: bool = True,
    receipts: list[DeliveryReceipt] | None = None,
    native_refs: list[NativeMessageRef] | None = None,
    outbox_items: list[DeliveryOutboxItem] | None = None,
) -> FakeStorage:
    """Build a FakeStorage pre-populated with the given data."""
    fs = FakeStorage()
    if include_event:
        fs._events[event_id] = make_storage_event(event_id=event_id)
    if receipts:
        fs._receipts[event_id] = receipts
    if native_refs:
        fs._native_refs[event_id] = native_refs
    if outbox_items:
        fs._outbox[event_id] = outbox_items
    return fs


# ===========================================================================
# A: Defensive ordering — native refs
# ===========================================================================


class _UnsortedNativeRefStorage(FakeStorage):
    """FakeStorage that returns native refs in *reverse* order deliberately."""

    async def list_native_refs_for_event(self, event_id: str) -> list[NativeMessageRef]:
        # Return refs in reverse order to prove collector re-sorts.
        return sorted(
            self._native_refs.get(event_id, []),
            key=lambda r: (r.created_at, r.id),
            reverse=True,
        )


class TestNativeRefDefensiveOrdering:
    """Collector defensively sorts native refs by (created_at, id)."""

    @pytest.mark.asyncio
    async def test_native_refs_sorted_by_created_at_then_id(self) -> None:
        t1 = datetime(2026, 1, 10, 8, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 10, 9, 0, 0, tzinfo=timezone.utc)
        t3 = datetime(2026, 1, 10, 9, 0, 0, tzinfo=timezone.utc)

        refs = [
            _make_native_ref(event_id="evt-nr-sort", ref_id="nref-z", created_at=t2),
            _make_native_ref(event_id="evt-nr-sort", ref_id="nref-a", created_at=t3),
            _make_native_ref(event_id="evt-nr-sort", ref_id="nref-m", created_at=t1),
        ]

        storage = _UnsortedNativeRefStorage()
        storage._events["evt-nr-sort"] = make_storage_event(event_id="evt-nr-sort")
        storage._native_refs["evt-nr-sort"] = refs

        collector = EvidenceCollector(storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event("evt-nr-sort")

        ids = [nr["id"] for nr in bundle.native_refs]
        # Expect: t1("nref-m"), t2("nref-a"), t2("nref-z") — created_at ascending, then id ascending.
        assert ids == ["nref-m", "nref-a", "nref-z"]


# ===========================================================================
# B: Defensive ordering — outbox items
# ===========================================================================


class _UnsortedOutboxStorage(FakeStorage):
    """FakeStorage that returns outbox items in *reverse* order deliberately."""

    async def list_outbox_items_for_event(
        self, event_id: str
    ) -> list[DeliveryOutboxItem]:
        return sorted(
            self._outbox.get(event_id, []),
            key=lambda i: (i.created_at or "", i.outbox_id),
            reverse=True,
        )


class TestOutboxItemDefensiveOrdering:
    """Collector defensively sorts outbox items by (created_at, outbox_id)."""

    @pytest.mark.asyncio
    async def test_outbox_items_sorted_by_created_at_then_outbox_id(self) -> None:
        items = [
            _make_outbox_item(
                event_id="evt-ob-sort",
                outbox_id="ob-z",
                created_at="2026-01-10T09:00:00+00:00",
            ),
            _make_outbox_item(
                event_id="evt-ob-sort",
                outbox_id="ob-a",
                created_at="2026-01-10T10:00:00+00:00",
            ),
            _make_outbox_item(
                event_id="evt-ob-sort",
                outbox_id="ob-m",
                created_at="2026-01-10T08:00:00+00:00",
            ),
        ]

        storage = _UnsortedOutboxStorage()
        storage._events["evt-ob-sort"] = make_storage_event(event_id="evt-ob-sort")
        storage._outbox["evt-ob-sort"] = items

        collector = EvidenceCollector(storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event("evt-ob-sort")

        outbox_ids = [oi["outbox_id"] for oi in bundle.outbox_items]
        assert outbox_ids == ["ob-m", "ob-z", "ob-a"]

    @pytest.mark.asyncio
    async def test_outbox_items_with_none_created_at(self) -> None:
        """Items with None created_at sort first (empty-string fallback)."""
        late = _make_outbox_item(
            event_id="evt-ob-null",
            outbox_id="ob-late",
            created_at="2026-01-10T12:00:00+00:00",
        )
        none_item = DeliveryOutboxItem(
            outbox_id="ob-none-a",
            event_id="evt-ob-null",
            route_id="route-1",
            delivery_plan_id="plan-1",
            target_adapter="adapter_a",
            status="sent",
            created_at=None,
            updated_at="2026-01-10T12:00:01+00:00",
        )

        storage = _UnsortedOutboxStorage()
        storage._events["evt-ob-null"] = make_storage_event(event_id="evt-ob-null")
        storage._outbox["evt-ob-null"] = [late, none_item]

        collector = EvidenceCollector(storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event("evt-ob-null")

        outbox_ids = [oi["outbox_id"] for oi in bundle.outbox_items]
        assert outbox_ids == ["ob-none-a", "ob-late"]


# ===========================================================================
# E: Datetime outbox timestamps
# ===========================================================================


class TestOutboxDatetimeTimestamps:
    """Outbox items with datetime objects in created_at/updated_at serialize safely."""

    @pytest.mark.asyncio
    async def test_datetime_outbox_timestamps_json_safe(self) -> None:
        dt_created = datetime(2026, 2, 20, 14, 30, 0, tzinfo=timezone.utc)
        dt_updated = datetime(2026, 2, 20, 14, 30, 5, tzinfo=timezone.utc)

        item = DeliveryOutboxItem(
            outbox_id="ob-dt",
            event_id="evt-dt",
            route_id="route-1",
            delivery_plan_id="plan-1",
            target_adapter="adapter_a",
            status="sent",
            created_at=dt_created,  # type: ignore[arg-type]
            updated_at=dt_updated,  # type: ignore[arg-type]
        )

        storage = _populated_fake(event_id="evt-dt", outbox_items=[item])
        collector = EvidenceCollector(storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event("evt-dt")

        # to_json() must succeed without TypeError on datetime.
        json_str = bundle.to_json()
        parsed = json.loads(json_str)

        ob = parsed["outbox_items"][0]
        assert ob["created_at"] == "2026-02-20T14:30:00+00:00"
        assert ob["updated_at"] == "2026-02-20T14:30:05+00:00"

    @pytest.mark.asyncio
    async def test_none_outbox_timestamps(self) -> None:
        """None timestamps stay None in output."""
        item = DeliveryOutboxItem(
            outbox_id="ob-null-ts",
            event_id="evt-null-ts",
            route_id="route-1",
            delivery_plan_id="plan-1",
            target_adapter="adapter_a",
            status="pending",
            created_at=None,
            updated_at=None,
        )

        storage = _populated_fake(event_id="evt-null-ts", outbox_items=[item])
        collector = EvidenceCollector(storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event("evt-null-ts")

        json_str = bundle.to_json()
        parsed = json.loads(json_str)
        ob = parsed["outbox_items"][0]
        assert ob["created_at"] is None
        assert ob["updated_at"] is None


# ===========================================================================
# G: Missing-backend-method warning
# ===========================================================================


class _NoOutboxStorage:
    """Minimal storage that has get, list_receipts_for_event, list_native_refs_for_event
    but does NOT have list_outbox_items_for_event."""

    def __init__(self) -> None:
        self._events: dict[str, CanonicalEvent] = {}
        self._receipts: dict[str, list[DeliveryReceipt]] = {}
        self._native_refs: dict[str, list[NativeMessageRef]] = {}

    async def get(self, event_id: str) -> CanonicalEvent | None:
        return self._events.get(event_id)

    async def list_receipts_for_event(self, event_id: str) -> list[DeliveryReceipt]:
        return self._receipts.get(event_id, [])

    async def list_native_refs_for_event(self, event_id: str) -> list[NativeMessageRef]:
        return self._native_refs.get(event_id, [])


class TestMissingOutboxMethodWarning:
    """Storage missing list_outbox_items_for_event produces warning and empty outbox."""

    @pytest.mark.asyncio
    async def test_missing_method_warning(self) -> None:
        storage = _NoOutboxStorage()
        storage._events["evt-no-ob"] = make_storage_event(event_id="evt-no-ob")

        collector = EvidenceCollector(storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event("evt-no-ob")

        assert bundle.outbox_items == ()
        assert any(
            "list_outbox_items_for_event not available on storage backend" in w
            for w in bundle.warnings
        )
