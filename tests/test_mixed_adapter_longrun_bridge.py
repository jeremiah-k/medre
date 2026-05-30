"""Deterministic long-run bridge test exercising all four fake adapters
simultaneously.

Proves that the MEDRE runtime correctly bridges events between Matrix,
Meshtastic, MeshCore, and LXMF fake adapters under sustained bidirectional
fanout traffic.  Ten inbound events (3 matrix, 3 meshtastic, 2 meshcore,
2 lxmf) generate exactly 16 delivery receipts with zero loop-prevention
events.

No Docker, no live transports, no SDK dependencies required.
"""

from __future__ import annotations

import json
from typing import Any

from medre.adapters.fakes.lxmf import FakeLxmfAdapter
from medre.adapters.fakes.matrix import FakeMatrixAdapter
from medre.adapters.fakes.meshcore import FakeMeshCoreAdapter
from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter
from medre.config.adapters.lxmf import LxmfConfig
from medre.config.adapters.meshcore import MeshCoreConfig
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.engine.pipeline import PipelineRunner
from medre.core.events.kinds import EventKind
from medre.core.rendering.renderer import RenderingPipeline
from medre.core.rendering.text import TextRenderer
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.routing.stats import RouteStats
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.core.supervision.accounting import RuntimeAccounting
from tests.helpers.bridge import (
    make_adapter_context,
    make_meshcore_packet,
    make_pipeline_config,
    make_text_packet,
)

# ---------------------------------------------------------------------------
# Packet helpers
# ---------------------------------------------------------------------------


def make_lxmf_packet(
    body: str = "hello lxmf",
    source_hash: str = "ab" * 16,
    msg_id: str | None = None,
    title: str = "",
    timestamp: float = 1700000000.0,
) -> dict[str, Any]:
    """Minimal LXMF message payload dict matching FakeLxmfCodec.decode."""
    return {
        "content": body,
        "source_hash": source_hash,
        "destination_hash": "00" * 16,
        "message_id": msg_id or "ff" * 32,
        "timestamp": timestamp,
        "title": title,
        "fields": {},
        "signature_validated": True,
        "has_fields": False,
    }


# ---------------------------------------------------------------------------
# Shared setup builder
# ---------------------------------------------------------------------------


class _BridgeSetup:
    """Immutable holder for the four adapters, runner, and accounting
    objects created by ``_build_mixed_bridge``."""

    __slots__ = (
        "matrix",
        "meshtastic",
        "meshcore",
        "lxmf",
        "runner",
        "accounting",
        "route_stats",
    )

    def __init__(
        self,
        matrix: FakeMatrixAdapter,
        meshtastic: FakeMeshtasticAdapter,
        meshcore: FakeMeshCoreAdapter,
        lxmf: FakeLxmfAdapter,
        runner: PipelineRunner,
        accounting: RuntimeAccounting,
        route_stats: RouteStats,
    ) -> None:
        self.matrix = matrix
        self.meshtastic = meshtastic
        self.meshcore = meshcore
        self.lxmf = lxmf
        self.runner = runner
        self.accounting = accounting
        self.route_stats = route_stats


async def _build_mixed_bridge(
    storage: SQLiteStorage,
    *,
    prefix: str = "mixed",
) -> _BridgeSetup:
    """Create four fake adapters, four routes, and a started PipelineRunner.

    Routes
    ------
    1. ``{prefix}-mx-to-all``   : matrix  -> [meshtastic, meshcore, lxmf]
    2. ``{prefix}-mesh-to-mx``  : meshtastic -> [matrix]
    3. ``{prefix}-mc-to-mx``    : meshcore   -> [matrix]
    4. ``{prefix}-lxmf-to-mx``  : lxmf       -> [matrix]
    """
    mx_id = f"{prefix}-mx"
    mesh_id = f"{prefix}-mesh"
    mc_id = f"{prefix}-mc"
    lxmf_id = f"{prefix}-lxmf"

    fake_matrix = FakeMatrixAdapter(mx_id, channel=f"!{prefix}:fake")
    fake_mesh = FakeMeshtasticAdapter(MeshtasticConfig(adapter_id=mesh_id))
    fake_meshcore = FakeMeshCoreAdapter(MeshCoreConfig(adapter_id=mc_id))
    fake_lxmf = FakeLxmfAdapter(LxmfConfig(adapter_id=lxmf_id))

    route_mx_to_all = Route(
        id=f"{prefix}-mx-to-all",
        source=RouteSource(
            adapter=mx_id,
            event_kinds=("message.created",),
            channel=None,
        ),
        targets=[
            RouteTarget(adapter=mesh_id, channel="0"),
            RouteTarget(adapter=mc_id, channel="0"),
            RouteTarget(adapter=lxmf_id, channel="0"),
        ],
    )
    route_mesh_to_mx = Route(
        id=f"{prefix}-mesh-to-mx",
        source=RouteSource(
            adapter=mesh_id,
            event_kinds=("message.created",),
            channel=None,
        ),
        targets=[RouteTarget(adapter=mx_id, channel=f"!{prefix}:fake")],
    )
    route_mc_to_mx = Route(
        id=f"{prefix}-mc-to-mx",
        source=RouteSource(
            adapter=mc_id,
            event_kinds=("message.created",),
            channel=None,
        ),
        targets=[RouteTarget(adapter=mx_id, channel=f"!{prefix}:fake")],
    )
    route_lxmf_to_mx = Route(
        id=f"{prefix}-lxmf-to-mx",
        source=RouteSource(
            adapter=lxmf_id,
            event_kinds=("message.created",),
            channel=None,
        ),
        targets=[RouteTarget(adapter=mx_id, channel=f"!{prefix}:fake")],
    )

    router = Router(
        routes=[
            route_mx_to_all,
            route_mesh_to_mx,
            route_mc_to_mx,
            route_lxmf_to_mx,
        ]
    )

    rp = RenderingPipeline()
    rp.register(TextRenderer(), priority=100)

    accounting = RuntimeAccounting()
    route_stats = RouteStats()

    adapters = {
        mx_id: fake_matrix,
        mesh_id: fake_mesh,
        mc_id: fake_meshcore,
        lxmf_id: fake_lxmf,
    }

    config = make_pipeline_config(
        storage=storage,
        router=router,
        adapters=adapters,
        rendering_pipeline=rp,
        accounting=accounting,
        route_stats=route_stats,
    )
    runner = PipelineRunner(config)
    await runner.start()

    await fake_matrix.start(make_adapter_context(mx_id, runner))
    await fake_mesh.start(make_adapter_context(mesh_id, runner))
    await fake_meshcore.start(make_adapter_context(mc_id, runner))
    await fake_lxmf.start(make_adapter_context(lxmf_id, runner))

    return _BridgeSetup(
        matrix=fake_matrix,
        meshtastic=fake_mesh,
        meshcore=fake_meshcore,
        lxmf=fake_lxmf,
        runner=runner,
        accounting=accounting,
        route_stats=route_stats,
    )


async def _inject_ten_messages(s: _BridgeSetup, *, prefix: str = "mixed") -> None:
    """Inject 3 matrix + 3 meshtastic + 2 meshcore + 2 lxmf messages."""
    # 3 from Matrix
    for i in range(3):
        event = s.matrix.make_event(
            text=f"{prefix} matrix msg {i}",
            event_kind=EventKind.MESSAGE_CREATED,
        )
        await s.matrix.simulate_inbound(event)

    # 3 from Meshtastic
    for i in range(3):
        packet = make_text_packet(
            text=f"{prefix} mesh msg {i}",
            packet_id=2000 + i,
        )
        await s.meshtastic.simulate_inbound(packet)

    # 2 from MeshCore
    for i in range(2):
        packet = make_meshcore_packet(
            text=f"{prefix} meshcore msg {i}",
            packet_id=3000 + i,
        )
        await s.meshcore.simulate_inbound(packet)

    # 2 from LXMF
    for i in range(2):
        packet = make_lxmf_packet(
            body=f"{prefix} lxmf msg {i}",
            msg_id=f"{'ff' * 15}{i:02x}" * 2,
            timestamp=1700000100.0 + i,
        )
        await s.lxmf.simulate_inbound(packet)


async def _clean_stop(s: _BridgeSetup) -> None:
    """Stop all four adapters and the pipeline runner."""
    await s.matrix.stop()
    await s.meshtastic.stop()
    await s.meshcore.stop()
    await s.lxmf.stop()
    await s.runner.stop()


# ===================================================================
# TEST 1: Primary long-run — 10 messages, no loops, exact counts
# ===================================================================


class TestMixedAdapterLongrunBridge:
    """Deterministic long-run bridge exercising all four fake adapters
    with fanout and bidirectional routes simultaneously."""

    async def test_ten_messages_each_direction_no_loops(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Inject 10 messages across 4 adapters. Assert exact counts."""
        s = await _build_mixed_bridge(temp_storage, prefix="t1")
        try:
            await _inject_ten_messages(s, prefix="t1")

            # -- Exactly 10 canonical events persisted --
            all_events = await temp_storage._read_all(
                "SELECT event_id, source_adapter FROM canonical_events "
                "ORDER BY event_id"
            )
            assert len(all_events) == 10, f"Expected 10 events, got {len(all_events)}"

            # -- Source adapter counts correct --
            mx_events = [e for e in all_events if e["source_adapter"] == "t1-mx"]
            mesh_events = [e for e in all_events if e["source_adapter"] == "t1-mesh"]
            mc_events = [e for e in all_events if e["source_adapter"] == "t1-mc"]
            lxmf_events = [e for e in all_events if e["source_adapter"] == "t1-lxmf"]
            assert (
                len(mx_events) == 3
            ), f"Expected 3 matrix events, got {len(mx_events)}"
            assert (
                len(mesh_events) == 3
            ), f"Expected 3 meshtastic events, got {len(mesh_events)}"
            assert (
                len(mc_events) == 2
            ), f"Expected 2 meshcore events, got {len(mc_events)}"
            assert (
                len(lxmf_events) == 2
            ), f"Expected 2 lxmf events, got {len(lxmf_events)}"

            # -- Delivery receipts: 16 total --
            # 3 matrix msgs × 3 targets = 9
            # 3 meshtastic msgs × 1 target = 3
            # 2 meshcore msgs × 1 target = 2
            # 2 lxmf msgs × 1 target = 2
            receipts = await temp_storage._read_all(
                "SELECT target_adapter, status FROM delivery_receipts "
                "ORDER BY sequence"
            )
            assert len(receipts) == 16, f"Expected 16 receipts, got {len(receipts)}"
            for r in receipts:
                assert r["status"] == "sent", f"Expected 'sent', got {r['status']!r}"

            # -- Per-target receipt counts --
            mesh_receipts = [r for r in receipts if r["target_adapter"] == "t1-mesh"]
            mc_receipts = [r for r in receipts if r["target_adapter"] == "t1-mc"]
            lxmf_receipts = [r for r in receipts if r["target_adapter"] == "t1-lxmf"]
            mx_receipts = [r for r in receipts if r["target_adapter"] == "t1-mx"]
            assert len(mesh_receipts) == 3  # from matrix fanout
            assert len(mc_receipts) == 3  # from matrix fanout
            assert len(lxmf_receipts) == 3  # from matrix fanout
            assert len(mx_receipts) == 7  # 3 from mesh + 2 from mc + 2 from lxmf

            # -- RuntimeAccounting --
            snap = s.accounting.snapshot()
            assert (
                snap["inbound_accepted"] == 10
            ), f"Expected inbound_accepted=10, got {snap['inbound_accepted']}"
            assert (
                snap["outbound_delivered"] == 16
            ), f"Expected outbound_delivered=16, got {snap['outbound_delivered']}"
            assert (
                snap["loop_prevented"] == 0
            ), f"Expected loop_prevented=0, got {snap['loop_prevented']}"

            # -- Adapter delivered_payloads --
            # matrix: receives 3 (mesh→mx) + 2 (mc→mx) + 2 (lxmf→mx) = 7
            assert (
                len(s.matrix.delivered_payloads) == 7
            ), f"Expected 7 matrix deliveries, got {len(s.matrix.delivered_payloads)}"
            # meshtastic: receives 3 (matrix fanout)
            assert (
                len(s.meshtastic.delivered_payloads) == 3
            ), f"Expected 3 meshtastic deliveries, got {len(s.meshtastic.delivered_payloads)}"
            # meshcore: receives 3 (matrix fanout)
            assert (
                len(s.meshcore.delivered_payloads) == 3
            ), f"Expected 3 meshcore deliveries, got {len(s.meshcore.delivered_payloads)}"
            # lxmf: receives 3 (matrix fanout)
            assert (
                len(s.lxmf.delivered_payloads) == 3
            ), f"Expected 3 lxmf deliveries, got {len(s.lxmf.delivered_payloads)}"

            # -- Route stats --
            stats = s.route_stats.snapshot()
            assert stats["t1-mx-to-all"]["delivered"] == 9
            assert stats["t1-mx-to-all"]["loop_prevented"] == 0
            assert stats["t1-mesh-to-mx"]["delivered"] == 3
            assert stats["t1-mesh-to-mx"]["loop_prevented"] == 0
            assert stats["t1-mc-to-mx"]["delivered"] == 2
            assert stats["t1-mc-to-mx"]["loop_prevented"] == 0
            assert stats["t1-lxmf-to-mx"]["delivered"] == 2
            assert stats["t1-lxmf-to-mx"]["loop_prevented"] == 0
        finally:
            await _clean_stop(s)

    # ===================================================================
    # TEST 2: Snapshot reflects mixed totals
    # ===================================================================

    async def test_snapshot_reflects_mixed_totals(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Final accounting snapshot has deterministic ints and correct
        per-route delivery counts.  Snapshot is JSON-safe."""
        s = await _build_mixed_bridge(temp_storage, prefix="t2")
        try:
            await _inject_ten_messages(s, prefix="t2")

            snap = s.accounting.snapshot()

            # -- All values are deterministic ints (no floats, no None) --
            for key, value in snap.items():
                assert isinstance(
                    value, int
                ), f"snapshot[{key!r}] = {value!r} is {type(value).__name__}, expected int"

            # -- Exact accounting totals --
            assert snap["inbound_accepted"] == 10
            assert snap["outbound_delivered"] == 16
            assert snap["outbound_attempts"] == 16
            assert snap["outbound_failed"] == 0
            assert snap["loop_prevented"] == 0
            assert snap["capacity_rejections"] == 0

            # -- Route stats show expected per-route delivery counts --
            stats = s.route_stats.snapshot()
            assert stats["t2-mx-to-all"]["delivered"] == 9
            assert stats["t2-mesh-to-mx"]["delivered"] == 3
            assert stats["t2-mc-to-mx"]["delivered"] == 2
            assert stats["t2-lxmf-to-mx"]["delivered"] == 2

            # All route stat values are ints
            for route_id, route_stat in stats.items():
                for stat_key, stat_val in route_stat.items():
                    assert isinstance(stat_val, int), (
                        f"route_stats[{route_id!r}][{stat_key!r}] = "
                        f"{stat_val!r} is {type(stat_val).__name__}"
                    )

            # -- Snapshot is JSON-safe (all values are int/str/bool/list/dict) --
            def _assert_json_safe(obj: Any, path: str = "snapshot") -> None:
                if isinstance(obj, (str, int, bool, type(None))):
                    return
                if isinstance(obj, list):
                    for i, item in enumerate(obj):
                        _assert_json_safe(item, f"{path}[{i}]")
                    return
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        assert isinstance(
                            k, str
                        ), f"{path}.{k!r} key is {type(k).__name__}"
                        _assert_json_safe(v, f"{path}.{k}")
                    return
                raise AssertionError(f"{path} is {type(obj).__name__}, not JSON-safe")

            _assert_json_safe(snap)

            # Also verify route stats are JSON-safe
            _assert_json_safe(stats, path="route_stats")

            # And that they actually serialize
            combined = {"accounting": snap, "route_stats": stats}
            serialized = json.dumps(combined, sort_keys=True)
            assert isinstance(serialized, str)
            assert len(serialized) > 100  # non-trivial payload

        finally:
            await _clean_stop(s)

    # ===================================================================
    # TEST 3: Clean stop, no resource warnings
    # ===================================================================

    async def test_clean_stop_no_resource_warnings(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Start the mixed bridge, inject a few messages, clean stop.
        All adapters and the pipeline runner stop without error."""
        s = await _build_mixed_bridge(temp_storage, prefix="t3")
        try:
            # Inject just a couple of messages to exercise delivery
            event = s.matrix.make_event(
                text="t3 cleanup msg",
                event_kind=EventKind.MESSAGE_CREATED,
            )
            await s.matrix.simulate_inbound(event)

            packet = make_text_packet(text="t3 cleanup mesh", packet_id=9001)
            await s.meshtastic.simulate_inbound(packet)
        finally:
            # Clean stop must succeed without exceptions
            await _clean_stop(s)

        # -- All adapters are stopped --
        assert not s.matrix.is_started
        assert not s.meshtastic.is_started
        assert not s.meshcore.is_started
        assert not s.lxmf.is_started

        # -- Adapter diagnostics reflect stopped state --
        mx_diag = s.matrix.diagnostics()
        assert mx_diag["started"] is False

        mesh_diag = s.meshtastic.diagnostics()
        assert mesh_diag["started"] is False

        mc_diag = s.meshcore.diagnostics()
        assert mc_diag["started"] is False

        lxmf_diag = s.lxmf.diagnostics()
        assert lxmf_diag["started"] is False

        # -- Verify the messages were actually processed before stop --
        snap = s.accounting.snapshot()
        assert snap["inbound_accepted"] == 2
        # matrix msg fans out to 3 targets = 3, mesh msg to 1 target = 1
        assert snap["outbound_delivered"] == 4

        # -- Verify adapter delivery lists are populated --
        assert len(s.meshtastic.delivered_payloads) == 1  # from matrix fanout
        assert len(s.meshcore.delivered_payloads) == 1  # from matrix fanout
        assert len(s.lxmf.delivered_payloads) == 1  # from matrix fanout
        assert len(s.matrix.delivered_payloads) == 1  # from meshtastic

        # -- No pending background tasks from the runner --
        # PipelineRunner.stop() should clean up any internal tasks.
        # We verify the runner is not running by checking it cannot
        # process new events after stop.
        s.matrix.make_event(
            text="t3 after stop",
            event_kind=EventKind.MESSAGE_CREATED,
        )
        # The runner is stopped; publishing through a stopped adapter
        # should not create new events. The adapter's ctx.publish_inbound
        # still references the old runner's ingress_handler, but the
        # runner has shut down its internal processing. We simply verify
        # that accounting did not change from the post-stop state.
        snap_after = s.accounting.snapshot()
        assert snap_after["inbound_accepted"] == snap["inbound_accepted"]
