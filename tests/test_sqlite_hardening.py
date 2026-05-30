"""Tests for SQLite storage decomposition hardening.

Covers: query builder limit validation, lazy executor lifecycle and
closed-executor guard, serde NativeRef construction guard, outbox metadata
decode fallback, write-batch atomicity, and plain-import scanner regex.
"""

from __future__ import annotations

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
        # If _use_aiosqlite is False, executor should have been created.
        if not s._use_aiosqlite:
            assert s._executor is not None
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
        assert s._closed is False
        await s.close()
        assert s._executor is None
        assert s._closed is True


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
