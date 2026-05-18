"""Track 7: Fake runtime soak and comprehensive happy-path tests.

Diagnostics snapshot stability across cycles, replay delivery isolation,
and full end-to-end pipeline verification — all with fake adapters and
in-memory storage.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from medre.adapters.fake_matrix import FakeMatrixAdapter
from medre.config.paths import MedrePaths, resolve
from medre.runtime.app import RuntimeState
from medre.runtime.builder import RuntimeBuilder
from medre.runtime.snapshot import SCHEMA_VERSION, build_runtime_snapshot

from tests.helpers.fake_runtime import (
    build_and_start,
    clean_stop,
    make_multi_adapter_config,
    make_two_adapter_config_with_route,
    wait_until,
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


# ===================================================================
# SOAK TESTS — Diagnostics & Replay
# ===================================================================


class TestSoakWithDiagnosticsSnapshots:
    """Diagnostics snapshots remain consistent across soak cycles."""

    @pytest.mark.asyncio
    async def test_snapshots_stable_across_3_cycles(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Runtime snapshots have consistent shape across 3 cycles."""
        config = make_multi_adapter_config()

        for _cycle in range(3):
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
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Fresh runtime per cycle has clean diagnostician counters."""
        config = make_multi_adapter_config()

        for _cycle in range(3):
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
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Same adapter accepts repeated deliveries without error."""
        config = make_multi_adapter_config()
        app = await build_and_start(config, tmp_paths)

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
            await clean_stop(app)

    @pytest.mark.asyncio
    async def test_cross_adapter_isolation_across_cycles(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Deliveries to one adapter never appear in another across cycles."""
        config, route = make_two_adapter_config_with_route()

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
        config, route = make_two_adapter_config_with_route()
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
            assert (
                "body" in payload.payload
            )  # MatrixRenderer produces {"body": ..., "msgtype": ...}
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
                "mx_beta",
                "",
                native_id,
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
            await clean_stop(app)
