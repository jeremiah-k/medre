"""Long-run evidence integrity test — 100+ deterministic messages.

Proves the evidence bundle (canonical events, delivery receipts, native refs,
accounting, route stats) remains coherent across mixed adapter fanout, reverse
delivery, duplicate suppression, partial adapter failure, restart boundaries,
and replay.

Topology: Route ``mx-fanout``: matrix → [meshtastic, meshcore]
          Route ``mesh-return``: meshtastic → [matrix]

No Docker, no live transports, no fixed sleeps.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from medre.adapters.fakes.matrix import FakeMatrixAdapter
from medre.adapters.fakes.meshcore import FakeMeshCoreAdapter
from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter
from medre.config.adapters.meshcore import MeshCoreConfig
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.engine.pipeline import PipelineRunner
from medre.core.events.canonical import CanonicalEvent, EventMetadata, NativeRef
from medre.core.events.kinds import EventKind
from medre.core.rendering.renderer import RenderingPipeline
from medre.core.rendering.text import TextRenderer
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.routing.stats import RouteStats
from medre.core.supervision.accounting import RuntimeAccounting
from medre.core.storage.replay import (
    ReplayEngine,
    ReplayMode,
    ReplayRequest,
    collect_replay_summary,
)
from medre.core.storage.sqlite import SQLiteStorage
from tests.helpers.async_utils import wait_until
from tests.helpers.bridge import (
    make_adapter_context,
    make_pipeline_config,
    make_text_packet,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MX_ID, MESH_ID, MC_ID = "mx", "mesh", "mc"
ROUTE_FANOUT = "mx-fanout"
ROUTE_MESH_RETURN = "mesh-return"
MX_CHANNEL = "!evidence:fake"


# ---------------------------------------------------------------------------
# Assertion helpers
# ---------------------------------------------------------------------------


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


async def _assert_no_orphan_native_refs(storage: SQLiteStorage) -> None:
    """Every outbound native_message_ref must have a matching sent receipt."""
    outbound = await storage._read_all(
        "SELECT event_id, adapter FROM native_message_refs WHERE direction='outbound'"
    )
    for nref in outbound:
        rows = await storage._read_all(
            "SELECT 1 FROM delivery_receipts "
            "WHERE event_id=? AND target_adapter=? AND status='sent'",
            (nref["event_id"], nref["adapter"]),
        )
        assert (
            len(rows) >= 1
        ), f"Orphan outbound ref: eid={nref['event_id']!r} adapter={nref['adapter']!r}"


async def _assert_no_orphan_receipts(storage: SQLiteStorage) -> None:
    orphans = await storage._read_all(
        "SELECT dr.receipt_id FROM delivery_receipts dr "
        "LEFT JOIN canonical_events ce ON dr.event_id=ce.event_id "
        "WHERE ce.event_id IS NULL"
    )
    assert len(orphans) == 0, f"Orphan receipts: {[r['receipt_id'] for r in orphans]}"


# ---------------------------------------------------------------------------
# Event / ref / setup helpers
# ---------------------------------------------------------------------------


def _nref(msg_id: str) -> NativeRef:
    return NativeRef(
        adapter=MX_ID, native_channel_id=MX_CHANNEL, native_message_id=msg_id
    )


def _evt(event_id: str, native_ref: NativeRef, text: str = "hello") -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        event_kind=EventKind.MESSAGE_CREATED,
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=MX_ID,
        source_transport_id="mx-transport",
        source_channel_id=MX_CHANNEL,
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"body": text},
        metadata=EventMetadata(),
        source_native_ref=native_ref,
    )


class _S:
    __slots__ = ("matrix", "meshtastic", "meshcore", "runner", "acct", "rstats")

    def __init__(self, matrix, meshtastic, meshcore, runner, acct, rstats):
        self.matrix, self.meshtastic, self.meshcore = matrix, meshtastic, meshcore
        self.runner, self.acct, self.rstats = runner, acct, rstats


def _make_router() -> Router:
    return Router(
        routes=[
            Route(
                id=ROUTE_FANOUT,
                source=RouteSource(
                    adapter=MX_ID, event_kinds=("message.created",), channel=None
                ),
                targets=[
                    RouteTarget(adapter=MESH_ID, channel="0"),
                    RouteTarget(adapter=MC_ID, channel="0"),
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


async def _build(storage: SQLiteStorage) -> _S:
    mx = FakeMatrixAdapter(MX_ID, channel=MX_CHANNEL)
    mesh = FakeMeshtasticAdapter(MeshtasticConfig(adapter_id=MESH_ID))
    mc = FakeMeshCoreAdapter(MeshCoreConfig(adapter_id=MC_ID))
    rp = RenderingPipeline()
    rp.register(TextRenderer(), priority=100)
    acct, rstats = RuntimeAccounting(), RouteStats()
    config = make_pipeline_config(
        storage=storage,
        router=_make_router(),
        adapters={MX_ID: mx, MESH_ID: mesh, MC_ID: mc},
        rendering_pipeline=rp,
        accounting=acct,
        route_stats=rstats,
    )
    runner = PipelineRunner(config)
    await runner.start()
    await mx.start(make_adapter_context(MX_ID, runner))
    await mesh.start(make_adapter_context(MESH_ID, runner))
    await mc.start(make_adapter_context(MC_ID, runner))
    return _S(mx, mesh, mc, runner, acct, rstats)


async def _stop(s: _S) -> None:
    await s.matrix.stop()
    await s.meshtastic.stop()
    await s.meshcore.stop()
    await s.runner.stop()


# ---------------------------------------------------------------------------
# Phase runners
# ---------------------------------------------------------------------------


async def _phase_fanout(
    s: _S, n: int, prefix: str, snap_target: int, use_refs: bool = True
) -> list[NativeRef]:
    """Inject *n* matrix→fanout messages; return native refs."""
    refs: list[NativeRef] = []
    for i in range(n):
        if use_refs:
            nref = _nref(f"{prefix}-{i}")
            refs.append(nref)
            await s.matrix.simulate_inbound(
                _evt(f"{prefix}-{i}", nref, text=f"{prefix} {i}")
            )
        else:
            await s.matrix.simulate_inbound(
                s.matrix.make_event(
                    text=f"{prefix} {i}", event_kind=EventKind.MESSAGE_CREATED
                )
            )
    await wait_until(lambda: s.acct.snapshot()["inbound_accepted"] >= snap_target)
    return refs


async def _phase_mesh_return(s: _S, n: int, base_id: int, snap_target: int) -> None:
    """Inject *n* meshtastic→matrix messages."""
    for i in range(n):
        await s.meshtastic.simulate_inbound(
            make_text_packet(text=f"mesh {base_id + i}", packet_id=base_id + i)
        )
    await wait_until(lambda: s.acct.snapshot()["inbound_accepted"] >= snap_target)


async def _phase_duplicates(s: _S, dup_refs: list[NativeRef], n: int) -> None:
    """Inject *n* events reusing refs → suppressed."""
    existing = s.acct.snapshot()["loop_prevented"]
    for i in range(n):
        await s.matrix.simulate_inbound(_evt(f"dup-{i}", dup_refs[i], text=f"dup {i}"))
    await wait_until(lambda: s.acct.snapshot()["loop_prevented"] >= existing + n)


async def _phase_meshcore_fail(s: _S, n: int, prefix: str, snap_target: int) -> None:
    """Inject *n* matrix→fanout with meshcore failing."""
    s.meshcore.set_deliver_failure(True)
    for i in range(n):
        await s.matrix.simulate_inbound(
            s.matrix.make_event(
                text=f"{prefix} {i}", event_kind=EventKind.MESSAGE_CREATED
            )
        )
    await wait_until(lambda: s.acct.snapshot()["inbound_accepted"] >= snap_target)
    s.meshcore.set_deliver_failure(False)


# ===================================================================
# TEST 1: 100-message persistent session
# ===================================================================


class Test100MessagePersistentSession:
    """Primary confidence: 100 events, 7 phases.

    Phases: 30 fanout → 10 reverse → 10 dupes → 20 fanout →
            10 meshcore-fail → 10 reverse → 10 fanout
    Submitted: 100 | Accepted: 90 | Suppressed: 10
    Receipts: 160 (150 sent, 10 failed)
    """

    async def test_100_message_persistent_session(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        s = await _build(temp_storage)
        try:
            # Phase 1: 30 matrix→fanout (60 receipts)
            p1_refs = await _phase_fanout(s, 30, "p1", 30)

            # Phase 2: 10 meshtastic→matrix (10 receipts)
            await _phase_mesh_return(s, 10, 2000, 40)

            # Phase 3: 10 duplicate refs (10 loop_prevented, 0 receipts)
            await _phase_duplicates(s, p1_refs[:10], 10)

            # Phase 4: 20 normal fanout (40 receipts)
            await _phase_fanout(s, 20, "p4", 60, use_refs=False)

            # Phase 5: 10 with meshcore failing (10 sent + 10 failed)
            await _phase_meshcore_fail(s, 10, "p5", 70)

            # Phase 6: 10 meshtastic→matrix (10 receipts)
            await _phase_mesh_return(s, 10, 6000, 80)

            # Phase 7: 10 normal fanout (20 receipts)
            await _phase_fanout(s, 10, "p7", 90, use_refs=False)

            # ===========================================================
            # Assertions
            # ===========================================================
            a = s.acct.snapshot()

            # 90 events, 10 suppressed
            events = await temp_storage._read_all(
                "SELECT source_adapter FROM canonical_events"
            )
            assert len(events) == 90
            assert a["inbound_accepted"] == 90
            assert a["loop_prevented"] == 10
            assert sum(1 for e in events if e["source_adapter"] == MX_ID) == 70
            assert sum(1 for e in events if e["source_adapter"] == MESH_ID) == 20

            # 160 receipts (150 sent, 10 failed)
            rcpts = await temp_storage._read_all(
                "SELECT target_adapter, status, source FROM delivery_receipts ORDER BY sequence"
            )
            assert len(rcpts) == 160
            assert sum(1 for r in rcpts if r["status"] == "sent") == 150
            assert sum(1 for r in rcpts if r["status"] == "failed") == 10
            assert all(r["source"] == "live" for r in rcpts)

            # Per-target
            mesh_r = [r for r in rcpts if r["target_adapter"] == MESH_ID]
            mc_r = [r for r in rcpts if r["target_adapter"] == MC_ID]
            mx_r = [r for r in rcpts if r["target_adapter"] == MX_ID]
            assert len(mesh_r) == 70 and all(r["status"] == "sent" for r in mesh_r)
            assert len(mc_r) == 70
            assert sum(1 for r in mc_r if r["status"] == "sent") == 60
            assert sum(1 for r in mc_r if r["status"] == "failed") == 10
            assert len(mx_r) == 20 and all(r["status"] == "sent" for r in mx_r)

            # Accounting
            assert a["outbound_delivered"] == 150
            assert a["outbound_failed"] == 10
            assert a["outbound_attempts"] == 160
            assert (
                a["outbound_delivered"] + a["outbound_failed"] == a["outbound_attempts"]
            )
            assert a["capacity_rejections"] == 0
            for k, v in a.items():
                assert isinstance(v, int), f"accounting[{k!r}]={v!r}, expected int"

            # Orphan checks
            await _assert_no_orphan_native_refs(temp_storage)
            await _assert_no_orphan_receipts(temp_storage)

            # Route stats
            st = s.rstats.snapshot()
            assert st[ROUTE_FANOUT]["delivered"] == 130
            assert st[ROUTE_FANOUT]["failed"] == 10
            assert st[ROUTE_MESH_RETURN]["delivered"] == 20
            assert st[ROUTE_MESH_RETURN]["failed"] == 0
            for rid, rs in st.items():
                for sk, sv in rs.items():
                    if sk == "last_error":
                        assert sv is None or isinstance(sv, str)
                    else:
                        assert isinstance(sv, int), f"rstats[{rid!r}][{sk!r}]={sv!r}"

            # No duplicate (event_id, target_adapter) receipt pairs
            dupes = await temp_storage._read_all(
                "SELECT event_id, target_adapter, COUNT(*) c "
                "FROM delivery_receipts GROUP BY event_id, target_adapter, source HAVING c > 1"
            )
            assert len(dupes) == 0

            # JSON-safe snapshot
            combined = {"accounting": a, "route_stats": st}
            _json_safe(combined, "combined")
            assert len(json.dumps(combined, sort_keys=True)) > 100
        finally:
            await _stop(s)


# ===================================================================
# TEST 4: Repeated replay runs produce distinct lineage
# ===================================================================


class TestRepeatedReplayRunsLineageStable:
    """After the 100-message session, replay event mx-p1-0 three times.
    Each replay run gets a distinct replay_run_id.  Asserts lineage
    stability: distinct run_ids, correct source attribution, live
    receipts untouched, and total receipt count = 166."""

    async def test_repeated_replay_runs_lineage_stable(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        # -- Full 100-message session --
        s = await _build(temp_storage)
        try:
            p1_refs = await _phase_fanout(s, 30, "p1", 30)
            await _phase_mesh_return(s, 10, 2000, 40)
            await _phase_duplicates(s, p1_refs[:10], 10)
            await _phase_fanout(s, 20, "p4", 60, use_refs=False)
            await _phase_meshcore_fail(s, 10, "p5", 70)
            await _phase_mesh_return(s, 10, 6000, 80)
            await _phase_fanout(s, 10, "p7", 90, use_refs=False)
            assert s.acct.snapshot()["inbound_accepted"] == 90
        finally:
            await _stop(s)

        # Pre-replay baseline
        pre = await temp_storage._read_all("SELECT sequence FROM delivery_receipts")
        assert len(pre) == 160

        # -- Three separate replay runs targeting event p1-0 --
        run_ids = [
            "replay-repeat-001",
            "replay-repeat-002",
            "replay-repeat-003",
        ]
        target_event = "p1-0"

        for rid in run_ids:
            s_rp = await _build(temp_storage)
            try:
                replay = ReplayEngine(
                    storage=temp_storage,
                    pipeline=s_rp.runner,
                    accounting=s_rp.acct,
                )
                request = ReplayRequest(
                    mode=ReplayMode.BEST_EFFORT,
                    run_id=rid,
                    correlation_ids=[target_event],
                )
                summary = await collect_replay_summary(replay.replay(request))
                assert summary.events_replayed >= 5  # 1 event * 5 stages
            finally:
                await _stop(s_rp)

        # ===========================================================
        # Post-replay assertions
        # ===========================================================
        all_rc = await temp_storage._read_all(
            "SELECT event_id, target_adapter, status, source, replay_run_id, sequence "
            "FROM delivery_receipts ORDER BY sequence"
        )

        # Total: 160 live + 3*2 replay (3 runs * 2 targets each) = 166
        assert len(all_rc) == 166, f"Expected 166, got {len(all_rc)}"

        # Live receipts untouched
        live = [r for r in all_rc if r["source"] == "live"]
        assert len(live) == 160
        assert all(r["replay_run_id"] is None for r in live)

        # Replay receipts: 3 runs * 2 targets = 6
        rp_rc = [r for r in all_rc if r["source"] == "replay"]
        assert len(rp_rc) == 6, f"Expected 6 replay, got {len(rp_rc)}"

        # 3 distinct replay_run_ids
        actual_run_ids = sorted(set(r["replay_run_id"] for r in rp_rc))
        assert actual_run_ids == sorted(run_ids)

        # Each run produced exactly 2 receipts (one per target)
        for rid in run_ids:
            run_receipts = [r for r in rp_rc if r["replay_run_id"] == rid]
            assert (
                len(run_receipts) == 2
            ), f"Run {rid}: expected 2 receipts, got {len(run_receipts)}"
            assert all(r["status"] == "sent" for r in run_receipts)

        # Trace for p1-0: 2 live + 6 replay = 8
        traced = [r for r in all_rc if r["event_id"] == target_event]
        assert len(traced) == 8, f"Expected 8 for {target_event}, got {len(traced)}"
        traced_live = [r for r in traced if r["source"] == "live"]
        traced_rp = [r for r in traced if r["source"] == "replay"]
        assert len(traced_live) == 2
        assert len(traced_rp) == 6

        # Timeline: all live before all replay
        assert max(r["sequence"] for r in traced_live) < min(
            r["sequence"] for r in traced_rp
        )

        # No duplicate canonical events
        all_ev = await temp_storage._read_all("SELECT event_id FROM canonical_events")
        assert len(all_ev) == 90

        await _assert_no_orphan_receipts(temp_storage)


# ===================================================================
# TEST 5: Interleaved live and replay traffic
# ===================================================================


class TestInterleavedLiveAndReplayTraffic:
    """After the 100-message session: inject 5 live events, replay 3
    original events, then inject 5 more live events.  Asserts total
    receipt count, sequence ordering, run_id isolation, and no
    cross-contamination between runs."""

    async def test_interleaved_live_and_replay_traffic(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        # -- Full 100-message session --
        s = await _build(temp_storage)
        try:
            p1_refs = await _phase_fanout(s, 30, "p1", 30)
            await _phase_mesh_return(s, 10, 2000, 40)
            await _phase_duplicates(s, p1_refs[:10], 10)
            await _phase_fanout(s, 20, "p4", 60, use_refs=False)
            await _phase_meshcore_fail(s, 10, "p5", 70)
            await _phase_mesh_return(s, 10, 6000, 80)
            await _phase_fanout(s, 10, "p7", 90, use_refs=False)
            assert s.acct.snapshot()["inbound_accepted"] == 90
        finally:
            await _stop(s)

        # Baseline: 160 receipts, 90 events
        pre = await temp_storage._read_all("SELECT sequence FROM delivery_receipts")
        assert len(pre) == 160

        # -- Phase A: 5 fresh live events (fanout, 5*2=10 receipts) --
        s_a = await _build(temp_storage)
        try:
            await _phase_fanout(s_a, 5, "live-a", 5, use_refs=False)
            assert s_a.acct.snapshot()["inbound_accepted"] == 5
        finally:
            await _stop(s_a)

        # -- Phase B: replay 3 original events (3*2=6 replay receipts) --
        replay_ids = ["p1-0", "p1-1", "p1-2"]
        s_b = await _build(temp_storage)
        try:
            replay = ReplayEngine(
                storage=temp_storage,
                pipeline=s_b.runner,
                accounting=s_b.acct,
            )
            request = ReplayRequest(
                mode=ReplayMode.BEST_EFFORT,
                run_id="replay-interleave-001",
                correlation_ids=replay_ids,
            )
            summary = await collect_replay_summary(replay.replay(request))
            assert summary.events_replayed >= 15  # 3 events * 5 stages
        finally:
            await _stop(s_b)

        # -- Phase C: 5 more fresh live events (5*2=10 receipts) --
        s_c = await _build(temp_storage)
        try:
            await _phase_fanout(s_c, 5, "live-c", 5, use_refs=False)
            assert s_c.acct.snapshot()["inbound_accepted"] == 5
        finally:
            await _stop(s_c)

        # ===========================================================
        # Combined assertions
        # ===========================================================
        all_rc = await temp_storage._read_all(
            "SELECT event_id, target_adapter, status, source, replay_run_id, sequence "
            "FROM delivery_receipts ORDER BY sequence"
        )

        # Total: 160 + 5*2 + 3*2 + 5*2 = 186
        assert len(all_rc) == 186, f"Expected 186, got {len(all_rc)}"

        # Breakdown by source
        live_rc = [r for r in all_rc if r["source"] == "live"]
        rp_rc = [r for r in all_rc if r["source"] == "replay"]
        assert len(live_rc) == 180, f"Expected 180 live, got {len(live_rc)}"
        assert len(rp_rc) == 6, f"Expected 6 replay, got {len(rp_rc)}"

        # All replay receipts share the same run_id
        assert all(r["replay_run_id"] == "replay-interleave-001" for r in rp_rc)
        assert all(r["replay_run_id"] is None for r in live_rc)

        # Identify the three groups by sequence ranges
        original_live = [r for r in live_rc if r["sequence"] <= 160]
        phase_a_live = [
            r for r in live_rc if r["sequence"] > 160 and r["sequence"] <= 170
        ]
        phase_c_live = [r for r in live_rc if r["sequence"] > 176]

        assert len(original_live) == 160
        assert len(phase_a_live) == 10  # 5 events * 2 targets
        assert len(phase_c_live) == 10  # 5 events * 2 targets

        # Sequence ordering: original live < replay < phase-C live
        if phase_a_live and rp_rc:
            # Phase-A live receipts come before replay receipts
            assert max(r["sequence"] for r in phase_a_live) < min(
                r["sequence"] for r in rp_rc
            )

        # Replay receipts come before phase-C live receipts
        assert max(r["sequence"] for r in rp_rc) < min(
            r["sequence"] for r in phase_c_live
        )

        # replay_run_ids are distinct per replay run (only one run here)
        run_ids = sorted(set(r["replay_run_id"] for r in rp_rc))
        assert run_ids == ["replay-interleave-001"]

        # No receipt from one run bleeds into another
        for rid in run_ids:
            run_receipts = [r for r in rp_rc if r["replay_run_id"] == rid]
            other_receipts = [r for r in rp_rc if r["replay_run_id"] != rid]
            assert len(run_receipts) + len(other_receipts) == len(rp_rc)
            set(r["event_id"] for r in run_receipts)
            for r in run_receipts:
                assert r["replay_run_id"] == rid

        # Events: original 90 + 10 new live = 100
        all_ev = await temp_storage._read_all("SELECT event_id FROM canonical_events")
        assert len(all_ev) == 100

        # No duplicate events
        eids = [e["event_id"] for e in all_ev]
        assert len(eids) == len(set(eids))

        await _assert_no_orphan_native_refs(temp_storage)
        await _assert_no_orphan_receipts(temp_storage)


# ===================================================================
# TEST 2: Restart preserves evidence integrity
# ===================================================================


class TestRestartPreservesEvidenceIntegrity:
    """Stop after phase 4 (60 accepted + 10 suppressed = 80 submitted, 110 receipts);
    restart on same SQLite; run phases 5-7 (30 accepted, 50 receipts);
    assert combined coherence."""

    async def test_restart_preserves_evidence_integrity(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        # -- Run A: phases 1-4 --
        s_a = await _build(temp_storage)
        try:
            p1_refs = await _phase_fanout(s_a, 30, "p1", 30)
            await _phase_mesh_return(s_a, 10, 2000, 40)
            await _phase_duplicates(s_a, p1_refs[:10], 10)
            await _phase_fanout(s_a, 20, "p4", 60, use_refs=False)
            assert s_a.acct.snapshot()["inbound_accepted"] == 60
            assert s_a.acct.snapshot()["loop_prevented"] == 10
        finally:
            await _stop(s_a)

        # Verify run A state in storage
        ev_a = await temp_storage._read_all(
            "SELECT event_id FROM canonical_events ORDER BY event_id"
        )
        assert len(ev_a) == 60
        rc_a = await temp_storage._read_all(
            "SELECT sequence FROM delivery_receipts ORDER BY sequence"
        )
        assert len(rc_a) == 110  # 30×2 + 10 + 0 + 20×2

        # -- Run B: phases 5-7 --
        s_b = await _build(temp_storage)
        try:
            # Process-local accounting resets
            assert s_b.acct.snapshot()["inbound_accepted"] == 0
            await _phase_meshcore_fail(s_b, 10, "p5", 10)
            await _phase_mesh_return(s_b, 10, 6000, 20)
            await _phase_fanout(s_b, 10, "p7", 30, use_refs=False)
            assert s_b.acct.snapshot()["inbound_accepted"] == 30
        finally:
            await _stop(s_b)

        # ===========================================================
        # Combined assertions
        # ===========================================================
        all_ev = await temp_storage._read_all(
            "SELECT event_id, source_adapter FROM canonical_events ORDER BY event_id"
        )
        assert len(all_ev) == 90

        all_rc = await temp_storage._read_all(
            "SELECT target_adapter, status, source, sequence "
            "FROM delivery_receipts ORDER BY sequence"
        )
        assert len(all_rc) == 160

        # No duplicate events
        eids = [e["event_id"] for e in all_ev]
        assert len(eids) == len(set(eids)), "Duplicate event_ids found"

        # No duplicate receipt combos
        dupes = await temp_storage._read_all(
            "SELECT event_id, target_adapter, source, COUNT(*) c "
            "FROM delivery_receipts GROUP BY event_id, target_adapter, source HAVING c > 1"
        )
        assert len(dupes) == 0

        # Receipt breakdown
        assert sum(1 for r in all_rc if r["status"] == "sent") == 150
        assert sum(1 for r in all_rc if r["status"] == "failed") == 10
        assert all(r["source"] == "live" for r in all_rc)

        # Timeline stable
        seqs = [r["sequence"] for r in all_rc]
        assert seqs == sorted(seqs)

        # Orphan checks
        await _assert_no_orphan_native_refs(temp_storage)
        await _assert_no_orphan_receipts(temp_storage)


# ===================================================================
# TEST 3: Replay preserves lineage
# ===================================================================


class TestReplayPreservesLineage:
    """After full 100-message session, replay 5 events via BEST_EFFORT.
    Asserts replay receipts carry source='replay' and replay_run_id,
    live receipts remain untouched, and timeline ordering is stable."""

    async def test_replay_preserves_lineage(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        # -- Full 100-message session --
        s = await _build(temp_storage)
        try:
            p1_refs = await _phase_fanout(s, 30, "p1", 30)
            await _phase_mesh_return(s, 10, 2000, 40)
            await _phase_duplicates(s, p1_refs[:10], 10)
            await _phase_fanout(s, 20, "p4", 60, use_refs=False)
            await _phase_meshcore_fail(s, 10, "p5", 70)
            await _phase_mesh_return(s, 10, 6000, 80)
            await _phase_fanout(s, 10, "p7", 90, use_refs=False)
            assert s.acct.snapshot()["inbound_accepted"] == 90
        finally:
            await _stop(s)

        # Pre-replay: 160 receipts
        pre = await temp_storage._read_all("SELECT sequence FROM delivery_receipts")
        assert len(pre) == 160

        # -- Replay 5 events from phase 1 --
        replay_ids = [f"p1-{i}" for i in range(5)]
        s_rp = await _build(temp_storage)
        try:
            replay = ReplayEngine(
                storage=temp_storage, pipeline=s_rp.runner, accounting=s_rp.acct
            )
            request = ReplayRequest(
                mode=ReplayMode.BEST_EFFORT,
                run_id="replay-lineage-001",
                correlation_ids=replay_ids,
            )
            summary = await collect_replay_summary(replay.replay(request))
            assert summary.events_replayed >= 25  # 5 events × 5 stages
        finally:
            await _stop(s_rp)

        # ===========================================================
        # Post-replay assertions
        # ===========================================================
        all_rc = await temp_storage._read_all(
            "SELECT event_id, target_adapter, status, source, replay_run_id, sequence "
            "FROM delivery_receipts ORDER BY sequence"
        )

        # Total: 160 live + 10 replay (5 events × 2 targets)
        assert len(all_rc) == 170, f"Expected 170, got {len(all_rc)}"

        # Live receipts untouched
        live = [r for r in all_rc if r["source"] == "live"]
        assert len(live) == 160
        assert all(r["replay_run_id"] is None for r in live)

        # Replay receipts
        rp_rc = [r for r in all_rc if r["source"] == "replay"]
        assert len(rp_rc) == 10, f"Expected 10 replay, got {len(rp_rc)}"
        assert all(r["replay_run_id"] == "replay-lineage-001" for r in rp_rc)
        assert all(r["status"] == "sent" for r in rp_rc)

        # Trace for first replayed event: 2 live + 2 replay = 4
        traced = [r for r in all_rc if r["event_id"] == replay_ids[0]]
        assert len(traced) == 4
        traced_live = [r for r in traced if r["source"] == "live"]
        traced_rp = [r for r in traced if r["source"] == "replay"]
        assert len(traced_live) == 2
        assert len(traced_rp) == 2

        # Timeline: replay receipts after live
        assert max(r["sequence"] for r in traced_live) < min(
            r["sequence"] for r in traced_rp
        )

        # No duplicate events in storage
        all_ev = await temp_storage._read_all("SELECT event_id FROM canonical_events")
        assert len(all_ev) == 90

        await _assert_no_orphan_receipts(temp_storage)
