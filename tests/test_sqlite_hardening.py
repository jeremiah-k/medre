"""Tests for SQLite storage decomposition hardening.

Covers: query builder limit validation, lazy executor lifecycle and
closed-executor guard, serde NativeRef construction guard, outbox metadata
decode fallback, write-batch atomicity, and plain-import scanner regex.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from medre.core.storage.backend import EventFilter
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

    async def test_executor_created_lazily_on_sync_path(self, tmp_path: Path) -> None:
        """Executor is created on first _run_in_thread (sync fallback path)."""
        s = SQLiteStorage(str(tmp_path / "test.db"))
        await s.initialize()
        try:
            # If _use_aiosqlite is False, executor should have been created.
            if not s._use_aiosqlite:
                assert s._executor is not None
        finally:
            await s.close()

    async def test_run_in_thread_raises_after_close(self, tmp_path: Path) -> None:
        """After close(), _run_in_thread must raise RuntimeError regardless
        of whether aiosqlite is available."""
        s = SQLiteStorage(str(tmp_path / "test.db"))
        await s.initialize()
        await s.close()
        with pytest.raises(RuntimeError, match="SQLiteStorage is closed"):
            await s._run_in_thread(lambda: None)

    async def test_close_sets_executor_none(self, tmp_path: Path) -> None:
        """close() must set _executor to None and _closed to True."""
        s = SQLiteStorage(str(tmp_path / "test.db"))
        await s.initialize()
        try:
            assert s._closed is False
        finally:
            await s.close()
        assert s._executor is None
        assert s._closed is True

    async def test_close_is_idempotent_repeated(self, tmp_path: Path) -> None:
        """Calling close() many times is safe — no errors, no state drift."""
        s = SQLiteStorage(str(tmp_path / "test.db"))
        await s.initialize()
        try:
            pass  # No assertion before close — just test idempotency.
        finally:
            for _ in range(5):
                await s.close()
        assert s._closed is True
        assert s._db is None
        assert s._executor is None

    async def test_executor_cleared_after_close(self, tmp_path: Path) -> None:
        """close() must clear _executor after full shutdown (wait=True via asyncio.to_thread)."""
        s = SQLiteStorage(str(tmp_path / "test.db"))
        await s.initialize()
        try:
            # Executor is created lazily — verify it exists on sync path.
            if not s._use_aiosqlite:
                # Force executor creation by submitting a task.
                await s._run_in_thread(lambda: None)
                assert s._executor is not None
        finally:
            await s.close()
        assert s._executor is None

    async def test_closed_flag_set_before_db_operations(self, tmp_path: Path) -> None:
        """_closed must be True *before* the DB close() runs."""
        s = SQLiteStorage(str(tmp_path / "test.db"))
        await s.initialize()
        try:
            if not s._use_aiosqlite:
                closed_during_close = False
                real_db = s._db

                class _InspectClose:
                    """Wrapper that checks _closed when close() is called."""

                    def close(self):
                        nonlocal closed_during_close
                        closed_during_close = s._closed
                        real_db.close()

                s._db = _InspectClose()
                await s.close()
                assert (
                    closed_during_close is True
                ), "_closed should be True when db.close() is called"
            else:
                await s.close()
        finally:
            await s.close()

    async def test_executor_cleared_even_if_db_close_raises(
        self, tmp_path: Path
    ) -> None:
        """If db.close() raises, executor must still be shut down and cleared.

        For the sync executor path, failure is injected into db.close() to
        prove the finally-block still clears the executor.  For the aiosqlite
        path there is no executor to protect, so we close normally and
        confirm the invariant holds — this avoids leaking the real aiosqlite
        connection (which would cause a ResourceWarning).
        """
        s = SQLiteStorage(str(tmp_path / "test.db"))
        await s.initialize()
        assert s._executor is not None or s._use_aiosqlite

        if s._use_aiosqlite:
            # aiosqlite has no private executor — close normally and verify.
            try:
                pass
            finally:
                await s.close()
            assert s._executor is None
        else:
            # Sync path — close the real connection ourselves, then install
            # a mock whose close() raises to prove executor cleanup still
            # happens in the finally-block.
            real_db = s._db
            real_db.close()

            class _FailingConn:
                def close(self):
                    raise RuntimeError("simulated sync db.close failure")

            s._db = _FailingConn()

            try:
                with pytest.raises(
                    RuntimeError, match="simulated sync db.close failure"
                ):
                    await s.close()
            finally:
                # Ensure cleanup even if pytest.raises doesn't match.
                await s.close()
            assert s._executor is None

    async def test_close_safe_when_db_is_none(self) -> None:
        """close() on a never-initialized storage clears executor if present."""
        s = SQLiteStorage(":memory:")
        assert s._db is None
        await s.close()
        assert s._closed is True
        assert s._db is None
        assert s._executor is None

    async def test_aiosqlite_close_survives_external_cancellation(
        self, tmp_path: Path
    ) -> None:
        """When external CancelledError arrives during close(), the aiosqlite
        connection still closes cleanly — no ResourceWarning, no leaked state.

        Regression test: before the shield fix, a CancelledError delivered at
        the ``await aiosqlite.close()`` checkpoint would abort the close
        before aiosqlite's internal thread was joined, causing
        ``ResourceWarning: ... was deleted before being closed``.
        """
        s = SQLiteStorage(str(tmp_path / "test.db"))
        await s.initialize()
        if not s._use_aiosqlite:
            await s.close()
            pytest.skip("aiosqlite not available — cancellation path is aiosqlite-only")

        real_close = s._db.close
        entered = asyncio.Event()
        finished_real_close = asyncio.Event()

        async def _slow_close() -> None:
            entered.set()
            # Hold the close open long enough for the cancel to arrive.
            await asyncio.sleep(0.15)
            await real_close()
            finished_real_close.set()

        s._db.close = _slow_close

        # Start close() in a background task.
        task = asyncio.create_task(s.close())

        # Wait until the slow close has started, then give the event loop a
        # tick so the shield await is definitely entered.
        await entered.wait()
        await asyncio.sleep(0)

        # Cancel the outer close() task while the inner close is still running.
        task.cancel()

        try:
            # The shield catches the cancel, waits for the inner close to finish,
            # then re-raises CancelledError to the caller.
            with pytest.raises(asyncio.CancelledError):
                await task

            # The shield must wait for the inner aiosqlite close to actually finish —
            # not just reach the boundary of the close.  This guards against future
            # regressions where the shield is removed or bypassed.
            assert finished_real_close.is_set(), "inner aiosqlite close never completed"
            # Storage must be in a clean closed state.
            assert s._executor is None
            assert s._closed is True
            assert s._db is None
        finally:
            await s.close()


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
