"""Pipeline capacity and shutdown rejection tests.

Verifies that delivery capacity rejection uses CAPACITY_REJECTION and
shutdown uses SHUTDOWN_REJECTION (not DEADLINE_EXCEEDED or generic errors).

Includes a golden-flow integration test proving that capacity rejection
produces coherent suppression → receipt → evidence output end-to-end.
"""

from __future__ import annotations

import pytest

from medre.adapters.fake_presentation import FakePresentationAdapter
from medre.adapters.fake_transport import FakeTransportAdapter
from medre.config.model import RuntimeLimits
from medre.core.engine.pipeline import PipelineRunner
from medre.core.planning.delivery_plan import DeliveryFailureKind
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.runtime.accounting import RuntimeAccounting
from medre.core.runtime.capacity import CapacityController
from medre.core.storage import SQLiteStorage
from medre.runtime.evidence._bundle import collect_evidence_bundle
from tests.helpers.pipeline import make_event, make_pipeline_config_for_pipeline

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_transport() -> FakeTransportAdapter:
    """An unstarted FakeTransportAdapter for creating test events."""
    return FakeTransportAdapter(adapter_id="fake_transport", channel="ch-0")


@pytest.fixture
def fake_presentation() -> FakePresentationAdapter:
    """A FakePresentationAdapter that records delivered events."""
    return FakePresentationAdapter(adapter_id="fake_presentation")


# ===================================================================
# Capacity / Shutdown rejection taxonomy
# ===================================================================


class TestCapacityRejectionTaxonomy:
    """Verify that delivery capacity rejection uses CAPACITY_REJECTION,
    not DEADLINE_EXCEEDED, and that shutdown uses SHUTDOWN_REJECTION.
    """

    async def test_capacity_exhausted_returns_capacity_rejection(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Capacity controller semaphore exhausted → CAPACITY_REJECTION."""
        adapter = FakePresentationAdapter(adapter_id="target")

        route = Route(
            id="cap-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="target")],
        )
        router = Router(routes=[route])

        accounting = RuntimeAccounting()
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"target": adapter},
        )
        config.runtime_accounting = accounting

        runner = PipelineRunner(config)

        # Use a capacity controller with 0 delivery slots → always rejects.
        limits = RuntimeLimits(
            max_inflight_deliveries=1,
            delivery_acquire_timeout_seconds=0.001,
        )
        cc = CapacityController(limits)
        runner.set_capacity_controller(cc)

        await runner.start()

        # Pre-acquire the single slot so the next acquire fails.
        await cc.acquire_delivery()

        event = make_event(event_id="cap-001", source_adapter="src")

        try:
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "permanent_failure"
            assert outcomes[0].failure_kind is DeliveryFailureKind.CAPACITY_REJECTION
            assert outcomes[0].error == "delivery_capacity_exceeded"

            # Semantics: capacity rejection persists a suppressed evidence receipt.
            assert outcomes[0].receipt is not None
            assert outcomes[0].receipt.status == "suppressed"
            assert outcomes[0].receipt.failure_kind == "capacity_rejection"
            receipt_rows = await temp_storage._read_all(
                "SELECT * FROM delivery_receipts WHERE event_id = ?",
                ("cap-001",),
            )
            assert (
                len(receipt_rows) >= 1
            ), "capacity rejection must persist a suppressed delivery receipt"
            assert receipt_rows[0]["status"] == "suppressed"

            # Accounting: capacity_rejections incremented.
            assert accounting.counters().capacity_rejections == 1
        finally:
            await runner.stop()

    async def test_shutdown_rejection_returns_shutdown_rejection(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Capacity controller stopped → SHUTDOWN_REJECTION."""
        adapter = FakePresentationAdapter(adapter_id="target")

        route = Route(
            id="shutdown-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="target")],
        )
        router = Router(routes=[route])

        accounting = RuntimeAccounting()
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"target": adapter},
        )
        config.runtime_accounting = accounting

        runner = PipelineRunner(config)

        limits = RuntimeLimits(max_inflight_deliveries=10)
        cc = CapacityController(limits)
        runner.set_capacity_controller(cc)

        await runner.start()

        # Stop accepting work so acquire returns False immediately.
        cc.stop_accepting()

        event = make_event(event_id="shutdown-001", source_adapter="src")

        try:
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "permanent_failure"
            assert outcomes[0].failure_kind is DeliveryFailureKind.SHUTDOWN_REJECTION
            assert outcomes[0].error == "delivery_rejected_shutdown"

            # Semantics: shutdown rejection persists a suppressed evidence receipt.
            assert outcomes[0].receipt is not None
            assert outcomes[0].receipt.status == "suppressed"
            assert outcomes[0].receipt.failure_kind == "shutdown_rejection"
            receipt_rows = await temp_storage._read_all(
                "SELECT * FROM delivery_receipts WHERE event_id = ?",
                ("shutdown-001",),
            )
            assert (
                len(receipt_rows) >= 1
            ), "shutdown rejection must persist a suppressed delivery receipt"
            assert receipt_rows[0]["status"] == "suppressed"

            # Accounting: capacity_rejections incremented.
            assert accounting.counters().capacity_rejections == 1
        finally:
            await runner.stop()


# ===================================================================
# Capacity rejection golden-flow: suppression → receipt → evidence
# ===================================================================


class TestCapacityRejectionGoldenFlow:
    """Golden-flow: capacity rejection produces CAPACITY_REJECTION, persists
    a suppressed receipt, and surfaces coherent evidence via
    incident_summary and delivery_state_by_adapter.

    The operator should be able to answer:
    - *Why* was delivery suppressed?  (capacity_rejection — semaphore full)
    - *Why* is it non-retryable?  (capacity rejection is never retryable;
      next_retry_at is None on the receipt)
    """

    async def test_capacity_rejection_suppression_receipt_evidence(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """End-to-end: capacity exhaustion → suppression receipt → evidence."""
        adapter = FakePresentationAdapter(adapter_id="target")
        event_id = "golden-cap-001"

        route = Route(
            id="golden-cap-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="target")],
        )
        router = Router(routes=[route])

        accounting = RuntimeAccounting()
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"target": adapter},
        )
        config.runtime_accounting = accounting

        runner = PipelineRunner(config)

        limits = RuntimeLimits(
            max_inflight_deliveries=1,
            delivery_acquire_timeout_seconds=0.001,
        )
        cc = CapacityController(limits)
        runner.set_capacity_controller(cc)

        await runner.start()

        # Pre-acquire the single slot so the next acquire fails.
        await cc.acquire_delivery()

        event = make_event(event_id=event_id, source_adapter="src")

        try:
            # ---- Phase 1: Pipeline outcome ----
            outcomes = await runner.handle_ingress(event)

            assert len(outcomes) == 1
            outcome = outcomes[0]

            # DeliveryOutcome failure_kind is CAPACITY_REJECTION;
            # status is permanent_failure.
            assert outcome.status == "permanent_failure"
            assert outcome.failure_kind is DeliveryFailureKind.CAPACITY_REJECTION
            assert outcome.error == "delivery_capacity_exceeded"

            # ---- Phase 2: Receipt contract ----
            assert outcome.receipt is not None
            receipt = outcome.receipt
            assert receipt.status == "suppressed"
            assert receipt.failure_kind == "capacity_rejection"
            assert receipt.next_retry_at is None
            assert receipt.attempt_number == 1

            # ---- Phase 3: Storage has suppressed receipt ----
            receipt_rows = await temp_storage._read_all(
                "SELECT * FROM delivery_receipts WHERE event_id = ?",
                (event_id,),
            )
            assert len(receipt_rows) >= 1, (
                "capacity rejection must persist a suppressed delivery receipt"
            )
            assert receipt_rows[0]["status"] == "suppressed"
            assert receipt_rows[0]["failure_kind"] == "capacity_rejection"

            # ---- Phase 4: Evidence incident_summary ----
            db_path = temp_storage._db_path
            report = await collect_evidence_bundle(
                storage_path=db_path,
                event_id=event_id,
            )

            storage_section = report["sections"]["storage"]
            assert storage_section["error"] is None, (
                f"Storage section error: {storage_section['error']}"
            )

            summary = storage_section["data"]["incident_summary"]
            assert summary["suppressed_count"] >= 1, (
                f"Expected suppressed_count >= 1, got {summary['suppressed_count']}"
            )
            assert summary["first_failure_kind"] == "capacity_rejection"
            assert summary["classification"] == "operational"
            assert summary["failed_count"] == 0

            # ---- Phase 5: delivery_state_by_adapter ----
            dsba = summary["delivery_state_by_adapter"]
            assert "target" in dsba, (
                f"Expected 'target' in delivery_state_by_adapter, got {list(dsba.keys())}"
            )
            target_state = dsba["target"]
            assert target_state["failure_kind"] == "capacity_rejection"
            assert target_state["retryable"] is False
            assert "target_channel" in target_state, (
                "delivery_state_by_adapter entry must include target_channel key"
            )

            # Accounting: capacity_rejections incremented.
            assert accounting.counters().capacity_rejections == 1
        finally:
            await runner.stop()
