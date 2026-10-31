"""Campaign evidence is a privileged, read-only inspection of its fixture DB."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import msgspec
import pytest

from medre.core.events import EventRelation
from medre.core.storage.sqlite.storage import SQLiteStorage
from scripts.adapter_campaign.evidence import snapshots
from tests.helpers.storage import make_storage_event


async def test_campaign_snapshots_preserve_body_attachment_and_relation(tmp_path):
    path = str(tmp_path / "campaign.db")
    writer = SQLiteStorage(path)
    await writer.initialize()
    reader = None
    try:
        event = make_storage_event(
            event_id="first",
            payload={
                "body": "nonce-test body",
                "text": "a different text",
                "attachment": {"filename": "photo.jpg", "size_bytes": 3},
            },
            relations=(
                EventRelation("reply", "external-parent", None, None, "quoted source"),
            ),
        )
        await writer.append(event)
        await writer.append(
            make_storage_event(
                event_id="wrong-field",
                payload={"body": "different body", "text": "nonce-test text"},
            )
        )
        await writer.append(
            make_storage_event(
                event_id="text-only", payload={"text": "nonce-test text"}
            )
        )
        reader = await SQLiteStorage.open_readonly(path)
        # The script must never borrow content through the write-transaction
        # replay API, even if that API remains available on the storage type.
        reader.read_event_for_replay = AsyncMock(
            side_effect=AssertionError("replay API takes a write lock")
        )
        matched = await snapshots(reader, "nonce-test")
        assert [r["event"]["event_id"] for r in matched] == ["first", "text-only"]
        assert matched[0]["event"]["payload"] == msgspec.to_builtins(event.payload)
        assert matched[0]["event"]["relations"][0]["fallback_text"] == "quoted source"
        assert matched[1]["event"]["payload"]["text"] == "nonce-test text"
        reader.read_event_for_replay.assert_not_called()
    finally:
        if reader is not None:
            await reader.close()
        await writer.close()


async def test_campaign_evidence_polling_does_not_compete_with_runtime_writer(tmp_path):
    path = str(tmp_path / "campaign-locked.db")
    writer = SQLiteStorage(path)
    await writer.initialize()
    reader = None
    try:
        await writer.append(
            make_storage_event(event_id="locked", payload={"body": "nonce-lock"})
        )
        reader = await SQLiteStorage.open_readonly(path)
        # In WAL mode this connection must be able to read while another
        # connection holds a RESERVED write lock. The old replay borrow used
        # BEGIN IMMEDIATE, which would wait and eventually fail SQLITE_BUSY.
        await writer._run_in_thread(writer._require_db().execute, "BEGIN IMMEDIATE")
        try:
            matches = await asyncio.wait_for(snapshots(reader, "nonce-lock"), 1.5)
            assert [r["event"]["event_id"] for r in matches] == ["locked"]
        finally:
            await writer._run_in_thread(writer._require_db().rollback)
    finally:
        if reader is not None:
            await reader.close()
        await writer.close()


@pytest.mark.parametrize("body", [None, 42, {}, ""])
async def test_campaign_secondary_text_cannot_override_an_explicit_body(tmp_path, body):
    storage = SQLiteStorage(str(tmp_path / "body-precedence.db"))
    await storage.initialize()
    reader = None
    try:
        await storage.append(
            make_storage_event(
                event_id="explicit-body", payload={"body": body, "text": "nonce-alias"}
            )
        )
        reader = await SQLiteStorage.open_readonly(storage._db_path)
        assert await snapshots(reader, "nonce-alias") == []
    finally:
        if reader is not None:
            await reader.close()
        await storage.close()


@pytest.mark.parametrize("count", [2, 3, 4])
async def test_campaign_bounds_the_enumerated_events_before_hydration(
    tmp_path, monkeypatch, count
):
    from scripts.adapter_campaign import evidence

    monkeypatch.setattr(evidence, "_EVENT_WINDOW", 3)
    storage = SQLiteStorage(str(tmp_path / "bounded.db"))
    await storage.initialize()
    reader = None
    try:
        for ordinal in range(count):
            await storage.append(
                make_storage_event(
                    event_id=f"event-{ordinal}", payload={"body": "unrelated"}
                )
            )
        reader = await SQLiteStorage.open_readonly(storage._db_path)
        if count < 3:
            assert await snapshots(reader, "nonce-unmatched") == []
        else:
            with pytest.raises(AssertionError, match="bounded event window"):
                await snapshots(reader, "nonce-unmatched")
    finally:
        if reader is not None:
            await reader.close()
        await storage.close()
