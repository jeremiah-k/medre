"""Tests for SQLite storage decomposition hardening.

Covers: query builder limit validation, lazy executor lifecycle and
closed-executor guard, serde NativeRef construction guard, outbox metadata
decode fallback, write-batch atomicity, and plain-import scanner regex.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from medre.core.storage.backend import EventFilter
import medre.core.storage.sqlite.storage as sqlite_storage_module
from medre.core.storage.sqlite.query import _build_query_sql
from medre.core.storage.sqlite.serde import _row_to_outbox_item, _row_to_relation
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.import_scanner import scan_dir_for_plain_imports

# ===================================================================
# 1. Query builder limit validation
# ===================================================================


class TestQueryBuilderValidation:
    """Validate EventFilter.limit runtime checks."""

    def test_negative_limit_raises_value_error(self) -> None:
        """Negative limit must raise ValueError."""
        filt = EventFilter(limit=-1)
        with pytest.raises(ValueError, match="non-negative"):
            _build_query_sql(filt)

    def test_zero_limit_is_valid(self) -> None:
        """LIMIT 0 is valid SQLite — should not raise."""
        filt = EventFilter(limit=0)
        sql, params = _build_query_sql(filt)
        assert "LIMIT ?" in sql
        assert params[-1] == 0

    def test_default_limit_is_valid(self) -> None:
        """Default limit (1000) should work without error."""
        filt = EventFilter()
        sql, params = _build_query_sql(filt)
        assert params[-1] == 1000

    def test_string_limit_raises_value_error(self) -> None:
        """Non-int limit must raise ValueError."""
        filt = EventFilter(limit="bad")  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="non-negative"):
            _build_query_sql(filt)


# ===================================================================
# 2. Lazy executor lifecycle
# ===================================================================


class TestExecutorLifecycle:
    """Test lazy executor creation and closed-executor guard."""

    @pytest.fixture
    async def store(self, tmp_path: Path) -> Any:
        """Create, initialize, and yield a SQLiteStorage; close on cleanup."""
        db_path = str(tmp_path / "test.db")
        s = SQLiteStorage(db_path)
        await s.initialize()
        yield s
        await s.close()

    async def test_executor_is_none_before_init(self) -> None:
        """Executor should not be created until first _run_in_thread call."""
        s = SQLiteStorage(":memory:")
        assert s._executor is None

    async def test_executor_created_by_initialize(self, tmp_path: Path) -> None:
        """Initialization creates the private single-worker executor."""
        s = SQLiteStorage(str(tmp_path / "test.db"))
        try:
            await s.initialize()
            assert s._executor is not None
        finally:
            await s.close()

    async def test_run_in_thread_raises_after_close(self, tmp_path: Path) -> None:
        """After close(), _run_in_thread must raise RuntimeError."""
        s = SQLiteStorage(str(tmp_path / "test.db"))
        await s.initialize()
        await s.close()
        with pytest.raises(RuntimeError, match="SQLiteStorage is closed"):
            await s._run_in_thread(lambda: None)

    async def test_close_sets_executor_none(self, tmp_path: Path) -> None:
        """close() must set _executor to None and _closed to True."""
        s = SQLiteStorage(str(tmp_path / "test.db"))
        try:
            await s.initialize()
            assert s._closed is False
        finally:
            await s.close()
        assert s._executor is None
        assert s._closed is True

    async def test_close_is_idempotent_repeated(self, tmp_path: Path) -> None:
        """Calling close() many times is safe — no errors, no state drift."""
        s = SQLiteStorage(str(tmp_path / "test.db"))
        try:
            await s.initialize()
            pass  # No assertion before close — just test idempotency.
        finally:
            for _ in range(5):
                await s.close()
        assert s._closed is True
        assert s._db is None
        assert s._executor is None

    async def test_executor_cleared_after_close(self, tmp_path: Path) -> None:
        """close() clears the executor after its worker fully shuts down."""
        s = SQLiteStorage(str(tmp_path / "test.db"))
        try:
            await s.initialize()
            assert s._executor is not None
        finally:
            await s.close()
        assert s._executor is None

    async def test_closed_flag_set_before_db_operations(self, tmp_path: Path) -> None:
        """_closed must be True *before* the DB close() runs."""
        s = SQLiteStorage(str(tmp_path / "test.db"))
        try:
            await s.initialize()
            closed_during_close = False
            real_db = s._db
            assert real_db is not None

            class _InspectClose:
                """Wrapper that checks _closed when close() is called."""

                def close(self) -> None:
                    nonlocal closed_during_close
                    closed_during_close = s._closed
                    real_db.close()

            s._db = _InspectClose()  # type: ignore[assignment]
            await s.close()
            assert closed_during_close is True
        finally:
            await s.close()

    async def test_executor_cleared_even_if_db_close_raises(
        self, tmp_path: Path
    ) -> None:
        """A close failure still clears the executor and permits retry."""
        s = SQLiteStorage(str(tmp_path / "test.db"))
        await s.initialize()
        assert s._executor is not None

        real_db = s._db
        assert real_db is not None
        real_db.close()

        class _FailingConn:
            def close(self) -> None:
                raise RuntimeError("simulated db close failure")

        failing = _FailingConn()
        s._db = failing  # type: ignore[assignment]
        with pytest.raises(RuntimeError, match="simulated db close failure"):
            await s.close()

        assert s._executor is None
        assert s._db is failing
        assert s._closed is False

        s._db = None
        await s.close()


    async def test_close_repeated_cancellation_keeps_closed_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Repeated cancellation cannot resurrect a closing connection."""
        s = SQLiteStorage(str(tmp_path / "test.db"))
        await s.initialize()
        entered = threading.Event()
        release = threading.Event()
        real_sync_close = sqlite_storage_module.sync_close

        def _slow_close(db: object, lock: threading.Lock) -> None:
            entered.set()
            if not release.wait(timeout=2):
                raise RuntimeError("test close barrier timed out")
            real_sync_close(db, lock)  # type: ignore[arg-type]

        monkeypatch.setattr(sqlite_storage_module, "sync_close", _slow_close)
        task = asyncio.create_task(s.close())
        assert await asyncio.to_thread(entered.wait, 2)

        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert s._closed is True
        assert s._db is None
        assert s._executor is None

    async def test_close_failure_after_cancellation_restores_retryable_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A genuine close failure wins over cancellation and restores state."""
        s = SQLiteStorage(":memory:")
        entered = threading.Event()
        release = threading.Event()

        class _FailingConn:
            def close(self) -> None:
                entered.set()
                if not release.wait(timeout=2):
                    raise RuntimeError("test close barrier timed out")
                raise RuntimeError("simulated db close failure")

        failing = _FailingConn()
        s._db = failing  # type: ignore[assignment]
        task = asyncio.create_task(s.close())
        assert await asyncio.to_thread(entered.wait, 2)

        task.cancel()
        release.set()
        with pytest.raises(RuntimeError, match="simulated db close failure") as exc_info:
            await task

        assert isinstance(exc_info.value.__cause__, asyncio.CancelledError)
        assert s._closed is False
        assert s._db is failing
        assert s._executor is None

    async def test_close_drains_executor_shutdown_before_reraising_cancellation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cancellation during executor shutdown is deferred until it finishes."""
        s = SQLiteStorage(str(tmp_path / "test.db"))
        await s.initialize()
        entered = threading.Event()
        release = threading.Event()
        real_shutdown = ThreadPoolExecutor.shutdown

        def _slow_shutdown(executor, *args, **kwargs) -> None:
            entered.set()
            if not release.wait(timeout=2):
                raise RuntimeError("test shutdown barrier timed out")
            real_shutdown(executor, *args, **kwargs)

        monkeypatch.setattr(ThreadPoolExecutor, "shutdown", _slow_shutdown)
        task = asyncio.create_task(s.close())
        assert await asyncio.to_thread(entered.wait, 2)

        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert s._closed is True
        assert s._db is None
        assert s._executor is None

    async def test_close_safe_when_db_is_none(self) -> None:
        """close() on a never-initialized storage clears executor if present."""
        s = SQLiteStorage(":memory:")
        assert s._db is None
        await s.close()
        assert s._closed is True
        assert s._db is None
        assert s._executor is None

    async def test_close_finishes_connection_close_before_reraising_cancellation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cancellation cannot leave the executor-owned connection half-closed."""
        s = SQLiteStorage(str(tmp_path / "test.db"))
        await s.initialize()
        entered = threading.Event()
        release = threading.Event()
        real_sync_close = sqlite_storage_module.sync_close

        def _slow_close(db: object, lock: threading.Lock) -> None:
            entered.set()
            if not release.wait(timeout=2):
                raise RuntimeError("test close barrier timed out")
            real_sync_close(db, lock)  # type: ignore[arg-type]

        monkeypatch.setattr(sqlite_storage_module, "sync_close", _slow_close)
        task = asyncio.create_task(s.close())
        assert await asyncio.to_thread(entered.wait, 2)

        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert s._closed is True
        assert s._db is None
        assert s._executor is None


# ===================================================================
# 3. Serde NativeRef construction guard
# ===================================================================


class TestSerdeNativeRefGuard:
    """Test _row_to_relation NativeRef construction guard."""

    def test_no_native_ref_when_adapter_missing(self) -> None:
        """When target_native_adapter is None/empty, NativeRef must not be built."""
        row: dict[str, Any] = {
            "relation_type": "reply",
            "target_event_id": "evt-1",
            "target_native_adapter": None,
            "target_native_channel_id": None,
            "target_native_message_id": "msg-1",
            "target_native_thread_id": None,
            "key": None,
            "fallback_text": None,
            "metadata": "{}",
        }
        rel = _row_to_relation(row)
        assert rel.target_native_ref is None

    def test_no_native_ref_when_message_id_missing(self) -> None:
        """When target_native_message_id is None, NativeRef must not be built."""
        row: dict[str, Any] = {
            "relation_type": "reply",
            "target_event_id": "evt-1",
            "target_native_adapter": "matrix",
            "target_native_channel_id": None,
            "target_native_message_id": None,
            "target_native_thread_id": None,
            "key": None,
            "fallback_text": None,
            "metadata": "{}",
        }
        rel = _row_to_relation(row)
        assert rel.target_native_ref is None

    def test_native_ref_built_when_both_present(self) -> None:
        """When both adapter and native_message_id are present, NativeRef is built."""
        row: dict[str, Any] = {
            "relation_type": "reply",
            "target_event_id": "evt-1",
            "target_native_adapter": "matrix",
            "target_native_channel_id": "!room:server",
            "target_native_message_id": "$event_id",
            "target_native_thread_id": None,
            "key": None,
            "fallback_text": None,
            "metadata": "{}",
        }
        rel = _row_to_relation(row)
        assert rel.target_native_ref is not None
        assert rel.target_native_ref.adapter == "matrix"
        assert rel.target_native_ref.native_message_id == "$event_id"


# ===================================================================
# 4. Outbox metadata decode
# ===================================================================


class TestOutboxMetadataDecode:
    """Test _row_to_outbox_item metadata decode behavior."""

    def _base_row(self, **overrides: Any) -> dict[str, Any]:
        """Return a minimal valid outbox row dict."""
        row: dict[str, Any] = {
            "outbox_id": "ob-1",
            "event_id": "evt-1",
            "route_id": "",
            "delivery_plan_id": "plan-1",
            "target_adapter": "matrix",
            "target_channel": None,
            "target_address": None,
            "attempt_number": 1,
            "status": "pending",
            "failure_kind": None,
            "failure_kind_detail": None,
            "next_attempt_at": None,
            "created_at": "2025-01-01T00:00:00",
            "updated_at": "2025-01-01T00:00:00",
            "last_attempt_at": None,
            "locked_at": None,
            "lease_until": None,
            "worker_id": None,
            "payload_hash": None,
            "receipt_id": None,
            "parent_receipt_id": None,
            "error_summary": None,
            "metadata": "{}",
        }
        row.update(overrides)
        return row

    def test_valid_json_metadata(self) -> None:
        """Valid JSON metadata is decoded correctly."""
        row = self._base_row(metadata='{"key": "value"}')
        item = _row_to_outbox_item(row)
        assert item.metadata == {"key": "value"}

    def test_corrupt_json_metadata_falls_back(self) -> None:
        """Corrupt JSON metadata falls back to empty dict."""
        row = self._base_row(metadata="{not valid json")
        item = _row_to_outbox_item(row)
        assert item.metadata == {}

    def test_none_metadata_falls_back(self) -> None:
        """None metadata falls back to empty dict."""
        row = self._base_row(metadata=None)
        item = _row_to_outbox_item(row)
        assert item.metadata == {}


# ===================================================================
# 5. Write-batch atomicity
# ===================================================================


class TestWriteBatchAtomicity:
    """Test that failed write batches leave no partial rows."""

    @pytest.fixture
    async def store(self, tmp_path: Path) -> Any:
        """Create, initialize, and yield a SQLiteStorage."""
        db_path = str(tmp_path / "test.db")
        s = SQLiteStorage(db_path)
        await s.initialize()
        yield s
        await s.close()

    async def test_failed_batch_leaves_no_events(self, store: SQLiteStorage) -> None:
        """A batch that fails due to a duplicate must not leave partial rows."""
        from medre.core.events import CanonicalEvent, EventMetadata
        from medre.core.storage.backend import DuplicateEventError

        event = CanonicalEvent(
            event_id="evt-dup",
            event_kind="message",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="test",
            source_transport_id="t1",
            source_channel_id="c1",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "hello"},
            metadata=EventMetadata(),
            depth=0,
            trace_id="trace-1",
            source_native_ref=None,
        )
        # First append succeeds.
        await store.append(event)

        # Second append with same event_id should raise DuplicateEventError.
        with pytest.raises(DuplicateEventError):
            await store.append(event)

        # Verify only one event exists (no partial duplicate).
        count = await store.count_events()
        assert count == 1


# ===================================================================
# 6. Plain import scanner
# ===================================================================


class TestPlainImportScanner:
    """Test the plain import scanner catches forbidden patterns."""

    def test_scanner_catches_bare_import(self, tmp_path: Path) -> None:
        """`import medre.core.storage` should be flagged."""
        f = tmp_path / "catch_bare.py"
        f.write_text("import medre.core.storage\n")
        violations = scan_dir_for_plain_imports(
            tmp_path,
            ("medre.core.storage",),
        )
        assert any("import medre.core.storage" in v for v in violations)

    def test_scanner_catches_import_as(self, tmp_path: Path) -> None:
        """`import medre.core.storage as s` should be flagged."""
        f = tmp_path / "catch_as.py"
        f.write_text("import medre.core.storage as s\n")
        violations = scan_dir_for_plain_imports(
            tmp_path,
            ("medre.core.storage",),
        )
        assert any("import medre.core.storage" in v for v in violations)

    def test_scanner_allows_submodule_import(self, tmp_path: Path) -> None:
        """`import medre.core.storage.backend` should NOT be flagged."""
        f = tmp_path / "allow_submod.py"
        f.write_text("import medre.core.storage.backend\n")
        violations = scan_dir_for_plain_imports(
            tmp_path,
            ("medre.core.storage",),
        )
        assert not any("storage.backend" in v for v in violations)
