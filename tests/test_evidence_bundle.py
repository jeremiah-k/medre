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

import msgspec
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
from medre.core.rendering.evidence import RenderingEvidence
from medre.core.rendering.renderer import RenderingContext, RenderingResult
from medre.core.storage.backend import DeliveryOutboxItem
from medre.core.storage.sqlite import SQLiteStorage
from tests.helpers.storage import make_storage_event

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
# NOTE: _make_receipt / _fixed_now are intentionally duplicated in
# tests/conformance/test_evidence_bundle_conformance.py (with different
# defaults) so each test file remains independently runnable without
# cross-directory imports coupling their setups together.
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
        assert bundle.outbox_items[0]["status"] == "sent"

        # JSON-safe.
        d = bundle.to_dict()
        json_str = json.dumps(d, sort_keys=True)
        assert json.loads(json_str) == d

        # No warnings about rendering_evidence.
        assert not any("rendering_evidence" in w for w in bundle.warnings)


class TestCollectorCanonicalEvidence:
    """Bundle parsing of canonical RenderingEvidence via ``from_context_and_result``."""

    @pytest.mark.asyncio
    async def test_parses_canonical_evidence_in_bundle(self) -> None:
        """Real RenderingEvidence serialized via to_dict() round-trips through bundle."""
        ctx = RenderingContext(
            delivery_strategy="direct",
            target_adapter="matrix_conf",
            target_platform="matrix",
            capability_level="native",
        )
        result = RenderingResult(
            event_id="evt-canon",
            target_adapter="matrix_conf",
            target_channel="!room:example.com",
            payload={"body": "hello"},
        )
        evidence = RenderingEvidence.from_context_and_result(
            renderer_name="matrix_rend",
            ctx=ctx,
            result=result,
        )
        ev_json = json.dumps(evidence.to_dict(), sort_keys=True)

        receipt = _make_receipt(
            "rcpt-canon",
            event_id="evt-canon",
            rendering_evidence=ev_json,
        )
        storage = _populated_fake(
            event_id="evt-canon",
            receipts=[receipt],
        )
        collector = EvidenceCollector(storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event("evt-canon")

        assert len(bundle.delivery_receipts) == 1
        parsed = bundle.delivery_receipts[0].rendering_evidence
        assert parsed is not None
        assert parsed["schema_version"] == "1"
        assert parsed["renderer"] == "matrix_rend"
        assert parsed["delivery_strategy"] == "direct"
        assert parsed["target_adapter"] == "matrix_conf"
        assert parsed["capability_level"] == "native"
        assert not any("rendering_evidence" in w for w in bundle.warnings)


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
# C: EvidenceCollector export test
# ===========================================================================


class TestEvidencePackageExport:
    """Public API surface of medre.core.evidence is importable and complete."""

    def test_imports_succeed(self) -> None:
        from medre.core.evidence import (
            EvidenceBundle,
            EvidenceCollector,
            ReceiptSummary,
        )

        assert EvidenceBundle is not None
        assert EvidenceCollector is not None
        assert ReceiptSummary is not None

    def test_evidence_collector_in_all(self) -> None:
        import medre.core.evidence

        assert "EvidenceCollector" in medre.core.evidence.__all__


# ===========================================================================
# D: Null payload/relation guard
# ===========================================================================


class TestNullPayloadRelationGuard:
    """_summarize_event handles explicit None values for payload/relations."""

    def test_payload_none(self) -> None:
        """Event with payload=None should not crash _summarize_event."""
        event = make_storage_event(event_id="evt-pnull")
        # Manipulate the encoded form to inject None values.
        from medre.core.evidence.collector import _summarize_event

        raw = msgspec.json.encode(event)
        full = msgspec.json.decode(raw)
        full["payload"] = None
        full["relations"] = None
        # Re-encode so _summarize_event processes it.
        patched_event = msgspec.json.decode(msgspec.json.encode(full))
        summary = _summarize_event(patched_event)

        assert summary["relation_count"] == 0
        assert summary["relation_types"] == []
        assert summary["payload_keys"] == []

    def test_relation_type_none(self) -> None:
        """Relation with relation_type=None should be filtered out."""
        from medre.core.evidence.collector import _summarize_event

        event = make_storage_event(event_id="evt-rtnull")
        raw = msgspec.json.encode(event)
        full = msgspec.json.decode(raw)
        full["relations"] = [{"relation_type": None}]
        full["payload"] = {"key1": "val1"}
        patched_event = msgspec.json.decode(msgspec.json.encode(full))
        summary = _summarize_event(patched_event)

        assert summary["relation_types"] == []
        assert summary["payload_keys"] == ["key1"]


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


# ===========================================================================
# H: msgspec.structs.asdict for ReceiptSummary
# ===========================================================================


class TestReceiptSummaryAsdict:
    """_receipt_summary_to_dict produces correct output via msgspec.structs.asdict."""

    def test_to_dict_shape_matches(self) -> None:
        rs = ReceiptSummary(
            receipt_id="rcpt-asdict",
            sequence=7,
            target_adapter="adapter_x",
            target_channel="chan-1",
            route_id="route-99",
            status="sent",
            attempt_number=3,
            source="replay",
            replay_run_id="run-abc",
            failure_kind=None,
            error=None,
            rendering_evidence={"key": "val"},
            created_at="2026-01-15T12:00:00+00:00",
        )

        bundle = EvidenceBundle(
            event_id="evt-asdict",
            delivery_receipts=(rs,),
            generated_at="2026-01-15T12:00:00+00:00",
        )
        d = bundle.to_dict()

        rcpt_dict = d["delivery_receipts"][0]
        assert rcpt_dict["receipt_id"] == "rcpt-asdict"
        assert rcpt_dict["sequence"] == 7
        assert rcpt_dict["target_adapter"] == "adapter_x"
        assert rcpt_dict["target_channel"] == "chan-1"
        assert rcpt_dict["route_id"] == "route-99"
        assert rcpt_dict["delivery_plan_id"] == ""
        assert rcpt_dict["status"] == "sent"
        assert rcpt_dict["attempt_number"] == 3
        assert rcpt_dict["source"] == "replay"
        assert rcpt_dict["replay_run_id"] == "run-abc"
        assert rcpt_dict["failure_kind"] is None
        assert rcpt_dict["error"] is None
        assert rcpt_dict["rendering_evidence"] == {"key": "val"}
        assert rcpt_dict["created_at"] == "2026-01-15T12:00:00+00:00"

        # Round-trip through JSON.
        json_str = json.dumps(d, sort_keys=True)
        assert json.loads(json_str) == d

    def test_defaults_round_trip(self) -> None:
        """ReceiptSummary with all defaults serializes cleanly."""
        rs = ReceiptSummary()
        bundle = EvidenceBundle(
            event_id="evt-defaults",
            delivery_receipts=(rs,),
            generated_at="2026-01-15T12:00:00+00:00",
        )
        d = bundle.to_dict()
        json_str = json.dumps(d, sort_keys=True)
        parsed = json.loads(json_str)
        assert parsed["delivery_receipts"][0]["receipt_id"] == ""
        assert parsed["delivery_receipts"][0]["sequence"] == 0


# ===========================================================================
# I: Defensive copy regression test for to_dict()
# ===========================================================================


class TestToDictDefensiveCopy:
    """to_dict() returns defensive copies; mutating the dict does not affect
    the original bundle fields."""

    def test_event_summary_mutation_is_isolated(self) -> None:
        """Mutating returned event_summary dict does not affect bundle."""
        nested_summary = {"event_kind": "message.created", "payload_keys": ["body"]}
        bundle = EvidenceBundle(
            event_id="evt-copy",
            event_summary=nested_summary,
            native_refs=({"id": "nref-1", "adapter": "a", "nested": {"x": 1}},),
            outbox_items=({"outbox_id": "ob-1", "status": "sent", "meta": {"y": 2}},),
            generated_at="2026-01-15T12:00:00+00:00",
        )
        d = bundle.to_dict()

        # Mutate the returned dict.
        d["event_summary"]["event_kind"] = "MUTATED"
        d["event_summary"]["payload_keys"].append("NEW_KEY")
        d["native_refs"][0]["adapter"] = "MUTATED"
        d["native_refs"][0]["nested"]["x"] = 999
        d["outbox_items"][0]["status"] = "MUTATED"
        d["outbox_items"][0]["meta"]["y"] = 999

        # Original bundle fields are unchanged.
        assert bundle.event_summary["event_kind"] == "message.created"
        assert bundle.event_summary["payload_keys"] == ["body"]
        assert bundle.native_refs[0]["adapter"] == "a"
        assert bundle.native_refs[0]["nested"]["x"] == 1
        assert bundle.outbox_items[0]["status"] == "sent"
        assert bundle.outbox_items[0]["meta"]["y"] == 2

    def test_rendering_evidence_mutation_is_isolated(self) -> None:
        """Mutating delivery_receipts[].rendering_evidence in to_dict() output
        does not affect the original bundle."""
        rs = ReceiptSummary(
            receipt_id="rcpt-re",
            sequence=1,
            target_adapter="adapter_a",
            status="sent",
            rendering_evidence={"key": "value", "nested": {"a": 1}},
            created_at="2026-01-15T12:00:00+00:00",
        )
        bundle = EvidenceBundle(
            event_id="evt-re-copy",
            delivery_receipts=(rs,),
            generated_at="2026-01-15T12:00:00+00:00",
        )
        d = bundle.to_dict()

        # Mutate both top-level and nested keys.
        d["delivery_receipts"][0]["rendering_evidence"]["key"] = "mutated"
        d["delivery_receipts"][0]["rendering_evidence"]["nested"]["a"] = 999

        # Original bundle's rendering_evidence is unchanged.
        orig = bundle.delivery_receipts[0].rendering_evidence
        assert orig is not None
        assert orig["key"] == "value"
        assert orig["nested"]["a"] == 1
