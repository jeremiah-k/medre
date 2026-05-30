"""Focused tests for the evidence bundle model, collector, and invalid evidence handling.

Covers the 11 required test cases:
1. EvidenceBundle.to_dict is JSON-safe.
2. collect_for_event includes canonical event summary.
3. collect_for_event includes delivery receipts in deterministic order.
4. collect_for_event parses valid rendering_evidence JSON.
5. collect_for_event warns on invalid rendering_evidence JSON.
6. collect_for_event includes native refs.
7. collect_for_event includes replay_run_ids from receipts.
8. collect_for_event includes queued and sent supplemental receipts.
9. collect_for_event includes suppressed receipt with no rendering evidence.
10. collect_for_event degrades gracefully when event is missing but receipts exist.
11. SQLite-backed collector round-trips evidence.
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
from medre.core.evidence.bundle import (
    BUNDLE_SCHEMA_VERSION,
    EvidenceBundle,
    ReceiptSummary,
)
from medre.core.evidence.collector import EvidenceCollector
from medre.core.storage.backend import DeliveryOutboxItem
from medre.core.storage.sqlite import SQLiteStorage
from tests.helpers.storage import make_storage_event

# ---------------------------------------------------------------------------
# Helpers
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
        created_at=created_at or datetime.now(timezone.utc),
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
        created_at=created_at or datetime.now(timezone.utc),
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
# Test cases
# ===========================================================================


class TestEvidenceBundleModel:
    """Tests for the EvidenceBundle model itself."""

    def test_to_dict_is_json_safe(self) -> None:
        """Case 1: EvidenceBundle.to_dict is JSON-safe."""
        bundle = EvidenceBundle(
            schema_version=BUNDLE_SCHEMA_VERSION,
            event_id="evt-json",
            event_summary={"event_kind": "message.created"},
            delivery_receipts=(
                ReceiptSummary(
                    receipt_id="rcpt-1",
                    sequence=1,
                    status="sent",
                    created_at="2026-01-15T12:00:00+00:00",
                ),
            ),
            native_refs=({"id": "nref-1", "adapter": "a"},),
            outbox_items=({"outbox_id": "ob-1"},),
            replay_run_ids=("run-1",),
            sources_seen=("live",),
            warnings=("warn-1",),
            generated_at="2026-01-15T12:00:00+00:00",
        )
        d = bundle.to_dict()

        # Must succeed without custom encoder.
        serialized = json.dumps(d, sort_keys=True)

        # Round-trip must produce identical output.
        assert json.loads(serialized) == d

        # Verify structure.
        assert d["schema_version"] == BUNDLE_SCHEMA_VERSION
        assert d["event_id"] == "evt-json"
        assert d["event_summary"]["event_kind"] == "message.created"
        assert len(d["delivery_receipts"]) == 1
        assert d["delivery_receipts"][0]["receipt_id"] == "rcpt-1"
        assert d["native_refs"][0]["id"] == "nref-1"
        assert d["outbox_items"][0]["outbox_id"] == "ob-1"
        assert d["replay_run_ids"] == ["run-1"]
        assert d["sources_seen"] == ["live"]
        assert d["warnings"] == ["warn-1"]

    def test_to_json_deterministic(self) -> None:
        """to_json produces deterministic output for identical inputs."""
        bundle = EvidenceBundle(
            event_id="evt-deterministic",
            generated_at="2026-01-15T12:00:00+00:00",
        )
        j1 = bundle.to_json()
        j2 = bundle.to_json()
        assert j1 == j2


class TestCollectorEventSummary:
    """Case 2: collect_for_event includes canonical event summary."""

    @pytest.mark.asyncio
    async def test_includes_event_summary(self) -> None:
        storage = _populated_fake(event_id="evt-sum")
        collector = EvidenceCollector(storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event("evt-sum")

        assert bundle.event_summary is not None
        assert bundle.event_summary["event_id"] == "evt-sum"
        assert bundle.event_summary["event_kind"] == "message.created"
        assert bundle.event_summary["source_adapter"] == "fake_transport"
        assert "payload_keys" in bundle.event_summary


class TestCollectorReceiptsOrder:
    """Case 3: collect_for_event includes delivery receipts in deterministic order."""

    @pytest.mark.asyncio
    async def test_receipts_ordered_by_sequence(self) -> None:
        r1 = _make_receipt("rcpt-3", sequence=3)
        r2 = _make_receipt("rcpt-1", sequence=1)
        r3 = _make_receipt("rcpt-2", sequence=2)

        storage = _populated_fake(
            event_id="evt-order",
            receipts=[r1, r2, r3],  # Out of order.
        )
        collector = EvidenceCollector(storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event("evt-order")

        ids = [r.receipt_id for r in bundle.delivery_receipts]
        assert ids == ["rcpt-1", "rcpt-2", "rcpt-3"]


class TestCollectorValidRenderingEvidence:
    """Case 4: collect_for_event parses valid rendering_evidence JSON."""

    @pytest.mark.asyncio
    async def test_parses_valid_json(self) -> None:
        evidence = json.dumps({"truncated": True, "delivery_strategy": "direct"})
        receipt = _make_receipt(
            "rcpt-ev",
            event_id="evt-ev",
            rendering_evidence=evidence,
        )
        storage = _populated_fake(
            event_id="evt-ev",
            receipts=[receipt],
        )
        collector = EvidenceCollector(storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event("evt-ev")

        r = bundle.delivery_receipts[0]
        assert r.rendering_evidence is not None
        assert r.rendering_evidence["truncated"] is True
        assert r.rendering_evidence["delivery_strategy"] == "direct"
        # No warnings for valid evidence.
        assert not any("rendering_evidence" in w for w in bundle.warnings)


class TestCollectorInvalidRenderingEvidence:
    """Case 5: collect_for_event warns on invalid rendering_evidence JSON."""

    @pytest.mark.asyncio
    async def test_warns_on_invalid_json(self) -> None:
        receipt = _make_receipt(
            "rcpt-bad",
            event_id="evt-bad",
            rendering_evidence="not-valid-json{{{",
        )
        storage = _populated_fake(
            event_id="evt-bad",
            receipts=[receipt],
        )
        collector = EvidenceCollector(storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event("evt-bad")

        r = bundle.delivery_receipts[0]
        assert r.rendering_evidence is None
        assert any("Invalid rendering_evidence" in w for w in bundle.warnings)
        assert any("rcpt-bad" in w for w in bundle.warnings)

    @pytest.mark.asyncio
    async def test_warns_on_non_object_json(self) -> None:
        receipt = _make_receipt(
            "rcpt-arr",
            event_id="evt-arr",
            rendering_evidence='["not", "an", "object"]',
        )
        storage = _populated_fake(
            event_id="evt-arr",
            receipts=[receipt],
        )
        collector = EvidenceCollector(storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event("evt-arr")

        r = bundle.delivery_receipts[0]
        assert r.rendering_evidence is None
        assert any("Non-object rendering_evidence" in w for w in bundle.warnings)

    @pytest.mark.asyncio
    async def test_none_rendering_evidence_no_warning(self) -> None:
        receipt = _make_receipt(
            "rcpt-none",
            event_id="evt-none",
            rendering_evidence=None,
        )
        storage = _populated_fake(
            event_id="evt-none",
            receipts=[receipt],
        )
        collector = EvidenceCollector(storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event("evt-none")

        assert not any("rendering_evidence" in w for w in bundle.warnings)


class TestCollectorNativeRefs:
    """Case 6: collect_for_event includes native refs."""

    @pytest.mark.asyncio
    async def test_includes_native_refs(self) -> None:
        nref = _make_native_ref(event_id="evt-nref", ref_id="nref-42")
        storage = _populated_fake(
            event_id="evt-nref",
            native_refs=[nref],
        )
        collector = EvidenceCollector(storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event("evt-nref")

        assert len(bundle.native_refs) == 1
        assert bundle.native_refs[0]["id"] == "nref-42"
        assert bundle.native_refs[0]["adapter"] == "adapter_a"


class TestCollectorReplayRunIds:
    """Case 7: collect_for_event includes replay_run_ids from receipts."""

    @pytest.mark.asyncio
    async def test_aggregates_replay_run_ids(self) -> None:
        r1 = _make_receipt(
            "rcpt-live", event_id="evt-replay", source="live", replay_run_id=None
        )
        r2 = _make_receipt(
            "rcpt-replay-1",
            event_id="evt-replay",
            source="replay",
            replay_run_id="run-z",
        )
        r3 = _make_receipt(
            "rcpt-replay-2",
            event_id="evt-replay",
            source="replay",
            replay_run_id="run-a",
        )

        storage = _populated_fake(
            event_id="evt-replay",
            receipts=[r1, r2, r3],
        )
        collector = EvidenceCollector(storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event("evt-replay")

        # Sorted lexicographically.
        assert bundle.replay_run_ids == ("run-a", "run-z")
        assert bundle.sources_seen == ("live", "replay")


class TestCollectorQueuedAndSent:
    """Case 8: collect_for_event includes queued and sent supplemental receipts."""

    @pytest.mark.asyncio
    async def test_includes_queued_and_sent(self) -> None:
        queued = _make_receipt(
            "rcpt-queued",
            event_id="evt-qs",
            status="queued",
            sequence=1,
        )
        sent = _make_receipt(
            "rcpt-sent",
            event_id="evt-qs",
            status="sent",
            sequence=2,
        )

        storage = _populated_fake(
            event_id="evt-qs",
            receipts=[queued, sent],
        )
        collector = EvidenceCollector(storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event("evt-qs")

        statuses = [r.status for r in bundle.delivery_receipts]
        assert statuses == ["queued", "sent"]


class TestCollectorSuppressed:
    """Case 9: collect_for_event includes suppressed receipt with no rendering evidence."""

    @pytest.mark.asyncio
    async def test_suppressed_no_rendering_evidence(self) -> None:
        suppressed = _make_receipt(
            "rcpt-sup",
            event_id="evt-sup",
            status="suppressed",
            rendering_evidence=None,
        )

        storage = _populated_fake(
            event_id="evt-sup",
            receipts=[suppressed],
        )
        collector = EvidenceCollector(storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event("evt-sup")

        r = bundle.delivery_receipts[0]
        assert r.status == "suppressed"
        assert r.rendering_evidence is None
        # No warnings about rendering_evidence for None.
        assert not any("rendering_evidence" in w for w in bundle.warnings)


class TestCollectorMissingEvent:
    """Case 10: collect_for_event degrades gracefully when event is missing."""

    @pytest.mark.asyncio
    async def test_missing_event_with_receipts(self) -> None:
        receipt = _make_receipt("rcpt-orphan", event_id="evt-missing")
        storage = _populated_fake(
            event_id="evt-missing",
            include_event=False,
            receipts=[receipt],
        )
        collector = EvidenceCollector(storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event("evt-missing")

        assert bundle.event_summary is None
        assert any("not found" in w for w in bundle.warnings)
        assert len(bundle.delivery_receipts) == 1
        assert bundle.delivery_receipts[0].receipt_id == "rcpt-orphan"

    @pytest.mark.asyncio
    async def test_completely_missing_event(self) -> None:
        storage = FakeStorage()
        collector = EvidenceCollector(storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event("evt-nothing")

        assert bundle.event_summary is None
        assert len(bundle.delivery_receipts) == 0
        assert len(bundle.native_refs) == 0
        assert any("No event" in w for w in bundle.warnings)


class TestSQLiteRoundTrip:
    """Case 11: SQLite-backed collector round-trips evidence."""

    @pytest.mark.asyncio
    async def test_sqlite_round_trip(self, temp_storage: SQLiteStorage) -> None:
        event = make_storage_event(event_id="evt-sqlite")
        await temp_storage.append(event)

        receipt = _make_receipt(
            "rcpt-sqlite",
            event_id="evt-sqlite",
            rendering_evidence='{"truncated": false}',
        )
        await temp_storage.append_receipt(receipt)

        nref = _make_native_ref(event_id="evt-sqlite", ref_id="nref-sqlite")
        await temp_storage.store_native_ref(nref)

        outbox = _make_outbox_item(event_id="evt-sqlite", outbox_id="ob-sqlite")
        await temp_storage.create_outbox_item(outbox)

        collector = EvidenceCollector(temp_storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event("evt-sqlite")

        # Event summary present.
        assert bundle.event_summary is not None
        assert bundle.event_summary["event_id"] == "evt-sqlite"

        # Receipt present with parsed evidence.
        assert len(bundle.delivery_receipts) == 1
        assert bundle.delivery_receipts[0].rendering_evidence == {"truncated": False}

        # Native ref present.
        assert len(bundle.native_refs) == 1
        assert bundle.native_refs[0]["id"] == "nref-sqlite"

        # Outbox item present.
        assert len(bundle.outbox_items) == 1
        assert bundle.outbox_items[0]["outbox_id"] == "ob-sqlite"

        # JSON-safe.
        d = bundle.to_dict()
        json_str = json.dumps(d, sort_keys=True)
        assert json.loads(json_str) == d

        # No warnings about rendering_evidence.
        assert not any("rendering_evidence" in w for w in bundle.warnings)
