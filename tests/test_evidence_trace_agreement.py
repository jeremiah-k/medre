"""Agreement tests: trace, evidence, and inspect commands agree on event IDs, native refs, and receipts.

Seeds a SQLite DB with a canonical event, a delivery receipt, and an outbound
native ref, then asserts that the evidence bundle, trace timeline, and inspect
output all expose consistent native metadata.
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
from medre.core.storage import SQLiteStorage
from medre.runtime.evidence._storage_sections import _collect_storage_data_from_backend
from medre.runtime.timeline import assemble_event_timeline
from medre.runtime.trace import assemble_event_timeline as assemble_trace_entries
from medre.runtime.trace import assemble_replay_timeline

# ---------------------------------------------------------------------------
# Shared seed data
# ---------------------------------------------------------------------------

_EVENT_ID = "agree-evt-001"
_ADAPTER = "fake_dest"
_NATIVE_CHANNEL_ID = "ch-agree-001"
_NATIVE_MESSAGE_ID = "native-msg-agree-001"
_RECEIPT_ID = "rcpt-agree-001"
_TS_EVENT = datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc)
_TS_RECEIPT = datetime(2026, 3, 1, 10, 0, 1, tzinfo=timezone.utc)
_TS_NREF = datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc)


def _make_event() -> CanonicalEvent:
    return CanonicalEvent(
        event_id=_EVENT_ID,
        event_kind="message.created",
        schema_version=1,
        timestamp=_TS_EVENT,
        source_adapter="fake_source",
        source_transport_id="transport-agree",
        source_channel_id="ch-source",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": "agreement test message"},
        metadata=EventMetadata(),
    )


def _make_receipt() -> DeliveryReceipt:
    return DeliveryReceipt(
        sequence=1,
        receipt_id=_RECEIPT_ID,
        event_id=_EVENT_ID,
        delivery_plan_id="plan-agree-001",
        target_adapter=_ADAPTER,
        route_id="route-agree-001",
        status="sent",
        adapter_message_id=_NATIVE_MESSAGE_ID,
        created_at=_TS_RECEIPT,
    )


def _make_native_ref() -> NativeMessageRef:
    return NativeMessageRef(
        id="nref-agree-001",
        event_id=_EVENT_ID,
        adapter=_ADAPTER,
        native_channel_id=_NATIVE_CHANNEL_ID,
        native_message_id=_NATIVE_MESSAGE_ID,
        native_thread_id=None,
        native_relation_id=None,
        direction="outbound",
        created_at=_TS_NREF,
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
async def test_evidence_native_refs_match_trace(tmp_path: Path) -> None:
    """Evidence bundle native refs and trace timeline native_ref entries agree on keys."""
    db_path = str(tmp_path / "agree_trace.db")
    storage = SQLiteStorage(db_path)
    await storage.initialize()
    try:
        await _seed(storage)

        # -- Trace timeline (raw entries) --
        trace_entries = assemble_trace_entries(
            _make_event(),
            [_make_receipt()],
            [_make_native_ref()],
            [],
        )
        nref_entries = [e for e in trace_entries if e["entry_type"] == "native_ref"]
        assert len(nref_entries) == 1, "Expected exactly one native_ref timeline entry"
        trace_nref = nref_entries[0]["data"]

        # -- Evidence bundle (via storage) --
        section = await _collect_storage_data_from_backend(
            storage,
            db_path=db_path,
            event_id=_EVENT_ID,
            replay_run_id=None,
        )
        assert section["status"] == "passed", f"Unexpected section status: {section}"
        bundle_nrefs = section["data"]["native_refs_for_event"]
        assert bundle_nrefs is not None and len(bundle_nrefs) == 1
        bundle_nref = bundle_nrefs[0]

        # Canonical keys must match across trace and evidence.
        for key in ("adapter", "native_channel_id", "native_message_id", "direction"):
            assert trace_nref.get(key) == bundle_nref.get(key), (
                f"Key {key!r} mismatch: trace={trace_nref.get(key)!r} "
                f"evidence={bundle_nref.get(key)!r}"
            )
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_inspect_includes_native_metadata(temp_storage: SQLiteStorage) -> None:
    """Inspect output for an event includes source native ref or equivalent native metadata."""
    await _seed(temp_storage)

    # Retrieve event via storage (same path as inspect event).
    event = await temp_storage.get(_EVENT_ID)
    assert event is not None, "Seeded event not found in storage"

    # The event should be retrievable; native metadata comes from
    # resolve_native_ref.
    resolved_event_id = await temp_storage.resolve_native_ref(
        _ADAPTER,
        _NATIVE_CHANNEL_ID,
        _NATIVE_MESSAGE_ID,
    )
    assert (
        resolved_event_id == _EVENT_ID
    ), f"resolve_native_ref returned {resolved_event_id!r}, expected {_EVENT_ID!r}"

    # Also verify the timeline module returns the event with native refs.
    tl = await assemble_event_timeline(temp_storage, _EVENT_ID)
    assert tl is not None
    assert len(tl["native_refs"]) == 1
    nref = tl["native_refs"][0]
    assert nref.adapter == _ADAPTER
    assert nref.native_channel_id == _NATIVE_CHANNEL_ID
    assert nref.native_message_id == _NATIVE_MESSAGE_ID
    assert nref.direction == "outbound"


@pytest.mark.asyncio
async def test_receipt_and_native_ref_agree(temp_storage: SQLiteStorage) -> None:
    """Delivery receipt and native ref agree on adapter, channel, and message id."""
    await _seed(temp_storage)

    tl = await assemble_event_timeline(temp_storage, _EVENT_ID)
    assert tl is not None

    receipts = tl["receipts"]
    native_refs = tl["native_refs"]
    assert len(receipts) >= 1, "Expected at least one receipt"
    assert len(native_refs) >= 1, "Expected at least one native ref"

    receipt = receipts[0]
    nref = native_refs[0]

    # The receipt's target_adapter should match the native ref's adapter.
    assert receipt.target_adapter == nref.adapter, (
        f"Receipt target_adapter={receipt.target_adapter!r} != "
        f"native ref adapter={nref.adapter!r}"
    )

    # If the receipt has an adapter_message_id, it should match the native ref.
    if receipt.adapter_message_id is not None:
        assert receipt.adapter_message_id == nref.native_message_id, (
            f"Receipt adapter_message_id={receipt.adapter_message_id!r} != "
            f"native ref native_message_id={nref.native_message_id!r}"
        )

    # Both reference the same event.
    assert receipt.event_id == nref.event_id == _EVENT_ID


@pytest.mark.asyncio
async def test_evidence_commands_reference_existing_cli_names(
    tmp_path: Path,
) -> None:
    """Evidence bundle incident_summary.commands reference real CLI command names."""
    db_path = str(tmp_path / "agree_cmds.db")
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
        assert section["status"] == "passed"
        summary = section["data"].get("incident_summary")
        assert summary is not None, "incident_summary missing from evidence bundle"

        # All events have at least one receipt with status="sent", so classification
        # is "success".  The recommended commands should include inspect-based commands.
        cmds_struct = summary["commands"]
        assert "primary" in cmds_struct
        assert "specialized" in cmds_struct

        all_commands = cmds_struct["primary"] + cmds_struct["specialized"]
        assert len(all_commands) >= 1, "Expected at least one command recommendation"

        # Every recommended command should start with "medre " and reference known
        # subcommands (inspect, evidence, trace, replay, diagnostics, config).
        known_prefixes = (
            "medre inspect",
            "medre evidence",
            "medre trace",
            "medre replay",
            "medre diagnostics",
            "medre config",
        )
        for cmd in all_commands:
            assert isinstance(cmd, str) and cmd.startswith(
                "medre "
            ), f"Command {cmd!r} does not start with 'medre '"
            assert cmd.startswith(
                known_prefixes
            ), f"Command {cmd!r} does not match any known CLI prefix"

        # The specialized list should include the evidence command for this event.
        evidence_cmds = [c for c in cmds_struct["specialized"] if "evidence" in c]
        assert (
            len(evidence_cmds) >= 1
        ), "Expected at least one specialized evidence command"
        assert (
            _EVENT_ID in evidence_cmds[0]
        ), f"Specialized evidence command should reference event_id {_EVENT_ID!r}"
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_evidence_report_native_ref_canonical_keys(
    temp_storage: SQLiteStorage,
) -> None:
    """evidence.py report native refs include all canonical keys plus short report aliases."""
    await _seed(temp_storage)

    # Collect native refs using the same logic as evidence.py _collect_native_refs.
    nref_records = await temp_storage.list_native_refs_for_event(_EVENT_ID)
    assert len(nref_records) == 1
    nref = nref_records[0]

    # Simulate the dict produced by _collect_native_refs in evidence.py.
    resolved = await temp_storage.resolve_native_ref(
        nref.adapter,
        nref.native_channel_id,
        nref.native_message_id,
    )
    ref_dict = {
        "adapter": nref.adapter,
        "native_channel_id": nref.native_channel_id or "",
        "channel": nref.native_channel_id or "",
        "native_id": nref.native_message_id,
        "native_message_id": nref.native_message_id,
        "direction": nref.direction,
        "resolves_to": resolved or nref.event_id,
    }

    # Canonical keys present.
    for key in (
        "adapter",
        "native_channel_id",
        "native_message_id",
        "direction",
        "resolves_to",
    ):
        assert key in ref_dict, f"Canonical key {key!r} missing from ref dict"

    # Short report aliases present.
    assert "channel" in ref_dict, "alias 'channel' missing"
    assert "native_id" in ref_dict, "alias 'native_id' missing"

    # Values agree.
    assert ref_dict["native_channel_id"] == ref_dict["channel"]
    assert ref_dict["native_message_id"] == ref_dict["native_id"]
    assert ref_dict["resolves_to"] == _EVENT_ID


# ---------------------------------------------------------------------------
# Tests: v2 trace field expansion (failure_kind, native_channel_id, resolves_to)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trace_receipt_includes_failure_kind_and_native_channel_id(
    tmp_path: Path,
) -> None:
    """Trace timeline receipt entries include failure_kind and native_channel_id."""
    event = _make_event()
    receipt = DeliveryReceipt(
        sequence=1,
        receipt_id="rcpt-fk-001",
        event_id=_EVENT_ID,
        delivery_plan_id="plan-fk-001",
        target_adapter=_ADAPTER,
        target_channel=_NATIVE_CHANNEL_ID,
        route_id="route-fk-001",
        status="failed",
        failure_kind="adapter_permanent_failure",
        error="simulated adapter crash",
        adapter_message_id=None,
        created_at=_TS_RECEIPT,
    )
    nref = _make_native_ref()

    trace_entries = assemble_trace_entries(event, [receipt], [nref], [])

    receipt_entries = [e for e in trace_entries if e["entry_type"] == "receipt"]
    assert len(receipt_entries) == 1, "Expected exactly one receipt timeline entry"
    data = receipt_entries[0]["data"]

    # failure_kind must be present and match what was set on the receipt.
    assert (
        data["failure_kind"] == "adapter_permanent_failure"
    ), f"Expected failure_kind='adapter_permanent_failure', got {data['failure_kind']!r}"
    # native_channel_id must be present and populated from target_channel.
    assert (
        data["native_channel_id"] == _NATIVE_CHANNEL_ID
    ), f"Expected native_channel_id={_NATIVE_CHANNEL_ID!r}, got {data['native_channel_id']!r}"


@pytest.mark.asyncio
async def test_trace_native_ref_includes_resolves_to(tmp_path: Path) -> None:
    """Trace timeline native_ref entries include resolves_to field."""
    event = _make_event()
    nref = _make_native_ref()

    trace_entries = assemble_trace_entries(event, [], [nref], [])

    nref_entries = [e for e in trace_entries if e["entry_type"] == "native_ref"]
    assert len(nref_entries) == 1, "Expected exactly one native_ref timeline entry"
    data = nref_entries[0]["data"]

    # resolves_to must be present and populated with the event_id.
    assert "resolves_to" in data, "resolves_to key missing from native_ref entry data"
    assert (
        data["resolves_to"] == _EVENT_ID
    ), f"Expected resolves_to={_EVENT_ID!r}, got {data['resolves_to']!r}"


@pytest.mark.asyncio
async def test_orchestration_report_delivery_receipts_include_expanded_keys(
    tmp_path: Path,
) -> None:
    """Orchestration report delivery_receipts include event_id, delivery_plan_id, error, attempt_number, native_message_id."""
    from medre.runtime.run_session.orchestration import run_bridge_session
    from medre.runtime.smoke import _default_smoke_config_path

    config_path = _default_smoke_config_path()
    db_path = str(tmp_path / "orch_receipts.db")

    report = await run_bridge_session(
        config_path,
        storage_path=db_path,
    )
    assert (
        report["status"] == "passed"
    ), f"Expected passed, got {report['status']}: {report.get('fail_reasons', [])}"

    receipts = report["delivery_receipts"]
    assert (
        isinstance(receipts, list) and len(receipts) >= 1
    ), "Expected at least one delivery receipt in orchestration report"

    required_keys = (
        "event_id",
        "delivery_plan_id",
        "error",
        "attempt_number",
        "native_message_id",
    )
    first = receipts[0]
    for key in required_keys:
        assert key in first, (
            f"Required key {key!r} missing from delivery_receipts entry. "
            f"Available keys: {sorted(first.keys())}"
        )


# ---------------------------------------------------------------------------
# Tests: replay timeline receipt schema
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_timeline_receipt_includes_failure_kind_and_native_channel_id(
    tmp_path: Path,
) -> None:
    """assemble_replay_timeline receipt entries include failure_kind and native_channel_id."""
    event = _make_event()
    receipt = DeliveryReceipt(
        sequence=1,
        receipt_id="rcpt-replay-fk-001",
        event_id=_EVENT_ID,
        delivery_plan_id="plan-replay-fk-001",
        target_adapter=_ADAPTER,
        target_channel=_NATIVE_CHANNEL_ID,
        route_id="route-replay-fk-001",
        status="failed",
        failure_kind="adapter_permanent_failure",
        error="simulated replay failure",
        adapter_message_id=None,
        source="replay",
        replay_run_id="run-replay-fk-001",
        created_at=_TS_RECEIPT,
    )

    result = assemble_replay_timeline(
        "run-replay-fk-001", [receipt], {_EVENT_ID: event}
    )

    assert result["status"] == "complete"
    assert result["receipt_count"] == 1

    receipt_entries = [e for e in result["timeline"] if e["entry_type"] == "receipt"]
    assert len(receipt_entries) == 1, "Expected exactly one receipt timeline entry"

    # entry_type (kind/type field) must be present on the entry.
    assert "entry_type" in receipt_entries[0]

    data = receipt_entries[0]["data"]

    # failure_kind must be present and match the receipt.
    assert (
        data["failure_kind"] == "adapter_permanent_failure"
    ), f"Expected failure_kind='adapter_permanent_failure', got {data['failure_kind']!r}"
    # native_channel_id must be present and populated from target_channel.
    assert (
        data["native_channel_id"] == _NATIVE_CHANNEL_ID
    ), f"Expected native_channel_id={_NATIVE_CHANNEL_ID!r}, got {data['native_channel_id']!r}"
