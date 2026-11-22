"""Channel-less native identity deduplication at ingress admission.

A native reference without a channel (``native_channel_id=None``) must use
the same null-safe identity tuple for the admission pre-check and for the
stored row, so re-admitting the same native message under a different
canonical event ID returns the first event and creates no second event or
work row.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from medre.core.events import NativeMessageRef
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage import make_storage_event


def _ref(event_id: str, native_message_id: str) -> NativeMessageRef:
    return NativeMessageRef(
        id=f"nref-{event_id}",
        event_id=event_id,
        adapter="radio",
        native_channel_id=None,
        native_message_id=native_message_id,
        native_thread_id=None,
        native_relation_id=None,
        direction="inbound",
        created_at=datetime.now(UTC),
    )


async def test_channelless_native_identity_admits_once(tmp_path: Path) -> None:
    storage = SQLiteStorage(str(tmp_path / "medre.db"))
    await storage.initialize()
    try:
        first = make_storage_event(event_id="evt-first", source_adapter="radio")
        result = await storage.admit_ingress(
            first, _ref("evt-first", "$native-one"), "live"
        )
        assert result.created is True

        second = make_storage_event(event_id="evt-second", source_adapter="radio")
        duplicate = await storage.admit_ingress(
            second, _ref("evt-second", "$native-one"), "live"
        )

        assert duplicate.created is False
        assert duplicate.event_id == "evt-first"
        assert await storage.get("evt-second") is None

        row = await storage._read_one(
            "SELECT event_id, native_channel_id FROM native_message_refs"
            " WHERE adapter = 'radio' AND native_message_id = '$native-one'"
        )
        assert row is not None
        assert row["event_id"] == "evt-first"
        assert row["native_channel_id"] is None

        work_rows = await storage._read_all(
            "SELECT event_id FROM durable_ingress_work ORDER BY event_id"
        )
        assert [str(item["event_id"]) for item in work_rows] == ["evt-first"]

        assert (
            await storage.resolve_native_ref("radio", None, "$native-one")
            == "evt-first"
        )
    finally:
        await storage.close()
