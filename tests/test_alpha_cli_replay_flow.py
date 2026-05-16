"""Alpha CLI tests: replay flow (dry_run, best_effort, full walkthrough).

Split from the original test_alpha_walkthrough_cli.py monolith.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from medre.cli import main

from tests.helpers.alpha_cli import (
    clean_path_env,
    seed_via_smoke_cli,
    smoke_config_path,
    write_replay_config,
)


# ---------------------------------------------------------------------------
# Tests: replay dry_run (config required)
# ---------------------------------------------------------------------------


class TestAlphaReplayDryRunCLI:
    """``medre replay --config <cfg> --mode dry_run --event <id> --json``."""

    def test_dry_run_exits_cleanly(self, tmp_path: Path) -> None:
        """DRY_RUN --json exits without error and returns valid JSON."""
        event_id, db_path = seed_via_smoke_cli(tmp_path)
        config_path = write_replay_config(tmp_path, db_path)

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

        event_id, db_path = seed_via_smoke_cli(tmp_path)
        config_path = write_replay_config(tmp_path, db_path)

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
        event_id, db_path = seed_via_smoke_cli(tmp_path)
        config_path = write_replay_config(tmp_path, db_path)

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

        event_id, db_path = seed_via_smoke_cli(tmp_path)
        config_path = write_replay_config(tmp_path, db_path)

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
    """Full alpha walkthrough: smoke → inspect → inspect flags → replay via main()."""

    def test_full_walkthrough_sequence(self, tmp_path: Path) -> None:
        """Prove the documented operator walkthrough sequence works via main().

        Phases (as documented in alpha-walkthrough.md):
        Phase 1: medre smoke --config <path> --storage-path <db> --json  → event_id
        Phase 2: medre inspect receipts --event <id> --storage-path <db>  (inspect-first)
        Phase 3: medre inspect event <id> --timeline --storage-path <db>  (deeper investigation)
                 medre inspect event <id> --evidence --storage-path <db>
        Phase 4: medre replay --config <path> --mode dry_run --event <id> --json    (lower-level)
                 medre replay --config <path> --mode best_effort --event <id> --json
        """
        config_path = smoke_config_path()

        # Phase 1: Optional local smoke seeds persistent DB
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

        # Phase 2: Inspect-first — check delivery receipts
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main([
                "inspect", "receipts",
                "--event", event_id,
                "--storage-path", str(db_path),
            ])
        assert "sent" in stdout_buf.getvalue()

        # Phase 3a: Deeper investigation — inspect event --timeline
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main([
                "inspect", "event",
                event_id,
                "--timeline",
                "--storage-path", str(db_path),
            ])
        result = json.loads(stdout_buf.getvalue())
        assert "event" in result
        assert "timeline" in result
        assert len(result["timeline"]) >= 1
        entry_types = [e.get("entry_type") for e in result["timeline"]]
        assert "receipt" in entry_types

        # Phase 3b: Deeper investigation — inspect event --evidence
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main([
                "inspect", "event",
                event_id,
                "--evidence",
                "--storage-path", str(db_path),
            ])
        result = json.loads(stdout_buf.getvalue())
        assert result["evidence"]["status"] in ("partial", "passed")

        # Phases 4: Replay uses config with SQLite pointing at the same DB
        replay_config = write_replay_config(tmp_path, db_path)

        # Phase 4a: Replay dry_run (lower-level, specialized)
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

        # Phase 4b: Replay best_effort (lower-level, specialized)
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
        """Verify the exact event_id from smoke appears in every downstream command (inspect-first path)."""
        config_path = smoke_config_path()

        # Phase 1: Seed via optional local smoke
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

        # Phase 2: Inspect-first — receipts
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main([
                "inspect", "receipts",
                "--event", event_id,
                "--storage-path", str(db_path),
            ])
        assert event_id in stdout_buf.getvalue()

        # Phase 3a: Deeper investigation — inspect event --timeline
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main([
                "inspect", "event",
                event_id,
                "--timeline",
                "--storage-path", str(db_path),
            ])
        result = json.loads(stdout_buf.getvalue())
        assert result["event"]["event_id"] == event_id
        assert len(result["timeline"]) >= 1

        # Phase 3b: Deeper investigation — inspect event --evidence
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main([
                "inspect", "event",
                event_id,
                "--evidence",
                "--storage-path", str(db_path),
            ])
        result = json.loads(stdout_buf.getvalue())
        assert (
            result["evidence"]["sections"]["storage"]["data"]["event"]["event_id"]
            == event_id
        )

        # Phase 4: Replay dry_run (lower-level, specialized)
        replay_config = write_replay_config(tmp_path, db_path)
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
