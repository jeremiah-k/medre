"""Alpha walkthrough CLI command-surface tests.

End-to-end tests for every documented CLI command in the alpha walkthrough.
Every test calls ``main([...])`` — the same entry point operators use —
proving the CLI command surface works without importing internal APIs.

Walkthrough sequence (as documented in alpha-walkthrough.md):
1. ``medre smoke --config <path> --storage-path <db> --json``
2. ``medre inspect receipts --event <id> --storage-path <db>``
3. ``medre trace event <id> --storage-path <db> --json``
4. ``medre evidence --event <id> --storage-path <db> --json``
5. ``medre replay --config <path> --mode dry_run --event <id> --json``
6. ``medre replay --config <path> --mode best_effort --event <id> --json``

Read-only commands (inspect, trace, evidence) use ``--storage-path`` to
bypass config-file loading. Replay requires ``--config`` for route/adapter
resolution.

For function-level smoke tests, see test_alpha_walkthrough.py.
For runtime-level replay/retry tests, see
test_alpha_walkthrough_runtime_retry_replay.py.
For replay CLI surface tests, see test_cli_replay_surface.py.
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from medre.cli import main


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _smoke_config_path() -> str:
    """Return path to the shipped fake-bridge-smoke.toml."""
    from medre.runtime.smoke import _default_smoke_config_path

    path = _default_smoke_config_path()
    assert path is not None, "examples/configs/fake-bridge-smoke.toml not found"
    return path


# TOML config with SQLite storage for replay tests.
_REPLAY_TOML = """\
[runtime]
name = "alpha-replay-walkthrough"
shutdown_timeout_seconds = 10

[logging]
level = "WARNING"
format = "text"

[storage]
backend = "sqlite"
path = {storage_path!r}

[adapters.matrix.fake_matrix]
enabled = true
adapter_kind = "fake"
homeserver = "https://fake.local"
user_id = "@bot:fake.local"
access_token = "fake"
room_allowlist = ["!room:fake.local"]
encryption_mode = "plaintext"

[adapters.meshtastic.fake_meshtastic]
enabled = true
adapter_kind = "fake"
connection_type = "fake"
meshnet_name = "alpha-walkthrough"

[routes.mx_to_mesh]
source_adapters = ["fake_matrix"]
dest_adapters = ["fake_meshtastic"]
directionality = "source_to_dest"
enabled = true
"""


def _write_replay_config(tmp_path: Path, db_path: Path) -> str:
    """Write a TOML config that points storage at *db_path* for replay."""
    cfg = tmp_path / "replay_config.toml"
    cfg.write_text(_REPLAY_TOML.format(storage_path=str(db_path)))
    return str(cfg)


def _seed_via_smoke_cli(tmp_path: Path) -> tuple[str, Path]:
    """Run ``main(["smoke", ...])`` to create a populated DB.

    Returns (event_id, db_path).
    """
    db_path = tmp_path / "walkthrough.db"
    config_path = _smoke_config_path()

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
        with pytest.raises(SystemExit) as exc_info:
            main([
                "smoke",
                "--config", config_path,
                "--storage-path", str(db_path),
                "--json",
            ])
    assert exc_info.value.code == 0, (
        f"Smoke seed failed (exit={exc_info.value.code}): "
        f"stderr={stderr_buf.getvalue()}"
    )
    report = json.loads(stdout_buf.getvalue())
    assert report["status"] == "passed", (
        f"Smoke report not passed: {report.get('fail_reasons', [])}"
    )
    event_id = report["event_id"]
    assert isinstance(event_id, str) and len(event_id) > 0

    return event_id, db_path


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


# ---------------------------------------------------------------------------
# Tests: smoke seeds DB
# ---------------------------------------------------------------------------


class TestAlphaSmokeSeedsCLI:
    """``medre smoke --config ... --storage-path ... --json`` via main()."""

    def test_smoke_json_creates_persistent_db(self, tmp_path: Path) -> None:
        """Smoke with --storage-path creates a SQLite file."""
        db_path = tmp_path / "smoke_seed.db"

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            with pytest.raises(SystemExit) as exc_info:
                main([
                    "smoke",
                    "--config", _smoke_config_path(),
                    "--storage-path", str(db_path),
                    "--json",
                ])
        assert exc_info.value.code == 0
        assert db_path.exists(), "SQLite DB should exist after smoke"

        report = json.loads(stdout_buf.getvalue())
        assert report["status"] == "passed"
        assert report["storage_path"] == str(db_path)

    def test_smoke_json_event_id_present(self, tmp_path: Path) -> None:
        """Smoke --json report has a non-empty event_id."""
        db_path = tmp_path / "seed_evt.db"

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            with pytest.raises(SystemExit) as exc_info:
                main([
                    "smoke",
                    "--config", _smoke_config_path(),
                    "--storage-path", str(db_path),
                    "--json",
                ])
        assert exc_info.value.code == 0
        report = json.loads(stdout_buf.getvalue())
        assert isinstance(report["event_id"], str)
        assert len(report["event_id"]) > 0


# ---------------------------------------------------------------------------
# Tests: inspect receipts with --storage-path
# ---------------------------------------------------------------------------


class TestAlphaInspectReceiptsCLI:
    """``medre inspect receipts --event <id> --storage-path <db>`` via main()."""

    def test_inspect_receipts_lists_receipts(self, tmp_path: Path) -> None:
        """inspect receipts --storage-path prints delivery receipts."""
        event_id, db_path = _seed_via_smoke_cli(tmp_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main([
                "inspect", "receipts",
                "--event", event_id,
                "--storage-path", str(db_path),
            ])

        output = stdout_buf.getvalue()
        assert "sent" in output

    def test_inspect_receipts_exits_cleanly(self, tmp_path: Path) -> None:
        """inspect receipts does not call sys.exit on success."""
        event_id, db_path = _seed_via_smoke_cli(tmp_path)

        # Should NOT raise SystemExit.
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            main([
                "inspect", "receipts",
                "--event", event_id,
                "--storage-path", str(db_path),
            ])


# ---------------------------------------------------------------------------
# Tests: trace event with --storage-path
# ---------------------------------------------------------------------------


class TestAlphaTraceEventCLI:
    """``medre trace event <id> --storage-path <db> --json`` via main()."""

    def test_trace_event_json_timeline(self, tmp_path: Path) -> None:
        """trace event --storage-path --json returns a JSON timeline."""
        event_id, db_path = _seed_via_smoke_cli(tmp_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main([
                "trace", "event",
                event_id,
                "--storage-path", str(db_path),
                "--json",
            ])

        timeline = json.loads(stdout_buf.getvalue())
        assert isinstance(timeline, list)
        assert len(timeline) >= 1

    def test_trace_event_json_has_receipt_entries(self, tmp_path: Path) -> None:
        """Timeline includes at least one receipt entry."""
        event_id, db_path = _seed_via_smoke_cli(tmp_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main([
                "trace", "event",
                event_id,
                "--storage-path", str(db_path),
                "--json",
            ])

        timeline = json.loads(stdout_buf.getvalue())
        entry_types = [e.get("entry_type") for e in timeline]
        assert "receipt" in entry_types, (
            f"Expected 'receipt' in timeline entry types, got: {entry_types}"
        )

    def test_trace_event_human_readable(self, tmp_path: Path) -> None:
        """trace event (no --json) prints human-readable timeline."""
        event_id, db_path = _seed_via_smoke_cli(tmp_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main([
                "trace", "event",
                event_id,
                "--storage-path", str(db_path),
            ])

        output = stdout_buf.getvalue()
        assert event_id in output
        assert "Timeline" in output
        assert "Summary" in output


# ---------------------------------------------------------------------------
# Tests: evidence with --storage-path
# ---------------------------------------------------------------------------


class TestAlphaEvidenceCLI:
    """``medre evidence --event <id> --storage-path <db> --json`` via main()."""

    def test_evidence_json_bundle(self, tmp_path: Path) -> None:
        """evidence --storage-path --json returns a valid evidence bundle."""
        event_id, db_path = _seed_via_smoke_cli(tmp_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main([
                "evidence",
                "--event", event_id,
                "--storage-path", str(db_path),
                "--json",
            ])

        bundle = json.loads(stdout_buf.getvalue())
        assert "status" in bundle
        assert bundle["status"] in ("ok", "partial", "passed")
        assert "sections" in bundle

    def test_evidence_storage_section_has_event(self, tmp_path: Path) -> None:
        """Evidence storage section contains the requested event."""
        event_id, db_path = _seed_via_smoke_cli(tmp_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main([
                "evidence",
                "--event", event_id,
                "--storage-path", str(db_path),
                "--json",
            ])

        bundle = json.loads(stdout_buf.getvalue())
        storage = bundle["sections"]["storage"]
        assert storage["data"]["event"] is not None
        assert storage["data"]["event"]["event_id"] == event_id

    def test_evidence_human_readable(self, tmp_path: Path) -> None:
        """evidence (no --json) prints human-readable summary."""
        event_id, db_path = _seed_via_smoke_cli(tmp_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main([
                "evidence",
                "--event", event_id,
                "--storage-path", str(db_path),
            ])

        output = stdout_buf.getvalue()
        assert "Evidence:" in output


# ---------------------------------------------------------------------------
# Tests: replay dry_run (config required)
# ---------------------------------------------------------------------------


class TestAlphaReplayDryRunCLI:
    """``medre replay --config <cfg> --mode dry_run --event <id> --json``."""

    def test_dry_run_exits_cleanly(self, tmp_path: Path) -> None:
        """DRY_RUN --json exits without error and returns valid JSON."""
        event_id, db_path = _seed_via_smoke_cli(tmp_path)
        config_path = _write_replay_config(tmp_path, db_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main([
                "replay",
                "--config", config_path,
                "--mode", "dry_run",
                "--event", event_id,
                "--json",
            ])

        summary = json.loads(stdout_buf.getvalue())
        assert summary["mode"] == "dry_run"
        assert summary["events_scanned"] >= 1
        assert summary["events_replayed"] >= 1

    def test_dry_run_no_side_effects(self, tmp_path: Path) -> None:
        """DRY_RUN does not create replay receipts."""
        import asyncio
        from medre.core.storage.sqlite import SQLiteStorage

        event_id, db_path = _seed_via_smoke_cli(tmp_path)
        config_path = _write_replay_config(tmp_path, db_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main([
                "replay",
                "--config", config_path,
                "--mode", "dry_run",
                "--event", event_id,
                "--json",
            ])

        async def _check() -> None:
            storage = SQLiteStorage(db_path=str(db_path))
            await storage.initialize()
            try:
                receipts = await storage.list_receipts_for_event(event_id)
                replay_receipts = [r for r in receipts if r.source == "replay"]
                assert len(replay_receipts) == 0, (
                    f"DRY_RUN should not create replay receipts, "
                    f"got {len(replay_receipts)}"
                )
            finally:
                await storage.close()

        asyncio.run(_check())


# ---------------------------------------------------------------------------
# Tests: replay best_effort (config required)
# ---------------------------------------------------------------------------


class TestAlphaReplayBestEffortCLI:
    """``medre replay --config <cfg> --mode best_effort --event <id> --json``."""

    def test_best_effort_exits_cleanly(self, tmp_path: Path) -> None:
        """BEST_EFFORT --json exits without error."""
        event_id, db_path = _seed_via_smoke_cli(tmp_path)
        config_path = _write_replay_config(tmp_path, db_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main([
                "replay",
                "--config", config_path,
                "--mode", "best_effort",
                "--event", event_id,
                "--json",
            ])

        summary = json.loads(stdout_buf.getvalue())
        assert summary["mode"] == "best_effort"

    def test_best_effort_creates_replay_receipts(self, tmp_path: Path) -> None:
        """BEST_EFFORT replay creates receipts with source='replay'."""
        import asyncio
        from medre.core.storage.sqlite import SQLiteStorage

        event_id, db_path = _seed_via_smoke_cli(tmp_path)
        config_path = _write_replay_config(tmp_path, db_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main([
                "replay",
                "--config", config_path,
                "--mode", "best_effort",
                "--event", event_id,
                "--json",
            ])

        async def _check() -> None:
            storage = SQLiteStorage(db_path=str(db_path))
            await storage.initialize()
            try:
                receipts = await storage.list_receipts_for_event(event_id)
                replay_receipts = [r for r in receipts if r.source == "replay"]
                assert len(replay_receipts) >= 1, (
                    f"Expected >= 1 replay receipt, got {len(replay_receipts)}"
                )
            finally:
                await storage.close()

        asyncio.run(_check())


# ---------------------------------------------------------------------------
# Test: full walkthrough sequence
# ---------------------------------------------------------------------------


class TestAlphaFullWalkthroughCLI:
    """Full alpha walkthrough: smoke → inspect → trace → evidence → replay via main()."""

    def test_full_walkthrough_sequence(self, tmp_path: Path) -> None:
        """Prove the documented operator walkthrough sequence works via main().

        Steps:
        1. medre smoke --config <path> --storage-path <db> --json  → event_id
        2. medre inspect receipts --event <id> --storage-path <db>
        3. medre trace event <id> --storage-path <db> --json
        4. medre evidence --event <id> --storage-path <db> --json
        5. medre replay --config <path> --mode dry_run --event <id> --json
        6. medre replay --config <path> --mode best_effort --event <id> --json
        """
        config_path = _smoke_config_path()

        # Step 1: Smoke seeds persistent DB
        db_path = tmp_path / "full_walkthrough.db"
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            with pytest.raises(SystemExit) as exc_info:
                main([
                    "smoke",
                    "--config", config_path,
                    "--storage-path", str(db_path),
                    "--json",
                ])
        assert exc_info.value.code == 0
        report = json.loads(stdout_buf.getvalue())
        assert report["status"] == "passed"
        event_id = report["event_id"]

        # Step 2: Inspect receipts
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main([
                "inspect", "receipts",
                "--event", event_id,
                "--storage-path", str(db_path),
            ])
        assert "sent" in stdout_buf.getvalue()

        # Step 3: Trace event
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main([
                "trace", "event",
                event_id,
                "--storage-path", str(db_path),
                "--json",
            ])
        timeline = json.loads(stdout_buf.getvalue())
        assert len(timeline) >= 1
        entry_types = [e.get("entry_type") for e in timeline]
        assert "receipt" in entry_types

        # Step 4: Evidence
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main([
                "evidence",
                "--event", event_id,
                "--storage-path", str(db_path),
                "--json",
            ])
        bundle = json.loads(stdout_buf.getvalue())
        assert bundle["status"] in ("ok", "partial", "passed")

        # Steps 5-6: Replay uses config with SQLite pointing at the same DB
        replay_config = _write_replay_config(tmp_path, db_path)

        # Step 5: Replay dry_run
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main([
                "replay",
                "--config", replay_config,
                "--mode", "dry_run",
                "--event", event_id,
                "--json",
            ])
        dry_summary = json.loads(stdout_buf.getvalue())
        assert dry_summary["mode"] == "dry_run"
        assert dry_summary["events_scanned"] >= 1

        # Step 6: Replay best_effort
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main([
                "replay",
                "--config", replay_config,
                "--mode", "best_effort",
                "--event", event_id,
                "--json",
            ])
        be_summary = json.loads(stdout_buf.getvalue())
        assert be_summary["mode"] == "best_effort"

    def test_event_id_flows_through_all_commands(self, tmp_path: Path) -> None:
        """Verify the exact event_id from smoke appears in every downstream command."""
        config_path = _smoke_config_path()

        # Seed
        db_path = tmp_path / "event_flow.db"
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            with pytest.raises(SystemExit) as exc_info:
                main([
                    "smoke",
                    "--config", config_path,
                    "--storage-path", str(db_path),
                    "--json",
                ])
        assert exc_info.value.code == 0
        event_id = json.loads(stdout_buf.getvalue())["event_id"]

        # Inspect receipts: event_id in the receipt output
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main([
                "inspect", "receipts",
                "--event", event_id,
                "--storage-path", str(db_path),
            ])
        assert event_id in stdout_buf.getvalue()

        # Trace: timeline entries reference the event
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main([
                "trace", "event",
                event_id,
                "--storage-path", str(db_path),
                "--json",
            ])
        timeline = json.loads(stdout_buf.getvalue())
        assert len(timeline) >= 1

        # Evidence: storage section has the event
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main([
                "evidence",
                "--event", event_id,
                "--storage-path", str(db_path),
                "--json",
            ])
        bundle = json.loads(stdout_buf.getvalue())
        assert bundle["sections"]["storage"]["data"]["event"]["event_id"] == event_id

        # Replay dry_run: summary includes the event
        replay_config = _write_replay_config(tmp_path, db_path)
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main([
                "replay",
                "--config", replay_config,
                "--mode", "dry_run",
                "--event", event_id,
                "--json",
            ])
        dry_summary = json.loads(stdout_buf.getvalue())
        assert dry_summary["events_replayed"] >= 1


# ---------------------------------------------------------------------------
# Test: no tracebacks on invalid inputs
# ---------------------------------------------------------------------------


class TestAlphaNoTracebacks:
    """Verify commands produce clean errors, not tracebacks."""

    def test_inspect_receipts_missing_storage_path_and_config(self) -> None:
        """inspect receipts without --storage-path or --config exits cleanly."""
        with pytest.raises(SystemExit):
            main(["inspect", "receipts", "--event", "nonexistent"])

    def test_trace_event_missing_storage_path_and_config(self) -> None:
        """trace event without --storage-path or --config exits cleanly."""
        with pytest.raises(SystemExit):
            main(["trace", "event", "nonexistent", "--json"])

    def test_replay_rejects_storage_path(self, tmp_path: Path) -> None:
        """replay --storage-path exits with an error message."""
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            with pytest.raises(SystemExit):
                main([
                    "replay",
                    "--config", _smoke_config_path(),
                    "--mode", "dry_run",
                    "--event", "evt-1",
                    "--storage-path", str(tmp_path / "test.db"),
                ])
        assert "not supported for replay" in stderr_buf.getvalue()
