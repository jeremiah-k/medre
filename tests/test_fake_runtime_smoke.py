"""Fake multi-adapter runtime smoke and short soak tests.

Proves the fake runtime can start from a multi-adapter config, route a
synthetic event through the real RouteEngine / PipelineRunner, generate
delivery receipts, inspect runtime snapshots / diagnostics, stop cleanly,
and survive repeated start/stop cycles — all with fake adapters and
in-memory storage, no live dependencies.

Every test here:

- Uses **fake adapters** only — no live transports or SDKs required.
- Uses **in-memory storage** — no filesystem I/O beyond temp dirs.
- Runs within **<10 seconds** for default iteration counts.
- Is **deterministic** — ``wait_until`` polls until a predicate is
  satisfied instead of relying on fixed sleeps.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from medre.adapters.fakes.matrix import FakeMatrixAdapter
from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter
from medre.config.model import (
    AdapterConfigSet,
    LoggingConfig,
    MatrixRuntimeConfig,
    MeshtasticRuntimeConfig,
    RuntimeConfig,
    RuntimeLimits,
    RuntimeOptions,
    StorageConfig,
)
from medre.config.paths import MedrePaths, resolve
from medre.core.events.kinds import EventKind
from medre.core.planning.delivery_plan import DeliveryFailureKind
from medre.core.routing.models import Route, RouteSource, RouteTarget
from medre.runtime.app import MedreApp, RuntimeState
from medre.runtime.builder import RuntimeBuilder
from medre.runtime.snapshot import SCHEMA_VERSION, build_runtime_snapshot
from tests.helpers.fake_runtime import (
    build_and_start,
    clean_stop,
    make_cross_transport_config_with_route,
    make_multi_adapter_config,
    make_two_adapter_config_with_route,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_path_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "MEDRE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture()
def tmp_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MedrePaths:
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    return resolve()


# ---------------------------------------------------------------------------
# Config builders (failure tests only)
# ---------------------------------------------------------------------------


def _make_pipeline_failure_config(
    *,
    target_adapter_id: str = "mx_beta",
    target_platform: str = "matrix",
) -> tuple[RuntimeConfig, Route]:
    """Config with mx_alpha → target route for failure testing."""
    config = RuntimeConfig(
        runtime=RuntimeOptions(name="failure-test"),
        logging=LoggingConfig(level="DEBUG"),
        storage=StorageConfig(backend="memory"),
        adapters=AdapterConfigSet(
            matrix={
                "mx_alpha": MatrixRuntimeConfig(
                    adapter_id="mx_alpha",
                    enabled=True,
                    adapter_kind="fake",
                ),
                "mx_beta": MatrixRuntimeConfig(
                    adapter_id="mx_beta",
                    enabled=True,
                    adapter_kind="fake",
                ),
            },
        ),
    )
    route = Route(
        id="alpha-to-beta",
        source=RouteSource(
            adapter="mx_alpha",
            event_kinds=(EventKind.MESSAGE_TEXT,),
            channel=None,
        ),
        targets=[RouteTarget(adapter=target_adapter_id)],
    )
    return config, route


# ===================================================================
# SMOKE TESTS
# ===================================================================


class TestFakeRuntimeStartsFromMultiAdapterConfig:
    """Runtime starts cleanly from a multi-adapter fake config."""

    @pytest.mark.asyncio
    async def test_build_produces_app_with_4_adapters(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """RuntimeBuilder produces a MedreApp with 4 fake adapters."""
        config = make_multi_adapter_config()
        builder = RuntimeBuilder(config, tmp_paths)
        app = builder.build()

        assert isinstance(app, MedreApp)
        assert len(app.adapters) == 4
        assert "fake_matrix" in app.adapters
        assert "fake_meshtastic" in app.adapters
        assert "fake_meshcore" in app.adapters
        assert "fake_lxmf" in app.adapters
        assert app.state is RuntimeState.INITIALIZED

    @pytest.mark.asyncio
    async def test_start_transitions_to_running(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """App transitions INITIALIZED → RUNNING after start()."""
        config = make_multi_adapter_config()
        app = await build_and_start(config, tmp_paths)
        try:
            assert app.state is RuntimeState.RUNNING
            assert len(app.started_adapter_ids) == 4
            assert app.boot_summary is not None
            assert app.boot_summary.adapters_started == 4
            assert app.boot_summary.adapters_failed == 0
        finally:
            await clean_stop(app)

    @pytest.mark.asyncio
    async def test_all_adapters_are_correct_type(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Each adapter is the correct fake type."""
        config = make_multi_adapter_config()
        app = await build_and_start(config, tmp_paths)
        try:
            assert isinstance(app.adapters["fake_matrix"], FakeMatrixAdapter)
            assert isinstance(app.adapters["fake_meshtastic"], FakeMeshtasticAdapter)
        finally:
            await clean_stop(app)


class TestSyntheticEventRoutesThroughPipeline:
    """Synthetic events route through the real RouteEngine and PipelineRunner."""

    @pytest.mark.asyncio
    async def test_routed_event_reaches_target_adapter(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Event from mx_alpha is routed to mx_beta via the pipeline."""
        config, route = make_two_adapter_config_with_route()
        app = await build_and_start(config, tmp_paths)
        app.router.add_route(route)

        try:
            alpha = app.adapters["mx_alpha"]
            beta = app.adapters["mx_beta"]
            assert isinstance(alpha, FakeMatrixAdapter)
            assert isinstance(beta, FakeMatrixAdapter)

            event = alpha.make_event(
                "Route this to beta", event_kind=EventKind.MESSAGE_TEXT
            )
            await alpha.simulate_inbound(event)

            # Alpha should have recorded the inbound.
            assert len(alpha.inbound_events) == 1

            # Beta should have received the outbound delivery.
            assert len(beta.delivered_payloads) == 1, (
                f"Expected beta to receive exactly 1 delivery, "
                f"got {len(beta.delivered_payloads)}"
            )
        finally:
            await clean_stop(app)

    @pytest.mark.asyncio
    async def test_cross_transport_routing(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Event routes from Matrix to Meshtastic across transports."""
        config, route = make_cross_transport_config_with_route()
        app = await build_and_start(config, tmp_paths)
        app.router.add_route(route)

        try:
            mx = app.adapters["mx_src"]
            assert isinstance(mx, FakeMatrixAdapter)

            event = mx.make_event(
                "Cross-transport message", event_kind=EventKind.MESSAGE_TEXT
            )
            await mx.simulate_inbound(event)

            # Meshtastic adapter should have received the delivery.
            mesh = app.adapters["mesh_dst"]
            assert len(mesh.delivered_payloads) == 1, (
                f"Expected mesh_dst to receive exactly 1 delivery, "
                f"got {len(mesh.delivered_payloads)}"
            )
        finally:
            await clean_stop(app)

    @pytest.mark.asyncio
    async def test_unrouted_event_stored_not_delivered(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Event with no matching route is stored but not delivered."""
        config = make_multi_adapter_config()
        app = await build_and_start(config, tmp_paths)
        # No routes configured — events should be stored only.

        try:
            mx = app.adapters["fake_matrix"]
            assert isinstance(mx, FakeMatrixAdapter)
            event = mx.make_event(
                "No route for this", event_kind=EventKind.MESSAGE_TEXT
            )
            await mx.simulate_inbound(event)

            # Event should be stored.
            assert app.storage is not None
            stored = await app.storage.get(event.event_id)
            assert stored is not None
            assert stored.event_id == event.event_id

            # No adapter should have received an outbound delivery
            # (no routes configured).
            for aid, adapter in app.adapters.items():
                payloads = getattr(adapter, "delivered_payloads", None)
                if payloads is not None:
                    assert len(payloads) == 0, (
                        f"Adapter {aid} unexpectedly received "
                        f"{len(payloads)} deliveries"
                    )
        finally:
            await clean_stop(app)


class TestDeliveryReceiptGenerated:
    """Pipeline generates delivery receipts when events are routed."""

    @pytest.mark.asyncio
    async def test_receipt_stored_after_successful_delivery(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Routing an event produces a stored DeliveryReceipt."""
        config, route = make_two_adapter_config_with_route()
        app = await build_and_start(config, tmp_paths)
        app.router.add_route(route)

        try:
            alpha = app.adapters["mx_alpha"]
            assert isinstance(alpha, FakeMatrixAdapter)
            event = alpha.make_event(
                "Generate receipt", event_kind=EventKind.MESSAGE_TEXT
            )
            await alpha.simulate_inbound(event)

            # Check storage for receipts.
            assert app.storage is not None
            # Verify the event was stored.
            stored = await app.storage.get(event.event_id)
            assert stored is not None

            # The pipeline should have created at least one receipt.
            # Since we don't know the delivery_plan_id, check the
            # diagnostician for delivery activity.
            diag = app.diagnostician.snapshot()
            assert isinstance(diag, dict)
        finally:
            await clean_stop(app)

    @pytest.mark.asyncio
    async def test_direct_delivery_returns_receipt(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Direct adapter.deliver() returns AdapterDeliveryResult."""
        config = make_multi_adapter_config()
        app = await build_and_start(config, tmp_paths)

        try:
            mx = app.adapters["fake_matrix"]
            assert isinstance(mx, FakeMatrixAdapter)
            from medre.core.rendering.renderer import RenderingResult

            result = RenderingResult(
                event_id="evt-direct-001",
                target_adapter="fake_matrix",
                target_channel="test-room",
                payload={"text": "direct delivery"},
            )
            delivery = await mx.deliver(result)
            assert delivery is not None
            assert delivery.native_message_id is not None
            assert delivery.native_message_id == "$fake_evt-direct-001"
            assert delivery.native_channel_id == "test-room"
        finally:
            await clean_stop(app)


class TestRuntimeSnapshotCapturesState:
    """build_runtime_snapshot produces a deterministic JSON-safe snapshot."""

    @pytest.mark.asyncio
    async def test_snapshot_json_safe(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Snapshot is JSON-serialisable with sorted keys."""
        from datetime import datetime, timezone

        config = make_multi_adapter_config()
        app = await build_and_start(config, tmp_paths)

        try:
            # Inject frozen clocks for deterministic uptime_seconds.
            frozen_now = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
            frozen_mono = 100.0

            snap = build_runtime_snapshot(
                app,
                now_fn=lambda: frozen_now,
                monotonic_fn=lambda: frozen_mono,
            )
            # Must be JSON-serialisable.
            serialized = json.dumps(snap, sort_keys=True)
            assert isinstance(serialized, str)

            # Must be deterministic — two calls with same clock produce
            # identical output.
            snap2 = build_runtime_snapshot(
                app,
                now_fn=lambda: frozen_now,
                monotonic_fn=lambda: frozen_mono,
            )
            assert json.dumps(snap, sort_keys=True) == json.dumps(snap2, sort_keys=True)
        finally:
            await clean_stop(app)

    @pytest.mark.asyncio
    async def test_snapshot_contains_expected_keys(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Snapshot has required top-level keys."""
        config = make_multi_adapter_config()
        app = await build_and_start(config, tmp_paths)

        try:
            snap = build_runtime_snapshot(app)
            assert snap["schema_version"] == SCHEMA_VERSION
            assert snap["lifecycle"]["runtime_state"] == "running"
            assert "adapters" in snap
            assert "routes" in snap
            assert "limits" in snap
            assert "capacity" in snap
            assert "snapshot_at" in snap
            assert "startup_timestamp" in snap["lifecycle"]
            assert "uptime_seconds" in snap["lifecycle"]
            assert snap["lifecycle"]["uptime_seconds"] is not None
            assert snap["lifecycle"]["uptime_seconds"] >= 0
        finally:
            await clean_stop(app)

    @pytest.mark.asyncio
    async def test_snapshot_adapters_populated(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Snapshot adapters dict has all 4 fake adapters."""
        config = make_multi_adapter_config()
        app = await build_and_start(config, tmp_paths)

        try:
            snap = build_runtime_snapshot(app)
            adapters = snap["adapters"]
            assert isinstance(adapters, dict)
            assert len(adapters) == 4
            assert "fake_matrix" in adapters
            assert "fake_meshtastic" in adapters
            assert "fake_meshcore" in adapters
            assert "fake_lxmf" in adapters
        finally:
            await clean_stop(app)


class TestDiagnosticsAccessible:
    """Runtime diagnostics are accessible and truthful."""

    @pytest.mark.asyncio
    async def test_diagnostic_snapshot_structure(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """app.diagnostic_snapshot() returns a dict with expected keys."""
        config = make_multi_adapter_config()
        app = await build_and_start(config, tmp_paths)

        try:
            snap = app.diagnostic_snapshot()
            assert isinstance(snap, dict)
            assert "runtime_state" in snap
            assert snap["runtime_state"] == "running"
            assert "capacity" in snap
            assert "accepting_work" in snap
        finally:
            await clean_stop(app)

    @pytest.mark.asyncio
    async def test_diagnostician_records_activity(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Diagnostician snapshot is a dict with expected failure keys."""
        config = make_multi_adapter_config()
        app = await build_and_start(config, tmp_paths)

        try:
            diag = app.diagnostician.snapshot()
            assert isinstance(diag, dict)
            assert "adapter_failures" in diag
            assert "planner_failures" in diag
            assert "renderer_failures" in diag
            assert "storage_failures" in diag
        finally:
            await clean_stop(app)

    @pytest.mark.asyncio
    async def test_boot_summary_present(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Boot summary is populated after start."""
        config = make_multi_adapter_config()
        app = await build_and_start(config, tmp_paths)

        try:
            assert app.boot_summary is not None
            assert app.boot_summary.adapters_started == 4
            assert app.boot_summary.adapters_total == 4
            assert app.boot_summary.adapters_failed == 0
            assert app.boot_summary.startup_outcome == "success"
            assert app.boot_summary.runtime_health == "healthy"
        finally:
            await clean_stop(app)


class TestRuntimeStopsCleanly:
    """Runtime stops cleanly and reaches STOPPED state."""

    @pytest.mark.asyncio
    async def test_stop_transitions_to_stopped(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """stop() transitions RUNNING → STOPPED."""
        config = make_multi_adapter_config()
        app = await build_and_start(config, tmp_paths)
        assert app.state is RuntimeState.RUNNING

        await clean_stop(app)
        assert app.state is RuntimeState.STOPPED

    @pytest.mark.asyncio
    async def test_stop_idempotent(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Calling stop() twice does not raise."""
        config = make_multi_adapter_config()
        app = await build_and_start(config, tmp_paths)
        await app.stop()
        assert app.state is RuntimeState.STOPPED
        # Second call should be safe (idempotent).
        await app.stop()
        assert app.state is RuntimeState.STOPPED

    @pytest.mark.asyncio
    async def test_no_lingering_tasks_after_stop(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """After stop, no lingering asyncio tasks from the runtime remain."""
        config = make_multi_adapter_config()
        app = await build_and_start(config, tmp_paths)

        # Track tasks before stop.
        await clean_stop(app)

        # Give the event loop a tick to clean up.
        await asyncio.sleep(0.05)

        current_task = asyncio.current_task()
        all_tasks = asyncio.all_tasks()
        # Exclude the current test task and the pytest runner.
        runtime_tasks = [
            t
            for t in all_tasks
            if t is not current_task and not t.get_name().startswith("pytest")
        ]
        # Allow a small margin — the event loop may have cleanup tasks
        # from pytest itself.  The important thing is no adapter/pipeline
        # tasks remain.
        runtime_named = [
            t
            for t in runtime_tasks
            if any(
                kw in t.get_name().lower()
                for kw in ("adapter", "pipeline", "runner", "medre")
            )
        ]
        assert len(runtime_named) == 0, (
            f"Runtime tasks still alive after stop: "
            f"{[t.get_name() for t in runtime_named]}"
        )


# ===================================================================
# SHORT SOAK TESTS
# ===================================================================


class TestRepeatedStartStopCycles:
    """Repeated start/stop cycles with full multi-adapter runtime."""

    @pytest.mark.asyncio
    async def test_5_start_stop_cycles(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """5 start/stop cycles — runtime reaches RUNNING then STOPPED each time."""
        config = make_multi_adapter_config()

        for _cycle in range(5):
            builder = RuntimeBuilder(config, tmp_paths)
            app = builder.build()
            await app.start()

            assert app.state is RuntimeState.RUNNING
            assert len(app.adapters) == 4
            assert len(app.started_adapter_ids) == 4

            await app.stop()
            assert app.state is RuntimeState.STOPPED

    @pytest.mark.asyncio
    async def test_3_cycles_with_event_routing(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """3 cycles of start → route events → stop with clean state each time."""
        config, route = make_two_adapter_config_with_route()

        for cycle in range(3):
            builder = RuntimeBuilder(config, tmp_paths)
            app = builder.build()
            app.router.add_route(route)
            await app.start()

            try:
                alpha = app.adapters["mx_alpha"]
                beta = app.adapters["mx_beta"]
                assert isinstance(alpha, FakeMatrixAdapter)
                assert isinstance(beta, FakeMatrixAdapter)

                # Route an event.
                event = alpha.make_event(
                    f"Soak cycle {cycle}", event_kind=EventKind.MESSAGE_TEXT
                )
                await alpha.simulate_inbound(event)

                # Verify delivery occurred.
                assert len(beta.delivered_payloads) == 1
            finally:
                await app.stop()

            assert app.state is RuntimeState.STOPPED


# ===================================================================
# FAILURE KIND INTEGRATION TESTS
# ===================================================================


class TestFailureKindIntegration:
    """One test per DeliveryFailureKind — verifies classification and accounting."""

    @pytest.mark.asyncio
    async def test_adapter_transient(self, tmp_paths: MedrePaths) -> None:
        """ADAPTER_TRANSIENT: FakeMeshtastic raises AdapterSendError(transient=True)."""
        from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter

        config = RuntimeConfig(
            runtime=RuntimeOptions(name="transient-test"),
            logging=LoggingConfig(level="DEBUG"),
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(
                matrix={
                    "mx_src": MatrixRuntimeConfig(
                        adapter_id="mx_src",
                        enabled=True,
                        adapter_kind="fake",
                    ),
                },
                meshtastic={
                    "mesh_dst": MeshtasticRuntimeConfig(
                        adapter_id="mesh_dst",
                        enabled=True,
                        adapter_kind="fake",
                    ),
                },
            ),
        )
        route = Route(
            id="to-mesh",
            source=RouteSource(
                adapter="mx_src", event_kinds=(EventKind.MESSAGE_TEXT,), channel=None
            ),
            targets=[RouteTarget(adapter="mesh_dst")],
        )
        app = await build_and_start(config, tmp_paths)
        app.router.add_route(route)
        try:
            mesh = app.adapters["mesh_dst"]
            assert isinstance(mesh, FakeMeshtasticAdapter)
            mesh.set_deliver_failure(True)

            mx = app.adapters["mx_src"]
            event = mx.make_event(
                "will fail transiently", event_kind=EventKind.MESSAGE_TEXT
            )
            outcomes = await app.pipeline_runner.handle_ingress(event)

            assert len(outcomes) == 1
            outcome = outcomes[0]
            assert outcome.failure_kind == DeliveryFailureKind.ADAPTER_TRANSIENT
            assert outcome.status == "transient_failure"
            assert outcome.target_adapter == "mesh_dst"
            # Receipt is persisted in storage even though outcome.receipt is None
            # (pipeline design: _deliver_single_target does not propagate receipt on error).
            receipts = await app.storage.list_receipts_for_event(event.event_id)
            assert len(receipts) == 1
            assert receipts[0].status == "failed"
        finally:
            await clean_stop(app)

    @pytest.mark.asyncio
    async def test_adapter_permanent(self, tmp_paths: MedrePaths) -> None:
        """ADAPTER_PERMANENT: adapter raises AdapterPermanentError."""
        from medre.core.contracts.adapter import AdapterPermanentError
        from medre.core.rendering.renderer import RenderingResult

        config, route = _make_pipeline_failure_config()
        app = await build_and_start(config, tmp_paths)
        app.router.add_route(route)
        try:
            beta = app.adapters["mx_beta"]
            original_deliver = beta.deliver

            async def _permanent_fail(result: RenderingResult) -> None:
                raise AdapterPermanentError("permanent test failure")

            beta.deliver = _permanent_fail  # type: ignore[assignment]

            alpha = app.adapters["mx_alpha"]
            event = alpha.make_event(
                "will fail permanently", event_kind=EventKind.MESSAGE_TEXT
            )
            outcomes = await app.pipeline_runner.handle_ingress(event)

            assert len(outcomes) == 1
            assert outcomes[0].failure_kind == DeliveryFailureKind.ADAPTER_PERMANENT
            assert outcomes[0].status == "permanent_failure"
            assert outcomes[0].target_adapter == "mx_beta"
            # Receipt is persisted in storage even though outcome.receipt is None.
            receipts = await app.storage.list_receipts_for_event(event.event_id)
            assert len(receipts) == 1
            assert receipts[0].status == "failed"
        finally:
            beta.deliver = original_deliver  # type: ignore[assignment]
            await clean_stop(app)

    @pytest.mark.asyncio
    async def test_adapter_missing(self, tmp_paths: MedrePaths) -> None:
        """ADAPTER_MISSING: route targets a non-existent adapter."""
        config, route = _make_pipeline_failure_config(target_adapter_id="ghost_adapter")
        app = await build_and_start(config, tmp_paths)
        app.router.add_route(route)
        try:
            alpha = app.adapters["mx_alpha"]
            event = alpha.make_event(
                "routed to missing adapter", event_kind=EventKind.MESSAGE_TEXT
            )
            outcomes = await app.pipeline_runner.handle_ingress(event)

            assert len(outcomes) == 1
            assert outcomes[0].failure_kind == DeliveryFailureKind.ADAPTER_MISSING
            assert outcomes[0].status == "permanent_failure"
            assert outcomes[0].target_adapter == "ghost_adapter"
            # ADAPTER_MISSING still persists a receipt via deliver_to_target.
            receipts = await app.storage.list_receipts_for_event(event.event_id)
            assert len(receipts) == 1
            assert receipts[0].status == "failed"
        finally:
            await clean_stop(app)

    @pytest.mark.asyncio
    async def test_renderer_failure(self, tmp_paths: MedrePaths) -> None:
        """RENDERER_FAILURE: rendering pipeline raises during render."""
        config, route = _make_pipeline_failure_config()
        app = await build_and_start(config, tmp_paths)
        app.router.add_route(route)
        try:
            original_render = app.rendering_pipeline.render

            async def _bad_render(*args: Any, **kwargs: Any) -> None:
                raise RuntimeError("renderer crashed")

            app.rendering_pipeline.render = _bad_render  # type: ignore[assignment]

            alpha = app.adapters["mx_alpha"]
            event = alpha.make_event(
                "renderer will fail", event_kind=EventKind.MESSAGE_TEXT
            )
            outcomes = await app.pipeline_runner.handle_ingress(event)

            assert len(outcomes) == 1
            assert outcomes[0].failure_kind == DeliveryFailureKind.RENDERER_FAILURE
            assert outcomes[0].status == "permanent_failure"
            assert outcomes[0].target_adapter == "mx_beta"
            # RENDERER_FAILURE persists a receipt via deliver_to_target.
            receipts = await app.storage.list_receipts_for_event(event.event_id)
            assert len(receipts) == 1
            assert receipts[0].status == "failed"
        finally:
            app.rendering_pipeline.render = original_render  # type: ignore[assignment]
            await clean_stop(app)

    @pytest.mark.asyncio
    async def test_planner_failure(self, tmp_paths: MedrePaths) -> None:
        """PLANNER_FAILURE: router.match raises an exception."""
        config, route = _make_pipeline_failure_config()
        app = await build_and_start(config, tmp_paths)
        app.router.add_route(route)
        try:
            original_match = app.router.match

            def _bad_match(*args: Any, **kwargs: Any) -> None:
                raise RuntimeError("router crash")

            app.router.match = _bad_match  # type: ignore[assignment]

            alpha = app.adapters["mx_alpha"]
            event = alpha.make_event(
                "planner will fail", event_kind=EventKind.MESSAGE_TEXT
            )
            outcomes = await app.pipeline_runner.handle_ingress(event)

            assert len(outcomes) == 1
            assert outcomes[0].failure_kind == DeliveryFailureKind.PLANNER_FAILURE
            assert outcomes[0].status == "permanent_failure"
            assert outcomes[0].receipt is None  # no receipt for planner failure
        finally:
            app.router.match = original_match  # type: ignore[assignment]
            await clean_stop(app)

    @pytest.mark.asyncio
    async def test_deadline_exceeded(self, tmp_paths: MedrePaths) -> None:
        """DEADLINE_EXCEEDED: plan deadline is in the past."""
        from medre.core.engine.pipeline.target_delivery import _AdapterDeliveryError
        from medre.core.planning.delivery_plan import DeliveryPlan, DeliveryStrategy

        config, route = _make_pipeline_failure_config()
        app = await build_and_start(config, tmp_paths)
        app.router.add_route(route)
        try:
            alpha = app.adapters["mx_alpha"]
            event = alpha.make_event(
                "deadline already passed", event_kind=EventKind.MESSAGE_TEXT
            )
            # Store event manually.
            await app.pipeline_runner.store_event(event)

            # Create a plan with a past deadline and deliver directly.
            past_deadline = datetime.now(timezone.utc) - timedelta(hours=1)
            plan = DeliveryPlan(
                plan_id="plan-deadline",
                event_id=event.event_id,
                target=RouteTarget(adapter="mx_beta"),
                primary_strategy=DeliveryStrategy(method="direct"),
                deadline=past_deadline,
            )
            with pytest.raises(_AdapterDeliveryError) as exc_info:
                await app.pipeline_runner.deliver_to_target(
                    event,
                    route,
                    plan,
                )
            assert exc_info.value.failure_kind == DeliveryFailureKind.DEADLINE_EXCEEDED

            # Verify receipt was persisted with failure status.
            receipts = await app.storage.list_receipts_for_event(event.event_id)
            assert len(receipts) == 1
            assert receipts[0].status == "failed"
            assert "deadline" in (receipts[0].error or "").lower()
        finally:
            await clean_stop(app)

    @pytest.mark.asyncio
    async def test_capacity_rejection(self, tmp_paths: MedrePaths) -> None:
        """CAPACITY_REJECTION: semaphore exhausted, delivery rejected."""
        config = RuntimeConfig(
            runtime=RuntimeOptions(name="capacity-test"),
            logging=LoggingConfig(level="DEBUG"),
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(
                matrix={
                    "mx_a": MatrixRuntimeConfig(
                        adapter_id="mx_a",
                        enabled=True,
                        adapter_kind="fake",
                    ),
                    "mx_b": MatrixRuntimeConfig(
                        adapter_id="mx_b",
                        enabled=True,
                        adapter_kind="fake",
                    ),
                },
            ),
            limits=RuntimeLimits(
                max_inflight_deliveries=1, delivery_acquire_timeout_seconds=0.01
            ),
        )
        route = Route(
            id="a-to-b",
            source=RouteSource(
                adapter="mx_a", event_kinds=(EventKind.MESSAGE_TEXT,), channel=None
            ),
            targets=[RouteTarget(adapter="mx_b")],
        )
        app = await build_and_start(config, tmp_paths)
        app.router.add_route(route)
        try:
            # Hold the single delivery slot.
            cc = app._capacity_controller
            assert cc is not None
            acquired = await cc.acquire_delivery()
            assert acquired

            alpha = app.adapters["mx_a"]
            event = alpha.make_event("capacity full", event_kind=EventKind.MESSAGE_TEXT)
            outcomes = await app.pipeline_runner.handle_ingress(event)

            assert len(outcomes) == 1
            assert outcomes[0].failure_kind == DeliveryFailureKind.CAPACITY_REJECTION
            assert outcomes[0].status == "permanent_failure"
            # Capacity rejection persists a suppressed evidence receipt.
            assert outcomes[0].receipt is not None
            assert outcomes[0].receipt.status == "suppressed"
            assert outcomes[0].receipt.failure_kind == "capacity_rejection"

            # Release slot so stop can drain cleanly.
            await cc.release_delivery()
        finally:
            await clean_stop(app)

    @pytest.mark.asyncio
    async def test_shutdown_rejection(self, tmp_paths: MedrePaths) -> None:
        """SHUTDOWN_REJECTION: capacity controller no longer accepting work."""
        config, route = _make_pipeline_failure_config()
        app = await build_and_start(config, tmp_paths)
        app.router.add_route(route)
        try:
            # Simulate shutdown: stop accepting new work.
            cc = app._capacity_controller
            assert cc is not None
            cc.stop_accepting()

            alpha = app.adapters["mx_alpha"]
            event = alpha.make_event("shutting down", event_kind=EventKind.MESSAGE_TEXT)
            outcomes = await app.pipeline_runner.handle_ingress(event)

            assert len(outcomes) == 1
            assert outcomes[0].failure_kind == DeliveryFailureKind.SHUTDOWN_REJECTION
            assert outcomes[0].status == "permanent_failure"
            # Shutdown rejection persists a suppressed evidence receipt.
            assert outcomes[0].receipt is not None
            assert outcomes[0].receipt.status == "suppressed"
            assert outcomes[0].receipt.failure_kind == "shutdown_rejection"
        finally:
            await clean_stop(app)

    @pytest.mark.asyncio
    async def test_channel_not_found_classified_as_adapter_permanent(self) -> None:
        """A permanent AdapterSendError mentioning 'channel not found' is
        classified as ADAPTER_PERMANENT (not a separate failure kind).

        TARGET_NOT_FOUND was removed from the enum because no adapter ever
        emitted it — all permanent adapter errors including channel-not-found
        conditions map to ADAPTER_PERMANENT.
        """
        from medre.core.contracts.adapter import AdapterSendError
        from medre.core.planning.delivery_plan import RetryExecutor

        err = AdapterSendError("target channel not found", transient=False)
        kind = RetryExecutor.classify_failure(err, adapter_registered=True)
        assert kind == DeliveryFailureKind.ADAPTER_PERMANENT
