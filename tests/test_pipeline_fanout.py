"""Pipeline fanout tests: mixed classification, scaling, ordering, and
faulty adapter handling.

Tests deterministic fanout with multiple targets, verifying each target
is classified independently and receipts are ordered correctly.
"""

from __future__ import annotations

import pytest

from medre.adapters.fake_presentation import FakePresentationAdapter
from medre.adapters.fake_transport import FakeTransportAdapter
from medre.core.engine.pipeline import PipelineRunner
from medre.core.observability.metrics import Diagnostician
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage import SQLiteStorage
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
# Mixed fanout with failure classification
# ===================================================================


class TestMixedFanoutClassification:
    """Deterministic partial fanout: each target classified independently."""

    async def test_three_targets_mixed_classification(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Three targets: success, transient, permanent — all classified."""

        from medre.core.planning.delivery_plan import DeliveryFailureKind

        good = FakePresentationAdapter(adapter_id="good")

        class _Transient:
            adapter_id = "transient"

            def __init__(self) -> None:
                self.received_events: list[object] = []

            async def deliver(self, payload: object) -> None:
                raise ConnectionError("timeout")

        class _Permanent:
            adapter_id = "permanent"

            def __init__(self) -> None:
                self.received_events: list[object] = []

            async def deliver(self, payload: object) -> None:
                raise RuntimeError("bad payload")

        transient = _Transient()
        permanent = _Permanent()

        route = Route(
            id="mixed-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[
                RouteTarget(adapter="good"),
                RouteTarget(adapter="transient"),
                RouteTarget(adapter="permanent"),
            ],
        )
        router = Router(routes=[route])

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"good": good, "transient": transient, "permanent": permanent},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="mixed-001", source_adapter="src")

        try:
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 3

            by_adapter = {o.target_adapter: o for o in outcomes}
            assert by_adapter["good"].status == "success"
            assert by_adapter["good"].failure_kind is None

            assert by_adapter["transient"].status == "transient_failure"
            assert (
                by_adapter["transient"].failure_kind
                is DeliveryFailureKind.ADAPTER_TRANSIENT
            )

            assert by_adapter["permanent"].status == "permanent_failure"
            assert (
                by_adapter["permanent"].failure_kind
                is DeliveryFailureKind.ADAPTER_PERMANENT
            )

            # Three distinct receipts stored.
            rows = await temp_storage._read_all(
                "SELECT * FROM delivery_receipts WHERE event_id = ?",
                ("mixed-001",),
            )
            assert len(rows) == 3
        finally:
            await runner.stop()

    async def test_fanout_receipts_target_scoped(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Receipts for different adapters are independent."""
        good_a = FakePresentationAdapter(adapter_id="a")
        good_b = FakePresentationAdapter(adapter_id="b")

        route = Route(
            id="scoped-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[
                RouteTarget(adapter="a"),
                RouteTarget(adapter="b"),
            ],
        )
        router = Router(routes=[route])

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"a": good_a, "b": good_b},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="scoped-001", source_adapter="src")

        try:
            await runner.handle_ingress(event)

            rows = await temp_storage._read_all(
                "SELECT * FROM delivery_receipts WHERE event_id = ? ORDER BY sequence ASC",
                ("scoped-001",),
            )
            assert len(rows) == 2
            adapters = {r["target_adapter"] for r in rows}
            assert adapters == {"a", "b"}
            # Each has its own attempt_number = 1
            for row in rows:
                assert row["attempt_number"] == 1
                assert row["parent_receipt_id"] is None
        finally:
            await runner.stop()


# ===================================================================
# Fanout scaling, ordering
# ===================================================================


class TestFanoutScaling:
    """Deterministic fanout scaling: 1..N targets, all produce receipts."""

    async def test_fanout_10_targets_all_succeed(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Fanout to 10 targets: all succeed, 10 receipts stored."""
        targets = [f"target-{i}" for i in range(10)]
        adapters = {t: FakePresentationAdapter(adapter_id=t) for t in targets}

        route_targets = [RouteTarget(adapter=t) for t in targets]
        route = Route(
            id="scale-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=route_targets,
        )
        router = Router(routes=[route])

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters=adapters,
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="scale-001", source_adapter="src")

        try:
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 10
            assert all(o.status == "success" for o in outcomes)

            # Every adapter received the rendered payload
            for t in targets:
                assert len(adapters[t].delivered_payloads) == 1
                assert adapters[t].delivered_payloads[0].event_id == "scale-001"

            # 10 distinct receipts
            rows = await temp_storage._read_all(
                "SELECT * FROM delivery_receipts WHERE event_id = ?",
                ("scale-001",),
            )
            assert len(rows) == 10
            receipt_adapters = {r["target_adapter"] for r in rows}
            assert receipt_adapters == set(targets)
        finally:
            await runner.stop()

    async def test_fanout_all_targets_fail(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Fanout to 5 targets where all fail: 5 permanent_failure outcomes."""
        from medre.adapters.fake_presentation import FaultyPresentationAdapter

        targets = [f"broken-{i}" for i in range(5)]
        adapters = {
            t: FaultyPresentationAdapter(adapter_id=t, failure_mode="permanent_fail")
            for t in targets
        }

        route_targets = [RouteTarget(adapter=t) for t in targets]
        route = Route(
            id="all-fail-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=route_targets,
        )
        router = Router(routes=[route])

        diag = Diagnostician()
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters=adapters,
        )
        config.diagnostician = diag
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="allfail-001", source_adapter="src")

        try:
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 5
            assert all(o.status == "permanent_failure" for o in outcomes)

            # Diagnostician recorded each failure
            snap = diag.snapshot()
            for t in targets:
                assert snap["adapter_failures"].get(t, 0) >= 1

            # 5 failed receipts
            rows = await temp_storage._read_all(
                "SELECT * FROM delivery_receipts WHERE event_id = ?",
                ("allfail-001",),
            )
            assert len(rows) == 5
            assert all(r["status"] == "failed" for r in rows)
        finally:
            await runner.stop()

    async def test_fanout_receipts_ordered_by_sequence(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Fanout receipts have monotonically increasing sequence numbers."""
        adapters = {
            f"ord-{i}": FakePresentationAdapter(adapter_id=f"ord-{i}") for i in range(5)
        }

        route = Route(
            id="seq-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter=f"ord-{i}") for i in range(5)],
        )
        router = Router(routes=[route])

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters=adapters,
        )
        runner = PipelineRunner(config)
        await runner.start()

        events = [
            make_event(event_id=f"seq-evt-{i}", source_adapter="src") for i in range(3)
        ]

        try:
            for event in events:
                await runner.handle_ingress(event)

            rows = await temp_storage._read_all(
                "SELECT sequence, event_id FROM delivery_receipts ORDER BY sequence ASC",
                (),
            )
            # 3 events × 5 targets = 15 receipts
            assert len(rows) == 15

            # Sequence numbers are strictly monotonic
            seqs = [r["sequence"] for r in rows]
            for i in range(1, len(seqs)):
                assert (
                    seqs[i] > seqs[i - 1]
                ), f"Sequence {seqs[i]} not > {seqs[i-1]} at index {i}"
        finally:
            await runner.stop()

    async def test_fanout_mixed_with_faulty_adapter(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Fanout: 2 good + 1 transient-faulty + 1 permanent-faulty."""
        from medre.adapters.fake_presentation import FaultyPresentationAdapter

        good_a = FakePresentationAdapter(adapter_id="good-a")
        good_b = FakePresentationAdapter(adapter_id="good-b")
        transient = FaultyPresentationAdapter(
            adapter_id="transient",
            failure_mode="transient_fail",
        )
        permanent = FaultyPresentationAdapter(
            adapter_id="permanent",
            failure_mode="permanent_fail",
        )

        route = Route(
            id="mixed-faulty-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[
                RouteTarget(adapter="good-a"),
                RouteTarget(adapter="transient"),
                RouteTarget(adapter="permanent"),
                RouteTarget(adapter="good-b"),
            ],
        )
        router = Router(routes=[route])

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={
                "good-a": good_a,
                "good-b": good_b,
                "transient": transient,
                "permanent": permanent,
            },
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="mixed-faulty-001", source_adapter="src")

        try:
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 4

            by_adapter = {o.target_adapter: o for o in outcomes}
            assert by_adapter["good-a"].status == "success"
            assert by_adapter["good-b"].status == "success"
            assert by_adapter["transient"].status == "transient_failure"
            assert by_adapter["permanent"].status == "permanent_failure"

            # Good adapters received payloads
            assert len(good_a.delivered_payloads) == 1
            assert len(good_b.delivered_payloads) == 1
            assert len(transient.delivered_payloads) == 0
            assert len(permanent.delivered_payloads) == 0
        finally:
            await runner.stop()
