"""Tests for --storage-path support on inspect, trace, and evidence commands.

Verifies that operators can run read-only commands directly against a SQLite
database file without needing a config file.  Covers:

- ``medre inspect event --storage-path <db> <event_id>``
- ``medre inspect receipts --storage-path <db> --event <id>``
- ``medre inspect native-ref --storage-path <db> --adapter A --message M``
- ``medre trace event --storage-path <db> <event_id>``
- ``medre trace replay --storage-path <db> <run_id>``
- ``medre evidence --storage-path <db>``

Plus negative cases: missing DB, invalid DB shape, mutually exclusive flags.
"""

from __future__ import annotations

import io
import json
import sqlite3
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from medre.cli import main
from medre.cli.exit_codes import EXIT_BUILD, EXIT_NOT_FOUND
from medre.core.events import (
    CanonicalEvent,
    DeliveryReceipt,
    EventMetadata,
    NativeMessageRef,
)
from medre.core.storage.sqlite.storage import SQLiteStorage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_cli(*args: str) -> tuple[str, str]:
    """Run CLI and return (stdout, stderr) pair. Catches SystemExit."""
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            main(list(args))
    except SystemExit:
        pass
    return stdout.getvalue(), stderr.getvalue()


def _run_cli_exit(*args: str) -> tuple[int, str, str]:
    """Run CLI expecting a SystemExit, returns (exit_code, stdout, stderr)."""
    stdout = io.StringIO()
    stderr = io.StringIO()
    code: int = 0
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            main(list(args))
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    return code, stdout.getvalue(), stderr.getvalue()


def _seed_db(
    db_path: str,
    event_id: str = "evt-sp-1",
    replay_run_id: str | None = None,
) -> None:
    """Synchronously seed a test database with an event, receipt, and native ref."""
    import asyncio

    async def _go() -> None:
        storage = SQLiteStorage(db_path)
        try:
            await storage.initialize()
            ts = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
            event = CanonicalEvent(
                event_id=event_id,
                event_kind="message.created",
                schema_version=1,
                timestamp=ts,
                source_adapter="test_adapter",
                source_transport_id="test-transport",
                source_channel_id="ch-sp",
                parent_event_id=None,
                lineage=(),
                relations=(),
                payload={"text": "storage-path test"},
                metadata=EventMetadata(),
            )
            await storage.append(event)

            await storage.store_native_ref(
                NativeMessageRef(
                    id="nref-sp-1",
                    event_id=event_id,
                    adapter="matrix",
                    native_channel_id="!room:test",
                    native_message_id="$sp-msg-1",
                    native_thread_id=None,
                    native_relation_id=None,
                    direction="outbound",
                    created_at=ts,
                )
            )

            rcpt_kwargs: dict[str, Any] = dict(
                receipt_id="rcpt-sp-1",
                event_id=event_id,
                delivery_plan_id="plan-sp-1",
                target_adapter="dest_adapter",
                status="sent",
                created_at=datetime(2026, 1, 15, 12, 0, 1, tzinfo=timezone.utc),
            )
            if replay_run_id is not None:
                rcpt_kwargs["source"] = "replay"
                rcpt_kwargs["replay_run_id"] = replay_run_id

            await storage.append_receipt(DeliveryReceipt(**rcpt_kwargs))
        finally:
            await storage.close()

    asyncio.run(_go())


def _smoke_config_path() -> str:
    """Return path to the shipped fake-bridge-smoke.yaml."""
    from medre.runtime.smoke import _default_smoke_config_path

    path = _default_smoke_config_path()
    assert path is not None
    return path


def _write_sqlite_smoke_config(tmp_path: Path, db_path: Path) -> str:
    """Write a YAML config with SQLite storage at *db_path* for smoke CLI tests."""
    from tests.helpers.walkthrough import EXAMPLES_SMOKE_CONFIG

    assert EXAMPLES_SMOKE_CONFIG.is_file()
    src = EXAMPLES_SMOKE_CONFIG.read_text()
    assert "backend: memory" in src
    sqlite_block = f"backend: sqlite\n  path: {str(db_path)!r}"
    derived = src.replace("backend: memory", sqlite_block)
    cfg = tmp_path / "smoke_sqlite.yaml"
    cfg.write_text(derived)
    assert "backend: sqlite" in derived
    return str(cfg)


def _seed_via_smoke_cli(tmp_path: Path) -> tuple[str, Path]:
    """Run smoke with SQLite config to create a populated DB.

    Returns (event_id, db_path).
    """
    db_path = tmp_path / "smoke_seed.db"
    cfg = _write_sqlite_smoke_config(tmp_path, db_path)

    stdout_buf = io.StringIO()
    with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    "smoke",
                    "--config",
                    cfg,
                    "--json",
                ]
            )
    assert exc_info.value.code == 0
    report = json.loads(stdout_buf.getvalue())
    assert report["status"] == "passed"
    return report["event_id"], db_path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_path_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "MEDRE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture()
def seeded_db(tmp_path: Path) -> Path:
    """Create and return path to a seeded test database."""
    db_path = tmp_path / "test.db"
    _seed_db(str(db_path))
    return db_path


@pytest.fixture()
def seeded_db_with_replay(tmp_path: Path) -> Path:
    """Create and return path to a test database with replay receipts."""
    db_path = tmp_path / "replay.db"
    _seed_db(str(db_path), event_id="evt-replay-sp", replay_run_id="run-sp-42")
    return db_path


# ---------------------------------------------------------------------------
# Tests: inspect event --storage-path
# ---------------------------------------------------------------------------


class TestInspectEventStoragePath:
    """``medre inspect event --storage-path <db> <event_id>``."""

    def test_inspect_event_returns_event_data(self, seeded_db: Path) -> None:
        """inspect event --storage-path prints the stored event."""
        stdout, stderr = _run_cli(
            "inspect",
            "event",
            "--storage-path",
            str(seeded_db),
            "evt-sp-1",
        )
        assert "evt-sp-1" in stdout
        assert "test_adapter" in stdout
        assert stderr == ""

    def test_inspect_event_not_found(self, seeded_db: Path) -> None:
        """inspect event with unknown ID exits EXIT_NOT_FOUND."""
        code, _, stderr = _run_cli_exit(
            "inspect",
            "event",
            "--storage-path",
            str(seeded_db),
            "nonexistent",
        )
        assert code == EXIT_NOT_FOUND

    def test_inspect_event_missing_db(self, tmp_path: Path) -> None:
        """inspect event with non-existent DB exits with error and clear stderr."""
        missing = tmp_path / "missing.db"
        code, _, stderr = _run_cli_exit(
            "inspect",
            "event",
            "--storage-path",
            str(missing),
            "evt-1",
        )
        assert code == EXIT_BUILD
        assert "storage error" in stderr.lower()
        assert "does not exist" in stderr.lower()

    def test_inspect_event_invalid_db(self, tmp_path: Path) -> None:
        """inspect event with a corrupt file exits with actionable error."""
        bad_db = tmp_path / "corrupt.db"
        bad_db.write_text("not a sqlite database")
        code, _, stderr = _run_cli_exit(
            "inspect",
            "event",
            "--storage-path",
            str(bad_db),
            "evt-1",
        )
        assert code == EXIT_BUILD
        assert "storage error" in stderr.lower()
        assert "uninitialised" in stderr.lower() or "schema" in stderr.lower()

    def test_inspect_event_config_not_accepted(self) -> None:
        """--config is not accepted by inspect (uses --storage-path only)."""
        code, _, stderr = _run_cli_exit(
            "inspect",
            "event",
            "--config",
            _smoke_config_path(),
            "--storage-path",
            "/dev/null",
            "evt-1",
        )
        assert code != 0
        assert "unrecognized" in stderr.lower()

    def test_inspect_event_from_smoke_db(self, tmp_path: Path) -> None:
        """inspect event works against a DB created by smoke."""
        event_id, db_path = _seed_via_smoke_cli(tmp_path)

        stdout, stderr = _run_cli(
            "inspect",
            "event",
            "--storage-path",
            str(db_path),
            event_id,
        )
        assert event_id in stdout
        assert stderr == ""

    def test_inspect_event_no_create_db(self, tmp_path: Path) -> None:
        """--storage-path does not create a missing database file."""
        missing = str(tmp_path / "never_created.db")
        _run_cli_exit(
            "inspect",
            "event",
            "--storage-path",
            missing,
            "evt-1",
        )
        assert not Path(missing).exists(), "DB file must not be created"


# ---------------------------------------------------------------------------
# Tests: inspect receipts --storage-path
# ---------------------------------------------------------------------------


class TestInspectReceiptsStoragePath:
    """``medre inspect receipts --storage-path <db> --event <id>``."""

    def test_inspect_receipts_returns_receipts(self, seeded_db: Path) -> None:
        stdout, stderr = _run_cli(
            "inspect",
            "receipts",
            "--storage-path",
            str(seeded_db),
            "--event",
            "evt-sp-1",
        )
        assert "sent" in stdout
        assert stderr == ""

    def test_inspect_receipts_missing_db(self, tmp_path: Path) -> None:
        missing = tmp_path / "missing.db"
        code, _, stderr = _run_cli_exit(
            "inspect",
            "receipts",
            "--storage-path",
            str(missing),
            "--event",
            "evt-1",
        )
        assert code == EXIT_BUILD
        assert "storage error" in stderr.lower()
        assert "does not exist" in stderr.lower()

    def test_inspect_receipts_no_create_db(self, tmp_path: Path) -> None:
        """--storage-path does not create a missing database file."""
        missing = str(tmp_path / "never_created.db")
        _run_cli_exit(
            "inspect",
            "receipts",
            "--storage-path",
            missing,
            "--event",
            "evt-1",
        )
        assert not Path(missing).exists(), "DB file must not be created"

    def test_inspect_receipts_from_smoke_db(self, tmp_path: Path) -> None:
        event_id, db_path = _seed_via_smoke_cli(tmp_path)

        stdout, stderr = _run_cli(
            "inspect",
            "receipts",
            "--storage-path",
            str(db_path),
            "--event",
            event_id,
        )
        assert "sent" in stdout

    def test_inspect_receipts_config_not_accepted(self, seeded_db: Path) -> None:
        """--config is not accepted by inspect receipts (uses --storage-path only)."""
        code, _, stderr = _run_cli_exit(
            "inspect",
            "receipts",
            "--config",
            _smoke_config_path(),
            "--storage-path",
            str(seeded_db),
            "--event",
            "evt-1",
        )
        assert code != 0
        assert "unrecognized" in stderr.lower()


# ---------------------------------------------------------------------------
# Tests: inspect native-ref --storage-path
# ---------------------------------------------------------------------------


class TestInspectNativeRefStoragePath:
    """``medre inspect native-ref --storage-path <db> --adapter A --message M``."""

    def test_inspect_native_ref_found(self, seeded_db: Path) -> None:
        stdout, stderr = _run_cli(
            "inspect",
            "native-ref",
            "--storage-path",
            str(seeded_db),
            "--adapter",
            "matrix",
            "--channel",
            "!room:test",
            "--message",
            "$sp-msg-1",
        )
        result = json.loads(stdout)
        assert result["resolves_to"] == "evt-sp-1"

    def test_inspect_native_ref_not_found(self, seeded_db: Path) -> None:
        code, _, _ = _run_cli_exit(
            "inspect",
            "native-ref",
            "--storage-path",
            str(seeded_db),
            "--adapter",
            "matrix",
            "--message",
            "$nonexistent",
        )
        assert code == EXIT_NOT_FOUND

    def test_inspect_native_ref_missing_db(self, tmp_path: Path) -> None:
        """native-ref with non-existent DB exits with error and clear stderr."""
        missing = tmp_path / "missing.db"
        code, _, stderr = _run_cli_exit(
            "inspect",
            "native-ref",
            "--storage-path",
            str(missing),
            "--adapter",
            "matrix",
            "--message",
            "$msg-1",
        )
        assert code == EXIT_BUILD
        assert "storage error" in stderr.lower()
        assert "does not exist" in stderr.lower()

    def test_inspect_native_ref_no_create_db(self, tmp_path: Path) -> None:
        """--storage-path does not create a missing database file."""
        missing = str(tmp_path / "never_created.db")
        _run_cli_exit(
            "inspect",
            "native-ref",
            "--storage-path",
            missing,
            "--adapter",
            "matrix",
            "--message",
            "$msg-1",
        )
        assert not Path(missing).exists(), "DB file must not be created"

    def test_inspect_native_ref_config_not_accepted(self, seeded_db: Path) -> None:
        """--config is not accepted by inspect native-ref (uses --storage-path only)."""
        code, _, stderr = _run_cli_exit(
            "inspect",
            "native-ref",
            "--config",
            _smoke_config_path(),
            "--storage-path",
            str(seeded_db),
            "--adapter",
            "matrix",
            "--message",
            "$msg-1",
        )
        assert code != 0
        assert "unrecognized" in stderr.lower()


# ---------------------------------------------------------------------------
# Tests: inspect replay --storage-path
# ---------------------------------------------------------------------------


class TestInspectReplayStoragePath:
    """``medre inspect replay --storage-path <db> <run_id>``."""

    def test_inspect_replay_missing_db(self, tmp_path: Path) -> None:
        """inspect replay with non-existent DB exits with error and clear stderr."""
        missing = tmp_path / "missing.db"
        code, _, stderr = _run_cli_exit(
            "inspect",
            "replay",
            "--storage-path",
            str(missing),
            "run-1",
        )
        assert code == EXIT_BUILD
        assert "storage error" in stderr.lower()
        assert "does not exist" in stderr.lower()

    def test_inspect_replay_no_create_db(self, tmp_path: Path) -> None:
        """--storage-path does not create a missing database file."""
        missing = str(tmp_path / "never_created.db")
        _run_cli_exit(
            "inspect",
            "replay",
            "--storage-path",
            missing,
            "run-1",
        )
        assert not Path(missing).exists(), "DB file must not be created"

    def test_inspect_replay_config_not_accepted(self, seeded_db: Path) -> None:
        """--config is not accepted by inspect replay (uses --storage-path only)."""
        code, _, stderr = _run_cli_exit(
            "inspect",
            "replay",
            "--config",
            _smoke_config_path(),
            "--storage-path",
            str(seeded_db),
            "run-1",
        )
        assert code != 0
        assert "unrecognized" in stderr.lower()


# ---------------------------------------------------------------------------
# Tests: trace event --storage-path
# ---------------------------------------------------------------------------


class TestTraceEventStoragePath:
    """``medre trace event --storage-path <db> <event_id>``."""

    def test_trace_event_json(self, seeded_db: Path) -> None:
        stdout, stderr = _run_cli(
            "trace",
            "event",
            "--storage-path",
            str(seeded_db),
            "evt-sp-1",
            "--json",
        )
        timeline = json.loads(stdout)
        assert isinstance(timeline, list)
        types = [e["entry_type"] for e in timeline]
        assert "event" in types
        assert "receipt" in types

    def test_trace_event_human_readable(self, seeded_db: Path) -> None:
        stdout, stderr = _run_cli(
            "trace",
            "event",
            "--storage-path",
            str(seeded_db),
            "evt-sp-1",
        )
        assert "Event: evt-sp-1" in stdout
        assert "Timeline" in stdout
        assert "Summary" in stdout

    def test_trace_event_not_found(self, seeded_db: Path) -> None:
        code, _, _ = _run_cli_exit(
            "trace",
            "event",
            "--storage-path",
            str(seeded_db),
            "nonexistent",
        )
        assert code == EXIT_NOT_FOUND

    def test_trace_event_missing_db(self, tmp_path: Path) -> None:
        missing = tmp_path / "missing.db"
        code, _, stderr = _run_cli_exit(
            "trace",
            "event",
            "--storage-path",
            str(missing),
            "evt-1",
        )
        assert code == EXIT_BUILD
        assert "storage error" in stderr.lower()
        assert "does not exist" in stderr.lower()

    def test_trace_event_no_create_db(self, tmp_path: Path) -> None:
        """--storage-path does not create a missing database file."""
        missing = str(tmp_path / "never_created.db")
        _run_cli_exit(
            "trace",
            "event",
            "--storage-path",
            missing,
            "evt-1",
        )
        assert not Path(missing).exists(), "DB file must not be created"

    def test_trace_event_config_not_accepted(self, seeded_db: Path) -> None:
        """--config is not accepted by trace event (uses --storage-path only)."""
        code, _, stderr = _run_cli_exit(
            "trace",
            "event",
            "--config",
            _smoke_config_path(),
            "--storage-path",
            str(seeded_db),
            "evt-1",
        )
        assert code != 0
        assert "unrecognized" in stderr.lower()

    def test_trace_event_from_smoke_db(self, tmp_path: Path) -> None:
        event_id, db_path = _seed_via_smoke_cli(tmp_path)

        stdout, stderr = _run_cli(
            "trace",
            "event",
            "--storage-path",
            str(db_path),
            event_id,
            "--json",
        )
        timeline = json.loads(stdout)
        assert isinstance(timeline, list)
        assert len(timeline) >= 1


# ---------------------------------------------------------------------------
# Tests: trace replay --storage-path
# ---------------------------------------------------------------------------


class TestTraceReplayStoragePath:
    """``medre trace replay --storage-path <db> <run_id>``."""

    def test_trace_replay_json(self, seeded_db_with_replay: Path) -> None:
        stdout, stderr = _run_cli(
            "trace",
            "replay",
            "--storage-path",
            str(seeded_db_with_replay),
            "run-sp-42",
            "--json",
        )
        result = json.loads(stdout)
        assert isinstance(result, dict)
        assert result["run_id"] == "run-sp-42"
        assert result["receipt_count"] == 1

    def test_trace_replay_not_found(self, seeded_db: Path) -> None:
        code, _, _ = _run_cli_exit(
            "trace",
            "replay",
            "--storage-path",
            str(seeded_db),
            "nonexistent-run",
        )
        assert code == EXIT_NOT_FOUND

    def test_trace_replay_missing_db(self, tmp_path: Path) -> None:
        missing = tmp_path / "missing.db"
        code, _, stderr = _run_cli_exit(
            "trace",
            "replay",
            "--storage-path",
            str(missing),
            "run-1",
        )
        assert code == EXIT_BUILD
        assert "storage error" in stderr.lower()
        assert "does not exist" in stderr.lower()

    def test_trace_replay_no_create_db(self, tmp_path: Path) -> None:
        """--storage-path does not create a missing database file."""
        missing = str(tmp_path / "never_created.db")
        _run_cli_exit(
            "trace",
            "replay",
            "--storage-path",
            missing,
            "run-1",
        )
        assert not Path(missing).exists(), "DB file must not be created"

    def test_trace_replay_config_not_accepted(self, seeded_db: Path) -> None:
        """--config is not accepted by trace replay (uses --storage-path only)."""
        code, _, stderr = _run_cli_exit(
            "trace",
            "replay",
            "--config",
            _smoke_config_path(),
            "--storage-path",
            str(seeded_db),
            "run-1",
        )
        assert code != 0
        assert "unrecognized" in stderr.lower()


# ---------------------------------------------------------------------------
# Tests: evidence --storage-path
# ---------------------------------------------------------------------------


class TestEvidenceStoragePath:
    """``medre evidence --storage-path <db>``."""

    def test_evidence_json_bundle(self, seeded_db: Path) -> None:
        stdout, stderr = _run_cli(
            "evidence",
            "--storage-path",
            str(seeded_db),
            "--json",
        )
        bundle = json.loads(stdout)
        assert bundle["status"] in ("passed", "partial")
        assert bundle["config_source"] == "storage_path"
        assert bundle["runtime_started"] is False
        assert "sections" in bundle

        sections = bundle["sections"]
        assert sections["config_summary"]["status"] == "skipped"
        assert sections["route_validation"]["status"] == "skipped"
        assert sections["diagnostics_snapshot"]["status"] == "skipped"
        assert sections["live_health"]["status"] == "skipped"
        assert sections["storage"]["status"] == "passed"

    def test_evidence_storage_section_has_counts(self, seeded_db: Path) -> None:
        stdout, stderr = _run_cli(
            "evidence",
            "--storage-path",
            str(seeded_db),
            "--json",
        )
        bundle = json.loads(stdout)
        storage_data = bundle["sections"]["storage"]["data"]
        assert storage_data["event_count"] == 1
        assert storage_data["receipt_count"] == 1

    def test_evidence_with_event_filter(self, seeded_db: Path) -> None:
        stdout, stderr = _run_cli(
            "evidence",
            "--storage-path",
            str(seeded_db),
            "--json",
            "--event",
            "evt-sp-1",
        )
        bundle = json.loads(stdout)
        storage_data = bundle["sections"]["storage"]["data"]
        assert storage_data["event"] is not None
        assert storage_data["event"]["event_id"] == "evt-sp-1"

    def test_evidence_missing_db(self, tmp_path: Path) -> None:
        missing = tmp_path / "missing.db"
        stdout, stderr = _run_cli(
            "evidence",
            "--storage-path",
            str(missing),
            "--json",
        )
        bundle = json.loads(stdout)
        assert bundle["sections"]["storage"]["status"] == "partial"
        assert bundle["sections"]["storage"]["data"]["db_exists"] is False
        assert "does not exist" in bundle["sections"]["storage"]["error"].lower()
        # Evidence never creates the missing file.
        assert not Path(str(missing)).exists()

    def test_evidence_invalid_db(self, tmp_path: Path) -> None:
        bad_db = tmp_path / "corrupt.db"
        bad_db.write_text("not a sqlite database")
        stdout, stderr = _run_cli(
            "evidence",
            "--storage-path",
            str(bad_db),
            "--json",
        )
        bundle = json.loads(stdout)
        assert bundle["sections"]["storage"]["status"] in ("partial", "error")
        assert bundle["sections"]["storage"]["data"]["db_exists"] is True
        assert (
            "cannot open" in bundle["sections"]["storage"]["error"].lower()
            or "uninitialised" in bundle["sections"]["storage"]["error"].lower()
            or "schema" in bundle["sections"]["storage"]["error"].lower()
        )

    def test_evidence_config_not_accepted(self, seeded_db: Path) -> None:
        """--config and --storage-path are mutually exclusive for evidence."""
        code, _, stderr = _run_cli_exit(
            "evidence",
            "--config",
            _smoke_config_path(),
            "--storage-path",
            str(seeded_db),
        )
        assert code != 0
        assert "unrecognized" in stderr.lower()

    def test_evidence_from_smoke_db(self, tmp_path: Path) -> None:
        event_id, db_path = _seed_via_smoke_cli(tmp_path)

        stdout, stderr = _run_cli(
            "evidence",
            "--storage-path",
            str(db_path),
            "--json",
            "--event",
            event_id,
        )
        bundle = json.loads(stdout)
        assert bundle["status"] in ("passed", "partial")
        assert bundle["sections"]["storage"]["data"]["event"] is not None

    def test_evidence_no_create_db(self, tmp_path: Path) -> None:
        """--storage-path does not create a missing database file."""
        missing = str(tmp_path / "never_created.db")
        _run_cli("evidence", "--storage-path", missing, "--json")
        assert not Path(missing).exists()

    def test_evidence_human_readable(self, seeded_db: Path) -> None:
        stdout, stderr = _run_cli(
            "evidence",
            "--storage-path",
            str(seeded_db),
        )
        assert "Evidence: PASSED" in stdout
        assert "Evidence: ERROR" not in stdout

    def test_evidence_human_readable_sections_show_passed(
        self, seeded_db: Path
    ) -> None:
        """Section markers must map 'passed' to ✓, not fall through to '?'."""
        stdout, _ = _run_cli(
            "evidence",
            "--storage-path",
            str(seeded_db),
        )
        # The storage section should show ✓ for 'passed' status.
        assert "✓ storage: passed" in stdout

    def test_evidence_rejects_refresh_health(self, seeded_db: Path) -> None:
        """--include-refresh-health is no longer accepted by evidence."""
        code, _, stderr = _run_cli_exit(
            "evidence",
            "--storage-path",
            str(seeded_db),
            "--include-refresh-health",
        )
        assert code != 0
        assert "unrecognized" in stderr.lower()


# ---------------------------------------------------------------------------
# Tests: invalid DB shape produces actionable output
# ---------------------------------------------------------------------------


class TestInvalidDBShape:
    """Verify that an invalid DB shape gives actionable error and nonzero exit."""

    def test_empty_file_is_not_valid_db(self, tmp_path: Path) -> None:
        """An empty file is not a valid SQLite database and reports actionable error."""
        bad_db = tmp_path / "empty.db"
        bad_db.write_bytes(b"")
        code, _, stderr = _run_cli_exit(
            "inspect",
            "event",
            "--storage-path",
            str(bad_db),
            "evt-1",
        )
        assert code == EXIT_BUILD
        assert "storage error" in stderr.lower()
        assert "uninitialised" in stderr.lower() or "schema" in stderr.lower()

    def test_wrong_schema_tables(self, tmp_path: Path) -> None:
        """A SQLite file with wrong tables produces actionable error and nonzero exit."""
        bad_db = tmp_path / "wrong_tables.db"
        conn = sqlite3.connect(str(bad_db))
        try:
            conn.execute("CREATE TABLE foo (id INTEGER PRIMARY KEY)")
            conn.commit()
        finally:
            conn.close()

        code, _, stderr = _run_cli_exit(
            "inspect",
            "event",
            "--storage-path",
            str(bad_db),
            "evt-1",
        )
        assert code == EXIT_BUILD
        assert "storage error" in stderr.lower()
        assert "schema" in stderr.lower() or "uninitialised" in stderr.lower()

    def test_no_create_on_invalid_shape(self, tmp_path: Path) -> None:
        """Invalid DB is never modified/created by read-only commands."""
        bad_db = tmp_path / "readonly.db"
        bad_db.write_bytes(b"\x00" * 100)
        original_size = bad_db.stat().st_size

        _run_cli_exit(
            "inspect",
            "event",
            "--storage-path",
            str(bad_db),
            "evt-1",
        )
        assert bad_db.stat().st_size == original_size, "File must not be modified"


# ---------------------------------------------------------------------------
# Tests: full walkthrough with --storage-path (no config file)
# ---------------------------------------------------------------------------


class TestFullWalkthroughStoragePath:
    """Full operator walkthrough: smoke → inspect → trace → evidence
    all using --storage-path instead of --config."""

    def test_full_walkthrough_storage_path(self, tmp_path: Path) -> None:
        """Prove the full walkthrough works with --storage-path and no config."""
        # Step 1: Smoke seeds a persistent DB
        db_path = tmp_path / "walkthrough.db"
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            with pytest.raises(SystemExit) as exc_info:
                main(
                    [
                        "smoke",
                        "--config",
                        _smoke_config_path(),
                        "--storage-path",
                        str(db_path),
                        "--json",
                    ]
                )
        assert exc_info.value.code == 0
        report = json.loads(stdout_buf.getvalue())
        assert report["status"] == "passed"
        event_id = report["event_id"]

        # Step 2: inspect event --storage-path (NO config file)
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "inspect",
                    "event",
                    "--storage-path",
                    str(db_path),
                    event_id,
                ]
            )
        assert event_id in stdout_buf.getvalue()

        # Step 3: inspect receipts --storage-path
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "inspect",
                    "receipts",
                    "--storage-path",
                    str(db_path),
                    "--event",
                    event_id,
                ]
            )
        assert "sent" in stdout_buf.getvalue()

        # Step 4: trace event --storage-path --json
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "trace",
                    "event",
                    "--storage-path",
                    str(db_path),
                    event_id,
                    "--json",
                ]
            )
        timeline = json.loads(stdout_buf.getvalue())
        assert len(timeline) >= 1
        entry_types = [e.get("entry_type") for e in timeline]
        assert "receipt" in entry_types

        # Step 5: evidence --storage-path --json
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "evidence",
                    "--storage-path",
                    str(db_path),
                    "--json",
                    "--event",
                    event_id,
                ]
            )
        bundle = json.loads(stdout_buf.getvalue())
        assert bundle["status"] in ("passed", "partial")
        assert bundle["config_source"] == "storage_path"
        assert bundle["runtime_started"] is False
