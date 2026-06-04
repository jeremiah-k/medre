"""Tests for SQLiteStorage: duplicate event error, UTC-aware defaults,
schema shape validation, integrity error classification, storage indexes,
open_readonly, and public count methods.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import timezone

import pytest

from medre.core.events import (
    DeliveryReceipt,
    NativeMessageRef,
)
from medre.core.storage.backend import (
    DuplicateEventError,
    StorageError,
    StorageInitializationError,
)
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage import make_storage_event

# ===================================================================
# DuplicateEventError on duplicate append
# ===================================================================


class TestDuplicateEventError:
    """Appending the same event_id twice raises DuplicateEventError."""

    async def test_duplicate_append_raises_duplicate_event_error(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Appending an event whose event_id already exists raises
        DuplicateEventError (a StorageError subclass)."""
        event = make_storage_event(event_id="evt-dup-test")
        await temp_storage.append(event)

        with pytest.raises(DuplicateEventError) as exc_info:
            await temp_storage.append(event)
        assert "evt-dup-test" in str(exc_info.value) or "Duplicate" in str(
            exc_info.value
        )

    async def test_duplicate_event_error_is_storage_error_subclass(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """DuplicateEventError is a subclass of StorageError."""
        event = make_storage_event(event_id="evt-subclass-test")
        await temp_storage.append(event)

        with pytest.raises(DuplicateEventError) as exc_info:
            await temp_storage.append(event)
        assert isinstance(exc_info.value, StorageError)


# ===================================================================
# UTC-aware default created_at
# ===================================================================


class TestUtcAwareDefaultCreatedAt:
    """NativeMessageRef and DeliveryReceipt default created_at is UTC-aware."""

    def test_native_message_ref_default_created_at_is_utc_aware(self) -> None:
        """NativeMessageRef(created_at not passed) gets a UTC-aware datetime."""
        ref = NativeMessageRef(
            id="nref-utc",
            event_id="evt-utc",
            adapter="test",
            native_channel_id="ch",
            native_message_id="msg",
            native_thread_id=None,
            native_relation_id=None,
            direction="inbound",
        )
        assert ref.created_at.tzinfo is not None
        assert ref.created_at.tzinfo == timezone.utc

    def test_delivery_receipt_default_created_at_is_utc_aware(self) -> None:
        """DeliveryReceipt(created_at not passed) gets a UTC-aware datetime."""
        receipt = DeliveryReceipt(
            receipt_id="rcpt-utc",
            event_id="evt-utc",
            delivery_plan_id="plan-utc",
            target_adapter="test",
        )
        assert receipt.created_at.tzinfo is not None
        assert receipt.created_at.tzinfo == timezone.utc


# ===================================================================
# Schema shape validation
# ===================================================================


class TestSchemaShapeValidation:
    """Detect old pre-release DBs whose schema_version=1 but column shape
    predates the current DDL.

    initialize() must raise StorageInitializationError with clear guidance
    to recreate the database.
    """

    async def test_old_event_relations_missing_columns(self) -> None:
        """An event_relations table lacking target_native_thread_id
        triggers StorageInitializationError even though schema_version=1."""
        import sqlite3

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            # Build an old-shape database: event_relations without
            # target_native_thread_id (and a few other newer columns).
            raw = sqlite3.connect(db_path)
            try:
                raw.executescript("""
                    CREATE TABLE IF NOT EXISTS canonical_events (
                        event_id TEXT PRIMARY KEY,
                        event_kind TEXT NOT NULL,
                        schema_version INTEGER NOT NULL,
                        timestamp TEXT NOT NULL,
                        source_adapter TEXT NOT NULL,
                        source_transport_id TEXT NOT NULL,
                        source_channel_id TEXT,
                        parent_event_id TEXT,
                        lineage TEXT NOT NULL DEFAULT '[]',
                        payload TEXT NOT NULL DEFAULT '{}',
                        metadata TEXT NOT NULL DEFAULT '{}',
                        depth INTEGER NOT NULL DEFAULT 0,
                        trace_id TEXT,
                        source_native_adapter TEXT,
                        source_native_channel_id TEXT,
                        source_native_message_id TEXT,
                        source_native_thread_id TEXT,
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS event_relations (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_id TEXT NOT NULL,
                        relation_type TEXT NOT NULL,
                        target_event_id TEXT,
                        target_native_adapter TEXT,
                        target_native_channel_id TEXT,
                        target_native_message_id TEXT,
                        key TEXT,
                        fallback_text TEXT,
                        metadata TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS native_message_refs (
                        id TEXT PRIMARY KEY,
                        event_id TEXT NOT NULL,
                        adapter TEXT NOT NULL,
                        native_channel_id TEXT,
                        native_message_id TEXT NOT NULL,
                        direction TEXT NOT NULL,
                        metadata TEXT,
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS delivery_receipts (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        receipt_id TEXT UNIQUE NOT NULL,
                        event_id TEXT NOT NULL,
                        delivery_plan_id TEXT NOT NULL,
                        target_adapter TEXT NOT NULL,
                        route_id TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL,
                        error TEXT,
                        adapter_message_id TEXT,
                        next_retry_at TEXT,
                        attempt_number INTEGER NOT NULL DEFAULT 1,
                        parent_receipt_id TEXT,
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS plugin_state (
                        plugin_id TEXT NOT NULL,
                        key TEXT NOT NULL,
                        value TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY(plugin_id, key)
                    );
                    CREATE TABLE IF NOT EXISTS _medre_schema_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    INSERT INTO _medre_schema_meta (key, value)
                        VALUES ('schema_version', '1');
                """)
            finally:
                raw.close()

            storage = SQLiteStorage(db_path=db_path)
            with pytest.raises(
                StorageInitializationError, match="schema shape mismatch"
            ):
                await storage.initialize()
        finally:
            os.unlink(db_path)

    async def test_old_native_message_refs_missing_columns(self) -> None:
        """A native_message_refs table lacking native_thread_id triggers
        StorageInitializationError."""
        import sqlite3

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            raw = sqlite3.connect(db_path)
            try:
                # Create all tables with current shape EXCEPT native_message_refs
                # which is missing native_thread_id and native_relation_id.
                raw.executescript("""
                    CREATE TABLE IF NOT EXISTS canonical_events (
                        event_id TEXT PRIMARY KEY,
                        event_kind TEXT NOT NULL,
                        schema_version INTEGER NOT NULL,
                        timestamp TEXT NOT NULL,
                        source_adapter TEXT NOT NULL,
                        source_transport_id TEXT NOT NULL,
                        source_channel_id TEXT,
                        parent_event_id TEXT,
                        lineage TEXT NOT NULL DEFAULT '[]',
                        payload TEXT NOT NULL DEFAULT '{}',
                        metadata TEXT NOT NULL DEFAULT '{}',
                        depth INTEGER NOT NULL DEFAULT 0,
                        trace_id TEXT,
                        source_native_adapter TEXT,
                        source_native_channel_id TEXT,
                        source_native_message_id TEXT,
                        source_native_thread_id TEXT,
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS event_relations (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_id TEXT NOT NULL,
                        relation_type TEXT NOT NULL,
                        target_event_id TEXT,
                        target_native_adapter TEXT,
                        target_native_channel_id TEXT,
                        target_native_message_id TEXT,
                        target_native_thread_id TEXT,
                        key TEXT,
                        fallback_text TEXT,
                        metadata TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS native_message_refs (
                        id TEXT PRIMARY KEY,
                        event_id TEXT NOT NULL,
                        adapter TEXT NOT NULL,
                        native_channel_id TEXT,
                        native_message_id TEXT NOT NULL,
                        direction TEXT NOT NULL,
                        metadata TEXT,
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS delivery_receipts (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        receipt_id TEXT UNIQUE NOT NULL,
                        event_id TEXT NOT NULL,
                        delivery_plan_id TEXT NOT NULL,
                        target_adapter TEXT NOT NULL,
                        route_id TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL,
                        error TEXT,
                        adapter_message_id TEXT,
                        next_retry_at TEXT,
                        attempt_number INTEGER NOT NULL DEFAULT 1,
                        parent_receipt_id TEXT,
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS plugin_state (
                        plugin_id TEXT NOT NULL,
                        key TEXT NOT NULL,
                        value TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY(plugin_id, key)
                    );
                    CREATE TABLE IF NOT EXISTS _medre_schema_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    INSERT INTO _medre_schema_meta (key, value)
                        VALUES ('schema_version', '1');
                """)
            finally:
                raw.close()

            storage = SQLiteStorage(db_path=db_path)
            with pytest.raises(
                StorageInitializationError, match="schema shape mismatch"
            ):
                await storage.initialize()
        finally:
            os.unlink(db_path)

    async def test_fresh_db_passes_shape_validation(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """A freshly initialized DB must pass shape validation without error."""
        # temp_storage fixture calls initialize() which now includes shape
        # validation.  If we reach this point, validation passed.
        event = make_storage_event()
        await temp_storage.append(event)
        retrieved = await temp_storage.get(event.event_id)
        assert retrieved is not None


# ===================================================================
# IntegrityError classification in _write_batch
# ===================================================================


class TestIntegrityErrorClassification:
    """_write_batch distinguishes canonical_events PK violations from other
    IntegrityErrors."""

    async def test_duplicate_event_raises_duplicate_event_error(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Appending a duplicate canonical event raises DuplicateEventError."""
        event = make_storage_event(event_id="evt-dup-classify")
        await temp_storage.append(event)

        with pytest.raises(DuplicateEventError):
            await temp_storage.append(event)

    async def test_non_canonical_integrity_error_raises_storage_error(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """A UNIQUE constraint violation on delivery_receipts (not
        canonical_events) raises StorageError, not DuplicateEventError."""
        event = make_storage_event(event_id="evt-unique-rcpt")
        await temp_storage.append(event)

        receipt = DeliveryReceipt(
            receipt_id="rcpt-dup-unique",
            event_id="evt-unique-rcpt",
            delivery_plan_id="plan-unique",
            target_adapter="adapter_u",
            status="sent",
        )
        await temp_storage.append_receipt(receipt)

        # Insert same receipt_id again — UNIQUE constraint on
        # delivery_receipts.receipt_id, NOT canonical_events.
        with pytest.raises(StorageError) as exc_info:
            await temp_storage.append_receipt(receipt)
        # Must NOT be a DuplicateEventError.
        assert not isinstance(exc_info.value, DuplicateEventError)


# ===================================================================
# Storage indexes
# ===================================================================


class TestStorageIndexes:
    """Targeted indexes matching actual query patterns are created on init."""

    @staticmethod
    async def _index_columns(
        storage: SQLiteStorage, table: str
    ) -> dict[str, frozenset[str]]:
        """Return {index_name: frozenset of column names} for *table*."""
        rows = await storage._read_all(f"PRAGMA index_list({table})", ())
        result: dict[str, frozenset[str]] = {}
        for row in rows:
            idx_name = row["name"]
            # Skip SQLite autoindices (internal names like sqlite_autoindex_...)
            if idx_name.startswith("sqlite_autoindex"):
                continue
            cols = await storage._read_all(f"PRAGMA index_info({idx_name})", ())
            result[idx_name] = frozenset(r["name"] for r in cols)
        return result

    async def test_canonical_events_timestamp_index(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """idx_events_timestamp on canonical_events(timestamp, event_id)."""
        indexes = await self._index_columns(temp_storage, "canonical_events")
        assert "idx_events_timestamp" in indexes
        assert indexes["idx_events_timestamp"] == frozenset({"timestamp", "event_id"})

    async def test_event_relations_event_id_index(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """idx_relations_event_id on event_relations(event_id, id)."""
        indexes = await self._index_columns(temp_storage, "event_relations")
        assert "idx_relations_event_id" in indexes
        assert indexes["idx_relations_event_id"] == frozenset({"event_id", "id"})

    async def test_native_message_refs_event_id_index(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """idx_nrefs_event_created on native_message_refs(event_id, created_at).

        Replaces the older idx_nrefs_event_id(event_id).  The composite
        index covers both the WHERE event_id=? filter and the ORDER BY
        created_at ASC used by _SELECT_NREFS_FOR_EVENT.

        The UNIQUE(adapter, native_channel_id, native_message_id) constraint
        creates an autoindex; we do NOT assert a manual index for that triple.
        """
        indexes = await self._index_columns(temp_storage, "native_message_refs")
        assert "idx_nrefs_event_created" in indexes
        assert indexes["idx_nrefs_event_created"] == frozenset(
            {"event_id", "created_at"}
        )

    async def test_receipts_plan_index(self, temp_storage: SQLiteStorage) -> None:
        """idx_receipts_plan on delivery_receipts(delivery_plan_id, target_adapter, target_channel, attempt_number, sequence).

        For composite indexes, column order matters for query planning.
        Assert the exact ordered column list via PRAGMA index_info.
        """
        rows = await temp_storage._read_all(
            "PRAGMA index_info('idx_receipts_plan')", ()
        )
        ordered_cols = [r["name"] for r in rows]
        assert ordered_cols == [
            "delivery_plan_id",
            "target_adapter",
            "target_channel",
            "attempt_number",
            "sequence",
        ], f"Column order mismatch: {ordered_cols!r}"

    async def test_receipts_event_index(self, temp_storage: SQLiteStorage) -> None:
        """idx_receipts_event on delivery_receipts(event_id, sequence)."""
        indexes = await self._index_columns(temp_storage, "delivery_receipts")
        assert "idx_receipts_event" in indexes
        assert indexes["idx_receipts_event"] == frozenset({"event_id", "sequence"})

    async def test_receipts_source_index(self, temp_storage: SQLiteStorage) -> None:
        """idx_receipts_source on delivery_receipts(source, replay_run_id)."""
        indexes = await self._index_columns(temp_storage, "delivery_receipts")
        assert "idx_receipts_source" in indexes
        assert indexes["idx_receipts_source"] == frozenset({"source", "replay_run_id"})

    async def test_receipts_replay_run_index(self, temp_storage: SQLiteStorage) -> None:
        """idx_receipts_replay_run on delivery_receipts(replay_run_id).

        Serves _SELECT_RECEIPTS_BY_REPLAY_RUN which filters by replay_run_id
        alone (without source).  idx_receipts_source(source, replay_run_id)
        cannot serve this query because source is not in the WHERE clause.
        """
        indexes = await self._index_columns(temp_storage, "delivery_receipts")
        assert "idx_receipts_replay_run" in indexes
        assert indexes["idx_receipts_replay_run"] == frozenset({"replay_run_id"})

    async def test_no_manual_index_for_unique_autoindex(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """No manual index duplicates the UNIQUE autoindex on
        native_message_refs(adapter, native_channel_id, native_message_id).

        The UNIQUE constraint already creates an autoindex; a manual
        duplicate would be redundant.
        """
        indexes = await self._index_columns(temp_storage, "native_message_refs")
        for name, cols in indexes.items():
            # None of our manual indexes should cover the UNIQUE triple.
            if cols == frozenset({"adapter", "native_channel_id", "native_message_id"}):
                pytest.fail(
                    f"Redundant manual index '{name}' duplicates UNIQUE autoindex"
                )


# ===================================================================
# open_readonly — strict read-only open for inspect commands
# ===================================================================


class TestOpenReadonly:
    """SQLiteStorage.open_readonly() opens existing DBs without mutation."""

    async def test_missing_file_raises(self) -> None:
        """open_readonly raises StorageInitializationError for missing file."""
        with pytest.raises(StorageInitializationError, match="does not exist"):
            await SQLiteStorage.open_readonly("/nonexistent/path/test.db")

    async def test_missing_file_not_created(self) -> None:
        """open_readonly does not create the file even transiently."""
        db_path = os.path.join(
            tempfile.gettempdir(), f"medre-test-nocreate-{os.getpid()}.db"
        )
        assert not os.path.exists(db_path)
        try:
            with pytest.raises(StorageInitializationError):
                await SQLiteStorage.open_readonly(db_path)
            assert not os.path.exists(db_path)
        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)

    async def test_valid_db_reads_events(self) -> None:
        """open_readonly on a valid initialized DB can read events."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            # Write phase — normal initialize.
            storage = SQLiteStorage(db_path)
            await storage.initialize()
            event = make_storage_event(event_id="readonly-evt-1")
            await storage.append(event)
            await storage.close()

            # Read-only phase.
            ro = await SQLiteStorage.open_readonly(db_path)
            retrieved = await ro.get("readonly-evt-1")
            assert retrieved is not None
            assert retrieved.event_id == "readonly-evt-1"
            await ro.close()
        finally:
            os.unlink(db_path)

    async def test_fresh_empty_db_raises(self) -> None:
        """open_readonly on a file with no tables raises StorageInitializationError."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            # File exists but is an empty SQLite database — no tables.
            raw = sqlite3.connect(db_path)
            try:
                pass  # Just create an empty DB file
            finally:
                raw.close()

            with pytest.raises(StorageInitializationError, match="no schema version"):
                await SQLiteStorage.open_readonly(db_path)
        finally:
            os.unlink(db_path)

    async def test_old_shape_db_raises(self) -> None:
        """open_readonly on an old-shape DB raises StorageInitializationError."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            raw = sqlite3.connect(db_path)
            try:
                # Minimal old-shape: event_relations without target_native_thread_id.
                raw.executescript("""
                    CREATE TABLE canonical_events (
                        event_id TEXT PRIMARY KEY,
                        event_kind TEXT NOT NULL,
                        schema_version INTEGER NOT NULL,
                        timestamp TEXT NOT NULL,
                        source_adapter TEXT NOT NULL,
                        source_transport_id TEXT NOT NULL,
                        source_channel_id TEXT,
                        parent_event_id TEXT,
                        lineage TEXT NOT NULL DEFAULT '[]',
                        payload TEXT NOT NULL DEFAULT '{}',
                        metadata TEXT NOT NULL DEFAULT '{}',
                        depth INTEGER NOT NULL DEFAULT 0,
                        trace_id TEXT,
                        source_native_adapter TEXT,
                        source_native_channel_id TEXT,
                        source_native_message_id TEXT,
                        source_native_thread_id TEXT,
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE event_relations (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_id TEXT NOT NULL,
                        relation_type TEXT NOT NULL,
                        target_event_id TEXT,
                        target_native_adapter TEXT,
                        target_native_channel_id TEXT,
                        target_native_message_id TEXT,
                        key TEXT,
                        fallback_text TEXT,
                        metadata TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE native_message_refs (
                        id TEXT PRIMARY KEY,
                        event_id TEXT NOT NULL,
                        adapter TEXT NOT NULL,
                        native_channel_id TEXT,
                        native_message_id TEXT NOT NULL,
                        direction TEXT NOT NULL,
                        metadata TEXT,
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE delivery_receipts (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        receipt_id TEXT UNIQUE NOT NULL,
                        event_id TEXT NOT NULL,
                        delivery_plan_id TEXT NOT NULL,
                        target_adapter TEXT NOT NULL,
                        route_id TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL,
                        error TEXT,
                        adapter_message_id TEXT,
                        next_retry_at TEXT,
                        attempt_number INTEGER NOT NULL DEFAULT 1,
                        parent_receipt_id TEXT,
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE plugin_state (
                        plugin_id TEXT NOT NULL,
                        key TEXT NOT NULL,
                        value TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY(plugin_id, key)
                    );
                    CREATE TABLE _medre_schema_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    INSERT INTO _medre_schema_meta (key, value)
                        VALUES ('schema_version', '1');
                """)
            finally:
                raw.close()

            with pytest.raises(
                StorageInitializationError, match="schema shape mismatch"
            ):
                await SQLiteStorage.open_readonly(db_path)
        finally:
            os.unlink(db_path)

    async def test_readonly_rejects_writes(self) -> None:
        """open_readonly connection rejects INSERT (SQLite mode=ro)."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            # Create a valid DB with one event.
            storage = SQLiteStorage(db_path)
            await storage.initialize()
            event = make_storage_event(event_id="ro-write-test")
            await storage.append(event)
            await storage.close()

            # Open read-only and attempt to write.
            ro = await SQLiteStorage.open_readonly(db_path)
            with pytest.raises(Exception):  # noqa: B017
                # SQLite will reject the INSERT in mode=ro.
                duplicate = make_storage_event(event_id="should-fail")
                await ro.append(duplicate)
            await ro.close()
        finally:
            os.unlink(db_path)


# ===================================================================
# Public count methods: count_native_refs, count_receipts_by_source,
# count_replay_runs
# ===================================================================


class TestPublicCountMethods:
    """Public count methods on SQLiteStorage."""

    async def test_count_native_refs_empty(self, temp_storage: SQLiteStorage) -> None:
        """count_native_refs returns 0 on a fresh database."""
        assert await temp_storage.count_native_refs() == 0

    async def test_count_native_refs_after_storing(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """count_native_refs returns the correct total after storing refs."""
        event = make_storage_event(event_id="evt-cnt-nref")
        await temp_storage.append(event)

        for i in range(3):
            ref = NativeMessageRef(
                id=f"nref-cnt-{i}",
                event_id="evt-cnt-nref",
                adapter=f"adapter_{i}",
                native_channel_id="ch-0",
                native_message_id=f"msg-{i}",
                native_thread_id=None,
                native_relation_id=None,
                direction="outbound",
            )
            await temp_storage.store_native_ref(ref)

        assert await temp_storage.count_native_refs() == 3

    async def test_count_receipts_by_source_live_only(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """count_receipts_by_source('live') counts only live receipts."""
        event = make_storage_event(event_id="evt-src-live")
        await temp_storage.append(event)

        for i in range(4):
            receipt = DeliveryReceipt(
                receipt_id=f"rcpt-src-live-{i}",
                event_id="evt-src-live",
                delivery_plan_id=f"plan-live-{i}",
                target_adapter=f"adapter_{i}",
                status="sent",
                source="live",
            )
            await temp_storage.append_receipt(receipt)

        assert await temp_storage.count_receipts_by_source("live") == 4
        assert await temp_storage.count_receipts_by_source("replay") == 0

    async def test_count_receipts_by_source_replay(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """count_receipts_by_source('replay') counts only replay receipts."""
        event = make_storage_event(event_id="evt-src-replay")
        await temp_storage.append(event)

        receipt = DeliveryReceipt(
            receipt_id="rcpt-src-replay-0",
            event_id="evt-src-replay",
            delivery_plan_id="plan-replay-0",
            target_adapter="adapter_r",
            status="sent",
            source="replay",
            replay_run_id="run-1",
        )
        await temp_storage.append_receipt(receipt)

        assert await temp_storage.count_receipts_by_source("replay") == 1
        assert await temp_storage.count_receipts_by_source("live") == 0

    async def test_count_receipts_by_source_mixed(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """count_receipts_by_source distinguishes live from replay in mixed data."""
        event = make_storage_event(event_id="evt-src-mix")
        await temp_storage.append(event)

        for i in range(2):
            r_live = DeliveryReceipt(
                receipt_id=f"rcpt-mix-live-{i}",
                event_id="evt-src-mix",
                delivery_plan_id=f"plan-mix-l-{i}",
                target_adapter=f"adapter_l_{i}",
                status="sent",
                source="live",
            )
            await temp_storage.append_receipt(r_live)

        for i in range(3):
            r_replay = DeliveryReceipt(
                receipt_id=f"rcpt-mix-replay-{i}",
                event_id="evt-src-mix",
                delivery_plan_id=f"plan-mix-r-{i}",
                target_adapter=f"adapter_r_{i}",
                status="sent",
                source="replay",
                replay_run_id=f"run-mix-{i}",
            )
            await temp_storage.append_receipt(r_replay)

        assert await temp_storage.count_receipts_by_source("live") == 2
        assert await temp_storage.count_receipts_by_source("replay") == 3

    async def test_count_receipts_by_source_empty(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """count_receipts_by_source returns 0 when no receipts exist."""
        assert await temp_storage.count_receipts_by_source("live") == 0
        assert await temp_storage.count_receipts_by_source("replay") == 0

    async def test_count_replay_runs_empty(self, temp_storage: SQLiteStorage) -> None:
        """count_replay_runs returns 0 when no replay receipts exist."""
        assert await temp_storage.count_replay_runs() == 0

    async def test_count_replay_runs_distinct(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """count_replay_runs returns the count of distinct replay_run_ids."""
        event = make_storage_event(event_id="evt-replay-runs")
        await temp_storage.append(event)

        # 3 receipts with run-a, 2 with run-b, 1 live (no run).
        for i in range(3):
            r = DeliveryReceipt(
                receipt_id=f"rcpt-rr-a-{i}",
                event_id="evt-replay-runs",
                delivery_plan_id=f"plan-rr-a-{i}",
                target_adapter=f"adapter_a_{i}",
                status="sent",
                source="replay",
                replay_run_id="run-a",
            )
            await temp_storage.append_receipt(r)

        for i in range(2):
            r = DeliveryReceipt(
                receipt_id=f"rcpt-rr-b-{i}",
                event_id="evt-replay-runs",
                delivery_plan_id=f"plan-rr-b-{i}",
                target_adapter=f"adapter_b_{i}",
                status="sent",
                source="replay",
                replay_run_id="run-b",
            )
            await temp_storage.append_receipt(r)

        # Live receipt — should not count.
        r_live = DeliveryReceipt(
            receipt_id="rcpt-rr-live",
            event_id="evt-replay-runs",
            delivery_plan_id="plan-rr-live",
            target_adapter="adapter_live",
            status="sent",
            source="live",
        )
        await temp_storage.append_receipt(r_live)

        assert await temp_storage.count_replay_runs() == 2

    async def test_count_receipts_by_source_unknown_source(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """count_receipts_by_source returns 0 for a source that has no matches."""
        assert await temp_storage.count_receipts_by_source("nonexistent") == 0
