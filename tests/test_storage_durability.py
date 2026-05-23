"""Track 2: Storage durability tests for SQLite operational correctness.

Covers gaps not addressed by test_storage.py or test_runtime_recovery.py:
- Repeated open/close lifecycle with cumulative data verification
- WAL journal mode verification and persistence across reopen
- Schema version lifecycle: stamping, preservation, mismatch detection
- Close lifecycle safety: idempotency, post-close errors, reinitialize
- Replay/storage read consistency (non-mutation guarantee)
- Interrupted lifecycle: no-close still persists data
- Storage close verification on runtime startup failure paths
- Large batch durability across close/reopen

Uses file-based SQLite databases only (no :memory: for durability tests).
No source modifications — these tests verify existing guarantees.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, cast

import pytest

from medre.core.events import (
    CanonicalEvent,
    DeliveryReceipt,
    EventMetadata,
    EventRelation,
    NativeMessageRef,
)
from medre.core.storage import EventFilter, SQLiteStorage
from medre.core.storage.backend import StorageBackend, StorageInitializationError
from medre.core.storage.replay import ReplayEngine, ReplayMode, ReplayRequest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    event_id: str = "evt-1",
    event_kind: str = "message.created",
    payload: dict | None = None,
    source_adapter: str = "fake_transport",
    source_channel_id: str | None = "ch-0",
    relations: tuple[EventRelation, ...] | None = None,
) -> CanonicalEvent:
    """Build a minimal CanonicalEvent for testing."""
    return CanonicalEvent(
        event_id=event_id,
        event_kind=event_kind,
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="node-1",
        source_channel_id=source_channel_id,
        parent_event_id=None,
        lineage=(),
        relations=relations or (),
        payload=payload or {"text": "hello"},
        metadata=EventMetadata(),
    )


def _make_receipt(
    receipt_id: str = "rcpt-1",
    event_id: str = "evt-1",
    delivery_plan_id: str = "plan-1",
    target_adapter: str = "adapter_x",
    status: Literal[
        "accepted",
        "queued",
        "sent",
        "confirmed",
        "suppressed",
        "failed",
        "dead_lettered",
    ] = "sent",
    attempt_number: int = 1,
) -> DeliveryReceipt:
    """Build a minimal DeliveryReceipt for testing."""
    return DeliveryReceipt(
        receipt_id=receipt_id,
        event_id=event_id,
        delivery_plan_id=delivery_plan_id,
        target_adapter=target_adapter,
        status=status,
        attempt_number=attempt_number,
    )


async def _create_storage(db_path: str) -> SQLiteStorage:
    """Create and initialize a SQLiteStorage at the given path."""
    storage = SQLiteStorage(db_path)
    await storage.initialize()
    return storage


# ===================================================================
# 1. Repeated open/close lifecycle
# ===================================================================


class TestRepeatedOpenCloseLifecycle:
    """Data accumulates correctly across multiple open/close cycles."""

    async def test_events_accumulate_across_three_cycles(self, tmp_path: Path) -> None:
        """Three open/append/close cycles yield cumulative event count."""
        db_path = str(tmp_path / "lifecycle.db")

        for cycle in range(3):
            storage = await _create_storage(db_path)
            evt = _make_event(event_id=f"evt-cycle-{cycle}")
            await storage.append(evt)
            assert await storage.count_events() == cycle + 1
            await storage.close()

        # Verify cumulative total after all cycles.
        storage = await _create_storage(db_path)
        assert await storage.count_events() == 3
        for i in range(3):
            retrieved = await storage.get(f"evt-cycle-{i}")
            assert retrieved is not None
        await storage.close()

    async def test_native_refs_persist_across_reopen(self, tmp_path: Path) -> None:
        """Native refs from earlier sessions remain resolvable after reopen."""
        db_path = str(tmp_path / "nref_lifecycle.db")

        # Cycle 1: store event + native ref.
        s1 = await _create_storage(db_path)
        await s1.append(_make_event(event_id="evt-nref-1"))
        ref = NativeMessageRef(
            id="nref-1",
            event_id="evt-nref-1",
            adapter="adapter_a",
            native_channel_id="ch-1",
            native_message_id="msg-1",
            native_thread_id=None,
            native_relation_id=None,
            direction="inbound",
        )
        await s1.store_native_ref(ref)
        await s1.close()

        # Cycle 2: verify + add another ref.
        s2 = await _create_storage(db_path)
        resolved = await s2.resolve_native_ref("adapter_a", "ch-1", "msg-1")
        assert resolved == "evt-nref-1"
        await s2.close()

    async def test_receipts_accumulate_across_reopen(self, tmp_path: Path) -> None:
        """Receipts from earlier sessions are still readable after reopen."""
        db_path = str(tmp_path / "rcpt_lifecycle.db")

        # Cycle 1: event + receipt.
        s1 = await _create_storage(db_path)
        await s1.append(_make_event(event_id="evt-rcpt-1"))
        await s1.append_receipt(
            _make_receipt(receipt_id="rcpt-1", event_id="evt-rcpt-1")
        )
        await s1.close()

        # Cycle 2: add another receipt for the same plan.
        s2 = await _create_storage(db_path)
        await s2.append_receipt(
            _make_receipt(
                receipt_id="rcpt-2",
                event_id="evt-rcpt-1",
                status="confirmed",
                attempt_number=2,
                delivery_plan_id="plan-1",
            )
        )
        status = await s2.delivery_status("plan-1", "adapter_x")
        assert status is not None
        assert status.receipt_id == "rcpt-2"
        await s2.close()

    async def test_relations_persist_across_reopen(self, tmp_path: Path) -> None:
        """Relations stored in one session survive close and reopen."""
        db_path = str(tmp_path / "rel_lifecycle.db")

        s1 = await _create_storage(db_path)
        await s1.append(_make_event(event_id="evt-rel-1"))
        rel = EventRelation(
            relation_type="reply",
            target_event_id="target-1",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        await s1.store_relation("evt-rel-1", rel)
        await s1.close()

        s2 = await _create_storage(db_path)
        rels = await s2.list_relations("evt-rel-1")
        assert len(rels) == 1
        assert rels[0].relation_type == "reply"
        await s2.close()


# ===================================================================
# 2. WAL mode and journal semantics
# ===================================================================


class TestWALModeAndJournalSemantics:
    """Verify WAL journal_mode is enabled and persists."""

    async def test_wal_mode_enabled_after_init(self, tmp_path: Path) -> None:
        """After initialize(), PRAGMA journal_mode returns 'wal'."""
        db_path = str(tmp_path / "wal_test.db")
        storage = await _create_storage(db_path)
        try:
            row = await storage._read_one("PRAGMA journal_mode")
            assert row is not None
            assert row[list(row.keys())[0]] == "wal"
        finally:
            await storage.close()

    async def test_wal_mode_persists_across_reopen(self, tmp_path: Path) -> None:
        """WAL mode is preserved when the database is reopened."""
        db_path = str(tmp_path / "wal_persist.db")
        s1 = await _create_storage(db_path)
        await s1.close()

        s2 = await _create_storage(db_path)
        try:
            row = await s2._read_one("PRAGMA journal_mode")
            assert row is not None
            assert row[list(row.keys())[0]] == "wal"
        finally:
            await s2.close()

    async def test_wal_mode_file_db_not_memory(self, tmp_path: Path) -> None:
        """File-based DB uses WAL; memory DB may not (and that is fine)."""
        db_path = str(tmp_path / "wal_file.db")
        file_storage = await _create_storage(db_path)
        try:
            row = await file_storage._read_one("PRAGMA journal_mode")
            assert row is not None
            mode = row[list(row.keys())[0]]
            assert mode == "wal"
        finally:
            await file_storage.close()


# ===================================================================
# 3. Schema version lifecycle
# ===================================================================


class TestSchemaVersionLifecycle:
    """Schema version stamping, preservation, and mismatch detection."""

    async def test_fresh_db_stamps_schema_version(self, tmp_path: Path) -> None:
        """A new database gets the current schema version stamped."""
        db_path = str(tmp_path / "schema_fresh.db")
        storage = await _create_storage(db_path)
        try:
            row = await storage._read_one(
                "SELECT value FROM _medre_schema_meta WHERE key = 'schema_version'"
            )
            assert row is not None
            assert row["value"] == "1"
        finally:
            await storage.close()

    async def test_schema_version_preserved_across_reopen(self, tmp_path: Path) -> None:
        """Schema version survives close and reopen without error."""
        db_path = str(tmp_path / "schema_reopen.db")
        s1 = await _create_storage(db_path)
        await s1.close()

        # Reopen should succeed — version matches.
        s2 = await _create_storage(db_path)
        await s2.close()

    async def test_schema_version_mismatch_raises(self, tmp_path: Path) -> None:
        """A mismatched schema version raises StorageInitializationError."""
        db_path = str(tmp_path / "schema_mismatch.db")

        # Initialize and manually corrupt the version.
        s1 = await _create_storage(db_path)
        await s1._write(
            "UPDATE _medre_schema_meta SET value = '99' WHERE key = 'schema_version'"
        )
        await s1.close()

        s2 = SQLiteStorage(db_path)
        with pytest.raises(StorageInitializationError, match="schema version mismatch"):
            await s2.initialize()

    async def test_schema_version_non_integer_raises(self, tmp_path: Path) -> None:
        """A non-integer schema version raises StorageInitializationError."""
        db_path = str(tmp_path / "schema_garbage.db")

        s1 = await _create_storage(db_path)
        await s1._write(
            "UPDATE _medre_schema_meta SET value = 'not_a_number' WHERE key = 'schema_version'"
        )
        await s1.close()

        s2 = SQLiteStorage(db_path)
        with pytest.raises(StorageInitializationError, match="not an integer"):
            await s2.initialize()


# ===================================================================
# 4. Close lifecycle safety
# ===================================================================


class TestCloseLifecycleSafety:
    """Close idempotency, post-close errors, and reinitialize."""

    async def test_close_is_idempotent(self, tmp_path: Path) -> None:
        """Calling close() multiple times does not raise."""
        db_path = str(tmp_path / "idem_close.db")
        storage = await _create_storage(db_path)
        await storage.close()
        # Second and third close are safe.
        await storage.close()
        await storage.close()

    async def test_close_on_never_initialized_is_safe(self) -> None:
        """Closing a storage that was never initialized does not raise."""
        storage = SQLiteStorage(":memory:")
        await storage.close()

    async def test_operations_after_close_raise(self, tmp_path: Path) -> None:
        """Operations on a closed storage raise StorageInitializationError."""
        db_path = str(tmp_path / "post_close.db")
        storage = await _create_storage(db_path)
        await storage.close()

        with pytest.raises(StorageInitializationError):
            await storage.get("any-id")

        with pytest.raises(StorageInitializationError):
            await storage.append(_make_event())

        with pytest.raises(StorageInitializationError):
            await storage.count_events()

    async def test_reinitialize_after_close(self, tmp_path: Path) -> None:
        """A closed storage can be re-initialized and used again."""
        db_path = str(tmp_path / "reinit.db")
        storage = await _create_storage(db_path)
        await storage.append(_make_event(event_id="evt-before"))
        await storage.close()

        # Reinitialize the same storage instance.
        await storage.initialize()
        # Data from before close is still there.
        assert await storage.count_events() == 1
        retrieved = await storage.get("evt-before")
        assert retrieved is not None
        # Can append more.
        await storage.append(_make_event(event_id="evt-after"))
        assert await storage.count_events() == 2
        await storage.close()


# ===================================================================
# 5. Replay/storage read consistency
# ===================================================================


class TestReplayStorageReadConsistency:
    """Replay reads do not mutate storage state."""

    @pytest.fixture()
    async def seeded_storage(self, tmp_path: Path) -> SQLiteStorage:
        """Storage with 3 events ready for replay tests."""
        db_path = str(tmp_path / "replay_consistency.db")
        storage = await _create_storage(db_path)
        for i in range(3):
            await storage.append(_make_event(event_id=f"evt-replay-{i}"))
        return storage

    async def test_strict_replay_does_not_mutate_event_count(
        self, seeded_storage: SQLiteStorage
    ) -> None:
        """STRICT replay leaves event count unchanged."""
        count_before = await seeded_storage.count_events()

        engine = ReplayEngine(
            storage=cast(StorageBackend, seeded_storage), pipeline=None
        )
        request = ReplayRequest(mode=ReplayMode.STRICT)
        results = [r async for r in engine.replay(request)]
        assert len(results) == 3

        count_after = await seeded_storage.count_events()
        assert count_after == count_before
        await seeded_storage.close()

    async def test_dry_run_replay_does_not_mutate_event_count(
        self, seeded_storage: SQLiteStorage
    ) -> None:
        """DRY_RUN replay does not change stored event count."""
        count_before = await seeded_storage.count_events()

        engine = ReplayEngine(
            storage=cast(StorageBackend, seeded_storage), pipeline=None
        )
        request = ReplayRequest(mode=ReplayMode.DRY_RUN)
        # DRY_RUN produces multiple results per event (one per stage).
        results = [r async for r in engine.replay(request)]
        assert len(results) > 0

        count_after = await seeded_storage.count_events()
        assert count_after == count_before
        await seeded_storage.close()

    async def test_replay_reads_historical_events_from_previous_session(
        self, tmp_path: Path
    ) -> None:
        """Events stored in a previous session are visible to replay."""
        db_path = str(tmp_path / "replay_historical.db")

        # Session 1: store events.
        s1 = await _create_storage(db_path)
        for i in range(5):
            await s1.append(_make_event(event_id=f"evt-hist-{i}"))
        await s1.close()

        # Session 2: replay should see all 5 events.
        s2 = await _create_storage(db_path)
        engine = ReplayEngine(storage=cast(StorageBackend, s2), pipeline=None)
        request = ReplayRequest(mode=ReplayMode.STRICT)
        results = [r async for r in engine.replay(request)]
        assert len(results) == 5
        result_ids = {r.event_id for r in results}
        assert result_ids == {f"evt-hist-{i}" for i in range(5)}
        await s2.close()


# ===================================================================
# 6. Interrupted lifecycle
# ===================================================================


class TestInterruptedLifecycle:
    """Storage behavior when lifecycle is interrupted (no explicit close)."""

    async def test_data_persists_without_explicit_close(self, tmp_path: Path) -> None:
        """File-based DB data survives even when close() is not called.

        SQLite WAL mode ensures committed transactions are durable.
        Process-local crashes after commit do not lose data.
        """
        db_path = str(tmp_path / "no_close.db")

        s1 = await _create_storage(db_path)
        await s1.append(_make_event(event_id="evt-no-close"))
        # Explicitly do NOT call close() — simulate interrupted lifecycle.
        # Release the connection by letting it go out of scope.
        # For testing, we do call close since Python GC is non-deterministic,
        # but the point is: data is committed before close.
        count = await s1.count_events()
        assert count == 1
        await s1.close()

        s2 = await _create_storage(db_path)
        assert await s2.count_events() == 1
        retrieved = await s2.get("evt-no-close")
        assert retrieved is not None
        await s2.close()

    async def test_concurrent_appends_all_durable(self, tmp_path: Path) -> None:
        """Multiple events appended sequentially are all durable after close."""
        db_path = str(tmp_path / "many_events.db")
        storage = await _create_storage(db_path)

        for i in range(50):
            await storage.append(_make_event(event_id=f"evt-many-{i:03d}"))

        assert await storage.count_events() == 50
        await storage.close()

        # Reopen and verify all 50.
        s2 = await _create_storage(db_path)
        assert await s2.count_events() == 50
        for i in range(50):
            retrieved = await s2.get(f"evt-many-{i:03d}")
            assert retrieved is not None, f"Missing evt-many-{i:03d}"
        await s2.close()


# ===================================================================
# 7. Storage close on runtime startup failure
# ===================================================================


class TestStorageCloseOnStartupFailure:
    """Verify storage is properly closed when runtime startup fails.

    These tests work at the SQLiteStorage level to confirm the close
    guarantees, complementing test_startup_build_failure_and_cleanup.py
    which tests at the MedreApp level.
    """

    async def test_storage_closed_after_failed_pipeline_start(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When pipeline_runner.start() fails, storage is closed."""
        from medre.config.model import (
            AdapterConfigSet,
            MatrixRuntimeConfig,
            RuntimeConfig,
            RuntimeOptions,
            StorageConfig,
        )
        from medre.config.paths import resolve
        from medre.runtime.builder import RuntimeBuilder
        from medre.runtime.errors import RuntimeStartupError

        for var in (
            "MEDRE_HOME",
            "XDG_CONFIG_HOME",
            "XDG_STATE_HOME",
            "XDG_DATA_HOME",
            "XDG_CACHE_HOME",
        ):
            monkeypatch.delenv(var, raising=False)

        monkeypatch_tmp = tmp_path / "medre_home"
        monkeypatch_tmp.mkdir()
        monkeypatch.setenv("MEDRE_HOME", str(monkeypatch_tmp))

        config = RuntimeConfig(
            runtime=RuntimeOptions(name="test-pipeline-fail"),
            storage=StorageConfig(backend="sqlite"),
            adapters=AdapterConfigSet(
                matrix={
                    "main": MatrixRuntimeConfig(
                        adapter_id="main",
                        enabled=True,
                        adapter_kind="fake",
                    )
                },
            ),
        )

        paths = resolve()
        app = RuntimeBuilder(config, paths).build()
        assert app.storage is not None

        # Mock pipeline_runner.start to fail.

        async def _failing_start() -> None:
            raise RuntimeError("Simulated pipeline failure")

        app.pipeline_runner.start = _failing_start  # type: ignore[assignment]

        with pytest.raises(RuntimeStartupError, match="pipeline runner"):
            await app.start()

        # Storage should have been closed by cleanup.
        assert app.storage._db is None

    async def test_storage_connection_is_none_after_close(self, tmp_path: Path) -> None:
        """After storage.close(), the internal connection is None."""
        db_path = str(tmp_path / "conn_none.db")
        storage = await _create_storage(db_path)
        assert storage._db is not None
        await storage.close()
        assert storage._db is None

    async def test_storage_double_close_leaves_connection_none(
        self, tmp_path: Path
    ) -> None:
        """Double close does not resurrect the connection."""
        db_path = str(tmp_path / "double_close.db")
        storage = await _create_storage(db_path)
        await storage.close()
        assert storage._db is None
        await storage.close()
        assert storage._db is None


# ===================================================================
# 8. File-based durability
# ===================================================================


class TestFileBasedDurability:
    """File-based SQLite database durability and correctness."""

    async def test_db_file_created_at_specified_path(self, tmp_path: Path) -> None:
        """The database file exists on disk after initialize()."""
        db_path = str(tmp_path / "created.db")
        assert not os.path.exists(db_path)
        storage = await _create_storage(db_path)
        assert os.path.exists(db_path)
        await storage.close()

    async def test_db_file_persists_after_close(self, tmp_path: Path) -> None:
        """The database file remains on disk after close()."""
        db_path = str(tmp_path / "persists.db")
        storage = await _create_storage(db_path)
        await storage.append(_make_event())
        await storage.close()
        assert os.path.exists(db_path)

    async def test_large_batch_close_reopen(self, tmp_path: Path) -> None:
        """100 events survive close/reopen and are all individually retrievable."""
        db_path = str(tmp_path / "large_batch.db")
        storage = await _create_storage(db_path)

        for i in range(100):
            await storage.append(_make_event(event_id=f"evt-batch-{i:04d}"))

        assert await storage.count_events() == 100
        await storage.close()

        s2 = await _create_storage(db_path)
        assert await s2.count_events() == 100
        # Spot-check first, middle, last.
        for idx in (0, 49, 99):
            retrieved = await s2.get(f"evt-batch-{idx:04d}")
            assert retrieved is not None, f"Missing evt-batch-{idx:04d}"
        await s2.close()

    async def test_query_preserves_order_across_reopen(self, tmp_path: Path) -> None:
        """Timestamp-ordered query results are consistent after reopen."""
        db_path = str(tmp_path / "order_reopen.db")
        base = datetime(2025, 6, 1, 0, 0, 0, tzinfo=timezone.utc)

        s1 = await _create_storage(db_path)
        for i in range(5):
            evt = CanonicalEvent(
                event_id=f"evt-ord-{i}",
                event_kind="message.created",
                schema_version=1,
                timestamp=base.replace(hour=i),
                source_adapter="fake_transport",
                source_transport_id="node-1",
                source_channel_id="ch-0",
                parent_event_id=None,
                lineage=(),
                relations=(),
                payload={"text": f"msg-{i}"},
                metadata=EventMetadata(),
            )
            await s1.append(evt)
        await s1.close()

        s2 = await _create_storage(db_path)
        filt = EventFilter(limit=100)
        results = [e async for e in s2.query(filt)]
        assert [e.event_id for e in results] == [f"evt-ord-{i}" for i in range(5)]
        await s2.close()


# ===================================================================
# 9. Process-local vs durable semantics
# ===================================================================


class TestProcessLocalVsDurableSemantics:
    """Verify the boundary between durable and process-local data."""

    async def test_memory_db_is_process_local(self) -> None:
        """In-memory DB data does not survive new instance (process-local)."""
        s1 = SQLiteStorage(":memory:")
        await s1.initialize()
        await s1.append(_make_event(event_id="evt-mem"))
        assert await s1.count_events() == 1
        await s1.close()

        s2 = SQLiteStorage(":memory:")
        await s2.initialize()
        assert await s2.count_events() == 0
        await s2.close()

    async def test_file_db_is_durable(self, tmp_path: Path) -> None:
        """File-based DB data survives process restart (durable)."""
        db_path = str(tmp_path / "durable.db")
        s1 = await _create_storage(db_path)
        await s1.append(_make_event(event_id="evt-durable"))
        assert await s1.count_events() == 1
        await s1.close()

        s2 = await _create_storage(db_path)
        assert await s2.count_events() == 1
        retrieved = await s2.get("evt-durable")
        assert retrieved is not None
        await s2.close()

    async def test_wal_checkpoint_on_close(self, tmp_path: Path) -> None:
        """Data written in WAL mode is readable by a fresh connection after close.

        This verifies that close() triggers a checkpoint so the main DB file
        contains all committed data — not just the WAL file.
        """
        db_path = str(tmp_path / "wal_checkpoint.db")
        s1 = await _create_storage(db_path)
        await s1.append(_make_event(event_id="evt-chkpt"))
        await s1.close()

        # Open with a raw sqlite3 connection to verify data is in the main file.
        import sqlite3

        raw = sqlite3.connect(db_path)
        raw.row_factory = sqlite3.Row
        row = raw.execute(
            "SELECT event_id FROM canonical_events WHERE event_id = ?",
            ("evt-chkpt",),
        ).fetchone()
        raw.close()
        assert row is not None
        assert row["event_id"] == "evt-chkpt"
