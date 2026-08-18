"""Atomic durable-ingress storage contract tests."""

from __future__ import annotations

from datetime import datetime, timezone

import msgspec
import pytest

from medre.core.events import NativeMessageRef, NativeRef
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage import make_storage_event


def _event(event_id: str, native_id: str):
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
        created_at=datetime.now(timezone.utc),
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
        with pytest.raises(ValueError, match="inbound_ref.event_id"):
            await storage.admit_ingress(
                event, _ref("evt-other", "$mismatch"), "live"
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
        checkpoint = await storage.get_adapter_checkpoint(
            "matrix-main", "classic_sync"
        )
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
