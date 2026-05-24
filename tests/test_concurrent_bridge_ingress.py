"""Concurrent ingress stress tests for the MEDRE bridge pipeline.

Proves that the pipeline handles concurrent inbound traffic
deterministically without duplicate events, deadlocks, semaphore
leaks, or corrupted state.  All four fake adapters are exercised
under asyncio.gather() concurrency.

No Docker, no live transports, no SDK dependencies required.
"""

from __future__ import annotations

import asyncio
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
from medre.core.supervision.accounting import RuntimeAccounting
from medre.core.storage.sqlite import SQLiteStorage
from tests.helpers.bridge import (
    make_adapter_context,
    make_meshcore_packet,
    make_pipeline_config,
    make_text_packet,
)


def _make_lxmf_packet(
    body: str = "hello lxmf",
    msg_id: str | None = None,
    timestamp: float = 1700000000.0,
) -> dict[str, Any]:
    return {
        "content": body,
        "source_hash": "ab" * 16,
        "destination_hash": "00" * 16,
        "message_id": msg_id or "ff" * 32,
        "timestamp": timestamp,
        "title": "",
        "fields": {},
        "signature_validated": True,
        "has_fields": False,
    }


class _Setup:
    """Immutable holder for adapters, runner, and accounting."""

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
        self, matrix, meshtastic, meshcore, lxmf, runner, accounting, route_stats
    ):
        self.matrix = matrix
        self.meshtastic = meshtastic
        self.meshcore = meshcore
        self.lxmf = lxmf
        self.runner = runner
        self.accounting = accounting
        self.route_stats = route_stats


async def _build_bridge(
    storage: SQLiteStorage,
    *,
    prefix: str,
    routes: list[Route],
) -> _Setup:
    """Generic bridge builder: creates 4 fake adapters from prefix, wires routes."""
    mx_id = f"{prefix}-mx"
    mesh_id = f"{prefix}-mesh"
    mc_id = f"{prefix}-mc"
    lxmf_id = f"{prefix}-lxmf"

    fake_matrix = FakeMatrixAdapter(mx_id, channel=f"!{prefix}:fake")
    fake_mesh = FakeMeshtasticAdapter(MeshtasticConfig(adapter_id=mesh_id))
    fake_meshcore = FakeMeshCoreAdapter(MeshCoreConfig(adapter_id=mc_id))
    fake_lxmf = FakeLxmfAdapter(LxmfConfig(adapter_id=lxmf_id))

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
        router=Router(routes=routes),
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

    return _Setup(
        fake_matrix,
        fake_mesh,
        fake_meshcore,
        fake_lxmf,
        runner,
        accounting,
        route_stats,
    )


def _bidir_routes(prefix: str) -> list[Route]:
    mx_id, mesh_id = f"{prefix}-mx", f"{prefix}-mesh"
    return [
        Route(
            id=f"{prefix}-mx-to-mesh",
            source=RouteSource(
                adapter=mx_id, event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter=mesh_id, channel="0")],
        ),
        Route(
            id=f"{prefix}-mesh-to-mx",
            source=RouteSource(
                adapter=mesh_id, event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter=mx_id, channel=f"!{prefix}:fake")],
        ),
    ]


async def _clean_stop(s: _Setup) -> None:
    await s.matrix.stop()
    await s.meshtastic.stop()
    await s.meshcore.stop()
    await s.lxmf.stop()
    await s.runner.stop()


# ===================================================================
# TEST 1: Concurrent Matrix + Meshtastic bidirectional ingress
# ===================================================================


class TestConcurrentMatrixAndMeshtasticIngress:
    """10 concurrent inbound events (5 matrix + 5 meshtastic)."""

    async def test_concurrent_matrix_and_meshtastic_ingress(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        s = await _build_bridge(temp_storage, prefix="t1", routes=_bidir_routes("t1"))
        try:
            mx_coros = [
                s.matrix.simulate_inbound(
                    s.matrix.make_event(
                        text=f"t1 mx {i}", event_kind=EventKind.MESSAGE_CREATED
                    )
                )
                for i in range(5)
            ]
            mesh_coros = [
                s.meshtastic.simulate_inbound(
                    make_text_packet(text=f"t1 mesh {i}", packet_id=1000 + i)
                )
                for i in range(5)
            ]
            await asyncio.gather(*(mx_coros + mesh_coros))

            all_events = await temp_storage._read_all(
                "SELECT event_id, source_adapter FROM canonical_events"
            )
            assert len(all_events) == 10

            assert sum(1 for e in all_events if e["source_adapter"] == "t1-mx") == 5
            assert sum(1 for e in all_events if e["source_adapter"] == "t1-mesh") == 5

            event_ids = [e["event_id"] for e in all_events]
            assert len(set(event_ids)) == 10, "Duplicate event_ids detected"

            receipts = await temp_storage._read_all(
                "SELECT target_adapter, status FROM delivery_receipts ORDER BY sequence"
            )
            assert len(receipts) == 10
            for r in receipts:
                assert r["status"] == "sent"
            assert sum(1 for r in receipts if r["target_adapter"] == "t1-mx") == 5
            assert sum(1 for r in receipts if r["target_adapter"] == "t1-mesh") == 5

            snap = s.accounting.snapshot()
            assert snap["inbound_accepted"] == 10
            assert snap["outbound_delivered"] == 10
            assert snap["loop_prevented"] == 0
            assert snap["outbound_failed"] == 0

            assert len(s.meshtastic.delivered_payloads) == 5
            assert len(s.matrix.delivered_payloads) == 5
        finally:
            await _clean_stop(s)


# ===================================================================
# TEST 2: Concurrent mixed adapter ingress (all 4 adapters)
# ===================================================================


class TestConcurrentMixedAdapterIngress:
    """8 concurrent events (2 from each of the 4 fake adapters)."""

    async def test_concurrent_mixed_adapter_ingress(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        p = "t2"
        mx_id, mesh_id, mc_id, lxmf_id = f"{p}-mx", f"{p}-mesh", f"{p}-mc", f"{p}-lxmf"
        routes = [
            Route(
                id=f"{p}-mx-to-mesh",
                source=RouteSource(
                    adapter=mx_id, event_kinds=("message.created",), channel=None
                ),
                targets=[RouteTarget(adapter=mesh_id, channel="0")],
            ),
            Route(
                id=f"{p}-mesh-to-mx",
                source=RouteSource(
                    adapter=mesh_id, event_kinds=("message.created",), channel=None
                ),
                targets=[RouteTarget(adapter=mx_id, channel=f"!{p}:fake")],
            ),
            Route(
                id=f"{p}-mc-to-mx",
                source=RouteSource(
                    adapter=mc_id, event_kinds=("message.created",), channel=None
                ),
                targets=[RouteTarget(adapter=mx_id, channel=f"!{p}:fake")],
            ),
            Route(
                id=f"{p}-lxmf-to-mx",
                source=RouteSource(
                    adapter=lxmf_id, event_kinds=("message.created",), channel=None
                ),
                targets=[RouteTarget(adapter=mx_id, channel=f"!{p}:fake")],
            ),
        ]
        s = await _build_bridge(temp_storage, prefix=p, routes=routes)
        try:
            coros = []
            for i in range(2):
                coros.append(
                    s.matrix.simulate_inbound(
                        s.matrix.make_event(
                            text=f"t2 mx {i}", event_kind=EventKind.MESSAGE_CREATED
                        )
                    )
                )
                coros.append(
                    s.meshtastic.simulate_inbound(
                        make_text_packet(text=f"t2 mesh {i}", packet_id=2000 + i)
                    )
                )
                coros.append(
                    s.meshcore.simulate_inbound(
                        make_meshcore_packet(text=f"t2 mc {i}", packet_id=3000 + i)
                    )
                )
                coros.append(
                    s.lxmf.simulate_inbound(
                        _make_lxmf_packet(
                            body=f"t2 lxmf {i}",
                            msg_id=f"{'ff'*15}{i:02x}" * 2,
                            timestamp=1700000100.0 + i,
                        )
                    )
                )

            await asyncio.gather(*coros)

            all_events = await temp_storage._read_all(
                "SELECT event_id, source_adapter FROM canonical_events"
            )
            assert len(all_events) == 8

            for aid in (f"{p}-mx", f"{p}-mesh", f"{p}-mc", f"{p}-lxmf"):
                assert sum(1 for e in all_events if e["source_adapter"] == aid) == 2

            assert len(set(e["event_id"] for e in all_events)) == 8

            receipts = await temp_storage._read_all(
                "SELECT target_adapter, status FROM delivery_receipts"
            )
            assert len(receipts) == 8
            for r in receipts:
                assert r["status"] == "sent"

            snap = s.accounting.snapshot()
            assert snap["inbound_accepted"] == 8
            assert snap["outbound_delivered"] == 8
            assert snap["loop_prevented"] == 0
            assert snap["outbound_failed"] == 0
        finally:
            await _clean_stop(s)


# ===================================================================
# TEST 3: Concurrent fanout — no duplicate receipts
# ===================================================================


class TestConcurrentFanoutNoDuplicates:
    """One source adapter fans out to 3 targets. 5 concurrent events
    produce exactly 15 receipts with no duplicates."""

    async def test_concurrent_fanout_no_duplicates(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        p = "t3"
        mx_id, mesh_id, mc_id, lxmf_id = f"{p}-mx", f"{p}-mesh", f"{p}-mc", f"{p}-lxmf"
        routes = [
            Route(
                id=f"{p}-fanout",
                source=RouteSource(
                    adapter=mx_id, event_kinds=("message.created",), channel=None
                ),
                targets=[
                    RouteTarget(adapter=mesh_id, channel="0"),
                    RouteTarget(adapter=mc_id, channel="0"),
                    RouteTarget(adapter=lxmf_id, channel="0"),
                ],
            )
        ]
        s = await _build_bridge(temp_storage, prefix=p, routes=routes)
        try:
            coros = [
                s.matrix.simulate_inbound(
                    s.matrix.make_event(
                        text=f"t3 fanout {i}", event_kind=EventKind.MESSAGE_CREATED
                    )
                )
                for i in range(5)
            ]
            await asyncio.gather(*coros)

            all_events = await temp_storage._read_all(
                "SELECT event_id, source_adapter FROM canonical_events"
            )
            assert len(all_events) == 5

            receipts = await temp_storage._read_all(
                "SELECT event_id, target_adapter, status FROM delivery_receipts ORDER BY sequence"
            )
            assert len(receipts) == 15
            for r in receipts:
                assert r["status"] == "sent"

            # No duplicate (event_id, target_adapter) pairs
            pairs = [(r["event_id"], r["target_adapter"]) for r in receipts]
            assert len(set(pairs)) == 15, "Duplicate (event_id, target_adapter) pairs"

            # Source adapter did NOT receive delivery
            assert not any(r["target_adapter"] == f"{p}-mx" for r in receipts)

            for target in (f"{p}-mesh", f"{p}-mc", f"{p}-lxmf"):
                assert sum(1 for r in receipts if r["target_adapter"] == target) == 5

            snap = s.accounting.snapshot()
            assert snap["inbound_accepted"] == 5
            assert snap["outbound_delivered"] == 15
            assert snap["loop_prevented"] == 0

            assert len(s.meshtastic.delivered_payloads) == 5
            assert len(s.meshcore.delivered_payloads) == 5
            assert len(s.lxmf.delivered_payloads) == 5
            assert len(s.matrix.delivered_payloads) == 0
        finally:
            await _clean_stop(s)


# ===================================================================
# TEST 4: Concurrent ingress during clean shutdown
# ===================================================================


class TestConcurrentIngressCleanShutdown:
    """Concurrent ingress events fired while the pipeline is stopping.
    Verifies no hangs and clean stop."""

    async def test_concurrent_ingress_clean_shutdown(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        s = await _build_bridge(temp_storage, prefix="t4", routes=_bidir_routes("t4"))

        coros = []
        for i in range(5):
            coros.append(
                s.matrix.simulate_inbound(
                    s.matrix.make_event(
                        text=f"t4 mx {i}", event_kind=EventKind.MESSAGE_CREATED
                    )
                )
            )
            coros.append(
                s.meshtastic.simulate_inbound(
                    make_text_packet(text=f"t4 mesh {i}", packet_id=4000 + i)
                )
            )

        gather_task = asyncio.gather(*coros, return_exceptions=True)
        await asyncio.sleep(0)
        await _clean_stop(s)

        results = await gather_task
        assert len(results) == 10
        assert not s.matrix.is_started
        assert not s.meshtastic.is_started

    async def test_shutdown_with_capacity_controller(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        from medre.config.model import RuntimeLimits
        from medre.core.supervision.capacity import CapacityController

        s = await _build_bridge(temp_storage, prefix="t4b", routes=_bidir_routes("t4b"))
        cc = CapacityController(
            RuntimeLimits(
                max_inflight_deliveries=2,
                max_inflight_replay_events=2,
                delivery_acquire_timeout_seconds=0.1,
            )
        )
        s.runner.set_capacity_controller(cc)

        try:
            coros = [
                s.matrix.simulate_inbound(
                    s.matrix.make_event(
                        text=f"t4b mx {i}", event_kind=EventKind.MESSAGE_CREATED
                    )
                )
                for i in range(8)
            ]
            gather_task = asyncio.gather(*coros, return_exceptions=True)
            await asyncio.sleep(0)

            cc.stop_accepting()
            assert not cc.accepting_work

            results = await gather_task
            assert len(results) == 8

            snap = cc.snapshot()
            assert isinstance(snap["delivery_current"], int)
            assert isinstance(snap["delivery_rejections"], int)
        finally:
            await _clean_stop(s)

    async def test_no_semaphore_leak_after_stress(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        from medre.config.model import RuntimeLimits
        from medre.core.supervision.capacity import CapacityController

        s = await _build_bridge(temp_storage, prefix="t4c", routes=_bidir_routes("t4c"))
        cc = CapacityController(
            RuntimeLimits(
                max_inflight_deliveries=5,
                max_inflight_replay_events=5,
                delivery_acquire_timeout_seconds=5.0,
            )
        )
        s.runner.set_capacity_controller(cc)

        try:
            coros = []
            for i in range(10):
                coros.append(
                    s.matrix.simulate_inbound(
                        s.matrix.make_event(
                            text=f"t4c mx {i}", event_kind=EventKind.MESSAGE_CREATED
                        )
                    )
                )
                coros.append(
                    s.meshtastic.simulate_inbound(
                        make_text_packet(text=f"t4c mesh {i}", packet_id=5000 + i)
                    )
                )

            await asyncio.gather(*coros)

            snap = cc.snapshot()
            assert (
                snap["delivery_current"] == 0
            ), f"Semaphore leak: delivery_current={snap['delivery_current']}"

            await _clean_stop(s)
            assert cc.snapshot()["delivery_current"] == 0
        except Exception:
            await _clean_stop(s)
            raise
