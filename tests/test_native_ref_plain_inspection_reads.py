"""Metadata-only native-reference reads keep the identity filters and order.

``_GET_NATIVE_REF_PLAIN`` and ``_SELECT_NREFS_FOR_EVENT_PLAIN`` carry the
same null-safe identity filters and ordering as the ordinary reads: a
metadata-only lookup for ``(adapter, None, message)`` finds the NULL-channel
mapping, and an event's two references list in stored order with masked
metadata and unchanged ordinary output.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from medre.core.events import NativeMessageRef
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage import make_storage_event


def _ref(
    ref_id: str,
    event_id: str,
    native_message_id: str,
    *,
    created_at: datetime,
) -> NativeMessageRef:
    return NativeMessageRef(
        id=ref_id,
        event_id=event_id,
        adapter="radio",
        native_channel_id=None,
        native_message_id=native_message_id,
        native_thread_id=None,
        native_relation_id=None,
        direction="inbound",
        metadata={"marker": native_message_id},
        created_at=created_at,
    )


async def test_metadata_only_native_ref_reads_keep_identity_and_order(
    tmp_path: Path,
) -> None:
    storage = SQLiteStorage(str(tmp_path / "medre.db"))
    await storage.initialize()
    try:
        event = make_storage_event(event_id="evt-1", source_adapter="radio")
        await storage.admit_ingress(event, None, "live")

        base = datetime.now(UTC)
        await storage.store_native_ref(
            _ref("nref-a", "evt-1", "$native-a", created_at=base)
        )
        await storage.store_native_ref(
            _ref(
                "nref-b",
                "evt-1",
                "$native-b",
                created_at=base + timedelta(seconds=1),
            )
        )

        single = await storage.get_native_ref("radio", None, "$native-a")
        assert single is not None
        assert single.id == "nref-a"
        assert single.event_id == "evt-1"

        listed = await storage.list_native_refs_for_event("evt-1")
        assert [ref.id for ref in listed] == ["nref-a", "nref-b"]

        plain_single = await storage.get_native_ref(
            "radio", None, "$native-a", metadata_only=True
        )
        assert plain_single is not None
        assert plain_single.id == "nref-a"
        assert plain_single.event_id == "evt-1"
        assert plain_single.native_channel_id is None
        assert plain_single.metadata == {}

        plain_listed = await storage.list_native_refs_for_event(
            "evt-1", metadata_only=True
        )
        assert [ref.id for ref in plain_listed] == ["nref-a", "nref-b"]
        assert all(ref.metadata == {} for ref in plain_listed)
    finally:
        await storage.close()
