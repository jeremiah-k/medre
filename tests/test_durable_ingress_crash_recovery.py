"""Crash-window characterization for durable ingress ownership."""

from __future__ import annotations

from datetime import datetime, timezone

import msgspec

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


def _ref(event_id: str, native_id: str, *, ref_id: str | None = None):
    return NativeMessageRef(
        id=ref_id or f"nref-{event_id}",
        event_id=event_id,
        adapter="matrix-main",
        native_channel_id="!room:example.org",
        native_message_id=native_id,
        native_thread_id=None,
        native_relation_id=None,
        direction="inbound",
        created_at=datetime.now(timezone.utc),
    )


async def test_failed_atomic_admission_rolls_back_canonical_event(tmp_path) -> None:
    storage = SQLiteStorage(str(tmp_path / "medre.db"))
    await storage.initialize()
    try:
        first = _event("evt-first", "$first")
        await storage.admit_ingress(first, _ref(first.event_id, "$first"), "live")

        second = _event("evt-second", "$second")
        try:
            await storage.admit_ingress(
                second,
                _ref(second.event_id, "$second", ref_id="nref-evt-first"),
                "live",
            )
        except Exception:
            pass
        else:
            raise AssertionError("expected native-ref primary-key collision")

        assert await storage.get(second.event_id) is None
        assert (
            await storage.resolve_native_ref(
                "matrix-main", "!room:example.org", "$second"
            )
            is None
        )
        assert await storage._read_one(
            "SELECT event_id FROM durable_ingress_work WHERE event_id = ?",
            (second.event_id,),
        ) is None
    finally:
        await storage.close()


async def test_admitted_pending_work_survives_process_restart(tmp_path) -> None:
    path = str(tmp_path / "medre.db")
    storage = SQLiteStorage(path)
    await storage.initialize()
    event = _event("evt-restart", "$restart")
    await storage.admit_ingress(event, _ref(event.event_id, "$restart"), "recovered")
    await storage.put_adapter_checkpoint("matrix-main", "classic_sync", "s20")
    await storage.close()

    reopened = SQLiteStorage(path)
    await reopened.initialize()
    try:
        assert await reopened.get(event.event_id) is not None
        assert (
            await reopened.resolve_native_ref(
                "matrix-main", "!room:example.org", "$restart"
            )
            == event.event_id
        )
        assert await reopened.count_ingress_work_by_status() == {"pending": 1}
        checkpoint = await reopened.get_adapter_checkpoint(
            "matrix-main", "classic_sync"
        )
        assert checkpoint is not None
        assert checkpoint.cursor == "s20"
    finally:
        await reopened.close()


async def test_expired_processing_lease_is_reclaimed_after_worker_crash(tmp_path) -> None:
    path = str(tmp_path / "medre.db")
    storage = SQLiteStorage(path)
    await storage.initialize()
    event = _event("evt-lease", "$lease")
    await storage.admit_ingress(event, _ref(event.event_id, "$lease"), "live")
    first_claim = await storage.claim_ingress_work(
        worker_id="worker-a", limit=1, lease_seconds=30
    )
    assert first_claim[0].attempts == 1
    await storage._write(
        "UPDATE durable_ingress_work SET lease_until=? WHERE event_id=?",
        ("2000-01-01T00:00:00+00:00", event.event_id),
    )
    await storage.close()

    reopened = SQLiteStorage(path)
    await reopened.initialize()
    try:
        second_claim = await reopened.claim_ingress_work(
            worker_id="worker-b", limit=1, lease_seconds=30
        )
        assert len(second_claim) == 1
        assert second_claim[0].event_id == event.event_id
        assert second_claim[0].attempts == 2
        assert second_claim[0].worker_id == "worker-b"
    finally:
        await reopened.close()


async def test_redecoded_native_event_after_restart_keeps_original_identity(tmp_path) -> None:
    path = str(tmp_path / "medre.db")
    storage = SQLiteStorage(path)
    await storage.initialize()
    original = _event("evt-original", "$same")
    await storage.admit_ingress(
        original, _ref(original.event_id, "$same"), "recovered"
    )
    await storage.close()

    reopened = SQLiteStorage(path)
    await reopened.initialize()
    try:
        replay = _event("evt-new-uuid", "$same")
        result = await reopened.admit_ingress(
            replay, _ref(replay.event_id, "$same"), "recovered"
        )
        assert result.created is False
        assert result.event_id == original.event_id
        assert await reopened.get(replay.event_id) is None
        assert await reopened.count_ingress_work_by_status() == {"pending": 1}
    finally:
        await reopened.close()
