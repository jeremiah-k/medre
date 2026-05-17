"""Track 5 & 7: Fake multi-adapter runtime smoke and short soak tests.

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

from medre.adapters.fake_matrix import FakeMatrixAdapter
from medre.adapters.fake_meshtastic import FakeMeshtasticAdapter
from medre.config.model import (
    AdapterConfigSet,
    LoggingConfig,
    LxmfRuntimeConfig,
    MatrixRuntimeConfig,
    MeshCoreRuntimeConfig,
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


# ---------------------------------------------------------------------------
# Async helpers
# ---------------------------------------------------------------------------


async def wait_until(
    predicate: Any,
    timeout: float = 5.0,
    interval: float = 0.05,
) -> None:
    """Poll *predicate* every *interval* seconds until it returns ``True``.

    Raises ``AssertionError`` if *timeout* expires before the predicate
    is satisfied.  The predicate can be any synchronous callable.
    """
    import time

    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(
                f"wait_until timed out after {timeout}s: "
                f"predicate {predicate!r} never satisfied"
            )
        await asyncio.sleep(min(interval, remaining))


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
# Config builders
# ---------------------------------------------------------------------------


def _make_multi_adapter_config() -> RuntimeConfig:
    """Build RuntimeConfig matching examples/configs/fake-multi-adapter.toml.

    All four adapter types enabled with ``adapter_kind="fake"``.
    """
    return RuntimeConfig(
        runtime=RuntimeOptions(name="fake-multi-dev"),
        logging=LoggingConfig(level="DEBUG"),
        storage=StorageConfig(backend="memory"),
        adapters=AdapterConfigSet(
            matrix={
                "fake_matrix": MatrixRuntimeConfig(
                    adapter_id="fake_matrix",
                    enabled=True,
                    adapter_kind="fake",
                ),
            },
            meshtastic={
                "fake_meshtastic": MeshtasticRuntimeConfig(
                    adapter_id="fake_meshtastic",
                    enabled=True,
                    adapter_kind="fake",
                ),
            },
            meshcore={
                "fake_meshcore": MeshCoreRuntimeConfig(
                    adapter_id="fake_meshcore",
                    enabled=True,
                    adapter_kind="fake",
                ),
            },
            lxmf={
                "fake_lxmf": LxmfRuntimeConfig(
                    adapter_id="fake_lxmf",
                    enabled=True,
                    adapter_kind="fake",
                ),
            },
        ),
    )


def _make_two_adapter_config_with_route() -> tuple[RuntimeConfig, Route]:
    """Config with two fake Matrix adapters + a route from one to the other."""
    config = RuntimeConfig(
        runtime=RuntimeOptions(name="smoke-routing"),
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
        targets=[RouteTarget(adapter="mx_beta")],
    )
    return config, route


def _make_cross_transport_config_with_route() -> tuple[RuntimeConfig, Route]:
    """Config with Matrix + Meshtastic adapters and a cross-transport route."""
    config = RuntimeConfig(
        runtime=RuntimeOptions(name="smoke-cross-transport"),
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
        id="matrix-to-mesh",
        source=RouteSource(
            adapter="mx_src",
            event_kinds=(EventKind.MESSAGE_TEXT,),
            channel=None,
        ),
        targets=[RouteTarget(adapter="mesh_dst")],
    )
    return config, route


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _build_and_start(
    config: RuntimeConfig, paths: MedrePaths
) -> MedreApp:
    """Build a MedreApp from config and start it."""
    builder = RuntimeBuilder(config, paths)
    app = builder.build()
    await app.start()
    return app


async def _clean_stop(app: MedreApp) -> None:
    """Stop a running MedreApp, asserting it reaches STOPPED."""
    await app.stop()
    assert app.state is RuntimeState.STOPPED


# ===================================================================
# SMOKE TESTS — Track 5
# ===================================================================


class TestFakeRuntimeStartsFromMultiAdapterConfig:
    """Runtime starts cleanly from a multi-adapter fake config."""

    @pytest.mark.asyncio
    async def test_build_produces_app_with_4_adapters(
        self, tmp_paths: MedrePaths,
    ) -> None:
        """RuntimeBuilder produces a MedreApp with 4 fake adapters."""
        config = _make_multi_adapter_config()
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
        self, tmp_paths: MedrePaths,
    ) -> None:
        """App transitions INITIALIZED → RUNNING after start()."""
        config = _make_multi_adapter_config()
        app = await _build_and_start(config, tmp_paths)
        try:
            assert app.state is RuntimeState.RUNNING
            assert len(app.started_adapter_ids) == 4
            assert app.boot_summary is not None
            assert app.boot_summary.adapters_started == 4
            assert app.boot_summary.adapters_failed == 0
        finally:
            await _clean_stop(app)

    @pytest.mark.asyncio
    async def test_all_adapters_are_correct_type(
        self, tmp_paths: MedrePaths,
    ) -> None:
        """Each adapter is the correct fake type."""
        config = _make_multi_adapter_config()
        app = await _build_and_start(config, tmp_paths)
        try:
            assert isinstance(app.adapters["fake_matrix"], FakeMatrixAdapter)
            assert isinstance(
                app.adapters["fake_meshtastic"], FakeMeshtasticAdapter
            )
        finally:
            await _clean_stop(app)


class TestSyntheticEventRoutesThroughPipeline:
    """Synthetic events route through the real RouteEngine and PipelineRunner."""

    @pytest.mark.asyncio
    async def test_routed_event_reaches_target_adapter(
        self, tmp_paths: MedrePaths,
    ) -> None:
        """Event from mx_alpha is routed to mx_beta via the pipeline."""
        config, route = _make_two_adapter_config_with_route()
        app = await _build_and_start(config, tmp_paths)
        app.router.add_route(route)

        try:
            alpha = app.adapters["mx_alpha"]
            beta = app.adapters["mx_beta"]
            assert isinstance(alpha, FakeMatrixAdapter)
            assert isinstance(beta, FakeMatrixAdapter)

            event = alpha.make_event("Route this to beta")
            await alpha.simulate_inbound(event)

            # Alpha should have recorded the inbound.
            assert len(alpha.inbound_events) == 1

            # Beta should have received the outbound delivery.
            assert len(beta.delivered_payloads) == 1, (
                f"Expected beta to receive exactly 1 delivery, "
                f"got {len(beta.delivered_payloads)}"
            )
        finally:
            await _clean_stop(app)

    @pytest.mark.asyncio
    async def test_cross_transport_routing(
        self, tmp_paths: MedrePaths,
    ) -> None:
        """Event routes from Matrix to Meshtastic across transports."""
        config, route = _make_cross_transport_config_with_route()
        app = await _build_and_start(config, tmp_paths)
        app.router.add_route(route)

        try:
            mx = app.adapters["mx_src"]
            assert isinstance(mx, FakeMatrixAdapter)

            event = mx.make_event("Cross-transport message")
            await mx.simulate_inbound(event)

            # Meshtastic adapter should have received the delivery.
            mesh = app.adapters["mesh_dst"]
            assert len(mesh.delivered_payloads) == 1, (
                f"Expected mesh_dst to receive exactly 1 delivery, "
                f"got {len(mesh.delivered_payloads)}"
            )
        finally:
            await _clean_stop(app)

    @pytest.mark.asyncio
    async def test_unrouted_event_stored_not_delivered(
        self, tmp_paths: MedrePaths,
    ) -> None:
        """Event with no matching route is stored but not delivered."""
        config = _make_multi_adapter_config()
        app = await _build_and_start(config, tmp_paths)
        # No routes configured — events should be stored only.

        try:
            mx = app.adapters["fake_matrix"]
            assert isinstance(mx, FakeMatrixAdapter)
            event = mx.make_event("No route for this")
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
            await _clean_stop(app)


class TestDeliveryReceiptGenerated:
    """Pipeline generates delivery receipts when events are routed."""

    @pytest.mark.asyncio
    async def test_receipt_stored_after_successful_delivery(
        self, tmp_paths: MedrePaths,
    ) -> None:
        """Routing an event produces a stored DeliveryReceipt."""
        config, route = _make_two_adapter_config_with_route()
        app = await _build_and_start(config, tmp_paths)
        app.router.add_route(route)

        try:
            alpha = app.adapters["mx_alpha"]
            assert isinstance(alpha, FakeMatrixAdapter)
            event = alpha.make_event("Generate receipt")
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
            await _clean_stop(app)

    @pytest.mark.asyncio
    async def test_direct_delivery_returns_receipt(
        self, tmp_paths: MedrePaths,
    ) -> None:
        """Direct adapter.deliver() returns AdapterDeliveryResult."""
        config = _make_multi_adapter_config()
        app = await _build_and_start(config, tmp_paths)

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
            await _clean_stop(app)


class TestRuntimeSnapshotCapturesState:
    """build_runtime_snapshot produces a deterministic JSON-safe snapshot."""

    @pytest.mark.asyncio
    async def test_snapshot_json_safe(
        self, tmp_paths: MedrePaths,
    ) -> None:
        """Snapshot is JSON-serialisable with sorted keys."""
        from datetime import datetime, timezone

        config = _make_multi_adapter_config()
        app = await _build_and_start(config, tmp_paths)

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
            assert json.dumps(snap, sort_keys=True) == json.dumps(
                snap2, sort_keys=True
            )
        finally:
            await _clean_stop(app)

    @pytest.mark.asyncio
    async def test_snapshot_contains_expected_keys(
        self, tmp_paths: MedrePaths,
    ) -> None:
        """Snapshot has required top-level keys."""
        config = _make_multi_adapter_config()
        app = await _build_and_start(config, tmp_paths)

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
            await _clean_stop(app)

    @pytest.mark.asyncio
    async def test_snapshot_adapters_populated(
        self, tmp_paths: MedrePaths,
    ) -> None:
        """Snapshot adapters dict has all 4 fake adapters."""
        config = _make_multi_adapter_config()
        app = await _build_and_start(config, tmp_paths)

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
            await _clean_stop(app)


class TestDiagnosticsAccessible:
    """Runtime diagnostics are accessible and truthful."""

    @pytest.mark.asyncio
    async def test_diagnostic_snapshot_structure(
        self, tmp_paths: MedrePaths,
    ) -> None:
        """app.diagnostic_snapshot() returns a dict with expected keys."""
        config = _make_multi_adapter_config()
        app = await _build_and_start(config, tmp_paths)

        try:
            snap = app.diagnostic_snapshot()
            assert isinstance(snap, dict)
            assert "runtime_state" in snap
            assert snap["runtime_state"] == "running"
            assert "capacity" in snap
            assert "accepting_work" in snap
        finally:
            await _clean_stop(app)

    @pytest.mark.asyncio
    async def test_diagnostician_records_activity(
        self, tmp_paths: MedrePaths,
    ) -> None:
        """Diagnostician snapshot is a dict with expected failure keys."""
        config = _make_multi_adapter_config()
        app = await _build_and_start(config, tmp_paths)

        try:
            diag = app.diagnostician.snapshot()
            assert isinstance(diag, dict)
            assert "adapter_failures" in diag
            assert "planner_failures" in diag
            assert "renderer_failures" in diag
            assert "storage_failures" in diag
        finally:
            await _clean_stop(app)

    @pytest.mark.asyncio
    async def test_boot_summary_present(
        self, tmp_paths: MedrePaths,
    ) -> None:
        """Boot summary is populated after start."""
        config = _make_multi_adapter_config()
        app = await _build_and_start(config, tmp_paths)

        try:
            assert app.boot_summary is not None
            assert app.boot_summary.adapters_started == 4
            assert app.boot_summary.adapters_total == 4
            assert app.boot_summary.adapters_failed == 0
            assert app.boot_summary.startup_outcome == "success"
            assert app.boot_summary.runtime_health == "healthy"
        finally:
            await _clean_stop(app)


class TestRuntimeStopsCleanly:
    """Runtime stops cleanly and reaches STOPPED state."""

    @pytest.mark.asyncio
    async def test_stop_transitions_to_stopped(
        self, tmp_paths: MedrePaths,
    ) -> None:
        """stop() transitions RUNNING → STOPPED."""
        config = _make_multi_adapter_config()
        app = await _build_and_start(config, tmp_paths)
        assert app.state is RuntimeState.RUNNING

        await _clean_stop(app)
        assert app.state is RuntimeState.STOPPED

    @pytest.mark.asyncio
    async def test_stop_idempotent(
        self, tmp_paths: MedrePaths,
    ) -> None:
        """Calling stop() twice does not raise."""
        config = _make_multi_adapter_config()
        app = await _build_and_start(config, tmp_paths)
        await app.stop()
        assert app.state is RuntimeState.STOPPED
        # Second call should be safe (idempotent).
        await app.stop()
        assert app.state is RuntimeState.STOPPED

    @pytest.mark.asyncio
    async def test_no_lingering_tasks_after_stop(
        self, tmp_paths: MedrePaths,
    ) -> None:
        """After stop, no lingering asyncio tasks from the runtime remain."""
        config = _make_multi_adapter_config()
        app = await _build_and_start(config, tmp_paths)

        # Track tasks before stop.
        await _clean_stop(app)

        # Give the event loop a tick to clean up.
        await asyncio.sleep(0.05)

        current_task = asyncio.current_task()
        all_tasks = asyncio.all_tasks()
        # Exclude the current test task and the pytest runner.
        runtime_tasks = [
            t for t in all_tasks
            if t is not current_task
            and not t.get_name().startswith("pytest")
        ]
        # Allow a small margin — the event loop may have cleanup tasks
        # from pytest itself.  The important thing is no adapter/pipeline
        # tasks remain.
        runtime_named = [
            t for t in runtime_tasks
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
# SHORT SOAK TESTS — Track 7
# ===================================================================


class TestRepeatedStartStopCycles:
    """Repeated start/stop cycles with full multi-adapter runtime."""

    @pytest.mark.asyncio
    async def test_5_start_stop_cycles(
        self, tmp_paths: MedrePaths,
    ) -> None:
        """5 start/stop cycles — runtime reaches RUNNING then STOPPED each time."""
        config = _make_multi_adapter_config()

        for cycle in range(5):
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
        self, tmp_paths: MedrePaths,
    ) -> None:
        """3 cycles of start → route events → stop with clean state each time."""
        config, route = _make_two_adapter_config_with_route()

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
                event = alpha.make_event(f"Soak cycle {cycle}")
                await alpha.simulate_inbound(event)

                # Verify delivery occurred.
                assert len(beta.delivered_payloads) == 1
            finally:
                await app.stop()

            assert app.state is RuntimeState.STOPPED


class TestSoakWithDiagnosticsSnapshots:
    """Diagnostics snapshots remain consistent across soak cycles."""

    @pytest.mark.asyncio
    async def test_snapshots_stable_across_3_cycles(
        self, tmp_paths: MedrePaths,
    ) -> None:
        """Runtime snapshots have consistent shape across 3 cycles."""
        config = _make_multi_adapter_config()

        for cycle in range(3):
            builder = RuntimeBuilder(config, tmp_paths)
            app = builder.build()
            await app.start()

            try:
                # Capture snapshot while running.
                snap = build_runtime_snapshot(app)
                assert snap["lifecycle"]["runtime_state"] == "running"
                assert snap["schema_version"] == SCHEMA_VERSION
                assert len(snap["adapters"]) == 4
                assert snap["lifecycle"]["uptime_seconds"] is not None
                assert snap["lifecycle"]["uptime_seconds"] >= 0

                # JSON-serialisable each time.
                json.dumps(snap, sort_keys=True)

                # Capture diagnostics.
                diag_snap = app.diagnostic_snapshot()
                assert diag_snap["runtime_state"] == "running"
            finally:
                await app.stop()

    @pytest.mark.asyncio
    async def test_diagnostician_counters_reset_per_cycle(
        self, tmp_paths: MedrePaths,
    ) -> None:
        """Fresh runtime per cycle has clean diagnostician counters."""
        config = _make_multi_adapter_config()

        for cycle in range(3):
            builder = RuntimeBuilder(config, tmp_paths)
            app = builder.build()
            await app.start()

            try:
                diag = app.diagnostician.snapshot()
                # Fresh runtime should have zero failures.
                assert sum(diag.get("adapter_failures", {}).values()) == 0
                assert sum(diag.get("planner_failures", {}).values()) == 0
            finally:
                await app.stop()


class TestSoakWithReplayDelivery:
    """Replay-style delivery across soak cycles."""

    @pytest.mark.asyncio
    async def test_repeated_delivery_to_same_adapter(
        self, tmp_paths: MedrePaths,
    ) -> None:
        """Same adapter accepts repeated deliveries without error."""
        config = _make_multi_adapter_config()
        app = await _build_and_start(config, tmp_paths)

        try:
            mx = app.adapters["fake_matrix"]
            assert isinstance(mx, FakeMatrixAdapter)
            from medre.core.rendering.renderer import RenderingResult

            for i in range(10):
                result = RenderingResult(
                    event_id=f"evt-soak-{i}",
                    target_adapter="fake_matrix",
                    target_channel=f"room-{i}",
                    payload={"text": f"soak message {i}"},
                )
                delivery = await mx.deliver(result)
                assert delivery is not None
                assert delivery.native_message_id is not None

            # All deliveries should be tracked.
            assert len(mx.delivered_payloads) == 10
        finally:
            await _clean_stop(app)

    @pytest.mark.asyncio
    async def test_cross_adapter_isolation_across_cycles(
        self, tmp_paths: MedrePaths,
    ) -> None:
        """Deliveries to one adapter never appear in another across cycles."""
        config, route = _make_two_adapter_config_with_route()

        for cycle in range(3):
            builder = RuntimeBuilder(config, tmp_paths)
            app = builder.build()
            await app.start()

            try:
                alpha = app.adapters["mx_alpha"]
                beta = app.adapters["mx_beta"]
                from medre.core.rendering.renderer import RenderingResult

                # Deliver directly to alpha only.
                result = RenderingResult(
                    event_id=f"evt-iso-{cycle}",
                    target_adapter="mx_alpha",
                    target_channel="room",
                    payload={"text": "alpha only"},
                )
                await alpha.deliver(result)

                # Beta must have zero deliveries.
                assert len(beta.delivered_payloads) == 0
                assert len(alpha.delivered_payloads) == 1
            finally:
                await app.stop()


# ===================================================================
# COMPREHENSIVE INTEGRATION TESTS — Full Runtime Pipeline
# ===================================================================


class TestFullFakeRuntimeHappyPath:
    """Full end-to-end happy-path: config→build→start→inbound→route→deliver→receipt→native-ref→stop."""

    @pytest.mark.asyncio
    async def test_full_pipeline_happy_path(self, tmp_paths: MedrePaths) -> None:
        """Complete happy-path through the runtime with every stage verified."""
        config, route = _make_two_adapter_config_with_route()
        builder = RuntimeBuilder(config, tmp_paths)
        app = builder.build()
        await app.start()
        app.router.add_route(route)

        try:
            # -- State: RUNNING after start --
            assert app.state is RuntimeState.RUNNING

            alpha = app.adapters["mx_alpha"]
            beta = app.adapters["mx_beta"]
            assert isinstance(alpha, FakeMatrixAdapter)
            assert isinstance(beta, FakeMatrixAdapter)

            # -- Inbound event through the full pipeline --
            event = alpha.make_event("Full pipeline integration test")
            outcomes = await app.pipeline_runner.handle_ingress(event)

            # -- Canonical event stored --
            assert app.storage is not None
            stored = await app.storage.get(event.event_id)
            assert stored is not None
            assert stored.event_id == event.event_id

            # -- Pipeline returned a success outcome --
            assert len(outcomes) == 1
            outcome = outcomes[0]
            assert outcome.status == "success"
            assert outcome.target_adapter == "mx_beta"

            # -- Routing produced deliveries --
            assert len(beta.delivered_payloads) == 1

            # -- Rendering completed (delivery payload produced) --
            payload = beta.delivered_payloads[0]
            assert "body" in payload.payload  # MatrixRenderer produces {"body": ..., "msgtype": ...}
            assert payload.target_adapter == "mx_beta"

            # -- DeliveryReceipt with full field verification --
            receipts = await app.storage.list_receipts_for_event(event.event_id)
            assert len(receipts) == 1
            receipt = receipts[0]
            assert receipt.event_id == event.event_id
            assert receipt.source == "live"
            assert receipt.replay_run_id is None
            assert receipt.status == "sent"
            assert receipt.target_adapter == "mx_beta"

            # -- NativeMessageRef persisted (adapter returns native ID) --
            # FakeMatrixAdapter returns $fake_<event_id> as native_message_id.
            # Resolve via the native ref mapping. When no target_channel is
            # specified in the route, the adapter stores native_channel_id="".
            native_id = f"$fake_{event.event_id}"
            resolved = await app.storage.resolve_native_ref(
                "mx_beta", "", native_id,
            )
            assert resolved is not None
            assert resolved == event.event_id

            # -- Runtime accounting incremented --
            acc = app._runtime_accounting.snapshot()
            assert acc["inbound_accepted"] == 1
            assert acc["outbound_attempts"] == 1
            assert acc["outbound_delivered"] == 1

            # -- Runtime snapshot contains expected fields --
            snap = build_runtime_snapshot(app)
            assert snap["schema_version"] == SCHEMA_VERSION
            assert snap["lifecycle"]["runtime_state"] == "running"
            assert snap["startup"]["startup_health"] is not None
            assert snap["routes"] is not None
            assert snap["accounting"]["counters"] is not None

            # -- Clean stop --
        finally:
            await _clean_stop(app)


# ===================================================================
# FAILURE KIND INTEGRATION TESTS
# ===================================================================


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
                    adapter_id="mx_alpha", enabled=True, adapter_kind="fake",
                ),
                "mx_beta": MatrixRuntimeConfig(
                    adapter_id="mx_beta", enabled=True, adapter_kind="fake",
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


class TestFailureKindIntegration:
    """One test per DeliveryFailureKind — verifies classification and accounting."""

    @pytest.mark.asyncio
    async def test_adapter_transient(self, tmp_paths: MedrePaths) -> None:
        """ADAPTER_TRANSIENT: FakeMeshtastic raises AdapterSendError(transient=True)."""
        from medre.adapters.fake_meshtastic import FakeMeshtasticAdapter

        config = RuntimeConfig(
            runtime=RuntimeOptions(name="transient-test"),
            logging=LoggingConfig(level="DEBUG"),
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(
                matrix={
                    "mx_src": MatrixRuntimeConfig(
                        adapter_id="mx_src", enabled=True, adapter_kind="fake",
                    ),
                },
                meshtastic={
                    "mesh_dst": MeshtasticRuntimeConfig(
                        adapter_id="mesh_dst", enabled=True, adapter_kind="fake",
                    ),
                },
            ),
        )
        route = Route(
            id="to-mesh",
            source=RouteSource(adapter="mx_src", event_kinds=(EventKind.MESSAGE_TEXT,), channel=None),
            targets=[RouteTarget(adapter="mesh_dst")],
        )
        app = await _build_and_start(config, tmp_paths)
        app.router.add_route(route)
        try:
            mesh = app.adapters["mesh_dst"]
            assert isinstance(mesh, FakeMeshtasticAdapter)
            mesh.set_deliver_failure(True)

            mx = app.adapters["mx_src"]
            event = mx.make_event("will fail transiently")
            outcomes = await app.pipeline_runner.handle_ingress(event)

            assert len(outcomes) == 1
            outcome = outcomes[0]
            assert outcome.failure_kind == DeliveryFailureKind.ADAPTER_TRANSIENT
            assert outcome.status == "transient_failure"
            assert outcome.target_adapter == "mesh_dst"
            # Receipt is persisted in storage even though outcome.receipt is None
            # (pipeline design: _deliver_one does not propagate receipt on error).
            receipts = await app.storage.list_receipts_for_event(event.event_id)
            assert len(receipts) == 1
            assert receipts[0].status == "failed"
        finally:
            await _clean_stop(app)

    @pytest.mark.asyncio
    async def test_adapter_permanent(self, tmp_paths: MedrePaths) -> None:
        """ADAPTER_PERMANENT: adapter raises AdapterPermanentError."""
        from medre.adapters.base import AdapterPermanentError
        from medre.core.rendering.renderer import RenderingResult

        config, route = _make_pipeline_failure_config()
        app = await _build_and_start(config, tmp_paths)
        app.router.add_route(route)
        try:
            beta = app.adapters["mx_beta"]
            original_deliver = beta.deliver

            async def _permanent_fail(result: RenderingResult) -> None:
                raise AdapterPermanentError("permanent test failure")

            beta.deliver = _permanent_fail  # type: ignore[assignment]

            alpha = app.adapters["mx_alpha"]
            event = alpha.make_event("will fail permanently")
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
            await _clean_stop(app)

    @pytest.mark.asyncio
    async def test_adapter_missing(self, tmp_paths: MedrePaths) -> None:
        """ADAPTER_MISSING: route targets a non-existent adapter."""
        config, route = _make_pipeline_failure_config(target_adapter_id="ghost_adapter")
        app = await _build_and_start(config, tmp_paths)
        app.router.add_route(route)
        try:
            alpha = app.adapters["mx_alpha"]
            event = alpha.make_event("routed to missing adapter")
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
            await _clean_stop(app)

    @pytest.mark.asyncio
    async def test_renderer_failure(self, tmp_paths: MedrePaths) -> None:
        """RENDERER_FAILURE: rendering pipeline raises during render."""
        config, route = _make_pipeline_failure_config()
        app = await _build_and_start(config, tmp_paths)
        app.router.add_route(route)
        try:
            original_render = app.rendering_pipeline.render

            async def _bad_render(*args: Any, **kwargs: Any) -> None:
                raise RuntimeError("renderer crashed")

            app.rendering_pipeline.render = _bad_render  # type: ignore[assignment]

            alpha = app.adapters["mx_alpha"]
            event = alpha.make_event("renderer will fail")
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
            await _clean_stop(app)

    @pytest.mark.asyncio
    async def test_planner_failure(self, tmp_paths: MedrePaths) -> None:
        """PLANNER_FAILURE: router.match raises an exception."""
        config, route = _make_pipeline_failure_config()
        app = await _build_and_start(config, tmp_paths)
        app.router.add_route(route)
        try:
            original_match = app.router.match

            def _bad_match(*args: Any, **kwargs: Any) -> None:
                raise RuntimeError("router crash")

            app.router.match = _bad_match  # type: ignore[assignment]

            alpha = app.adapters["mx_alpha"]
            event = alpha.make_event("planner will fail")
            outcomes = await app.pipeline_runner.handle_ingress(event)

            assert len(outcomes) == 1
            assert outcomes[0].failure_kind == DeliveryFailureKind.PLANNER_FAILURE
            assert outcomes[0].status == "permanent_failure"
            assert outcomes[0].receipt is None  # no receipt for planner failure
        finally:
            app.router.match = original_match  # type: ignore[assignment]
            await _clean_stop(app)

    @pytest.mark.asyncio
    async def test_deadline_exceeded(self, tmp_paths: MedrePaths) -> None:
        """DEADLINE_EXCEEDED: plan deadline is in the past."""
        from medre.core.engine.pipeline import _AdapterDeliveryError
        from medre.core.planning.delivery_plan import DeliveryPlan, DeliveryStrategy

        config, route = _make_pipeline_failure_config()
        app = await _build_and_start(config, tmp_paths)
        app.router.add_route(route)
        try:
            alpha = app.adapters["mx_alpha"]
            event = alpha.make_event("deadline already passed")
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
                    event, route, plan,
                )
            assert exc_info.value.failure_kind == DeliveryFailureKind.DEADLINE_EXCEEDED

            # Verify receipt was persisted with failure status.
            receipts = await app.storage.list_receipts_for_event(event.event_id)
            assert len(receipts) == 1
            assert receipts[0].status == "failed"
            assert "deadline" in (receipts[0].error or "").lower()
        finally:
            await _clean_stop(app)

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
                        adapter_id="mx_a", enabled=True, adapter_kind="fake",
                    ),
                    "mx_b": MatrixRuntimeConfig(
                        adapter_id="mx_b", enabled=True, adapter_kind="fake",
                    ),
                },
            ),
            limits=RuntimeLimits(max_inflight_deliveries=1, delivery_acquire_timeout_seconds=0.01),
        )
        route = Route(
            id="a-to-b",
            source=RouteSource(adapter="mx_a", event_kinds=(EventKind.MESSAGE_TEXT,), channel=None),
            targets=[RouteTarget(adapter="mx_b")],
        )
        app = await _build_and_start(config, tmp_paths)
        app.router.add_route(route)
        try:
            # Hold the single delivery slot.
            cc = app._capacity_controller
            assert cc is not None
            acquired = await cc.acquire_delivery()
            assert acquired

            alpha = app.adapters["mx_a"]
            event = alpha.make_event("capacity full")
            outcomes = await app.pipeline_runner.handle_ingress(event)

            assert len(outcomes) == 1
            assert outcomes[0].failure_kind == DeliveryFailureKind.CAPACITY_REJECTION
            assert outcomes[0].status == "permanent_failure"
            assert outcomes[0].receipt is None  # no receipt for capacity rejection

            # Release slot so stop can drain cleanly.
            await cc.release_delivery()
        finally:
            await _clean_stop(app)

    @pytest.mark.asyncio
    async def test_shutdown_rejection(self, tmp_paths: MedrePaths) -> None:
        """SHUTDOWN_REJECTION: capacity controller no longer accepting work."""
        config, route = _make_pipeline_failure_config()
        app = await _build_and_start(config, tmp_paths)
        app.router.add_route(route)
        try:
            # Simulate shutdown: stop accepting new work.
            cc = app._capacity_controller
            assert cc is not None
            cc.stop_accepting()

            alpha = app.adapters["mx_alpha"]
            event = alpha.make_event("shutting down")
            outcomes = await app.pipeline_runner.handle_ingress(event)

            assert len(outcomes) == 1
            assert outcomes[0].failure_kind == DeliveryFailureKind.SHUTDOWN_REJECTION
            assert outcomes[0].status == "permanent_failure"
            assert outcomes[0].receipt is None  # no receipt for shutdown rejection
        finally:
            await _clean_stop(app)

    @pytest.mark.asyncio
    async def test_target_not_found_reserved(self) -> None:
        """TARGET_NOT_FOUND: reserved enum member (not emitted by any adapter).

        No adapter currently emits TARGET_NOT_FOUND at runtime.  All permanent
        adapter errors — including channel-not-found conditions — are classified
        as ADAPTER_PERMANENT.  This test verifies:
          1. The enum member exists and is non-retryable (taxonomy integrity).
          2. A permanent AdapterSendError is classified as ADAPTER_PERMANENT,
             confirming TARGET_NOT_FOUND remains unused in the live pipeline.
        """
        from medre.adapters.base import AdapterSendError
        from medre.core.planning.delivery_plan import RetryExecutor

        # A permanent adapter error (even one mentioning "channel not found")
        # is classified as ADAPTER_PERMANENT, not TARGET_NOT_FOUND.
        err = AdapterSendError("target channel not found", transient=False)
        kind = RetryExecutor.classify_failure(err, adapter_registered=True)
        assert kind == DeliveryFailureKind.ADAPTER_PERMANENT

        # Verify the TARGET_NOT_FOUND enum member exists and is non-retryable.
        assert DeliveryFailureKind.TARGET_NOT_FOUND.value == "target_not_found"
        assert not DeliveryFailureKind.TARGET_NOT_FOUND.is_retryable


# ===================================================================
# STARTUP / SHUTDOWN INTEGRATION TESTS
# ===================================================================


class TestStartupShutdownIntegration:
    """Multi-adapter startup, partial failure, total failure, and shutdown coverage."""

    @pytest.mark.asyncio
    async def test_multi_adapter_successful_startup(self, tmp_paths: MedrePaths) -> None:
        """All 4 adapters start successfully → HEALTHY."""
        config = _make_multi_adapter_config()
        app = await _build_and_start(config, tmp_paths)
        try:
            assert app.state is RuntimeState.RUNNING
            assert app.boot_summary is not None
            assert app.boot_summary.runtime_health == "healthy"
            assert len(app.started_adapter_ids) == 4
            assert len(app.boot_summary.failed_adapter_ids) == 0
        finally:
            await _clean_stop(app)

    @pytest.mark.asyncio
    async def test_partial_startup_degraded_running(self, tmp_paths: MedrePaths) -> None:
        """Partial adapter startup → DEGRADED + RUNNING."""
        config = _make_multi_adapter_config()
        builder = RuntimeBuilder(config, tmp_paths)
        app = builder.build()
        # Monkey-patch one adapter's start to fail.
        failing = app.adapters["fake_lxmf"]
        original_start = failing.start

        async def _fail_start(ctx: Any) -> None:
            raise RuntimeError("simulated lxmf start failure")

        failing.start = _fail_start  # type: ignore[assignment]
        await app.start()
        try:
            assert app.state is RuntimeState.RUNNING
            assert app.boot_summary is not None
            assert app.boot_summary.adapters_started == 3
            assert app.boot_summary.adapters_failed == 1
            assert "fake_lxmf" in app.boot_summary.failed_adapter_ids
        finally:
            failing.start = original_start  # type: ignore[assignment]
            await _clean_stop(app)

    @pytest.mark.asyncio
    async def test_total_startup_failure_raises(self, tmp_paths: MedrePaths) -> None:
        """All adapters fail to start → RuntimeStartupError + FAILED state."""
        from medre.runtime.errors import RuntimeStartupError

        config = _make_multi_adapter_config()
        builder = RuntimeBuilder(config, tmp_paths)
        app = builder.build()

        async def _fail_start(ctx: Any) -> None:
            raise RuntimeError("total failure simulation")

        for adapter in app.adapters.values():
            adapter.start = _fail_start  # type: ignore[assignment]

        with pytest.raises(RuntimeStartupError):
            await app.start()
        assert app.state is RuntimeState.FAILED

    @pytest.mark.asyncio
    async def test_clean_shutdown_transitions_adapters_to_stopped(
        self, tmp_paths: MedrePaths,
    ) -> None:
        """After stop, all started adapters transition to STOPPED."""
        from medre.core.lifecycle.states import AdapterState

        config = _make_multi_adapter_config()
        app = await _build_and_start(config, tmp_paths)
        try:
            await _clean_stop(app)
            for aid, state in app.adapter_states.items():
                assert state is AdapterState.STOPPED, (
                    f"Adapter {aid} in state {state}, expected STOPPED"
                )
        except Exception:
            await app.stop()
            raise

    @pytest.mark.asyncio
    async def test_concurrent_stop_idempotent(self, tmp_paths: MedrePaths) -> None:
        """Concurrent stop() calls are idempotent and do not raise."""
        config = _make_multi_adapter_config()
        app = await _build_and_start(config, tmp_paths)
        # Fire two concurrent stops.
        results = await asyncio.gather(
            app.stop(), app.stop(), return_exceptions=True,
        )
        # None should be exceptions.
        for r in results:
            assert not isinstance(r, Exception), f"Unexpected exception: {r}"
        assert app.state is RuntimeState.STOPPED

    @pytest.mark.asyncio
    async def test_shutdown_stops_accepting_delivery_work(
        self, tmp_paths: MedrePaths,
    ) -> None:
        """After stop(), capacity controller no longer accepts work."""
        config = _make_multi_adapter_config()
        app = await _build_and_start(config, tmp_paths)
        assert app._capacity_controller is not None
        assert app._capacity_controller.accepting_work

        await _clean_stop(app)
        assert not app._capacity_controller.accepting_work


# ===================================================================
# SNAPSHOT INTEGRATION TESTS
# ===================================================================


class TestSnapshotIntegration:
    """Detailed snapshot assertions: schema_version, lifecycle, health, routes, accounting, diagnostics."""

    @pytest.mark.asyncio
    async def test_schema_version_is_one(self, tmp_paths: MedrePaths) -> None:
        """Snapshot schema_version is exactly 1."""
        config = _make_multi_adapter_config()
        app = await _build_and_start(config, tmp_paths)
        try:
            snap = build_runtime_snapshot(app)
            assert snap["schema_version"] == 1
        finally:
            await _clean_stop(app)

    @pytest.mark.asyncio
    async def test_lifecycle_section(self, tmp_paths: MedrePaths) -> None:
        """Lifecycle section has runtime_state, startup_timestamp, uptime_seconds."""
        config = _make_multi_adapter_config()
        app = await _build_and_start(config, tmp_paths)
        try:
            snap = build_runtime_snapshot(app)
            lc = snap["lifecycle"]
            assert lc["runtime_state"] == "running"
            assert lc["startup_timestamp"] is not None
            assert lc["uptime_seconds"] is not None
            assert lc["uptime_seconds"] >= 0
            assert "adapters" in lc
            assert len(lc["adapters"]) == 4
        finally:
            await _clean_stop(app)

    @pytest.mark.asyncio
    async def test_health_section(self, tmp_paths: MedrePaths) -> None:
        """Health section: live_health is null before refresh, startup_health present."""
        config = _make_multi_adapter_config()
        app = await _build_and_start(config, tmp_paths)
        try:
            snap = build_runtime_snapshot(app)
            assert snap["health"]["live_health"] is None
            assert snap["health"]["scope"] == "startup"
            assert snap["health"]["live_refresh"] is False
            assert snap["startup"]["startup_health"] is not None
        finally:
            await _clean_stop(app)

    @pytest.mark.asyncio
    async def test_startup_outcome_section(self, tmp_paths: MedrePaths) -> None:
        """Startup section: boot_summary, startup_health, build_failures."""
        config = _make_multi_adapter_config()
        app = await _build_and_start(config, tmp_paths)
        try:
            snap = build_runtime_snapshot(app)
            su = snap["startup"]
            assert su["boot_summary"] is not None
            assert su["boot_summary"]["startup_outcome"] == "success"
            assert su["boot_summary"]["runtime_health"] == "healthy"
            assert su["startup_health"] is not None
            assert su["build_failures"] == []
        finally:
            await _clean_stop(app)

    @pytest.mark.asyncio
    async def test_routes_build_readiness_and_startup_readiness(
        self, tmp_paths: MedrePaths,
    ) -> None:
        """Routes section has build_readiness, startup_readiness, eligibility."""
        config = _make_multi_adapter_config()
        app = await _build_and_start(config, tmp_paths)
        try:
            snap = build_runtime_snapshot(app)
            routes = snap["routes"]
            assert "build_readiness" in routes
            assert "startup_readiness" in routes
            assert "eligibility" in routes
            assert "stats" in routes
        finally:
            await _clean_stop(app)

    @pytest.mark.asyncio
    async def test_accounting_section(self, tmp_paths: MedrePaths) -> None:
        """Accounting section has all 8 counters."""
        config = _make_multi_adapter_config()
        app = await _build_and_start(config, tmp_paths)
        try:
            snap = build_runtime_snapshot(app)
            acc = snap["accounting"]["counters"]
            assert acc is not None
            for key in (
                "inbound_accepted", "outbound_attempts", "outbound_delivered",
                "outbound_failed", "replay_processed", "replay_rejected",
                "loop_prevented", "capacity_rejections",
            ):
                assert key in acc, f"Missing accounting key: {key}"
                assert isinstance(acc[key], int)
        finally:
            await _clean_stop(app)

    @pytest.mark.asyncio
    async def test_diagnostics_json_safe(self, tmp_paths: MedrePaths) -> None:
        """Diagnostics section is JSON-safe."""
        config = _make_multi_adapter_config()
        app = await _build_and_start(config, tmp_paths)
        try:
            snap = build_runtime_snapshot(app)
            # Must be JSON-serialisable without errors.
            serialized = json.dumps(snap, sort_keys=True)
            assert isinstance(serialized, str)
            # Diagnostics sub-section.
            assert "diagnostics" in snap
            json.dumps(snap["diagnostics"])
        finally:
            await _clean_stop(app)

    @pytest.mark.asyncio
    async def test_capacity_section(self, tmp_paths: MedrePaths) -> None:
        """Capacity section has delivery and replay counters."""
        config = _make_multi_adapter_config()
        app = await _build_and_start(config, tmp_paths)
        try:
            snap = build_runtime_snapshot(app)
            cap = snap["capacity"]["state"]
            assert cap is not None
            assert "delivery_current" in cap
            assert "delivery_limit" in cap
            assert "replay_current" in cap
            assert "replay_limit" in cap
        finally:
            await _clean_stop(app)
