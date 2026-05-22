"""Agreement tests: evidence/trace agree on delivery reliability metadata.

Seeds a SQLite DB with env-style adapter IDs (radio-a → matrix-fake) and
asserts that the evidence bundle and trace timeline expose consistent
delivery metadata including route_id, target_adapter, attempt_number,
and native ref canonical keys.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from medre.core.events import (
    CanonicalEvent,
    DeliveryReceipt,
    EventMetadata,
    NativeMessageRef,
)
from medre.core.storage.sqlite import SQLiteStorage
from medre.runtime.evidence._storage_sections import _collect_storage_data_from_backend
from medre.runtime.trace import assemble_event_timeline as assemble_trace_entries

# ---------------------------------------------------------------------------
# Shared seed data
# ---------------------------------------------------------------------------

_EVENT_ID = "delivery-evt-001"
_SOURCE_ADAPTER = "radio-a"
_TARGET_ADAPTER = "matrix-fake"
_ROUTE_ID = "radio-to-matrix"
_NATIVE_CHANNEL_ID = "!room:matrix-fake"
_NATIVE_MESSAGE_ID = "native-msg-delivery-001"


def _make_event() -> CanonicalEvent:
    return CanonicalEvent(
        event_id=_EVENT_ID,
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime(2026, 5, 1, 10, 0, 0, tzinfo=timezone.utc),
        source_adapter=_SOURCE_ADAPTER,
        source_transport_id="meshtastic",
        source_channel_id="ch-radio",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": "delivery reliability test"},
        metadata=EventMetadata(),
    )


def _make_receipt(attempt_number: int = 1, status: str = "sent") -> DeliveryReceipt:
    return DeliveryReceipt(
        sequence=1,
        receipt_id=f"rcpt-delivery-{attempt_number:03d}",
        event_id=_EVENT_ID,
        delivery_plan_id="plan-delivery-001",
        target_adapter=_TARGET_ADAPTER,
        route_id=_ROUTE_ID,
        status=status,
        adapter_message_id=_NATIVE_MESSAGE_ID,
        attempt_number=attempt_number,
        source="live",
        created_at=datetime(2026, 5, 1, 10, 0, 1, tzinfo=timezone.utc),
    )


def _make_native_ref() -> NativeMessageRef:
    return NativeMessageRef(
        id="nref-delivery-001",
        event_id=_EVENT_ID,
        adapter=_TARGET_ADAPTER,
        native_channel_id=_NATIVE_CHANNEL_ID,
        native_message_id=_NATIVE_MESSAGE_ID,
        native_thread_id=None,
        native_relation_id=None,
        direction="outbound",
        created_at=datetime(2026, 5, 1, 10, 0, 0, tzinfo=timezone.utc),
    )


async def _seed(storage: SQLiteStorage) -> None:
    """Write one event + one receipt + one outbound native ref."""
    await storage.append(_make_event())
    await storage.append_receipt(_make_receipt())
    await storage.store_native_ref(_make_native_ref())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evidence_receipt_has_route_id(tmp_path: Path) -> None:
    """Evidence storage section includes receipt with route_id and target metadata."""
    db_path = str(tmp_path / "delivery_evidence.db")
    storage = SQLiteStorage(db_path)
    await storage.initialize()
    try:
        await _seed(storage)

        section = await _collect_storage_data_from_backend(
            storage,
            db_path=db_path,
            event_id=_EVENT_ID,
            replay_run_id=None,
        )
        assert section["status"] == "passed", f"Unexpected section status: {section}"

        # Receipt count >= 1
        assert section["data"]["receipt_count"] >= 1, (
            f"Expected receipt_count >= 1, got {section['data']['receipt_count']}"
        )

        # Timeline entries contain receipts with the expected metadata.
        timeline = section["data"]["timeline"]
        assert timeline is not None, "Timeline should not be None"
        receipt_entries = [e for e in timeline if e["entry_type"] == "receipt"]
        assert len(receipt_entries) >= 1, "Expected at least one receipt entry in timeline"
        receipt_data = receipt_entries[0]["data"]

        assert receipt_data["route_id"] == _ROUTE_ID, (
            f"Expected route_id={_ROUTE_ID!r}, got {receipt_data.get('route_id')!r}"
        )
        assert receipt_data["target_adapter"] == _TARGET_ADAPTER, (
            f"Expected target_adapter={_TARGET_ADAPTER!r}, got {receipt_data.get('target_adapter')!r}"
        )
        assert receipt_data["event_id"] == _EVENT_ID, (
            f"Expected event_id={_EVENT_ID!r}, got {receipt_data.get('event_id')!r}"
        )
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_trace_includes_route_id_and_target(tmp_path: Path) -> None:
    """Trace timeline includes receipt with route_id and target_adapter."""
    event = _make_event()
    receipt = _make_receipt()
    nref = _make_native_ref()

    trace_entries = assemble_trace_entries(event, [receipt], [nref], [])

    entry_types = {e["entry_type"] for e in trace_entries}
    assert "event" in entry_types, "Expected 'event' entry type in trace"
    assert "receipt" in entry_types, "Expected 'receipt' entry type in trace"
    assert "native_ref" in entry_types, "Expected 'native_ref' entry type in trace"

    receipt_entries = [e for e in trace_entries if e["entry_type"] == "receipt"]
    assert len(receipt_entries) == 1, "Expected exactly one receipt entry"
    receipt_data = receipt_entries[0]["data"]

    assert receipt_data["route_id"] == _ROUTE_ID, (
        f"Expected route_id={_ROUTE_ID!r}, got {receipt_data.get('route_id')!r}"
    )
    assert receipt_data["target_adapter"] == _TARGET_ADAPTER, (
        f"Expected target_adapter={_TARGET_ADAPTER!r}, got {receipt_data.get('target_adapter')!r}"
    )
    assert receipt_data["event_id"] == _EVENT_ID, (
        f"Expected event_id={_EVENT_ID!r}, got {receipt_data.get('event_id')!r}"
    )


@pytest.mark.asyncio
async def test_evidence_and_trace_agree_on_delivery_metadata(tmp_path: Path) -> None:
    """Evidence storage section and trace timeline agree on all delivery metadata."""
    db_path = str(tmp_path / "delivery_agree.db")
    storage = SQLiteStorage(db_path)
    await storage.initialize()
    try:
        await _seed(storage)

        # -- Evidence (via storage) --
        section = await _collect_storage_data_from_backend(
            storage,
            db_path=db_path,
            event_id=_EVENT_ID,
            replay_run_id=None,
        )
        assert section["status"] == "passed"
        ev_timeline = section["data"]["timeline"]
        assert ev_timeline is not None

        ev_receipt = [e for e in ev_timeline if e["entry_type"] == "receipt"]
        assert len(ev_receipt) >= 1
        ev_receipt_data = ev_receipt[0]["data"]

        ev_nref = [e for e in ev_timeline if e["entry_type"] == "native_ref"]
        assert len(ev_nref) >= 1
        ev_nref_data = ev_nref[0]["data"]

        # -- Trace (pure function) --
        trace_entries = assemble_trace_entries(
            _make_event(), [_make_receipt()], [_make_native_ref()], []
        )
        tr_receipt = [e for e in trace_entries if e["entry_type"] == "receipt"]
        assert len(tr_receipt) >= 1
        tr_receipt_data = tr_receipt[0]["data"]

        tr_nref = [e for e in trace_entries if e["entry_type"] == "native_ref"]
        assert len(tr_nref) >= 1
        tr_nref_data = tr_nref[0]["data"]

        # Both reference same event_id
        assert ev_receipt_data["event_id"] == tr_receipt_data["event_id"] == _EVENT_ID

        # Both reference same target_adapter
        assert ev_receipt_data["target_adapter"] == tr_receipt_data["target_adapter"] == _TARGET_ADAPTER

        # Both reference same route_id
        assert ev_receipt_data["route_id"] == tr_receipt_data["route_id"] == _ROUTE_ID

        # Native ref canonical keys agree across evidence and trace
        for key in ("adapter", "native_channel_id", "native_message_id", "direction"):
            assert ev_nref_data.get(key) == tr_nref_data.get(key), (
                f"Native ref key {key!r} mismatch: "
                f"evidence={ev_nref_data.get(key)!r} trace={tr_nref_data.get(key)!r}"
            )
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_receipt_attempt_number_in_both(tmp_path: Path) -> None:
    """Evidence and trace both expose attempt_number and agree on its value."""
    attempt = 3
    receipt = _make_receipt(attempt_number=attempt, status="sent")
    nref = _make_native_ref()

    # -- Trace (pure function) --
    trace_entries = assemble_trace_entries(_make_event(), [receipt], [nref], [])
    tr_receipt = [e for e in trace_entries if e["entry_type"] == "receipt"]
    assert len(tr_receipt) == 1
    tr_data = tr_receipt[0]["data"]

    assert "attempt_number" in tr_data, "attempt_number missing from trace receipt"
    assert tr_data["attempt_number"] == attempt, (
        f"Trace attempt_number: expected {attempt}, got {tr_data['attempt_number']}"
    )

    # -- Evidence (via storage) --
    db_path = str(tmp_path / "delivery_attempt.db")
    storage = SQLiteStorage(db_path)
    await storage.initialize()
    try:
        await storage.append(_make_event())
        await storage.append_receipt(receipt)
        await storage.store_native_ref(nref)

        section = await _collect_storage_data_from_backend(
            storage,
            db_path=db_path,
            event_id=_EVENT_ID,
            replay_run_id=None,
        )
        assert section["status"] == "passed"
        ev_timeline = section["data"]["timeline"]
        assert ev_timeline is not None

        ev_receipt = [e for e in ev_timeline if e["entry_type"] == "receipt"]
        assert len(ev_receipt) >= 1
        ev_data = ev_receipt[0]["data"]

        assert "attempt_number" in ev_data, "attempt_number missing from evidence receipt"
        assert ev_data["attempt_number"] == attempt, (
            f"Evidence attempt_number: expected {attempt}, got {ev_data['attempt_number']}"
        )

        # Both agree on the value
        assert ev_data["attempt_number"] == tr_data["attempt_number"], (
            f"Evidence/trace disagree on attempt_number: "
            f"evidence={ev_data['attempt_number']}, trace={tr_data['attempt_number']}"
        )
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_native_ref_canonical_keys_in_evidence_and_trace(tmp_path: Path) -> None:
    """Native refs in evidence and trace both include all canonical keys."""
    event = _make_event()
    nref = _make_native_ref()
    receipt = _make_receipt()

    canonical_keys = ("adapter", "native_channel_id", "native_message_id", "direction")

    # -- Trace --
    trace_entries = assemble_trace_entries(event, [receipt], [nref], [])
    tr_nref = [e for e in trace_entries if e["entry_type"] == "native_ref"]
    assert len(tr_nref) == 1, "Expected exactly one native_ref in trace"
    tr_data = tr_nref[0]["data"]

    for key in canonical_keys:
        assert key in tr_data, f"Trace native_ref missing canonical key {key!r}"

    # -- Evidence --
    db_path = str(tmp_path / "delivery_nref_keys.db")
    storage = SQLiteStorage(db_path)
    await storage.initialize()
    try:
        await storage.append(event)
        await storage.append_receipt(receipt)
        await storage.store_native_ref(nref)

        section = await _collect_storage_data_from_backend(
            storage,
            db_path=db_path,
            event_id=_EVENT_ID,
            replay_run_id=None,
        )
        assert section["status"] == "passed"
        ev_timeline = section["data"]["timeline"]
        assert ev_timeline is not None

        ev_nref = [e for e in ev_timeline if e["entry_type"] == "native_ref"]
        assert len(ev_nref) >= 1, "Expected at least one native_ref in evidence timeline"
        ev_data = ev_nref[0]["data"]

        for key in canonical_keys:
            assert key in ev_data, f"Evidence native_ref missing canonical key {key!r}"

        # Values agree
        for key in canonical_keys:
            assert tr_data[key] == ev_data[key], (
                f"Native ref key {key!r} disagreement: "
                f"trace={tr_data[key]!r} evidence={ev_data[key]!r}"
            )
    finally:
        await storage.close()
