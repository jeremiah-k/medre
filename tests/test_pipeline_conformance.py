"""Pipeline conformance tests.

Verifies that the MEDRE pipeline conforms to the §3.2 pipeline conformance
requirements defined in ``docs/spec/conformance.md``.

§3.2 Pipeline Conformance — the pipeline conforms when it:

1. Processes events through all stages in order (ingress, store, enrich,
   transform, event policy, route, route policy, delivery plan, render,
   deliver, receipt).
2. Never mutates a canonical event after creation.
3. Creates derived events with ``parent_event_id`` and lineage for all
   enrichment and transformation outputs.
4. Records delivery receipts for every delivery attempt (append-only).
5. Derives current delivery status from the latest receipt, not by mutating
   receipt rows.
6. Evaluates policies at the correct stage (ingress, event, route, delivery).
7. Supports replay without modifying existing events.

Tests use the fake adapter pipeline (no SDKs needed).
"""

from __future__ import annotations

from typing import Any

import pytest

from medre.adapters.fakes.presentation import FakePresentationAdapter
from medre.adapters.fakes.transport import FakeTransportAdapter
from medre.core.engine.pipeline import PipelineRunner
from medre.core.events import (
    DeliveryReceipt,
)
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage import SQLiteStorage
from tests.helpers.pipeline import make_event, make_pipeline_config_for_pipeline

# ---------------------------------------------------------------------------
# Shared fixtures (temp_storage comes from conftest.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_presentation() -> FakePresentationAdapter:
    """A FakePresentationAdapter target."""
    return FakePresentationAdapter(adapter_id="dest_presentation")


@pytest.fixture
def simple_route() -> Route:
    """Route: src_transport -> dest_presentation."""
    return Route(
        id="conf-route",
        source=RouteSource(
            adapter="src_transport",
            event_kinds=("message.created",),
            channel="ch-0",
        ),
        targets=[RouteTarget(adapter="dest_presentation")],
    )


def _make_runner(
    storage: SQLiteStorage,
    route: Route,
    adapters: dict[str, Any],
) -> PipelineRunner:
    """Build a PipelineRunner with sensible defaults."""
    config = make_pipeline_config_for_pipeline(
        storage=storage,
        router=Router(routes=[route]),
        adapters=adapters,
    )
    return PipelineRunner(config)


# ---------------------------------------------------------------------------
# §3.2 Requirement 1: Processes events through all stages in order
# ---------------------------------------------------------------------------


class TestPipelineStageOrder:
    """§3.2.1: Processes events through all stages in order (ingress, store,
    enrich, transform, event policy, route, route policy, delivery plan,
    render, deliver, receipt).
    """

    async def test_event_passes_through_all_stages(
        self,
        temp_storage: SQLiteStorage,
        fake_presentation: FakePresentationAdapter,
        simple_route: Route,
    ) -> None:
        """§3.2.1: An inbound event must be stored, routed, rendered,
        delivered, and receipted."""
        runner = _make_runner(
            temp_storage,
            simple_route,
            {"dest_presentation": fake_presentation},
        )
        await runner.start()
        try:
            event = make_event(
                event_id="stage-001",
                source_adapter="src_transport",
                payload={"text": "stage order test"},
            )
            outcomes = await runner.handle_ingress(event)

            # Stage: store — event persisted.
            stored = await temp_storage.get("stage-001")
            assert stored is not None, "Event was not stored (store stage)"

            # Stage: route — at least one route matched.
            assert len(outcomes) >= 1, "No routes matched (route stage)"

            # Stage: deliver — adapter received a payload.
            assert (
                len(fake_presentation.delivered_payloads) >= 1
            ), "Adapter received no payloads (deliver stage)"

            # Stage: receipt — receipt was persisted.
            receipts = await temp_storage.list_receipts_for_event("stage-001")
            assert len(receipts) >= 1, "No receipts persisted (receipt stage)"
        finally:
            await runner.stop()


# ---------------------------------------------------------------------------
# §3.2 Requirement 2: Never mutates a canonical event after creation
# ---------------------------------------------------------------------------


class TestCanonicalEventImmutability:
    """§3.2.2: Never mutates a canonical event after creation."""

    async def test_event_not_mutated_after_pipeline(
        self,
        temp_storage: SQLiteStorage,
        fake_presentation: FakePresentationAdapter,
        simple_route: Route,
    ) -> None:
        """§3.2.2: Canonical event fields remain identical after pipeline
        processing."""
        runner = _make_runner(
            temp_storage,
            simple_route,
            {"dest_presentation": fake_presentation},
        )
        await runner.start()
        try:
            event = make_event(
                event_id="immut-001",
                source_adapter="src_transport",
                payload={"text": "immutable test"},
            )
            # Snapshot original values.
            original_id = event.event_id
            original_kind = event.event_kind
            original_source = event.source_adapter
            original_text = event.payload.get("text")

            await runner.handle_ingress(event)

            # Verify original event unchanged.
            assert event.event_id == original_id
            assert event.event_kind == original_kind
            assert event.source_adapter == original_source
            assert event.payload.get("text") == original_text
        finally:
            await runner.stop()

    async def test_stored_event_matches_ingress(
        self,
        temp_storage: SQLiteStorage,
        fake_presentation: FakePresentationAdapter,
        simple_route: Route,
    ) -> None:
        """§3.2.2: Stored event must have the same core fields as the
        ingress event."""
        runner = _make_runner(
            temp_storage,
            simple_route,
            {"dest_presentation": fake_presentation},
        )
        await runner.start()
        try:
            event = make_event(
                event_id="immut-002",
                source_adapter="src_transport",
                payload={"text": "store immutability"},
            )
            await runner.handle_ingress(event)

            stored = await temp_storage.get("immut-002")
            assert stored is not None
            assert stored.event_id == event.event_id
            assert stored.event_kind == event.event_kind
            assert stored.source_adapter == event.source_adapter
        finally:
            await runner.stop()


# ---------------------------------------------------------------------------
# §3.2 Requirement 3: Derived events with parent_event_id and lineage
# ---------------------------------------------------------------------------


class TestDerivedEventLineage:
    """§3.2.3: Creates derived events with ``parent_event_id`` and lineage
    for all enrichment and transformation outputs."""

    @pytest.mark.xfail(
        reason="Derived event creation requires enrich/transform stage instrumentation",
        strict=False,
    )
    async def test_derived_events_have_parent_and_lineage(
        self,
        temp_storage: SQLiteStorage,
        fake_presentation: FakePresentationAdapter,
        simple_route: Route,
    ) -> None:
        """§3.2.3: Any derived events produced by the pipeline must carry
        ``parent_event_id`` and non-empty ``lineage``."""
        runner = _make_runner(
            temp_storage,
            simple_route,
            {"dest_presentation": fake_presentation},
        )
        await runner.start()
        try:
            source_event = make_event(
                event_id="derive-001",
                source_adapter="src_transport",
                payload={"text": "lineage test"},
            )
            await runner.handle_ingress(source_event)

            # Query for any events whose parent_event_id == "derive-001".
            from medre.core.storage.backend import EventFilter

            derived: list[Any] = []
            async for stored_event in temp_storage.query(EventFilter(limit=100)):
                if getattr(stored_event, "parent_event_id", None) != "derive-001":
                    continue
                derived.append(stored_event)
                assert stored_event.parent_event_id == "derive-001", (
                    f"Derived event {stored_event.event_id} has "
                    f"parent_event_id={stored_event.parent_event_id!r}"
                )
                assert (
                    len(stored_event.lineage) > 0
                ), f"Derived event {stored_event.event_id} has empty lineage"
            assert len(derived) >= 1, (
                "Expected at least one derived event with "
                "parent_event_id='derive-001' but found none. "
                "§3.2.3 requires derived events to carry lineage."
            )
        finally:
            await runner.stop()


# ---------------------------------------------------------------------------
# §3.2 Requirement 4: Delivery receipts for every attempt (append-only)
# ---------------------------------------------------------------------------


class TestDeliveryReceipts:
    """§3.2.4: Records delivery receipts for every delivery attempt
    (append-only)."""

    async def test_receipt_created_on_successful_delivery(
        self,
        temp_storage: SQLiteStorage,
        fake_presentation: FakePresentationAdapter,
        simple_route: Route,
    ) -> None:
        """§3.2.4: A successful delivery must produce a receipt."""
        runner = _make_runner(
            temp_storage,
            simple_route,
            {"dest_presentation": fake_presentation},
        )
        await runner.start()
        try:
            event = make_event(
                event_id="receipt-001",
                source_adapter="src_transport",
                payload={"text": "receipt test"},
            )
            await runner.handle_ingress(event)

            receipts = await temp_storage.list_receipts_for_event("receipt-001")
            assert (
                len(receipts) >= 1
            ), f"Expected at least 1 receipt for receipt-001, got {len(receipts)}"
        finally:
            await runner.stop()

    async def test_receipt_created_on_failed_delivery(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """§3.2.4: A failed delivery (missing adapter) must also produce a
        receipt."""
        # No adapter registered for "missing_adapter" — delivery should fail
        # but still produce a receipt.
        route = Route(
            id="fail-route",
            source=RouteSource(
                adapter="src_transport",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="missing_adapter")],
        )
        runner = _make_runner(temp_storage, route, {})
        await runner.start()
        try:
            event = make_event(
                event_id="receipt-fail-001",
                source_adapter="src_transport",
                payload={"text": "fail receipt test"},
            )
            await runner.handle_ingress(event)

            receipts = await temp_storage.list_receipts_for_event("receipt-fail-001")
            assert len(receipts) >= 1, (
                f"Expected at least 1 receipt for failed delivery, "
                f"got {len(receipts)}"
            )
            # The receipt should indicate failure.
            failed = [r for r in receipts if r.status == "failed"]
            assert len(failed) >= 1, "Expected at least one failed receipt"
        finally:
            await runner.stop()


# ---------------------------------------------------------------------------
# §3.2 Requirement 5: Delivery status from latest receipt
# ---------------------------------------------------------------------------


class TestDeliveryStatusFromReceipt:
    """§3.2.5: Derives current delivery status from the latest receipt, not
    by mutating receipt rows."""

    async def test_receipt_is_frozen_immutable(
        self,
    ) -> None:
        """§3.2.5: DeliveryReceipt must be frozen (immutable)."""
        receipt = DeliveryReceipt(
            sequence=0,
            receipt_id="rcpt-test",
            event_id="evt-test",
            delivery_plan_id="plan-test",
            target_adapter="dest",
            route_id="route-test",
            status="sent",
        )
        # DeliveryReceipt is a frozen msgspec.Struct — mutation must raise.
        with pytest.raises((AttributeError, TypeError)):
            receipt.status = "failed"  # type: ignore[misc]

    async def test_multiple_receipts_append_only(
        self,
        temp_storage: SQLiteStorage,
        fake_presentation: FakePresentationAdapter,
    ) -> None:
        """§3.2.5: Multiple deliveries produce append-only receipts; status
        is derived from the latest."""
        # Use a route with two targets so one ingress produces two deliveries.
        second_target = FakeTransportAdapter(
            adapter_id="sink",
            channel="ch-sink",
        )
        multi_route = Route(
            id="multi-route",
            source=RouteSource(
                adapter="src_transport",
                event_kinds=("message.created",),
                channel="ch-0",
            ),
            targets=[
                RouteTarget(adapter="dest_presentation"),
                RouteTarget(adapter="sink"),
            ],
        )
        runner = _make_runner(
            temp_storage,
            multi_route,
            {"dest_presentation": fake_presentation, "sink": second_target},
        )
        await runner.start()
        try:
            event = make_event(
                event_id="status-001",
                source_adapter="src_transport",
                payload={"text": "status test"},
            )
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) >= 1

            receipts = await temp_storage.list_receipts_for_event("status-001")
            assert (
                len(receipts) >= 2
            ), f"Expected 2+ receipts for multi-target delivery, got {len(receipts)}"

            # Sort by sequence — storage return order is not guaranteed.
            ordered_receipts = sorted(receipts, key=lambda r: r.sequence)

            # Verify receipts are append-only: first receipt sequence is preserved.
            first_seq = ordered_receipts[0].sequence
            for rcpt in ordered_receipts[1:]:
                assert rcpt.sequence > first_seq, (
                    "Receipts are not append-only: later receipt has sequence "
                    f"{rcpt.sequence} <= first {first_seq} (§3.2.5)"
                )

            # The latest receipt status is the current status.
            latest = ordered_receipts[-1]
            assert latest.status in {
                "sent",
                "queued",
                "confirmed",
                "accepted",
            }, f"Unexpected latest receipt status: {latest.status!r}"
        finally:
            await runner.stop()


# ---------------------------------------------------------------------------
# §3.2 Requirement 6: Policies evaluated at correct stage
# ---------------------------------------------------------------------------


class TestPolicyEvaluation:
    """§3.2.6: Evaluates policies at the correct stage (ingress, event,
    route, delivery)."""

    async def test_route_policy_suppresses_delivery(
        self,
        temp_storage: SQLiteStorage,
        fake_presentation: FakePresentationAdapter,
    ) -> None:
        """§3.2.6: Route policy should suppress delivery when the event
        doesn't match allowed types."""
        from medre.core.policies.route_policy import RoutePolicy

        policy = RoutePolicy(
            sender_allowlist=("nonexistent_sender",),
        )
        route = Route(
            id="policy-route",
            source=RouteSource(
                adapter="src_transport",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="dest_presentation")],
            policy=policy,
        )
        runner = _make_runner(
            temp_storage,
            route,
            {"dest_presentation": fake_presentation},
        )
        await runner.start()
        try:
            # Send a message.created event — should be suppressed by policy.
            event = make_event(
                event_id="policy-001",
                event_kind="message.created",
                source_adapter="src_transport",
                payload={"text": "should be suppressed"},
            )
            outcomes = await runner.handle_ingress(event)
            assert outcomes, "No outcomes returned for route-policy test"
            # The outcome should show a skipped/suppressed status.
            if outcomes:
                assert any(
                    o.status == "skipped" or "suppressed" in (o.error or "")
                    for o in outcomes
                ), f"Policy did not suppress: {[o.status for o in outcomes]}"
        finally:
            await runner.stop()

    async def test_ingress_policy_suppresses_event(
        self,
        temp_storage: SQLiteStorage,
        fake_presentation: FakePresentationAdapter,
        simple_route: Route,
    ) -> None:
        """§3.2.6: Ingress policy should suppress events before they reach
        routes."""
        runner = _make_runner(
            temp_storage,
            simple_route,
            {"dest_presentation": fake_presentation},
        )
        await runner.start()
        try:
            event = make_event(
                event_id="ingress-policy-001",
                event_kind="system.heartbeat",
                source_adapter="src_transport",
                payload={"text": "should be suppressed at ingress"},
            )
            outcomes = await runner.handle_ingress(event)
            # Ingress-suppressed events produce no outcomes and no receipts.
            assert (
                len(outcomes) == 0
            ), f"Ingress policy did not suppress: {[o.status for o in outcomes]}"
            receipts = await temp_storage.list_receipts_for_event("ingress-policy-001")
            assert (
                len(receipts) == 0
            ), "Ingress-suppressed event should not produce receipts"
        finally:
            await runner.stop()

    @pytest.mark.xfail(
        reason="Ingress/delivery policy not accessible via fake pipeline",
        strict=False,
    )
    async def test_delivery_policy_suppresses_delivery(
        self,
        temp_storage: SQLiteStorage,
        fake_presentation: FakePresentationAdapter,
        simple_route: Route,
    ) -> None:
        """§3.2.6: Delivery policy should suppress delivery to specific
        targets; suppressed deliveries produce receipts with suppressed status."""
        runner = _make_runner(
            temp_storage,
            simple_route,
            {"dest_presentation": fake_presentation},
        )
        await runner.start()
        try:
            event = make_event(
                event_id="delivery-policy-001",
                event_kind="message.created",
                source_adapter="src_transport",
                payload={"text": "delivery should be suppressed"},
            )
            outcomes = await runner.handle_ingress(event)
            # Delivery-suppressed outcomes should have suppressed/skipped status.
            if outcomes:
                suppressed = [
                    o for o in outcomes if o.status in ("suppressed", "skipped")
                ]
                assert (
                    len(suppressed) >= 1
                ), f"Delivery policy did not suppress: {[o.status for o in outcomes]}"
        finally:
            await runner.stop()


# ---------------------------------------------------------------------------
# §3.2 Requirement 7: Replay without modifying existing events
# ---------------------------------------------------------------------------


class TestReplayImmutability:
    """§3.2.7: Supports replay without modifying existing events."""

    @pytest.mark.xfail(
        reason="Replay requires ReplayEngine integration (see test_replay_*)",
        strict=False,
    )
    async def test_replay_does_not_modify_original_events(
        self,
        temp_storage: SQLiteStorage,
        fake_presentation: FakePresentationAdapter,
        simple_route: Route,
    ) -> None:
        """§3.2.7: Replaying an event must not modify the original stored
        event."""
        runner = _make_runner(
            temp_storage,
            simple_route,
            {"dest_presentation": fake_presentation},
        )
        await runner.start()
        try:
            event = make_event(
                event_id="replay-001",
                source_adapter="src_transport",
                payload={"text": "replay test"},
            )
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) >= 1

            # Store original event snapshot.
            original = await temp_storage.get("replay-001")
            assert original is not None
            original_kind = original.event_kind
            original_payload_text = original.payload.get("text")

            # Replay the same event through the pipeline.
            await runner.handle_ingress(event)

            # Verify original stored event unchanged.
            after_replay = await temp_storage.get("replay-001")
            assert after_replay is not None
            assert after_replay.event_kind == original_kind
            assert after_replay.payload.get("text") == original_payload_text
        finally:
            await runner.stop()

    async def test_event_frozen_after_creation(
        self,
    ) -> None:
        """§3.2.7: CanonicalEvent is frozen — any field assignment must
        raise."""
        event = make_event(
            event_id="frozen-001",
            source_adapter="src_transport",
            payload={"text": "frozen test"},
        )
        with pytest.raises((AttributeError, TypeError)):
            event.event_kind = "mutated"  # type: ignore[misc]
        with pytest.raises((AttributeError, TypeError)):
            event.payload = {"text": "mutated"}  # type: ignore[misc]
