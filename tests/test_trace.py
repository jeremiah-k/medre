"""Tests for list_native_refs_for_event storage API,
medre.runtime.trace timeline assembly, and medre trace CLI commands.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from medre.core.events import (
    CanonicalEvent,
    DeliveryReceipt,
    EventMetadata,
    EventRelation,
    NativeMessageRef,
    NativeRef,
)
from medre.core.storage import SQLiteStorage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    event_id: str = "evt-1",
    event_kind: str = "message.created",
    source_adapter: str = "fake_transport",
    timestamp: datetime | None = None,
    relations: tuple[EventRelation, ...] | None = None,
) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        event_kind=event_kind,
        schema_version=1,
        timestamp=timestamp or datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="node-1",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=relations or (),
        payload={"text": "hello"},
        metadata=EventMetadata(),
    )


def _make_native_ref(
    ref_id: str = "nref-1",
    event_id: str = "evt-1",
    adapter: str = "adapter_a",
    channel: str = "ch-0",
    message_id: str = "msg-0",
    direction: str = "outbound",
    created_at: datetime | None = None,
) -> NativeMessageRef:
    return NativeMessageRef(
        id=ref_id,
        event_id=event_id,
        adapter=adapter,
        native_channel_id=channel,
        native_message_id=message_id,
        native_thread_id=None,
        native_relation_id=None,
        direction=direction,
        created_at=created_at or datetime.now(timezone.utc),
    )


def _make_receipt(
    receipt_id: str = "rcpt-1",
    event_id: str = "evt-1",
    target_adapter: str = "dest_adapter",
    status: str = "sent",
    source: str = "live",
    replay_run_id: str | None = None,
    created_at: datetime | None = None,
) -> DeliveryReceipt:
    return DeliveryReceipt(
        receipt_id=receipt_id,
        event_id=event_id,
        delivery_plan_id="plan-1",
        target_adapter=target_adapter,
        status=status,
        source=source,
        replay_run_id=replay_run_id,
        created_at=created_at or datetime.now(timezone.utc),
    )


# ===================================================================
# Storage: list_native_refs_for_event
# ===================================================================


class TestListNativeRefsForEvent:
    """list_native_refs_for_event returns all native refs for an event."""

    async def test_returns_refs_for_event(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Native refs for an event are returned in created_at order."""
        event = _make_event(event_id="evt-nrefs-1")
        await temp_storage.append(event)

        ref_a = _make_native_ref(
            ref_id="nref-a", event_id="evt-nrefs-1",
            adapter="adapter_a", message_id="msg-a",
        )
        ref_b = _make_native_ref(
            ref_id="nref-b", event_id="evt-nrefs-1",
            adapter="adapter_b", message_id="msg-b",
        )
        await temp_storage.store_native_ref(ref_a)
        await temp_storage.store_native_ref(ref_b)

        refs = await temp_storage.list_native_refs_for_event("evt-nrefs-1")
        assert len(refs) == 2
        ids = {r.id for r in refs}
        assert ids == {"nref-a", "nref-b"}

    async def test_returns_empty_for_unknown_event(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Unknown event_id returns empty list."""
        refs = await temp_storage.list_native_refs_for_event("nonexistent")
        assert refs == []

    async def test_returns_empty_for_event_with_no_refs(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Event with no native refs returns empty list."""
        event = _make_event(event_id="evt-no-nrefs")
        await temp_storage.append(event)

        refs = await temp_storage.list_native_refs_for_event("evt-no-nrefs")
        assert refs == []

    async def test_does_not_return_refs_for_other_events(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Only refs for the specified event_id are returned."""
        event_a = _make_event(event_id="evt-a")
        event_b = _make_event(event_id="evt-b")
        await temp_storage.append(event_a)
        await temp_storage.append(event_b)

        ref_a = _make_native_ref(
            ref_id="nref-a", event_id="evt-a",
            adapter="adapter_a", message_id="msg-a",
        )
        ref_b = _make_native_ref(
            ref_id="nref-b", event_id="evt-b",
            adapter="adapter_b", message_id="msg-b",
        )
        await temp_storage.store_native_ref(ref_a)
        await temp_storage.store_native_ref(ref_b)

        refs = await temp_storage.list_native_refs_for_event("evt-a")
        assert len(refs) == 1
        assert refs[0].id == "nref-a"

    async def test_fields_round_trip(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """All NativeMessageRef fields survive storage round-trip."""
        event = _make_event(event_id="evt-fields")
        await temp_storage.append(event)

        ts = datetime(2026, 3, 15, 10, 30, 0, tzinfo=timezone.utc)
        ref = NativeMessageRef(
            id="nref-fields",
            event_id="evt-fields",
            adapter="matrix",
            native_channel_id="!room:test",
            native_message_id="$msg-001",
            native_thread_id="thread-42",
            native_relation_id=None,
            direction="outbound",
            metadata={"extra": "data"},
            created_at=ts,
        )
        await temp_storage.store_native_ref(ref)

        refs = await temp_storage.list_native_refs_for_event("evt-fields")
        assert len(refs) == 1
        got = refs[0]
        assert got.id == "nref-fields"
        assert got.event_id == "evt-fields"
        assert got.adapter == "matrix"
        assert got.native_channel_id == "!room:test"
        assert got.native_message_id == "$msg-001"
        assert got.native_thread_id == "thread-42"
        assert got.native_relation_id is None
        assert got.direction == "outbound"
        assert got.metadata == {"extra": "data"}
        assert got.created_at == ts

    async def test_ordered_by_created_at(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Native refs are returned in created_at ascending order."""
        event = _make_event(event_id="evt-order")
        await temp_storage.append(event)

        base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        for i in range(5):
            ref = _make_native_ref(
                ref_id=f"nref-ord-{i}",
                event_id="evt-order",
                adapter=f"adapter_{i}",
                message_id=f"msg-{i}",
                created_at=base.replace(hour=12 + i),
            )
            await temp_storage.store_native_ref(ref)

        refs = await temp_storage.list_native_refs_for_event("evt-order")
        assert len(refs) == 5
        for i in range(1, len(refs)):
            assert refs[i].created_at >= refs[i - 1].created_at


# ===================================================================
# Trace module: assemble_event_timeline
# ===================================================================


class TestAssembleEventTimeline:
    """assemble_event_timeline builds a chronological timeline."""

    def test_basic_timeline_structure(self) -> None:
        """Timeline contains event, receipt, native_ref, and relation entries."""
        from medre.runtime.trace import assemble_event_timeline

        ts = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        event = _make_event(event_id="evt-tl-1", timestamp=ts)

        receipt = _make_receipt(
            receipt_id="rcpt-tl-1",
            event_id="evt-tl-1",
            created_at=datetime(2026, 1, 15, 12, 0, 1, tzinfo=timezone.utc),
        )
        nref = _make_native_ref(
            ref_id="nref-tl-1",
            event_id="evt-tl-1",
            created_at=datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
        )
        relation = EventRelation(
            relation_type="reply",
            target_event_id="target-1",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )

        timeline = assemble_event_timeline(event, [receipt], [nref], [relation])

        types = [e["entry_type"] for e in timeline]
        assert "event" in types
        assert "receipt" in types
        assert "native_ref" in types
        assert "relation" in types

    def test_timeline_sorted_chronologically(self) -> None:
        """Timeline entries are sorted by (timestamp, ordinal)."""
        from medre.runtime.trace import assemble_event_timeline

        ts = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        event = _make_event(event_id="evt-sort", timestamp=ts)

        # Receipt with later timestamp.
        receipt = _make_receipt(
            receipt_id="rcpt-sort-1",
            event_id="evt-sort",
            created_at=datetime(2026, 1, 15, 12, 0, 5, tzinfo=timezone.utc),
        )

        timeline = assemble_event_timeline(event, [receipt], [], [])

        # Event should come before receipt.
        assert timeline[0]["entry_type"] == "event"
        assert timeline[1]["entry_type"] == "receipt"
        assert timeline[0]["timestamp"] <= timeline[1]["timestamp"]

    def test_empty_receipts_and_refs(self) -> None:
        """Timeline with only the event is valid."""
        from medre.runtime.trace import assemble_event_timeline

        ts = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        event = _make_event(event_id="evt-empty", timestamp=ts)

        timeline = assemble_event_timeline(event, [], [], [])
        assert len(timeline) == 1
        assert timeline[0]["entry_type"] == "event"
        assert timeline[0]["data"]["event_id"] == "evt-empty"

    def test_timeline_entries_have_required_keys(self) -> None:
        """Each timeline entry has timestamp, ordinal, entry_type, data."""
        from medre.runtime.trace import assemble_event_timeline

        ts = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        event = _make_event(event_id="evt-keys", timestamp=ts)
        receipt = _make_receipt(receipt_id="rcpt-keys", event_id="evt-keys")

        timeline = assemble_event_timeline(event, [receipt], [], [])
        for entry in timeline:
            assert "timestamp" in entry
            assert "ordinal" in entry
            assert "entry_type" in entry
            assert "data" in entry

    def test_timeline_json_safe(self) -> None:
        """Timeline entries are JSON-serialisable (no datetime objects)."""
        from medre.runtime.trace import assemble_event_timeline

        ts = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        event = _make_event(event_id="evt-json", timestamp=ts)
        receipt = _make_receipt(receipt_id="rcpt-json", event_id="evt-json")
        nref = _make_native_ref(ref_id="nref-json", event_id="evt-json")

        timeline = assemble_event_timeline(event, [receipt], [nref], [])

        # Must not raise — every value is JSON-serialisable.
        serialised = json.dumps(timeline, sort_keys=True)
        assert isinstance(serialised, str)

    def test_bounded_max_entries(self) -> None:
        """Timeline is bounded to 1000 entries."""
        from medre.runtime.trace import assemble_event_timeline, _MAX_TIMELINE_ENTRIES

        ts = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        event = _make_event(event_id="evt-bounded", timestamp=ts)

        # Create more than _MAX_TIMELINE_ENTRIES receipts.
        base_ts = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        receipts = [
            _make_receipt(
                receipt_id=f"rcpt-bnd-{i}",
                event_id="evt-bounded",
                created_at=base_ts.replace(minute=i // 60, second=i % 60),
            )
            for i in range(_MAX_TIMELINE_ENTRIES + 100)
        ]

        timeline = assemble_event_timeline(event, receipts, [], [])
        assert len(timeline) <= _MAX_TIMELINE_ENTRIES

    def test_relations_precede_event(self) -> None:
        """Relations have ordinal < 0 so they appear before the event entry."""
        from medre.runtime.trace import assemble_event_timeline

        ts = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        event = _make_event(event_id="evt-rels", timestamp=ts)
        relation = EventRelation(
            relation_type="reply",
            target_event_id="target-1",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )

        timeline = assemble_event_timeline(event, [], [], [relation])
        # Relation should come before event in the timeline.
        types = [e["entry_type"] for e in timeline]
        rel_idx = types.index("relation")
        evt_idx = types.index("event")
        assert rel_idx < evt_idx


# ===================================================================
# Trace module: assemble_replay_timeline
# ===================================================================


class TestAssembleReplayTimeline:
    """assemble_replay_timeline builds a replay timeline."""

    def test_basic_replay_timeline(self) -> None:
        """Replay timeline has run_id, status, receipt_count, timeline."""
        from medre.runtime.trace import assemble_replay_timeline

        ts = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        event = _make_event(event_id="evt-replay-1", timestamp=ts)
        receipt = _make_receipt(
            receipt_id="rcpt-rpl-1",
            event_id="evt-replay-1",
            source="replay",
            replay_run_id="run-1",
            created_at=datetime(2026, 1, 15, 12, 0, 1, tzinfo=timezone.utc),
        )

        result = assemble_replay_timeline("run-1", [receipt], {"evt-replay-1": event})

        assert result["run_id"] == "run-1"
        assert result["status"] == "complete"
        assert result["receipt_count"] == 1
        assert "evt-replay-1" in result["event_ids"]
        assert len(result["timeline"]) >= 1

    def test_empty_receipts(self) -> None:
        """Empty receipt list returns empty timeline."""
        from medre.runtime.trace import assemble_replay_timeline

        result = assemble_replay_timeline("run-empty", [], {})
        assert result["status"] == "empty"
        assert result["receipt_count"] == 0
        assert result["timeline"] == []

    def test_partial_status_when_event_missing(self) -> None:
        """Status is 'partial' when events are missing from cache."""
        from medre.runtime.trace import assemble_replay_timeline

        receipt = _make_receipt(
            receipt_id="rcpt-partial",
            event_id="evt-missing",
            source="replay",
            replay_run_id="run-partial",
        )

        result = assemble_replay_timeline("run-partial", [receipt], {})
        assert result["status"] == "partial"

    def test_event_summary_included(self) -> None:
        """Timeline includes event_summary entries when events are cached."""
        from medre.runtime.trace import assemble_replay_timeline

        ts = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        event = _make_event(event_id="evt-sum", timestamp=ts)
        receipt = _make_receipt(
            receipt_id="rcpt-sum",
            event_id="evt-sum",
            source="replay",
            replay_run_id="run-sum",
        )

        result = assemble_replay_timeline("run-sum", [receipt], {"evt-sum": event})
        types = [e["entry_type"] for e in result["timeline"]]
        assert "event_summary" in types

    def test_json_safe_output(self) -> None:
        """Replay timeline output is JSON-serialisable."""
        from medre.runtime.trace import assemble_replay_timeline

        ts = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        event = _make_event(event_id="evt-rpl-json", timestamp=ts)
        receipt = _make_receipt(
            receipt_id="rcpt-rpl-json",
            event_id="evt-rpl-json",
            source="replay",
            replay_run_id="run-json",
        )

        result = assemble_replay_timeline("run-json", [receipt], {"evt-rpl-json": event})
        serialised = json.dumps(result, sort_keys=True)
        assert isinstance(serialised, str)

    def test_unique_event_ids(self) -> None:
        """event_ids contains unique IDs preserving insertion order."""
        from medre.runtime.trace import assemble_replay_timeline

        receipts = [
            _make_receipt(
                receipt_id=f"rcpt-uniq-{i}",
                event_id="evt-same",
                source="replay",
                replay_run_id="run-uniq",
            )
            for i in range(3)
        ]

        result = assemble_replay_timeline("run-uniq", receipts, {})
        assert result["event_ids"] == ["evt-same"]


# ===================================================================
# Trace module: timeline_to_json
# ===================================================================


class TestTimelineToJson:
    """timeline_to_json produces deterministic sorted JSON."""

    def test_deterministic_keys(self) -> None:
        """JSON output has sorted keys."""
        from medre.runtime.trace import timeline_to_json

        data = {"z": 1, "a": 2}
        output = timeline_to_json(data)
        parsed = json.loads(output)
        keys = list(parsed.keys())
        assert keys == sorted(keys)

    def test_handles_datetime(self) -> None:
        """datetime values are serialised as strings."""
        from medre.runtime.trace import timeline_to_json

        ts = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        data = {"timestamp": ts}
        output = timeline_to_json(data)
        assert "2026-01-15" in output


# ===================================================================
# CLI: medre trace event
# ===================================================================


CONFIG_TRACE_SQLITE = """\
[runtime]
name = "test-trace"

[storage]
backend = "sqlite"
path = "{state}/trace.db"
"""


def _seed_trace_db(
    db_path: str,
    event_id: str = "evt-trace-1",
    replay_run_id: str | None = None,
    with_native_ref: bool = True,
    with_relation: bool = True,
) -> None:
    """Synchronously seed a trace test database."""
    import asyncio
    from medre.core.storage.sqlite import SQLiteStorage

    async def _seed() -> None:
        storage = SQLiteStorage(db_path)
        await storage.initialize()

        ts = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        relation = EventRelation(
            relation_type="reply",
            target_event_id="target-evt",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        ) if with_relation else None

        event = CanonicalEvent(
            event_id=event_id,
            event_kind="message.created",
            schema_version=1,
            timestamp=ts,
            source_adapter="test_adapter",
            source_transport_id="test-transport",
            source_channel_id="ch-trace",
            parent_event_id=None,
            lineage=(),
            relations=(relation,) if relation else (),
            payload={"text": "trace test message"},
            metadata=EventMetadata(),
        )
        await storage.append(event)

        if with_native_ref:
            await storage.store_native_ref(NativeMessageRef(
                id="nref-trace-1",
                event_id=event_id,
                adapter="matrix",
                native_channel_id="!room:test",
                native_message_id="$trace-msg-1",
                native_thread_id=None,
                native_relation_id=None,
                direction="outbound",
                created_at=ts,
            ))

        rcpt_kwargs: dict[str, Any] = dict(
            receipt_id="rcpt-trace-1",
            event_id=event_id,
            delivery_plan_id="plan-trace-1",
            target_adapter="dest_adapter",
            status="sent",
            created_at=datetime(2026, 1, 15, 12, 0, 1, tzinfo=timezone.utc),
        )
        if replay_run_id is not None:
            rcpt_kwargs["source"] = "replay"
            rcpt_kwargs["replay_run_id"] = replay_run_id

        await storage.append_receipt(DeliveryReceipt(**rcpt_kwargs))
        await storage.close()

    asyncio.run(_seed())


@pytest.fixture()
def config_trace_sqlite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Config with sqlite storage and seeded data for trace testing."""
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    db_path = str(tmp_path / "state" / "trace.db")
    _seed_trace_db(db_path)
    p = tmp_path / "config.toml"
    p.write_text(CONFIG_TRACE_SQLITE)
    return p


@pytest.fixture()
def config_trace_replay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Config with sqlite storage and seeded replay receipts."""
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    db_path = str(tmp_path / "state" / "trace.db")
    _seed_trace_db(db_path, event_id="evt-replay-trace", replay_run_id="run-trace-42")
    p = tmp_path / "config.toml"
    p.write_text(CONFIG_TRACE_SQLITE)
    return p


@pytest.fixture()
def config_trace_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Config with sqlite storage but no seeded data."""
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    db_path = str(tmp_path / "state" / "trace.db")
    # Just initialize the DB, no data.
    import asyncio
    from medre.core.storage.sqlite import SQLiteStorage

    async def _init() -> None:
        storage = SQLiteStorage(db_path)
        await storage.initialize()
        await storage.close()

    asyncio.run(_init())
    p = tmp_path / "config.toml"
    p.write_text(CONFIG_TRACE_SQLITE)
    return p


# CLI helpers (reuse pattern from test_cli.py)


def _run_cli(*args: str) -> str:
    """Run CLI with given args, capture stdout, and return output."""
    import io
    from contextlib import redirect_stdout, redirect_stderr
    from medre.cli import main

    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            main(list(args))
    except SystemExit as e:
        if e.code not in (None, 0):
            raise
    return stdout.getvalue()


def _run_cli_both(*args: str) -> tuple[str, str]:
    """Run CLI and return (stdout, stderr) pair."""
    import io
    from contextlib import redirect_stdout, redirect_stderr
    from medre.cli import main

    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            main(list(args))
    except SystemExit:
        pass
    return stdout.getvalue(), stderr.getvalue()


class TestTraceEventParser:
    """Tests for 'medre trace' argument parsing."""

    def test_trace_requires_subcommand(self) -> None:
        with pytest.raises(SystemExit):
            _run_cli("trace")

    def test_trace_unknown_subcommand(self) -> None:
        with pytest.raises(SystemExit):
            _run_cli("trace", "nonexistent")

    def test_trace_event_requires_event_id(self) -> None:
        with pytest.raises(SystemExit):
            _run_cli("trace", "event", "--config", "/nonexistent/config.toml")


class TestTraceEvent:
    """Tests for 'medre trace event' command."""

    def test_event_found_json(
        self, config_trace_sqlite: Path,
    ) -> None:
        """trace event --json returns parseable JSON timeline."""
        output = _run_cli(
            "trace", "event", "evt-trace-1",
            "--config", str(config_trace_sqlite),
            "--json",
        )
        parsed = json.loads(output)
        assert isinstance(parsed, list)
        types = [e["entry_type"] for e in parsed]
        assert "event" in types
        assert "receipt" in types

    def test_event_timeline_contains_expected_entries(
        self, config_trace_sqlite: Path,
    ) -> None:
        """Timeline includes event, receipt, native_ref, and relation entries."""
        output = _run_cli(
            "trace", "event", "evt-trace-1",
            "--config", str(config_trace_sqlite),
            "--json",
        )
        parsed = json.loads(output)
        types = [e["entry_type"] for e in parsed]
        assert "event" in types
        assert "receipt" in types
        assert "native_ref" in types
        assert "relation" in types

    def test_event_not_found(
        self, config_trace_sqlite: Path,
    ) -> None:
        """trace event with unknown ID exits EXIT_NOT_FOUND."""
        from medre.cli import EXIT_NOT_FOUND

        with pytest.raises(SystemExit) as exc_info:
            _run_cli(
                "trace", "event", "nonexistent",
                "--config", str(config_trace_sqlite),
            )
        assert exc_info.value.code == EXIT_NOT_FOUND

    def test_event_not_found_stderr(
        self, config_trace_sqlite: Path,
    ) -> None:
        """Error message mentions the missing event."""
        stdout, stderr = _run_cli_both(
            "trace", "event", "nonexistent",
            "--config", str(config_trace_sqlite),
        )
        assert "event not found" in stderr
        assert "nonexistent" in stderr

    def test_event_human_readable(
        self, config_trace_sqlite: Path,
    ) -> None:
        """Human-readable output includes event info."""
        output = _run_cli(
            "trace", "event", "evt-trace-1",
            "--config", str(config_trace_sqlite),
        )
        assert "Event: evt-trace-1 (message.created) from test_adapter" in output
        assert "Timeline (4 entries):" in output
        assert "Summary:" in output
        assert "Receipts:" in output
        assert "Native refs:" in output
        assert "Relations:" in output

    def test_event_json_deterministic(
        self, config_trace_sqlite: Path,
    ) -> None:
        """JSON output has sorted keys."""
        output = _run_cli(
            "trace", "event", "evt-trace-1",
            "--config", str(config_trace_sqlite),
            "--json",
        )
        parsed = json.loads(output)
        for entry in parsed:
            keys = list(entry.keys())
            assert keys == sorted(keys)

    def test_event_memory_backend_exits_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Memory backend exits with EXIT_CONFIG."""
        from medre.cli import EXIT_CONFIG

        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
        config_text = """\
[runtime]
name = "test-trace-mem"

[storage]
backend = "memory"
"""
        cfg = tmp_path / "config.toml"
        cfg.write_text(config_text)

        with pytest.raises(SystemExit) as exc_info:
            _run_cli("trace", "event", "evt-1", "--config", str(cfg))
        assert exc_info.value.code == EXIT_CONFIG


class TestTraceReplay:
    """Tests for 'medre trace replay' command."""

    def test_replay_found_json(
        self, config_trace_replay: Path,
    ) -> None:
        """trace replay --json returns parseable JSON timeline."""
        output = _run_cli(
            "trace", "replay", "run-trace-42",
            "--config", str(config_trace_replay),
            "--json",
        )
        parsed = json.loads(output)
        assert isinstance(parsed, dict)
        assert parsed["run_id"] == "run-trace-42"
        assert parsed["status"] == "complete"
        assert parsed["receipt_count"] == 1

    def test_replay_timeline_has_entries(
        self, config_trace_replay: Path,
    ) -> None:
        """Replay timeline contains receipt and event_summary entries."""
        output = _run_cli(
            "trace", "replay", "run-trace-42",
            "--config", str(config_trace_replay),
            "--json",
        )
        parsed = json.loads(output)
        types = [e["entry_type"] for e in parsed["timeline"]]
        assert "receipt" in types
        assert "event_summary" in types

    def test_replay_not_found(
        self, config_trace_sqlite: Path,
    ) -> None:
        """trace replay with unknown run_id exits EXIT_NOT_FOUND."""
        from medre.cli import EXIT_NOT_FOUND

        with pytest.raises(SystemExit) as exc_info:
            _run_cli(
                "trace", "replay", "nonexistent-run",
                "--config", str(config_trace_sqlite),
            )
        assert exc_info.value.code == EXIT_NOT_FOUND

    def test_replay_not_found_stderr(
        self, config_trace_sqlite: Path,
    ) -> None:
        """Error message mentions the missing run."""
        stdout, stderr = _run_cli_both(
            "trace", "replay", "nonexistent-run",
            "--config", str(config_trace_sqlite),
        )
        assert "no receipts found" in stderr
        assert "nonexistent-run" in stderr

    def test_replay_human_readable(
        self, config_trace_replay: Path,
    ) -> None:
        """Human-readable output includes replay info."""
        output = _run_cli(
            "trace", "replay", "run-trace-42",
            "--config", str(config_trace_replay),
        )
        assert "Replay timeline: run-trace-42" in output
        assert "complete" in output
        assert "Receipts:" in output

    def test_replay_json_deterministic(
        self, config_trace_replay: Path,
    ) -> None:
        """JSON output has sorted keys."""
        output = _run_cli(
            "trace", "replay", "run-trace-42",
            "--config", str(config_trace_replay),
            "--json",
        )
        parsed = json.loads(output)
        keys = list(parsed.keys())
        assert keys == sorted(keys)


# ===================================================================
# Enriched receipt entries in assemble_event_timeline
# ===================================================================


class TestEnrichedReceiptEntries:
    """Receipt entries include full fields: receipt_id, event_id, route_id,
    delivery_plan_id, target_adapter, status, failure_kind, error,
    attempt_number, source, replay_run_id, native_message_id, native_channel_id."""

    def test_receipt_includes_all_required_fields(self) -> None:
        from medre.runtime.trace import assemble_event_timeline

        ts = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        event = _make_event(event_id="evt-rcpt-fields", timestamp=ts)
        receipt = _make_receipt(
            receipt_id="rcpt-fields-1",
            event_id="evt-rcpt-fields",
            created_at=datetime(2026, 1, 15, 12, 0, 1, tzinfo=timezone.utc),
        )

        timeline = assemble_event_timeline(event, [receipt], [], [])
        receipt_entry = next(e for e in timeline if e["entry_type"] == "receipt")
        data = receipt_entry["data"]

        required = [
            "receipt_id", "event_id", "route_id", "delivery_plan_id",
            "target_adapter", "status", "failure_kind", "error",
            "attempt_number", "source", "replay_run_id",
            "native_message_id", "native_channel_id",
        ]
        for field in required:
            assert field in data, f"Missing field: {field}"

    def test_receipt_values_populated_from_struct(self) -> None:
        from medre.runtime.trace import assemble_event_timeline

        ts = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        event = _make_event(event_id="evt-rcpt-vals", timestamp=ts)
        receipt = _make_receipt(
            receipt_id="rcpt-vals-1",
            event_id="evt-rcpt-vals",
            created_at=datetime(2026, 1, 15, 12, 0, 1, tzinfo=timezone.utc),
        )

        timeline = assemble_event_timeline(event, [receipt], [], [])
        data = next(e for e in timeline if e["entry_type"] == "receipt")["data"]

        assert data["receipt_id"] == "rcpt-vals-1"
        assert data["event_id"] == "evt-rcpt-vals"
        assert data["delivery_plan_id"] == "plan-1"
        assert data["target_adapter"] == "dest_adapter"
        assert data["status"] == "sent"
        assert data["source"] == "live"
        assert data["attempt_number"] == 1
        assert data["replay_run_id"] is None
        assert data["error"] is None
        assert data["failure_kind"] is None
        assert data["native_message_id"] is None
        assert data["native_channel_id"] is None

    def test_receipt_replay_fields(self) -> None:
        from medre.runtime.trace import assemble_event_timeline

        ts = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        event = _make_event(event_id="evt-rcpt-replay", timestamp=ts)
        receipt = _make_receipt(
            receipt_id="rcpt-replay-1",
            event_id="evt-rcpt-replay",
            source="replay",
            replay_run_id="run-xyz",
            created_at=datetime(2026, 1, 15, 12, 0, 1, tzinfo=timezone.utc),
        )

        timeline = assemble_event_timeline(event, [receipt], [], [])
        data = next(e for e in timeline if e["entry_type"] == "receipt")["data"]

        assert data["source"] == "replay"
        assert data["replay_run_id"] == "run-xyz"


# ===================================================================
# Enriched native_ref entries in assemble_event_timeline
# ===================================================================


class TestEnrichedNativeRefEntries:
    """Native_ref entries include: event_id, adapter, native_channel_id,
    native_message_id, native_thread_id, direction."""

    def test_native_ref_includes_event_id(self) -> None:
        from medre.runtime.trace import assemble_event_timeline

        ts = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        event = _make_event(event_id="evt-nref-enriched", timestamp=ts)
        nref = _make_native_ref(
            ref_id="nref-enriched-1",
            event_id="evt-nref-enriched",
            created_at=ts,
        )

        timeline = assemble_event_timeline(event, [], [nref], [])
        data = next(e for e in timeline if e["entry_type"] == "native_ref")["data"]

        assert data["event_id"] == "evt-nref-enriched"

    def test_native_ref_includes_thread_id(self) -> None:
        from medre.runtime.trace import assemble_event_timeline

        ts = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        event = _make_event(event_id="evt-nref-thread", timestamp=ts)
        nref = NativeMessageRef(
            id="nref-thread-1",
            event_id="evt-nref-thread",
            adapter="matrix",
            native_channel_id="!room:test",
            native_message_id="$msg-thread",
            native_thread_id="thread-42",
            native_relation_id=None,
            direction="outbound",
            created_at=ts,
        )

        timeline = assemble_event_timeline(event, [], [nref], [])
        data = next(e for e in timeline if e["entry_type"] == "native_ref")["data"]

        assert data["native_thread_id"] == "thread-42"

    def test_native_ref_thread_id_none_when_absent(self) -> None:
        from medre.runtime.trace import assemble_event_timeline

        ts = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        event = _make_event(event_id="evt-nref-nothread", timestamp=ts)
        nref = _make_native_ref(
            ref_id="nref-nothread-1",
            event_id="evt-nref-nothread",
            created_at=ts,
        )

        timeline = assemble_event_timeline(event, [], [nref], [])
        data = next(e for e in timeline if e["entry_type"] == "native_ref")["data"]

        assert data["native_thread_id"] is None

    def test_native_ref_all_fields(self) -> None:
        from medre.runtime.trace import assemble_event_timeline

        ts = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        event = _make_event(event_id="evt-nref-all", timestamp=ts)
        nref = _make_native_ref(
            ref_id="nref-all-1",
            event_id="evt-nref-all",
            adapter="meshtastic",
            channel="!ch:1",
            message_id="$msg-all",
            direction="inbound",
            created_at=ts,
        )

        timeline = assemble_event_timeline(event, [], [nref], [])
        data = next(e for e in timeline if e["entry_type"] == "native_ref")["data"]

        assert data["id"] == "nref-all-1"
        assert data["event_id"] == "evt-nref-all"
        assert data["adapter"] == "meshtastic"
        assert data["native_channel_id"] == "!ch:1"
        assert data["native_message_id"] == "$msg-all"
        assert data["direction"] == "inbound"


# ===================================================================
# Enriched replay timeline
# ===================================================================


class TestEnrichedReplayTimeline:
    """assemble_replay_timeline includes full receipt fields,
    missing_event_ids, duplicate_send_caveat."""

    def test_replay_receipt_includes_full_fields(self) -> None:
        from medre.runtime.trace import assemble_replay_timeline

        ts = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        event = _make_event(event_id="evt-rpl-fields", timestamp=ts)
        receipt = _make_receipt(
            receipt_id="rcpt-rpl-fields",
            event_id="evt-rpl-fields",
            source="replay",
            replay_run_id="run-fields",
            created_at=datetime(2026, 1, 15, 12, 0, 1, tzinfo=timezone.utc),
        )

        result = assemble_replay_timeline(
            "run-fields", [receipt], {"evt-rpl-fields": event},
        )
        receipt_entry = next(
            e for e in result["timeline"] if e["entry_type"] == "receipt"
        )
        data = receipt_entry["data"]

        required = [
            "receipt_id", "event_id", "route_id", "delivery_plan_id",
            "target_adapter", "status", "failure_kind", "error",
            "attempt_number", "source", "replay_run_id",
            "native_message_id", "native_channel_id",
        ]
        for field in required:
            assert field in data, f"Missing field: {field}"

        assert data["source"] == "replay"
        assert data["replay_run_id"] == "run-fields"

    def test_replay_missing_event_ids_populated(self) -> None:
        from medre.runtime.trace import assemble_replay_timeline

        receipt = _make_receipt(
            receipt_id="rcpt-miss",
            event_id="evt-missing-1",
            source="replay",
            replay_run_id="run-miss",
        )

        result = assemble_replay_timeline("run-miss", [receipt], {})

        assert result["missing_event_ids"] == ["evt-missing-1"]
        assert result["status"] == "partial"

    def test_replay_no_missing_event_ids_when_complete(self) -> None:
        from medre.runtime.trace import assemble_replay_timeline

        ts = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        event = _make_event(event_id="evt-complete", timestamp=ts)
        receipt = _make_receipt(
            receipt_id="rcpt-complete",
            event_id="evt-complete",
            source="replay",
            replay_run_id="run-complete",
        )

        result = assemble_replay_timeline(
            "run-complete", [receipt], {"evt-complete": event},
        )

        assert result["missing_event_ids"] == []
        assert result["status"] == "complete"

    def test_replay_duplicate_send_caveat_present(self) -> None:
        from medre.runtime.trace import assemble_replay_timeline

        ts = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        event = _make_event(event_id="evt-caveat", timestamp=ts)
        receipt = _make_receipt(
            receipt_id="rcpt-caveat",
            event_id="evt-caveat",
            source="replay",
            replay_run_id="run-caveat",
        )

        result = assemble_replay_timeline(
            "run-caveat", [receipt], {"evt-caveat": event},
        )

        assert "duplicate_send_caveat" in result
        assert isinstance(result["duplicate_send_caveat"], str)
        assert "deduplicate" in result["duplicate_send_caveat"].lower()

    def test_replay_empty_has_caveat(self) -> None:
        from medre.runtime.trace import assemble_replay_timeline

        result = assemble_replay_timeline("run-empty-caveat", [], {})

        assert "duplicate_send_caveat" in result
        assert result["missing_event_ids"] == []
        assert result["status"] == "empty"


# ===================================================================
# Best-effort warning text (cli.py)
# ===================================================================


class TestBestEffortWarningText:
    """_BEST_EFFORT_WARNING must not claim replay records are
    NOT distinguishable; it must mention source='replay',
    replay_run_id, traceability is NOT dedupe, and duplicate-send risk."""

    def test_warning_mentions_distinguishable(self) -> None:
        from medre.cli.replay_commands import _BEST_EFFORT_WARNING

        assert "distinguishable" in _BEST_EFFORT_WARNING.lower()
        # Must NOT say "NOT distinguishable from live records"
        assert "NOT" not in _BEST_EFFORT_WARNING or \
            "NOT distinguishable" not in _BEST_EFFORT_WARNING

    def test_warning_mentions_source_replay(self) -> None:
        from medre.cli.replay_commands import _BEST_EFFORT_WARNING

        assert "source='replay'" in _BEST_EFFORT_WARNING
        assert "replay_run_id" in _BEST_EFFORT_WARNING

    def test_warning_mentions_traceability_not_dedupe(self) -> None:
        from medre.cli.replay_commands import _BEST_EFFORT_WARNING

        assert "traceability" in _BEST_EFFORT_WARNING.lower()
        assert "dedupe" in _BEST_EFFORT_WARNING.lower() or \
            "NOT dedupe" in _BEST_EFFORT_WARNING

    def test_warning_mentions_duplicate_send_risk(self) -> None:
        from medre.cli.replay_commands import _BEST_EFFORT_WARNING

        assert "duplicate" in _BEST_EFFORT_WARNING.lower()


# ===================================================================
# Public sanitize_error (snapshot.py)
# ===================================================================


class TestPublicSanitizeError:
    """sanitize_error is exported as a public function from snapshot.py."""

    def test_import_from_public_name(self) -> None:
        from medre.observability.sanitization import sanitize_error

        assert callable(sanitize_error)

    def test_sanitize_error_redacts_tokens(self) -> None:
        from medre.observability.sanitization import sanitize_error

        result = sanitize_error("Error: token syt_abc123def456 for user")
        assert "syt_abc123def456" not in result
        assert "[REDACTED]" in result

    def test_sanitize_error_in_all(self) -> None:
        import medre.observability.sanitization as sanitization_mod

        assert "sanitize_error" in sanitization_mod.__all__

    def test_evidence_imports_public_name(self) -> None:
        """evidence package uses the direct import from observability.sanitization."""
        import inspect
        from medre.runtime.evidence import _helpers

        source = inspect.getsource(_helpers)
        assert "from medre.observability.sanitization import sanitize_error" in source
