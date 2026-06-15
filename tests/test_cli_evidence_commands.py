"""Tests for 'medre inspect event' augmented output: --timeline, --evidence, --recovery, combined flags,
and 'medre inspect replay' subcommand, plus --storage-path with augmented commands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from medre.cli import main
from tests.helpers.cli import (
    CONFIG_INSPECT_SQLITE,
    _run_cli,
    _run_cli_both,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("MEDRE_HOME", "MEDRE_CONFIG"):
        monkeypatch.delenv(var, raising=False)


def _seed_inspect_db(
    db_path: str,
    event_id: str = "evt-inspect-1",
    source_adapter: str = "test_adapter",
    replay_run_id: str | None = None,
    native_adapter: str | None = None,
    native_channel_id: str | None = None,
    native_message_id: str | None = None,
) -> None:
    """Synchronously seed an inspect test database with an event + receipt + native ref."""
    import asyncio
    from datetime import datetime, timezone

    from medre.core.events import (
        CanonicalEvent,
        DeliveryReceipt,
        EventMetadata,
        NativeMessageRef,
    )
    from medre.core.storage.sqlite.storage import SQLiteStorage

    async def _seed() -> None:
        storage = SQLiteStorage(db_path)
        try:
            await storage.initialize()
            event = CanonicalEvent(
                event_id=event_id,
                event_kind="message.created",
                schema_version=1,
                timestamp=datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
                source_adapter=source_adapter,
                source_transport_id="test-transport",
                source_channel_id="ch-inspect",
                parent_event_id=None,
                lineage=(),
                relations=(),
                payload={"text": "inspect test message"},
                metadata=EventMetadata(),
            )
            await storage.append(event)

            receipt_kwargs: dict = dict(
                sequence=1,
                receipt_id="rcpt-inspect-1",
                event_id=event_id,
                delivery_plan_id="plan-inspect-1",
                target_adapter="dest_adapter",
                route_id="route-inspect",
                status="sent",
                created_at=datetime(2026, 1, 15, 12, 0, 1, tzinfo=timezone.utc),
            )
            if replay_run_id is not None:
                receipt_kwargs["source"] = "replay"
                receipt_kwargs["replay_run_id"] = replay_run_id

            await storage.append_receipt(DeliveryReceipt(**receipt_kwargs))

            if native_adapter is not None and native_message_id is not None:
                await storage.store_native_ref(
                    NativeMessageRef(
                        id="nref-inspect-1",
                        event_id=event_id,
                        adapter=native_adapter,
                        native_channel_id=native_channel_id,
                        native_message_id=native_message_id,
                        native_thread_id=None,
                        native_relation_id=None,
                        direction="outbound",
                        created_at=datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
                    )
                )
        finally:
            await storage.close()

    asyncio.run(_seed())


def _seed_inspect_failed_db(
    db_path: str,
    event_id: str = "evt-inspect-fail-1",
    receipt_status: str = "failed",
    receipt_error: str | None = "TimeoutError: connection timed out",
) -> None:
    """Synchronously seed a DB with an event and a failed receipt."""
    import asyncio
    from datetime import datetime, timezone

    from medre.core.events import (
        CanonicalEvent,
        EventMetadata,
        NativeMessageRef,
    )
    from medre.core.storage.sqlite.storage import SQLiteStorage

    async def _seed() -> None:
        import msgspec

        from medre.core.events.canonical import DeliveryReceipt as DR

        storage = SQLiteStorage(db_path)
        await storage.initialize()

        event = CanonicalEvent(
            event_id=event_id,
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
            source_adapter="test_adapter",
            source_transport_id="test-transport",
            source_channel_id="ch-inspect",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "inspect failed message"},
            metadata=EventMetadata(),
        )
        await storage.append(event)

        receipt_json = {
            "sequence": 1,
            "receipt_id": "rcpt-fail-1",
            "event_id": event_id,
            "delivery_plan_id": "plan-fail-1",
            "target_adapter": "dest_adapter",
            "route_id": "route-fail",
            "status": receipt_status,
            "error": receipt_error,
            "created_at": datetime(
                2026, 1, 15, 12, 0, 1, tzinfo=timezone.utc
            ).isoformat(),
        }
        receipt = msgspec.json.decode(msgspec.json.encode(receipt_json), type=DR)
        await storage.append_receipt(receipt)

        await storage.store_native_ref(
            NativeMessageRef(
                id="nref-fail-1",
                event_id=event_id,
                adapter="matrix",
                native_channel_id="!room:test",
                native_message_id="$fail-msg-1",
                native_thread_id=None,
                native_relation_id=None,
                direction="outbound",
                created_at=datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
            )
        )

        await storage.close()

    asyncio.run(_seed())


@pytest.fixture()
def db_inspect_sqlite(tmp_path: Path) -> str:
    """Seeded SQLite DB path for inspect tests."""
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    db_path = str(tmp_path / "state" / "inspect.db")
    _seed_inspect_db(db_path)
    return db_path


@pytest.fixture()
def config_inspect_sqlite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Config with sqlite storage pointing at a temp MEDRE_HOME, with seeded DB."""
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    db_path = str(tmp_path / "state" / "inspect.db")
    _seed_inspect_db(db_path)
    p = tmp_path / "config.yaml"
    p.write_text(CONFIG_INSPECT_SQLITE)
    return p


@pytest.fixture()
def db_inspect_failed(tmp_path: Path) -> str:
    """Seeded SQLite DB with failed receipt."""
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    db_path = str(tmp_path / "state" / "inspect.db")
    _seed_inspect_failed_db(db_path)
    return db_path


@pytest.fixture()
def config_inspect_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Config with sqlite storage and seeded failed receipt."""
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    db_path = str(tmp_path / "state" / "inspect.db")
    _seed_inspect_failed_db(db_path)
    p = tmp_path / "config.yaml"
    p.write_text(CONFIG_INSPECT_SQLITE)
    return p


@pytest.fixture()
def db_inspect_replay_for_inspect(tmp_path: Path) -> str:
    """Seeded SQLite DB with replay receipts for inspect replay."""
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    db_path = str(tmp_path / "state" / "inspect.db")
    _seed_inspect_db(
        db_path,
        event_id="evt-inspect-replay-1",
        replay_run_id="run-inspect-99",
    )
    return db_path


@pytest.fixture()
def config_inspect_replay_for_inspect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Config with sqlite storage and seeded replay receipts for inspect replay."""
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    db_path = str(tmp_path / "state" / "inspect.db")
    _seed_inspect_db(
        db_path,
        event_id="evt-inspect-replay-1",
        replay_run_id="run-inspect-99",
    )
    p = tmp_path / "config.yaml"
    p.write_text(CONFIG_INSPECT_SQLITE)
    return p


# ---------------------------------------------------------------------------
# inspect event — no flags unchanged
# ---------------------------------------------------------------------------


class TestInspectEventNoFlagsUnchanged:
    """Verify that 'inspect event' without flags still works identically."""

    def test_no_flags_returns_event_json(
        self,
        db_inspect_sqlite: str,
    ) -> None:
        """Without flags, output is the same as before: just the event JSON."""
        output = _run_cli(
            "inspect",
            "event",
            "evt-inspect-1",
            "--storage-path",
            db_inspect_sqlite,
        )
        parsed = json.loads(output)
        assert parsed["event_id"] == "evt-inspect-1"
        assert parsed["event_kind"] == "message.created"
        assert "timeline" not in parsed
        assert "evidence" not in parsed
        assert "recovery" not in parsed

    def test_no_flags_deterministic(
        self,
        db_inspect_sqlite: str,
    ) -> None:
        """Without flags, output has sorted keys (deterministic)."""
        output = _run_cli(
            "inspect",
            "event",
            "evt-inspect-1",
            "--storage-path",
            db_inspect_sqlite,
        )
        parsed = json.loads(output)
        keys = list(parsed.keys())
        assert keys == sorted(keys)


# ---------------------------------------------------------------------------
# inspect event --timeline
# ---------------------------------------------------------------------------


class TestInspectEventTimeline:
    """Tests for 'medre inspect event --timeline'."""

    def test_timeline_includes_event_and_entries(
        self,
        db_inspect_sqlite: str,
    ) -> None:
        """--timeline output has 'event' dict and 'timeline' list."""
        output = _run_cli(
            "inspect",
            "event",
            "evt-inspect-1",
            "--storage-path",
            db_inspect_sqlite,
            "--timeline",
        )
        parsed = json.loads(output)
        assert "event" in parsed
        assert parsed["event"]["event_id"] == "evt-inspect-1"
        assert "timeline" in parsed
        assert isinstance(parsed["timeline"], list)
        assert len(parsed["timeline"]) > 0

    def test_timeline_entries_match_trace_event(
        self,
        db_inspect_sqlite: str,
    ) -> None:
        """Timeline entries from 'inspect event --timeline' are semantically
        identical to 'trace event --json'."""
        inspect_output = _run_cli(
            "inspect",
            "event",
            "evt-inspect-1",
            "--storage-path",
            db_inspect_sqlite,
            "--timeline",
        )
        inspect_parsed = json.loads(inspect_output)

        trace_output = _run_cli(
            "trace",
            "event",
            "evt-inspect-1",
            "--storage-path",
            db_inspect_sqlite,
            "--json",
        )
        trace_parsed = json.loads(trace_output)

        inspect_entries = inspect_parsed["timeline"]
        trace_entries = trace_parsed
        assert len(inspect_entries) == len(trace_entries)
        inspect_types = [e["entry_type"] for e in inspect_entries]
        trace_types = [e["entry_type"] for e in trace_entries]
        assert inspect_types == trace_types

    def test_timeline_json_deterministic(
        self,
        db_inspect_sqlite: str,
    ) -> None:
        """Timeline JSON has sorted keys."""
        output = _run_cli(
            "inspect",
            "event",
            "evt-inspect-1",
            "--storage-path",
            db_inspect_sqlite,
            "--timeline",
        )
        parsed = json.loads(output)
        keys = list(parsed.keys())
        assert keys == sorted(keys)
        for entry in parsed["timeline"]:
            entry_keys = list(entry.keys())
            assert entry_keys == sorted(entry_keys)

    def test_timeline_event_not_found(
        self,
        db_inspect_sqlite: str,
    ) -> None:
        """--timeline with missing event exits EXIT_NOT_FOUND."""
        from medre.cli import EXIT_NOT_FOUND

        with pytest.raises(SystemExit) as exc_info:
            _run_cli(
                "inspect",
                "event",
                "nonexistent-event",
                "--storage-path",
                db_inspect_sqlite,
                "--timeline",
            )
        assert exc_info.value.code == EXIT_NOT_FOUND


# ---------------------------------------------------------------------------
# inspect event --evidence
# ---------------------------------------------------------------------------


class TestInspectEventEvidence:
    """Tests for 'medre inspect event --evidence'."""

    def test_evidence_includes_event_and_bundle(
        self,
        db_inspect_sqlite: str,
    ) -> None:
        """--evidence output has 'event' dict and 'evidence' bundle."""
        output = _run_cli(
            "inspect",
            "event",
            "evt-inspect-1",
            "--storage-path",
            db_inspect_sqlite,
            "--evidence",
        )
        parsed = json.loads(output)
        assert "event" in parsed
        assert parsed["event"]["event_id"] == "evt-inspect-1"
        assert "evidence" in parsed
        bundle = parsed["evidence"]
        assert bundle["schema_version"] == 1
        assert "sections" in bundle

    def test_evidence_no_runtime_started(
        self,
        db_inspect_sqlite: str,
    ) -> None:
        """--evidence does not start the runtime."""
        output = _run_cli(
            "inspect",
            "event",
            "evt-inspect-1",
            "--storage-path",
            db_inspect_sqlite,
            "--evidence",
        )
        parsed = json.loads(output)
        assert parsed["evidence"]["runtime_started"] is False

    def test_evidence_json_deterministic(
        self,
        db_inspect_sqlite: str,
    ) -> None:
        """Evidence output has sorted keys."""
        output = _run_cli(
            "inspect",
            "event",
            "evt-inspect-1",
            "--storage-path",
            db_inspect_sqlite,
            "--evidence",
        )
        parsed = json.loads(output)
        keys = list(parsed.keys())
        assert keys == sorted(keys)


# ---------------------------------------------------------------------------
# inspect event --recovery
# ---------------------------------------------------------------------------


class TestInspectEventRecovery:
    """Tests for 'medre inspect event --recovery'."""

    def test_recovery_includes_event_and_runbook(
        self,
        db_inspect_failed: str,
    ) -> None:
        """--recovery output has 'event' dict and 'recovery' runbook."""
        output = _run_cli(
            "inspect",
            "event",
            "evt-inspect-fail-1",
            "--storage-path",
            db_inspect_failed,
            "--recovery",
        )
        parsed = json.loads(output)
        assert "event" in parsed
        assert parsed["event"]["event_id"] == "evt-inspect-fail-1"
        assert "recovery" in parsed
        runbook = parsed["recovery"]
        assert runbook["scope"] == "event"
        assert runbook["event_id"] == "evt-inspect-fail-1"
        assert "failed_targets" in runbook
        assert "failure_classification" in runbook
        assert "recommended_commands" in runbook
        assert "timeline" in runbook

    def test_recovery_failed_targets_populated(
        self,
        db_inspect_failed: str,
    ) -> None:
        """Failed receipt produces failed_targets entry."""
        output = _run_cli(
            "inspect",
            "event",
            "evt-inspect-fail-1",
            "--storage-path",
            db_inspect_failed,
            "--recovery",
        )
        parsed = json.loads(output)
        runbook = parsed["recovery"]
        assert len(runbook["failed_targets"]) == 1
        ft = runbook["failed_targets"][0]
        assert ft["status"] == "failed"
        assert ft["target_adapter"] == "dest_adapter"

    def test_recovery_runbook_matches_recover_output(
        self,
        db_inspect_failed: str,
    ) -> None:
        """Recovery runbook from 'inspect event --recovery' is semantically
        identical to 'recover --event --json'."""
        inspect_output = _run_cli(
            "inspect",
            "event",
            "evt-inspect-fail-1",
            "--storage-path",
            db_inspect_failed,
            "--recovery",
        )
        inspect_parsed = json.loads(inspect_output)
        inspect_runbook = inspect_parsed["recovery"]

        recover_output = _run_cli(
            "recover",
            "--storage-path",
            db_inspect_failed,
            "--event",
            "evt-inspect-fail-1",
            "--json",
        )
        recover_parsed = json.loads(recover_output)

        assert inspect_runbook["scope"] == recover_parsed["scope"]
        assert inspect_runbook["event_id"] == recover_parsed["event_id"]
        assert inspect_runbook["total_receipts"] == recover_parsed["total_receipts"]
        assert len(inspect_runbook["failed_targets"]) == len(
            recover_parsed["failed_targets"]
        )
        assert (
            inspect_runbook["failure_classification"]
            == recover_parsed["failure_classification"]
        )

    def test_recovery_no_failures(
        self,
        db_inspect_sqlite: str,
    ) -> None:
        """--recovery on an event with only successful receipts produces empty failed_targets."""
        output = _run_cli(
            "inspect",
            "event",
            "evt-inspect-1",
            "--storage-path",
            db_inspect_sqlite,
            "--recovery",
        )
        parsed = json.loads(output)
        runbook = parsed["recovery"]
        assert runbook["failed_targets"] == []

    def test_recovery_json_deterministic(
        self,
        db_inspect_failed: str,
    ) -> None:
        """Recovery output has sorted keys."""
        output = _run_cli(
            "inspect",
            "event",
            "evt-inspect-fail-1",
            "--storage-path",
            db_inspect_failed,
            "--recovery",
        )
        parsed = json.loads(output)
        keys = list(parsed.keys())
        assert keys == sorted(keys)


# ---------------------------------------------------------------------------
# inspect event — combined flags
# ---------------------------------------------------------------------------


class TestInspectEventCombinedFlags:
    """Tests for combining multiple inspect event flags."""

    def test_timeline_and_evidence(
        self,
        db_inspect_sqlite: str,
    ) -> None:
        """--timeline --evidence produces both sections."""
        output = _run_cli(
            "inspect",
            "event",
            "evt-inspect-1",
            "--storage-path",
            db_inspect_sqlite,
            "--timeline",
            "--evidence",
        )
        parsed = json.loads(output)
        assert "event" in parsed
        assert "timeline" in parsed
        assert "evidence" in parsed
        assert "recovery" not in parsed

    def test_all_three_flags(
        self,
        db_inspect_failed: str,
    ) -> None:
        """--timeline --evidence --recovery produces all sections."""
        output = _run_cli(
            "inspect",
            "event",
            "evt-inspect-fail-1",
            "--storage-path",
            db_inspect_failed,
            "--timeline",
            "--evidence",
            "--recovery",
        )
        parsed = json.loads(output)
        assert "event" in parsed
        assert "timeline" in parsed
        assert "evidence" in parsed
        assert "recovery" in parsed

    def test_combined_json_deterministic(
        self,
        db_inspect_failed: str,
    ) -> None:
        """Combined flags output has sorted keys."""
        output = _run_cli(
            "inspect",
            "event",
            "evt-inspect-fail-1",
            "--storage-path",
            db_inspect_failed,
            "--timeline",
            "--evidence",
            "--recovery",
        )
        parsed = json.loads(output)
        keys = list(parsed.keys())
        assert keys == sorted(keys)


# ---------------------------------------------------------------------------
# inspect replay
# ---------------------------------------------------------------------------


class TestInspectReplay:
    """Tests for 'medre inspect replay' subcommand."""

    def test_replay_found_returns_json(
        self,
        db_inspect_replay_for_inspect: str,
    ) -> None:
        """inspect replay returns JSON replay timeline."""
        output = _run_cli(
            "inspect",
            "replay",
            "run-inspect-99",
            "--storage-path",
            db_inspect_replay_for_inspect,
        )
        parsed = json.loads(output)
        assert isinstance(parsed, dict)
        assert parsed["run_id"] == "run-inspect-99"
        assert parsed["status"] == "complete"
        assert parsed["receipt_count"] == 1

    def test_replay_matches_trace_replay(
        self,
        db_inspect_replay_for_inspect: str,
    ) -> None:
        """inspect replay output is semantically identical to trace replay --json."""
        inspect_output = _run_cli(
            "inspect",
            "replay",
            "run-inspect-99",
            "--storage-path",
            db_inspect_replay_for_inspect,
        )
        inspect_parsed = json.loads(inspect_output)

        trace_output = _run_cli(
            "trace",
            "replay",
            "run-inspect-99",
            "--storage-path",
            db_inspect_replay_for_inspect,
            "--json",
        )
        trace_parsed = json.loads(trace_output)

        assert inspect_parsed["run_id"] == trace_parsed["run_id"]
        assert inspect_parsed["status"] == trace_parsed["status"]
        assert inspect_parsed["receipt_count"] == trace_parsed["receipt_count"]
        inspect_types = [e["entry_type"] for e in inspect_parsed["timeline"]]
        trace_types = [e["entry_type"] for e in trace_parsed["timeline"]]
        assert inspect_types == trace_types

    def test_replay_not_found(
        self,
        db_inspect_sqlite: str,
    ) -> None:
        """inspect replay with unknown run_id exits EXIT_NOT_FOUND."""
        from medre.cli import EXIT_NOT_FOUND

        with pytest.raises(SystemExit) as exc_info:
            _run_cli(
                "inspect",
                "replay",
                "nonexistent-run",
                "--storage-path",
                db_inspect_sqlite,
            )
        assert exc_info.value.code == EXIT_NOT_FOUND

    def test_replay_not_found_stderr(
        self,
        db_inspect_sqlite: str,
    ) -> None:
        """Error message mentions the missing run."""
        _stdout, stderr = _run_cli_both(
            "inspect",
            "replay",
            "nonexistent-run",
            "--storage-path",
            db_inspect_sqlite,
        )
        assert "no receipts found" in stderr
        assert "nonexistent-run" in stderr

    def test_replay_json_deterministic(
        self,
        db_inspect_replay_for_inspect: str,
    ) -> None:
        """JSON output has sorted keys."""
        output = _run_cli(
            "inspect",
            "replay",
            "run-inspect-99",
            "--storage-path",
            db_inspect_replay_for_inspect,
        )
        parsed = json.loads(output)
        keys = list(parsed.keys())
        assert keys == sorted(keys)

    def test_replay_requires_run_id(self) -> None:
        """inspect replay requires a run_id argument."""
        with pytest.raises(SystemExit):
            main(["inspect", "replay", "--storage-path", "/nonexistent"])


# ---------------------------------------------------------------------------
# inspect — augmented with --storage-path
# ---------------------------------------------------------------------------


class TestInspectAugmentedStoragePath:
    """Verify --storage-path works with augmented inspect commands."""

    def test_timeline_with_storage_path(
        self,
        db_inspect_sqlite: str,
    ) -> None:
        """--timeline works with --storage-path."""
        output = _run_cli(
            "inspect",
            "event",
            "evt-inspect-1",
            "--storage-path",
            db_inspect_sqlite,
            "--timeline",
        )
        parsed = json.loads(output)
        assert "event" in parsed
        assert "timeline" in parsed

    def test_replay_with_storage_path(
        self,
        db_inspect_replay_for_inspect: str,
    ) -> None:
        """inspect replay works with --storage-path."""
        output = _run_cli(
            "inspect",
            "replay",
            "run-inspect-99",
            "--storage-path",
            db_inspect_replay_for_inspect,
        )
        parsed = json.loads(output)
        assert parsed["run_id"] == "run-inspect-99"

    def test_recovery_with_storage_path(
        self,
        db_inspect_failed: str,
    ) -> None:
        """--recovery works with --storage-path."""
        output = _run_cli(
            "inspect",
            "event",
            "evt-inspect-fail-1",
            "--storage-path",
            db_inspect_failed,
            "--recovery",
        )
        parsed = json.loads(output)
        assert "recovery" in parsed
        assert len(parsed["recovery"]["failed_targets"]) == 1
