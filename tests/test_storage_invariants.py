"""Storage invariant test suite.

Proves structural integrity of SQLite storage across operations.
Every test asserts an invariant that must hold regardless of
operation order, restarts, or replay activity.

Groups:
  1. Cross-reference invariants  (receipts/native refs → real events)
  2. Source invariants           (replay vs live semantics)
  3. Ordering invariants         (deterministic sequences)
  4. Duplicate suppression       (idempotency)
  5. Replay lineage invariants   (run grouping)
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from typing import Any

from medre.core.events import (
    CanonicalEvent,
    DeliveryReceipt,
    EventMetadata,
    NativeMessageRef,
)
from medre.core.storage.sqlite.storage import SQLiteStorage

# -- Helpers ----------------------------------------------------------------


def _evt(
    eid: str = "evt-1",
    adapter: str = "fake_transport",
    ch: str | None = "ch-0",
) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=eid,
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=adapter,
        source_transport_id="node-1",
        source_channel_id=ch,
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": "hello"},
        metadata=EventMetadata(),
    )


def _rcpt(
    rid: str,
    eid: str,
    plan: str = "plan-1",
    adapter: str = "adapter_a",
    source: str = "live",
    run_id: str | None = None,
) -> DeliveryReceipt:
    return DeliveryReceipt(
        receipt_id=rid,
        event_id=eid,
        delivery_plan_id=plan,
        target_adapter=adapter,
        status="sent",  # type: ignore[arg-type]
        source=source,
        replay_run_id=run_id,
    )


def _nref(
    nid: str,
    eid: str,
    adapter: str = "fake_transport",
    ch: str | None = "ch-0",
    msg: str | None = None,
) -> NativeMessageRef:
    return NativeMessageRef(
        id=nid,
        event_id=eid,
        adapter=adapter,
        native_channel_id=ch,
        native_message_id=msg or f"msg-{nid}",
        native_thread_id=None,
        native_relation_id=None,
        direction="inbound",
    )


async def _all_rcpts(s: SQLiteStorage) -> list[dict[str, Any]]:
    return await s._read_all(
        "SELECT * FROM delivery_receipts ORDER BY sequence ASC", ()
    )


async def _all_nrefs(s: SQLiteStorage) -> list[dict[str, Any]]:
    return await s._read_all("SELECT * FROM native_message_refs ORDER BY id ASC", ())


async def _all_eids(s: SQLiteStorage) -> set[str]:
    rows = await s._read_all("SELECT event_id FROM canonical_events", ())
    return {r["event_id"] for r in rows}


# ===================================================================
# Group 1: Cross-reference invariants
# ===================================================================


class TestCrossReferenceInvariants:
    """Every receipt and native ref must reference a real canonical event."""

    async def _seed(self, s: SQLiteStorage, n: int) -> None:
        for i in range(n):
            await s.append(_evt(eid=f"x-evt-{i}"))
            await s.append_receipt(_rcpt(f"x-rcpt-{i}", f"x-evt-{i}"))

    async def test_every_receipt_references_existing_event(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        await self._seed(temp_storage, 7)
        for row in await _all_rcpts(temp_storage):
            assert (
                await temp_storage.get(row["event_id"]) is not None
            ), f"Receipt {row['receipt_id']} refs missing event {row['event_id']!r}"

    async def test_every_native_ref_references_existing_event(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        for i in range(6):
            await temp_storage.append(_evt(eid=f"nr-evt-{i}"))
            await temp_storage.store_native_ref(_nref(f"nr-{i}", f"nr-evt-{i}"))
        for row in await _all_nrefs(temp_storage):
            assert (
                await temp_storage.get(row["event_id"]) is not None
            ), f"NativeRef {row['id']} refs missing event {row['event_id']!r}"

    async def test_no_orphan_receipts_after_close_reopen(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db = f.name
        try:
            s = SQLiteStorage(db_path=db)
            await s.initialize()
            try:
                for i in range(4):
                    await s.append(_evt(eid=f"or-evt-{i}"))
                    await s.append_receipt(_rcpt(f"or-rcpt-{i}", f"or-evt-{i}"))
            finally:
                await s.close()
            s2 = SQLiteStorage(db_path=db)
            await s2.initialize()
            try:
                eids = await _all_eids(s2)
                for row in await _all_rcpts(s2):
                    assert row["event_id"] in eids
            finally:
                await s2.close()
        finally:
            os.unlink(db)

    async def test_no_orphan_native_refs_after_close_reopen(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db = f.name
        try:
            s = SQLiteStorage(db_path=db)
            await s.initialize()
            try:
                for i in range(3):
                    await s.append(_evt(eid=f"onr-evt-{i}"))
                    await s.store_native_ref(_nref(f"onr-{i}", f"onr-evt-{i}"))
            finally:
                await s.close()
            s2 = SQLiteStorage(db_path=db)
            await s2.initialize()
            try:
                eids = await _all_eids(s2)
                for row in await _all_nrefs(s2):
                    assert row["event_id"] in eids
            finally:
                await s2.close()
        finally:
            os.unlink(db)


# ===================================================================
# Group 2: Source invariants
# ===================================================================


class TestSourceInvariants:
    """Receipt source semantics: replay vs live, replay_run_id correctness."""

    async def test_replay_receipt_always_has_replay_run_id(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        for i in range(3):
            await temp_storage.append(_evt(eid=f"src-evt-{i}"))
            await temp_storage.append_receipt(
                _rcpt(
                    f"rp-rcpt-{i}", f"src-evt-{i}", source="replay", run_id=f"run-{i}"
                )
            )
        rows = await temp_storage._read_all(
            "SELECT * FROM delivery_receipts WHERE source='replay'", ()
        )
        assert len(rows) == 3
        for r in rows:
            assert r["replay_run_id"] is not None
            assert r["replay_run_id"] != ""

    async def test_live_receipt_never_has_replay_run_id(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        for i in range(4):
            await temp_storage.append(_evt(eid=f"lv-evt-{i}"))
            await temp_storage.append_receipt(
                _rcpt(f"lv-rcpt-{i}", f"lv-evt-{i}", source="live")
            )
        rows = await temp_storage._read_all(
            "SELECT * FROM delivery_receipts WHERE source='live'", ()
        )
        assert len(rows) == 4
        for r in rows:
            assert r["replay_run_id"] is None

    async def test_source_is_never_null_or_empty(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        for i in range(5):
            await temp_storage.append(_evt(eid=f"sc-evt-{i}"))
            await temp_storage.append_receipt(
                _rcpt(
                    f"sc-rcpt-{i}",
                    f"sc-evt-{i}",
                    source="live" if i % 2 == 0 else "replay",
                    run_id=f"run-{i}" if i % 2 else None,
                )
            )
        for row in await _all_rcpts(temp_storage):
            assert row["source"] in ("live", "replay")

    async def test_replay_run_id_is_unique_per_run(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        await temp_storage.append(_evt(eid="shared-evt"))
        for j in range(3):
            await temp_storage.append_receipt(
                _rcpt(
                    f"ra-{j}",
                    "shared-evt",
                    plan=f"pa-{j}",
                    adapter=f"aa-{j}",
                    source="replay",
                    run_id="run-alpha",
                )
            )
        for j in range(2):
            await temp_storage.append_receipt(
                _rcpt(
                    f"rb-{j}",
                    "shared-evt",
                    plan=f"pb-{j}",
                    adapter=f"ab-{j}",
                    source="replay",
                    run_id="run-beta",
                )
            )
        a = await temp_storage.list_receipts_by_replay_run("run-alpha")
        b = await temp_storage.list_receipts_by_replay_run("run-beta")
        assert len(a) == 3
        assert len(b) == 2
        assert all(r.replay_run_id == "run-alpha" for r in a)
        assert all(r.replay_run_id == "run-beta" for r in b)
        assert {r.receipt_id for r in a}.isdisjoint({r.receipt_id for r in b})


# ===================================================================
# Group 3: Ordering invariants
# ===================================================================


class TestOrderingInvariants:
    """Sequence numbers and ordering are deterministic across restarts."""

    async def test_receipt_ordering_deterministic_across_restart(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db = f.name
        try:
            s = SQLiteStorage(db_path=db)
            await s.initialize()
            try:
                for i in range(6):
                    await s.append(_evt(eid=f"ord-evt-{i}"))
                    await s.append_receipt(_rcpt(f"ord-rcpt-{i}", f"ord-evt-{i}"))
                order1 = [r["receipt_id"] for r in await _all_rcpts(s)]
            finally:
                await s.close()
            s2 = SQLiteStorage(db_path=db)
            await s2.initialize()
            try:
                order2 = [r["receipt_id"] for r in await _all_rcpts(s2)]
            finally:
                await s2.close()
            assert order1 == order2
        finally:
            os.unlink(db)

    async def test_timeline_ordering_stable_after_replay_append(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        for i in range(4):
            await temp_storage.append(_evt(eid=f"tl-evt-{i}"))
            await temp_storage.append_receipt(_rcpt(f"live-tl-{i}", f"tl-evt-{i}"))
        live_before = await temp_storage._read_all(
            "SELECT receipt_id FROM delivery_receipts WHERE source='live' ORDER BY sequence ASC",
            (),
        )
        live_ids_before = [r["receipt_id"] for r in live_before]
        for i in range(2):
            await temp_storage.append_receipt(
                _rcpt(
                    f"replay-tl-{i}",
                    f"tl-evt-{i}",
                    source="replay",
                    run_id="run-tl",
                )
            )
        all_seqs = [r["sequence"] for r in await _all_rcpts(temp_storage)]
        assert all_seqs == sorted(all_seqs)
        live_after = await temp_storage._read_all(
            "SELECT receipt_id FROM delivery_receipts WHERE source='live' ORDER BY sequence ASC",
            (),
        )
        assert live_ids_before == [r["receipt_id"] for r in live_after]

    async def test_native_ref_ordering_deterministic(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        await temp_storage.append(_evt(eid="nro-evt"))
        for i in range(5):
            await temp_storage.store_native_ref(
                _nref(f"nro-{i}", "nro-evt", adapter=f"adapter-{i}")
            )
        ids1 = [r.id for r in await temp_storage.list_native_refs_for_event("nro-evt")]
        ids2 = [r.id for r in await temp_storage.list_native_refs_for_event("nro-evt")]
        ids3 = [r.id for r in await temp_storage.list_native_refs_for_event("nro-evt")]
        assert ids1 == ids2 == ids3


# ===================================================================
# Group 4: Duplicate suppression invariants
# ===================================================================


class TestDuplicateSuppression:
    """Idempotent native ref insertion; same adapter+message deduped,
    different adapters allowed."""

    async def test_duplicate_native_ref_rejected(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        await temp_storage.append(_evt(eid="dup-evt"))
        await temp_storage.store_native_ref(
            _nref("nd-1", "dup-evt", adapter="ax", ch="ch0", msg="msg-dup")
        )
        await temp_storage.store_native_ref(
            _nref("nd-2", "dup-evt", adapter="ax", ch="ch0", msg="msg-dup")
        )
        rows = await temp_storage._read_all(
            "SELECT * FROM native_message_refs WHERE adapter=? AND native_channel_id=? AND native_message_id=?",
            ("ax", "ch0", "msg-dup"),
        )
        assert len(rows) == 1
        assert rows[0]["id"] == "nd-1"

    async def test_duplicate_different_adapters_allowed(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        await temp_storage.append(_evt(eid="ma-evt"))
        await temp_storage.store_native_ref(
            _nref("na", "ma-evt", adapter="adapter_a", ch="ch-s", msg="shared-msg")
        )
        await temp_storage.store_native_ref(
            _nref("nb", "ma-evt", adapter="adapter_b", ch="ch-s", msg="shared-msg")
        )
        assert (
            await temp_storage.resolve_native_ref("adapter_a", "ch-s", "shared-msg")
            == "ma-evt"
        )
        assert (
            await temp_storage.resolve_native_ref("adapter_b", "ch-s", "shared-msg")
            == "ma-evt"
        )
        rows = await temp_storage._read_all(
            "SELECT * FROM native_message_refs WHERE native_message_id=?",
            ("shared-msg",),
        )
        assert len(rows) == 2


# ===================================================================
# Group 5: Replay lineage invariants
# ===================================================================


class TestReplayLineageInvariants:
    """Replay run grouping is deterministic; no receipt in wrong group."""

    async def test_replay_lineage_grouping_deterministic(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        await temp_storage.append(_evt(eid="lin-evt"))
        run_ids = [f"lin-run-{i}" for i in range(3)]
        for rid in run_ids:
            await temp_storage.append_receipt(
                _rcpt(
                    f"rcpt-{rid}",
                    "lin-evt",
                    plan=f"plan-{rid}",
                    adapter="adapter_lin",
                    source="replay",
                    run_id=rid,
                )
            )
        for rid in run_ids:
            rcpts = await temp_storage.list_receipts_by_replay_run(rid)
            assert len(rcpts) == 1
            assert rcpts[0].replay_run_id == rid
            assert rcpts[0].source == "replay"
        all_runs = {r["replay_run_id"] for r in await _all_rcpts(temp_storage)}
        assert all_runs == set(run_ids)

    async def test_replay_receipts_ordered_within_run(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        for i in range(4):
            await temp_storage.append(_evt(eid=f"lo-evt-{i}"))
            await temp_storage.append_receipt(
                _rcpt(
                    f"rcpt-lo-{i}",
                    f"lo-evt-{i}",
                    plan=f"plo-{i}",
                    adapter=f"alo-{i}",
                    source="replay",
                    run_id="lin-order-run",
                )
            )
        rcpts = await temp_storage.list_receipts_by_replay_run("lin-order-run")
        assert len(rcpts) == 4
        seqs = [r.sequence for r in rcpts]
        assert seqs == sorted(seqs)

    async def test_live_and_replay_receipts_coexist_without_interference(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        await temp_storage.append(_evt(eid="coex-evt"))
        await temp_storage.append_receipt(
            _rcpt("rcpt-coex-live", "coex-evt", source="live")
        )
        await temp_storage.append_receipt(
            _rcpt(
                "rcpt-coex-replay",
                "coex-evt",
                source="replay",
                run_id="run-coex",
            )
        )
        all_r = await temp_storage.list_receipts_for_event("coex-evt")
        assert len(all_r) == 2
        live = [r for r in all_r if r.source == "live"]
        replay = [r for r in all_r if r.source == "replay"]
        assert len(live) == 1 and live[0].replay_run_id is None
        assert len(replay) == 1 and replay[0].replay_run_id == "run-coex"

    async def test_replay_receipts_survive_storage_restart(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db = f.name
        try:
            s = SQLiteStorage(db_path=db)
            await s.initialize()
            try:
                for i in range(3):
                    await s.append(_evt(eid=f"srv-evt-{i}"))
                    await s.append_receipt(
                        _rcpt(
                            f"srv-rcpt-{i}",
                            f"srv-evt-{i}",
                            source="replay",
                            run_id="run-srv",
                        )
                    )
            finally:
                await s.close()
            s2 = SQLiteStorage(db_path=db)
            await s2.initialize()
            try:
                rcpts = await s2.list_receipts_by_replay_run("run-srv")
                assert len(rcpts) == 3
                for r in rcpts:
                    assert r.source == "replay"
                    assert r.replay_run_id == "run-srv"
                    assert await s2.get(r.event_id) is not None
            finally:
                await s2.close()
        finally:
            os.unlink(db)
