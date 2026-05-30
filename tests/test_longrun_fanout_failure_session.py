"""Long-run fanout + partial failure session test.

Combines patterns from ``test_mixed_adapter_longrun_bridge`` and
``test_mixed_failure_under_traffic`` into a single ambitious session test
proving MEDRE bridge correctness under sustained mixed-adapter fanout traffic
with interspersed failures and duplicate suppression.

Topology
--------
Route ``mx-to-fanout``: source ``mx`` → targets [``mesh``, ``mc``, ``lxmf``]
Route ``mesh-to-mx``:   source ``mesh`` → targets [``mx``]

Four fake adapters: Matrix (source + reverse target), Meshtastic, MeshCore,
LXMF.  The pipeline processes 50 inbound events through six phases exercising
fanout, reverse-direction delivery, duplicate suppression, transient adapter
failure, and recovery.

No Docker, no live transports, no fixed sleeps.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from medre.adapters.fakes.lxmf import FakeLxmfAdapter
from medre.adapters.fakes.matrix import FakeMatrixAdapter
from medre.adapters.fakes.meshcore import FakeMeshCoreAdapter
from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter
from medre.config.adapters.lxmf import LxmfConfig
from medre.config.adapters.meshcore import MeshCoreConfig
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.engine.pipeline import PipelineRunner
from medre.core.events.canonical import CanonicalEvent, EventMetadata, NativeRef
from medre.core.events.kinds import EventKind
from medre.core.rendering.renderer import RenderingPipeline
from medre.core.rendering.text import TextRenderer
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.routing.stats import RouteStats
from medre.core.storage import SQLiteStorage
from medre.core.supervision.accounting import RuntimeAccounting
from tests.helpers.async_utils import wait_until
from tests.helpers.bridge import (
    make_adapter_context,
    make_pipeline_config,
    make_text_packet,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MX_ID, MESH_ID, MC_ID, LXMF_ID = "mx", "mesh", "mc", "lxmf"
ROUTE_FANOUT = "mx-to-fanout"
ROUTE_MESH_RETURN = "mesh-to-mx"
MX_CHANNEL = "!fanout:fake"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _evt(
    event_id: str,
    native_ref: NativeRef,
    source_adapter: str = MX_ID,
    text: str = "hello",
) -> CanonicalEvent:
    """Create a CanonicalEvent with source_native_ref."""
    return CanonicalEvent(
        event_id=event_id,
        event_kind=EventKind.MESSAGE_CREATED,
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id=f"{source_adapter}-transport",
        source_channel_id=MX_CHANNEL,
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"body": text},
        metadata=EventMetadata(),
        source_native_ref=native_ref,
    )


def _nref(msg_id: str) -> NativeRef:
    return NativeRef(
        adapter=MX_ID,
        native_channel_id=MX_CHANNEL,
        native_message_id=msg_id,
    )


class _S:
    """Immutable holder for adapters, runner, and accounting."""

    __slots__ = ("matrix", "meshtastic", "meshcore", "lxmf", "runner", "acct", "rstats")

    def __init__(self, matrix, meshtastic, meshcore, lxmf, runner, acct, rstats):
        self.matrix = matrix
        self.meshtastic = meshtastic
        self.meshcore = meshcore
        self.lxmf = lxmf
        self.runner = runner
        self.acct = acct
        self.rstats = rstats


async def _build(storage: SQLiteStorage) -> _S:
    """Create 4 fake adapters, 2 routes, and a started PipelineRunner."""
    mx = FakeMatrixAdapter(MX_ID, channel=MX_CHANNEL)
    mesh = FakeMeshtasticAdapter(MeshtasticConfig(adapter_id=MESH_ID))
    mc = FakeMeshCoreAdapter(MeshCoreConfig(adapter_id=MC_ID))
    lxmf = FakeLxmfAdapter(LxmfConfig(adapter_id=LXMF_ID))

    router = Router(
        routes=[
            Route(
                id=ROUTE_FANOUT,
                source=RouteSource(
                    adapter=MX_ID, event_kinds=("message.created",), channel=None
                ),
                targets=[
                    RouteTarget(adapter=MESH_ID, channel="0"),
                    RouteTarget(adapter=MC_ID, channel="0"),
                    RouteTarget(adapter=LXMF_ID, channel="0"),
                ],
            ),
            Route(
                id=ROUTE_MESH_RETURN,
                source=RouteSource(
                    adapter=MESH_ID, event_kinds=("message.created",), channel=None
                ),
                targets=[RouteTarget(adapter=MX_ID, channel=MX_CHANNEL)],
            ),
        ]
    )

    rp = RenderingPipeline()
    rp.register(TextRenderer(), priority=100)
    acct = RuntimeAccounting()
    rstats = RouteStats()

    adapters = {MX_ID: mx, MESH_ID: mesh, MC_ID: mc, LXMF_ID: lxmf}
    config = make_pipeline_config(
        storage=storage,
        router=router,
        adapters=adapters,
        rendering_pipeline=rp,
        accounting=acct,
        route_stats=rstats,
    )
    runner = PipelineRunner(config)
    await runner.start()

    await mx.start(make_adapter_context(MX_ID, runner))
    await mesh.start(make_adapter_context(MESH_ID, runner))
    await mc.start(make_adapter_context(MC_ID, runner))
    await lxmf.start(make_adapter_context(LXMF_ID, runner))

    return _S(mx, mesh, mc, lxmf, runner, acct, rstats)


async def _stop(s: _S) -> None:
    await s.matrix.stop()
    await s.meshtastic.stop()
    await s.meshcore.stop()
    await s.lxmf.stop()
    await s.runner.stop()


def _json_safe(obj: Any, path: str = "root") -> None:
    if isinstance(obj, (str, int, bool, type(None))):
        return
    if isinstance(obj, list):
        for i, item in enumerate(obj):
            _json_safe(item, f"{path}[{i}]")
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            assert isinstance(k, str), f"{path}: key {k!r} not str"
            _json_safe(v, f"{path}.{k}")
        return
    raise AssertionError(f"{path} is {type(obj).__name__}, not JSON-safe")


# ===================================================================
# TEST 1: 50-message mixed deterministic session
# ===================================================================


class Test50MessageMixedDeterministicSession:
    """Primary confidence test: 50 events across 6 phases.

    Phase 1: 20 normal matrix→fanout (all succeed)
    Phase 2:  5 meshtastic→matrix (reverse direction)
    Phase 3:  5 matrix→fanout with duplicate native_refs (suppressed)
    Phase 4: 10 normal matrix→fanout
    Phase 5:  5 matrix→fanout where meshcore fails (set_deliver_failure)
    Phase 6:  5 normal matrix→fanout (meshcore recovers)

    Total submitted: 50 | Accepted: 45 | Suppressed: 5
    """

    async def test_50_message_mixed_deterministic_session(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        s = await _build(temp_storage)
        try:
            snap = s.acct.snapshot

            # -- Phase 1: 20 matrix→fanout, all succeed --
            p1_refs: list[NativeRef] = []
            for i in range(20):
                nref = _nref(f"mx-p1-{i}")
                p1_refs.append(nref)
                await s.matrix.simulate_inbound(_evt(f"p1-{i}", nref, text=f"p1 {i}"))
            await wait_until(lambda: snap()["inbound_accepted"] >= 20)

            # -- Phase 2: 5 meshtastic→matrix (reverse) --
            for i in range(5):
                await s.meshtastic.simulate_inbound(
                    make_text_packet(text=f"p2 mesh {i}", packet_id=2000 + i)
                )
            await wait_until(lambda: snap()["inbound_accepted"] >= 25)

            # -- Phase 3: 5 duplicates (reuse phase 1 refs 0..4) --
            for i in range(5):
                await s.matrix.simulate_inbound(
                    _evt(f"p3-dup-{i}", p1_refs[i], text=f"p3 dup {i}")
                )
            await wait_until(lambda: snap()["loop_prevented"] >= 5)

            # -- Phase 4: 10 normal matrix→fanout --
            for i in range(10):
                await s.matrix.simulate_inbound(
                    s.matrix.make_event(
                        text=f"p4 {i}", event_kind=EventKind.MESSAGE_CREATED
                    )
                )
            await wait_until(lambda: snap()["inbound_accepted"] >= 35)

            # -- Phase 5: 5 matrix→fanout, meshcore fails --
            s.meshcore.set_deliver_failure(True)
            for i in range(5):
                await s.matrix.simulate_inbound(
                    s.matrix.make_event(
                        text=f"p5 {i}", event_kind=EventKind.MESSAGE_CREATED
                    )
                )
            await wait_until(lambda: snap()["inbound_accepted"] >= 40)
            s.meshcore.set_deliver_failure(False)

            # -- Phase 6: 5 normal matrix→fanout (meshcore recovers) --
            for i in range(5):
                await s.matrix.simulate_inbound(
                    s.matrix.make_event(
                        text=f"p6 {i}", event_kind=EventKind.MESSAGE_CREATED
                    )
                )
            await wait_until(lambda: snap()["inbound_accepted"] >= 45)

            # ===========================================================
            # Assertions
            # ===========================================================

            # -- Canonical events: 45 persisted (50 submitted - 5 dups) --
            events = await temp_storage._read_all(
                "SELECT source_adapter FROM canonical_events"
            )
            assert len(events) == 45
            assert sum(1 for e in events if e["source_adapter"] == MX_ID) == 40
            assert sum(1 for e in events if e["source_adapter"] == MESH_ID) == 5

            # -- Accounting --
            a = snap()
            assert a["inbound_accepted"] == 45
            assert a["loop_prevented"] == 5
            # 45 accepted: 40 on fanout (3 targets each) + 5 on mesh-return (1 target)
            assert a["outbound_attempts"] == 125
            # fanout: 40×3=120 minus 5 meshcore failures = 115; mesh-return: 5
            assert a["outbound_delivered"] == 120
            assert a["outbound_failed"] == 5
            assert (
                a["outbound_delivered"] + a["outbound_failed"] == a["outbound_attempts"]
            )
            assert a["capacity_rejections"] == 0
            assert a["replay_processed"] == 0
            assert a["replay_rejected"] == 0
            for k, v in a.items():
                assert isinstance(v, int), f"accounting[{k!r}]={v!r}, expected int"

            # -- Delivery receipts --
            rcpts = await temp_storage._read_all(
                "SELECT target_adapter, status FROM delivery_receipts ORDER BY sequence"
            )
            assert len(rcpts) == 125
            assert sum(1 for r in rcpts if r["status"] == "sent") == 120
            assert sum(1 for r in rcpts if r["status"] == "failed") == 5

            # Per-target
            mesh_r = [r for r in rcpts if r["target_adapter"] == MESH_ID]
            mc_r = [r for r in rcpts if r["target_adapter"] == MC_ID]
            lxmf_r = [r for r in rcpts if r["target_adapter"] == LXMF_ID]
            mx_r = [r for r in rcpts if r["target_adapter"] == MX_ID]

            assert len(mesh_r) == 40 and all(r["status"] == "sent" for r in mesh_r)
            assert len(mc_r) == 40
            assert sum(1 for r in mc_r if r["status"] == "sent") == 35
            assert sum(1 for r in mc_r if r["status"] == "failed") == 5
            assert len(lxmf_r) == 40 and all(r["status"] == "sent" for r in lxmf_r)
            assert len(mx_r) == 5 and all(r["status"] == "sent" for r in mx_r)

            # No duplicate (event_id, target_adapter) pairs
            dupes = await temp_storage._read_all(
                "SELECT event_id, target_adapter, COUNT(*) c "
                "FROM delivery_receipts GROUP BY event_id, target_adapter HAVING c > 1"
            )
            assert len(dupes) == 0

            # -- Adapter delivered_payloads --
            assert len(s.matrix.delivered_payloads) == 5  # from meshtastic reverse
            assert len(s.meshtastic.delivered_payloads) == 40  # from matrix fanout
            assert len(s.meshcore.delivered_payloads) == 35  # 40 - 5 failures
            assert len(s.lxmf.delivered_payloads) == 40  # from matrix fanout

            # -- RouteStats --
            st = s.rstats.snapshot()
            assert st[ROUTE_FANOUT]["delivered"] == 115
            assert st[ROUTE_FANOUT]["failed"] == 5
            assert (
                st[ROUTE_FANOUT]["loop_prevented"] == 0
            )  # dedup tracked in global accounting, not per-route
            assert st[ROUTE_MESH_RETURN]["delivered"] == 5
            assert st[ROUTE_MESH_RETURN]["failed"] == 0
            assert st[ROUTE_MESH_RETURN]["loop_prevented"] == 0

            for rid, rs in st.items():
                for sk, sv in rs.items():
                    if sk == "last_error":
                        assert sv is None or isinstance(
                            sv, str
                        ), f"rstats[{rid!r}][{sk!r}]={sv!r}"
                    else:
                        assert isinstance(sv, int), f"rstats[{rid!r}][{sk!r}]={sv!r}"

            # -- Snapshot JSON-safe --
            combined = {"accounting": a, "route_stats": st}
            _json_safe(combined, "combined")
            assert len(json.dumps(combined, sort_keys=True)) > 50

        finally:
            await _stop(s)


# ===================================================================
# TEST 2: Restart boundary mid-run
# ===================================================================


async def _run_batch(
    storage: SQLiteStorage,
    n: int,
    prefix: str,
    *,
    dup_refs: list[NativeRef] | None = None,
) -> tuple[int, RuntimeAccounting]:
    """Run *n* matrix messages through a fresh pipeline; return (accepted, accounting)."""
    mx = FakeMatrixAdapter(MX_ID, channel=MX_CHANNEL)
    mesh = FakeMeshtasticAdapter(MeshtasticConfig(adapter_id=MESH_ID))
    mc = FakeMeshCoreAdapter(MeshCoreConfig(adapter_id=MC_ID))
    lxmf = FakeLxmfAdapter(LxmfConfig(adapter_id=LXMF_ID))

    router = Router(
        routes=[
            Route(
                id=ROUTE_FANOUT,
                source=RouteSource(
                    adapter=MX_ID, event_kinds=("message.created",), channel=None
                ),
                targets=[
                    RouteTarget(adapter=MESH_ID, channel="0"),
                    RouteTarget(adapter=MC_ID, channel="0"),
                    RouteTarget(adapter=LXMF_ID, channel="0"),
                ],
            ),
            Route(
                id=ROUTE_MESH_RETURN,
                source=RouteSource(
                    adapter=MESH_ID, event_kinds=("message.created",), channel=None
                ),
                targets=[RouteTarget(adapter=MX_ID, channel=MX_CHANNEL)],
            ),
        ]
    )

    rp = RenderingPipeline()
    rp.register(TextRenderer(), priority=100)
    acct = RuntimeAccounting()
    rstats = RouteStats()

    config = make_pipeline_config(
        storage=storage,
        router=router,
        adapters={MX_ID: mx, MESH_ID: mesh, MC_ID: mc, LXMF_ID: lxmf},
        rendering_pipeline=rp,
        accounting=acct,
        route_stats=rstats,
    )
    runner = PipelineRunner(config)
    await runner.start()
    await mx.start(make_adapter_context(MX_ID, runner))
    await mesh.start(make_adapter_context(MESH_ID, runner))
    await mc.start(make_adapter_context(MC_ID, runner))
    await lxmf.start(make_adapter_context(LXMF_ID, runner))

    try:
        for i in range(n):
            nref = (
                dup_refs[i]
                if (dup_refs and i < len(dup_refs))
                else _nref(f"{prefix}-nref-{i}")
            )
            await mx.simulate_inbound(_evt(f"{prefix}-{i}", nref, text=f"{prefix} {i}"))

        await wait_until(
            lambda: acct.snapshot()["inbound_accepted"]
            + acct.snapshot()["loop_prevented"]
            >= n,
            timeout=10.0,
        )
    finally:
        await mx.stop()
        await mesh.stop()
        await mc.stop()
        await lxmf.stop()
        await runner.stop()

    return acct.snapshot()["inbound_accepted"], acct


class TestRestartBoundaryMidRun:
    """Stopping and restarting preserves storage, resets process-local
    accounting, and native-ref dedup works across restarts."""

    async def test_restart_boundary_mid_run(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Run 1: 10 fresh messages.  Run 2: 10 messages (3 cross-restart dups + 7 fresh)."""

        # -- Run 1 --
        accepted_1, acct_1 = await _run_batch(temp_storage, 10, "run1")
        assert accepted_1 == 10

        ev1 = await temp_storage._read_all("SELECT event_id FROM canonical_events")
        assert len(ev1) == 10
        rc1 = await temp_storage._read_all("SELECT sequence FROM delivery_receipts")
        assert len(rc1) == 30  # 10 events × 3 targets

        # -- Run 2 (3 duplicates from run 1 + 7 fresh) --
        dup_refs = [_nref(f"run1-nref-{i}") for i in range(3)]
        accepted_2, acct_2 = await _run_batch(
            temp_storage, 10, "run2", dup_refs=dup_refs
        )
        assert accepted_2 == 7

        # Run 2 accounting is process-local
        a2 = acct_2.snapshot()
        assert a2["inbound_accepted"] == 7
        assert a2["loop_prevented"] == 3
        assert a2["outbound_attempts"] == 21  # 7 × 3 targets
        assert a2["outbound_delivered"] == 21
        assert a2["outbound_failed"] == 0

        # -- Cumulative storage --
        all_ev = await temp_storage._read_all(
            "SELECT event_id FROM canonical_events ORDER BY event_id"
        )
        assert len(all_ev) == 17  # 10 + 7
        assert sum(1 for e in all_ev if e["event_id"].startswith("run1")) == 10
        assert sum(1 for e in all_ev if e["event_id"].startswith("run2")) == 7

        all_rc = await temp_storage._read_all(
            "SELECT status FROM delivery_receipts ORDER BY sequence"
        )
        assert len(all_rc) == 51  # 30 + 21
        assert all(r["status"] == "sent" for r in all_rc)

        # No duplicate receipts across restarts
        dupes = await temp_storage._read_all(
            "SELECT event_id, target_adapter, COUNT(*) c "
            "FROM delivery_receipts GROUP BY event_id, target_adapter HAVING c > 1"
        )
        assert len(dupes) == 0

        # Run 1 accounting unchanged after run 2
        a1 = acct_1.snapshot()
        assert a1["inbound_accepted"] == 10
        assert a1["loop_prevented"] == 0
        assert a1["outbound_delivered"] == 30
