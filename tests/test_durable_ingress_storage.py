"""Atomic durable-ingress storage contract tests."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from datetime import UTC, datetime

import msgspec
import pytest

from medre.core.events import CanonicalEvent, EventRelation, NativeMessageRef, NativeRef
from medre.core.storage.backend import DuplicateEventError, StorageError
from medre.core.storage.sqlite.schema import _SCHEMA
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage import make_storage_event


class _AsyncCursor:
    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self._cursor = cursor

    def __await__(self) -> Generator[None, None, _AsyncCursor]:
        async def _ready() -> _AsyncCursor:
            return self

        return _ready().__await__()

    async def __aenter__(self) -> _AsyncCursor:
        return self

    async def __aexit__(
        self, _exc_type: object, _exc: object, _tb: object
    ) -> None:
        self._cursor.close()

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    async def fetchone(self) -> sqlite3.Row | None:
        return self._cursor.fetchone()

    async def fetchall(self) -> list[sqlite3.Row]:
        return self._cursor.fetchall()


class _AsyncConnection:
    def __init__(self, db: sqlite3.Connection, *, fail_rollback: bool = False) -> None:
        self._db = db
        self._fail_rollback = fail_rollback

    def execute(
        self, sql: str, params: tuple[object, ...] = ()
    ) -> _AsyncCursor:
        return _AsyncCursor(self._db.execute(sql, params))

    async def commit(self) -> None:
        self._db.commit()

    async def rollback(self) -> None:
        if self._fail_rollback:
            raise sqlite3.OperationalError("rollback failed")
        self._db.rollback()

    async def close(self) -> None:
        self._db.close()


def _async_storage(*, fail_rollback: bool = False) -> SQLiteStorage:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(_SCHEMA)
    storage = SQLiteStorage(":memory:")
    storage._use_aiosqlite = True
    storage._db = _AsyncConnection(db, fail_rollback=fail_rollback)
    return storage


def _event(event_id: str, native_id: str) -> CanonicalEvent:
    event = make_storage_event(event_id=event_id, source_adapter="matrix")
    return msgspec.structs.replace(
        event,
        source_native_ref=NativeRef(
            adapter="matrix-main",
            native_channel_id="!room:example.org",
            native_message_id=native_id,
        ),
    )


def _ref(event_id: str, native_id: str) -> NativeMessageRef:
    return NativeMessageRef(
        id=f"nref-{event_id}",
        event_id=event_id,
        adapter="matrix-main",
        native_channel_id="!room:example.org",
        native_message_id=native_id,
        native_thread_id=None,
        native_relation_id=None,
        direction="inbound",
        created_at=datetime.now(UTC),
    )


async def test_atomic_admission_persists_event_ref_and_pending_work(tmp_path) -> None:
    storage = SQLiteStorage(str(tmp_path / "medre.db"))
    await storage.initialize()
    try:
        event = _event("evt-live", "$native-live")
        result = await storage.admit_ingress(
            event,
            _ref(event.event_id, "$native-live"),
            "live",
        )

        assert result.created is True
        assert result.event_id == event.event_id
        assert result.work_status == "pending"
        assert await storage.get(event.event_id) is not None
        assert (
            await storage.resolve_native_ref(
                "matrix-main", "!room:example.org", "$native-live"
            )
            == event.event_id
        )
        row = await storage._read_one(
            "SELECT provenance, status FROM durable_ingress_work WHERE event_id = ?",
            (event.event_id,),
        )
        assert row == {"provenance": "live", "status": "pending"}
    finally:
        await storage.close()


async def test_unsupported_provenance_is_rejected(tmp_path) -> None:
    storage = SQLiteStorage(str(tmp_path / "medre.db"))
    await storage.initialize()
    try:
        event = _event("evt-bad-provenance", "$bad-provenance")
        with pytest.raises(ValueError, match="unsupported ingress provenance"):
            await storage.admit_ingress(
                event, _ref(event.event_id, "$bad-provenance"), "backfill"
            )
    finally:
        await storage.close()


async def test_mismatched_inbound_ref_event_id_is_rejected(tmp_path) -> None:
    storage = SQLiteStorage(str(tmp_path / "medre.db"))
    await storage.initialize()
    try:
        event = _event("evt-mismatch", "$mismatch")
        with pytest.raises(ValueError, match=r"inbound_ref\.event_id"):
            await storage.admit_ingress(event, _ref("evt-other", "$mismatch"), "live")
    finally:
        await storage.close()


async def test_duplicate_canonical_id_with_new_native_identity_is_rejected(
    tmp_path,
) -> None:
    storage = SQLiteStorage(str(tmp_path / "medre.db"))
    await storage.initialize()
    try:
        first = _event("evt-same-id", "$first-native")
        await storage.admit_ingress(
            first, _ref(first.event_id, "$first-native"), "live"
        )
        replay = _event("evt-same-id", "$second-native")

        with pytest.raises(DuplicateEventError, match="Duplicate event"):
            await storage.admit_ingress(
                replay, _ref(replay.event_id, "$second-native"), "recovered"
            )
    finally:
        await storage.close()


async def test_duplicate_native_admission_returns_original_identity(tmp_path) -> None:
    storage = SQLiteStorage(str(tmp_path / "medre.db"))
    await storage.initialize()
    try:
        first = _event("evt-original", "$same-native")
        await storage.admit_ingress(
            first,
            _ref(first.event_id, "$same-native"),
            "live",
        )

        replay = _event("evt-redecoded", "$same-native")
        result = await storage.admit_ingress(
            replay,
            _ref(replay.event_id, "$same-native"),
            "recovered",
        )

        assert result.created is False
        assert result.event_id == first.event_id
        assert result.provenance == "live"
        assert await storage.get(replay.event_id) is None
    finally:
        await storage.close()


@pytest.mark.parametrize(
    ("update_sql", "invalid_value", "error_match"),
    [
        (
            "UPDATE durable_ingress_work SET provenance = ? WHERE event_id = ?",
            "unknown",
            "invalid durable ingress provenance",
        ),
        (
            "UPDATE durable_ingress_work SET status = ? WHERE event_id = ?",
            "unknown",
            "invalid durable ingress work status",
        ),
    ],
)
async def test_duplicate_admission_rejects_corrupt_persisted_state(
    tmp_path, update_sql: str, invalid_value: str, error_match: str
) -> None:
    storage = SQLiteStorage(str(tmp_path / "medre.db"))
    await storage.initialize()
    try:
        first = _event("evt-corrupt", "$corrupt-native")
        await storage.admit_ingress(
            first,
            _ref(first.event_id, "$corrupt-native"),
            "live",
        )
        await storage._write(
            update_sql,
            (invalid_value, first.event_id),
        )

        replay = _event("evt-redecoded", "$corrupt-native")
        with pytest.raises(StorageError, match=error_match):
            await storage.admit_ingress(
                replay,
                _ref(replay.event_id, "$corrupt-native"),
                "recovered",
            )
    finally:
        await storage.close()


async def test_explicit_suppression_persists_relations_without_routing(
    tmp_path,
) -> None:
    storage = SQLiteStorage(str(tmp_path / "medre.db"))
    await storage.initialize()
    try:
        event = _event("evt-suppressed", "$suppressed")
        relation = EventRelation(
            relation_type="reply",
            target_event_id=None,
            target_native_ref=NativeRef(
                adapter="matrix-main",
                native_channel_id="!room:example.org",
                native_message_id="$parent",
            ),
            key=None,
            fallback_text="parent",
        )
        event = msgspec.structs.replace(event, relations=(relation,))

        result = await storage.admit_ingress(
            event,
            _ref(event.event_id, "$suppressed"),
            "live",
            suppress_routing=True,
        )

        assert result.work_status == "suppressed_history"
        assert await storage.list_relations(event.event_id) == [relation]
    finally:
        await storage.close()


async def test_history_admission_is_durable_but_suppressed(tmp_path) -> None:
    storage = SQLiteStorage(str(tmp_path / "medre.db"))
    await storage.initialize()
    try:
        event = _event("evt-history", "$history")
        result = await storage.admit_ingress(
            event,
            _ref(event.event_id, "$history"),
            "history",
        )
        assert result.work_status == "suppressed_history"
        row = await storage._read_one(
            "SELECT status FROM durable_ingress_work WHERE event_id = ?",
            (event.event_id,),
        )
        assert row == {"status": "suppressed_history"}
    finally:
        await storage.close()


async def test_adapter_checkpoint_round_trips_and_updates(tmp_path) -> None:
    storage = SQLiteStorage(str(tmp_path / "medre.db"))
    await storage.initialize()
    try:
        await storage.put_adapter_checkpoint(
            "matrix-main", "classic_sync", "s1", metadata_json='{"abandoned":[]}'
        )
        await storage.put_adapter_checkpoint(
            "matrix-main", "classic_sync", "s2", metadata_json='{"abandoned":["!r"]}'
        )
        checkpoint = await storage.get_adapter_checkpoint("matrix-main", "classic_sync")
        assert checkpoint is not None
        assert checkpoint.cursor == "s2"
        assert checkpoint.metadata_json == '{"abandoned":["!r"]}'
    finally:
        await storage.close()


async def test_duplicate_legacy_ref_repairs_missing_durable_work(tmp_path) -> None:
    storage = SQLiteStorage(str(tmp_path / "medre.db"))
    await storage.initialize()
    try:
        event = _event("evt-legacy", "$legacy")
        await storage.append(event)
        await storage.store_native_ref(_ref(event.event_id, "$legacy"))

        replay = _event("evt-redecoded", "$legacy")
        result = await storage.admit_ingress(
            replay, _ref(replay.event_id, "$legacy"), "recovered"
        )

        assert result.created is False
        assert result.event_id == event.event_id
        assert result.work_status == "pending"
        assert await storage.count_ingress_work_by_status() == {"pending": 1}
    finally:
        await storage.close()


async def test_checkpoint_rejects_malformed_metadata_json(tmp_path) -> None:
    storage = SQLiteStorage(str(tmp_path / "medre.db"))
    await storage.initialize()
    try:
        import pytest

        with pytest.raises(ValueError, match="valid JSON"):
            await storage.put_adapter_checkpoint(
                "matrix-main", "classic_sync", "s1", metadata_json="{"
            )
        with pytest.raises(ValueError, match="JSON object"):
            await storage.put_adapter_checkpoint(
                "matrix-main", "classic_sync", "s1", metadata_json="[]"
            )
    finally:
        await storage.close()


async def test_work_lifecycle_renews_releases_reclaims_and_completes(tmp_path) -> None:
    storage = SQLiteStorage(str(tmp_path / "medre.db"))
    await storage.initialize()
    try:
        event = _event("evt-lifecycle", "$lifecycle")
        await storage.admit_ingress(event, _ref(event.event_id, "$lifecycle"), "live")

        [first] = await storage.claim_ingress_work(
            worker_id="worker-1", limit=1, lease_seconds=30
        )
        assert first.status == "processing"
        assert first.attempts == 1
        assert await storage.renew_ingress_work_lease(
            event.event_id, worker_id="worker-1", lease_seconds=60
        )
        assert not await storage.renew_ingress_work_lease(
            event.event_id, worker_id="other", lease_seconds=60
        )
        with pytest.raises(ValueError, match="lease_seconds must be positive"):
            await storage.renew_ingress_work_lease(
                event.event_id, worker_id="worker-1", lease_seconds=0
            )
        assert not await storage.complete_ingress_work(
            event.event_id, worker_id="other"
        )

        error = "x" * 1200
        assert await storage.release_ingress_work(
            event.event_id, worker_id="worker-1", error=error
        )
        row = await storage._read_one(
            "SELECT status, last_error FROM durable_ingress_work WHERE event_id=?",
            (event.event_id,),
        )
        assert row == {"status": "pending", "last_error": "x" * 1000}

        [second] = await storage.claim_ingress_work(
            worker_id="worker-2", limit=1, lease_seconds=30
        )
        assert second.attempts == 2
        assert await storage.complete_ingress_work(
            event.event_id, worker_id="worker-2"
        )
        assert await storage.count_ingress_work_by_status() == {"completed": 1}
    finally:
        await storage.close()


async def test_work_lifecycle_terminal_failure_and_lost_ownership(tmp_path) -> None:
    storage = SQLiteStorage(str(tmp_path / "medre.db"))
    await storage.initialize()
    try:
        event = _event("evt-failed", "$failed")
        await storage.admit_ingress(event, _ref(event.event_id, "$failed"), "recovered")
        await storage.claim_ingress_work(worker_id="worker-1", limit=1)

        assert not await storage.fail_ingress_work(
            event.event_id, worker_id="other", error="wrong owner"
        )
        assert await storage.fail_ingress_work(
            event.event_id, worker_id="worker-1", error="terminal"
        )
        assert not await storage.release_ingress_work(
            event.event_id, worker_id="worker-1", error="too late"
        )
        assert await storage.count_ingress_work_by_status() == {"failed": 1}
    finally:
        await storage.close()


async def test_missing_checkpoint_and_missing_existing_work_return_expected_results(
    tmp_path,
) -> None:
    storage = SQLiteStorage(str(tmp_path / "medre.db"))
    await storage.initialize()
    try:
        checkpoint = await storage.get_adapter_checkpoint("matrix-main", "classic_sync")
        assert checkpoint is None
        with pytest.raises(StorageError, match="durable ingress work missing"):
            await storage._admission_result_for_existing("evt-missing")
    finally:
        await storage.close()


async def test_aiosqlite_path_admits_deduplicates_claims_and_checkpoints() -> None:
    storage = _async_storage()
    try:
        first = _event("evt-async", "$async")
        created = await storage.admit_ingress(
            first, _ref(first.event_id, "$async"), "live"
        )
        replay = _event("evt-async-redecoded", "$async")
        duplicate = await storage.admit_ingress(
            replay, _ref(replay.event_id, "$async"), "recovered"
        )

        assert created.created is True
        assert duplicate.event_id == first.event_id
        assert duplicate.created is False

        await storage.put_adapter_checkpoint(
            "matrix-main", "classic_sync", "s1", metadata_json='{"ok":true}'
        )
        checkpoint = await storage.get_adapter_checkpoint(
            "matrix-main", "classic_sync"
        )
        assert checkpoint is not None and checkpoint.cursor == "s1"

        [item] = await storage.claim_ingress_work(
            worker_id="async-worker", limit=1, lease_seconds=30
        )
        assert item.event_id == first.event_id
        assert await storage.renew_ingress_work_lease(
            first.event_id, worker_id="async-worker", lease_seconds=60
        )
        assert await storage.release_ingress_work(
            first.event_id, worker_id="async-worker", error="retry"
        )
        [retried] = await storage.claim_ingress_work(
            worker_id="async-worker-2", limit=1, lease_seconds=30
        )
        assert retried.attempts == 2
        assert await storage.complete_ingress_work(
            first.event_id, worker_id="async-worker-2"
        )
        assert await storage.count_ingress_work_by_status() == {"completed": 1}
    finally:
        await storage.close()


async def test_aiosqlite_path_deduplicates_canonical_id_and_repairs_work() -> None:
    storage = _async_storage()
    try:
        event = make_storage_event(
            event_id="evt-async-canonical", source_adapter="matrix"
        )
        first = await storage.admit_ingress(event, None, "live")
        assert first.created is True
        await storage._write(
            "DELETE FROM durable_ingress_work WHERE event_id=?", (event.event_id,)
        )

        duplicate = await storage.admit_ingress(event, None, "recovered")

        assert duplicate.created is False
        assert duplicate.event_id == event.event_id
        assert duplicate.provenance == "recovered"
        assert duplicate.work_status == "pending"
    finally:
        await storage.close()


async def test_aiosqlite_path_rolls_back_admission_and_rowcount_errors() -> None:
    storage = _async_storage()
    try:
        first = _event("evt-async-first", "$first")
        await storage.admit_ingress(
            first, _ref(first.event_id, "$first"), "live"
        )
        second = _event("evt-async-second", "$second")
        colliding_ref = NativeMessageRef(
            id=f"nref-{first.event_id}",
            event_id=second.event_id,
            adapter="matrix-main",
            native_channel_id="!room:example.org",
            native_message_id="$second",
            native_thread_id=None,
            native_relation_id=None,
            direction="inbound",
            created_at=datetime.now(UTC),
        )

        with pytest.raises(StorageError, match="Durable ingress admission failed"):
            await storage.admit_ingress(second, colliding_ref, "live")
        assert await storage.get(second.event_id) is None

        with pytest.raises(StorageError, match="Database write failed"):
            await storage._write_rowcount("UPDATE missing_table SET value=1")
    finally:
        await storage.close()


async def test_aiosqlite_path_wraps_checkpoint_and_claim_driver_errors() -> None:
    checkpoint_storage = _async_storage()
    try:
        await checkpoint_storage._write("DROP TABLE adapter_checkpoints")
        with pytest.raises(StorageError, match="Checkpoint write failed"):
            await checkpoint_storage.put_adapter_checkpoint(
                "matrix-main", "classic_sync", "s1"
            )
    finally:
        await checkpoint_storage.close()

    claim_storage = _async_storage()
    try:
        await claim_storage._write("DROP TABLE durable_ingress_work")
        with pytest.raises(StorageError, match="Ingress work claim failed"):
            await claim_storage.claim_ingress_work(worker_id="worker", limit=1)
    finally:
        await claim_storage.close()


async def test_aiosqlite_admission_preserves_driver_error_when_rollback_fails() -> None:
    storage = _async_storage(fail_rollback=True)
    try:
        await storage._write("DROP TABLE canonical_events")
        event = _event("evt-async-driver-error", "$driver-error")

        with pytest.raises(StorageError, match="Durable ingress admission failed"):
            await storage.admit_ingress(
                event, _ref(event.event_id, "$driver-error"), "live"
            )
        with pytest.raises(StorageError, match="Database write failed"):
            await storage._write_rowcount("UPDATE missing_table SET value=1")
    finally:
        await storage.close()


async def test_aiosqlite_claim_preserves_driver_error_when_rollback_fails() -> None:
    storage = _async_storage(fail_rollback=True)
    try:
        await storage._write("DROP TABLE durable_ingress_work")
        with pytest.raises(StorageError, match="Ingress work claim failed"):
            await storage.claim_ingress_work(worker_id="worker", limit=1)
    finally:
        await storage.close()
