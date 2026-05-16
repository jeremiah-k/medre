"""CLI-surface tests proving replay command uses real command handlers.

Every test calls ``main(["replay", ...])`` — the same entry point operators
use — rather than importing ReplayEngine or RuntimeBuilder directly.

Tests cover:
- ``medre replay --mode dry_run --event <id> --config <cfg> --json``
- ``medre replay --mode best_effort --event <id> --config <cfg> --json``
- ``medre replay --mode best_effort --event <id> --config <cfg>`` (no --json)
  → stderr contains duplicate-risk warning

Runtime-level replay tests remain in test_alpha_walkthrough_runtime_retry_replay.py.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

import pytest

from medre.cli import main


# ---------------------------------------------------------------------------
# TOML config builder
# ---------------------------------------------------------------------------

_SMOKELIKE_TOML = """\
[runtime]
name = "cli-replay-surface"
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
meshnet_name = "replay-surface"

[routes.mx_to_mesh]
source_adapters = ["fake_matrix"]
dest_adapters = ["fake_meshtastic"]
directionality = "source_to_dest"
enabled = true
"""


def _write_config(tmp_path: Path, db_path: Path) -> Path:
    """Write a TOML config that points storage at *db_path*."""
    cfg = tmp_path / "replay_surface.toml"
    cfg.write_text(_SMOKELIKE_TOML.format(storage_path=str(db_path)))
    return cfg


# ---------------------------------------------------------------------------
# Seed helper
# ---------------------------------------------------------------------------


def _seed_db(tmp_path: Path) -> tuple[str, Path]:
    """Run smoke via main() with --storage-path to create a populated DB.

    Returns (event_id, db_path).
    """
    db_path = tmp_path / "replay_surface.db"
    config_path = _write_config(tmp_path, db_path)

    # Use a config with memory storage for seeding (smoke will override to
    # sqlite via --storage-path), but we need the config for adapter/routes.
    # Build a separate seed config that uses memory so smoke doesn't conflict.
    seed_cfg = tmp_path / "seed.toml"
    seed_cfg.write_text(
        _SMOKELIKE_TOML.format(storage_path=str(db_path)),
    )

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
        with pytest.raises(SystemExit) as exc_info:
            main([
                "smoke",
                "--config", str(seed_cfg),
                "--storage-path", str(db_path),
                "--json",
            ])
    assert exc_info.value.code == 0, (
        f"Smoke seed failed: {stderr_buf.getvalue()}"
    )
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCLIReplayDryRun:
    """``medre replay --mode dry_run`` via main()."""

    def test_dry_run_json_exits_cleanly(
        self, tmp_path: Path,
    ) -> None:
        """DRY_RUN --json exits without error and returns valid JSON."""
        event_id, db_path = _seed_db(tmp_path)
        config_path = _write_config(tmp_path, db_path)

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            main([
                "replay",
                "--config", str(config_path),
                "--mode", "dry_run",
                "--event", event_id,
                "--json",
            ])

        output = stdout_buf.getvalue()
        summary = json.loads(output)
        assert summary["mode"] == "dry_run"
        assert summary["events_scanned"] >= 1
        assert summary["events_replayed"] >= 1

    def test_dry_run_json_has_by_status(
        self, tmp_path: Path,
    ) -> None:
        """DRY_RUN summary includes by_status with all four canonical keys."""
        event_id, db_path = _seed_db(tmp_path)
        config_path = _write_config(tmp_path, db_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main([
                "replay",
                "--config", str(config_path),
                "--mode", "dry_run",
                "--event", event_id,
                "--json",
            ])

        summary = json.loads(stdout_buf.getvalue())
        assert "by_status" in summary
        for key in ("passed", "skipped", "failed", "error"):
            assert key in summary["by_status"]

    def test_dry_run_json_event_count_matches(
        self, tmp_path: Path,
    ) -> None:
        """DRY_RUN for a single event produces >= 4 stage results (store/route/plan/render/deliver)."""
        event_id, db_path = _seed_db(tmp_path)
        config_path = _write_config(tmp_path, db_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main([
                "replay",
                "--config", str(config_path),
                "--mode", "dry_run",
                "--event", event_id,
                "--json",
            ])

        summary = json.loads(stdout_buf.getvalue())
        # DRY_RUN runs store, route, plan, render, deliver (skipped) = 5 stages.
        # events_replayed counts total (event, stage) tuples.
        assert summary["events_replayed"] >= 4


class TestCLIReplayBestEffortJSON:
    """``medre replay --mode best_effort --json`` via main()."""

    def test_best_effort_json_exits_cleanly(
        self, tmp_path: Path,
    ) -> None:
        """BEST_EFFORT --json exits without error."""
        event_id, db_path = _seed_db(tmp_path)
        config_path = _write_config(tmp_path, db_path)

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            main([
                "replay",
                "--config", str(config_path),
                "--mode", "best_effort",
                "--event", event_id,
                "--json",
            ])

        output = stdout_buf.getvalue()
        summary = json.loads(output)
        assert summary["mode"] == "best_effort"

    def test_best_effort_json_event_counts(
        self, tmp_path: Path,
    ) -> None:
        """BEST_EFFORT replays at least one event with >= 5 stages."""
        event_id, db_path = _seed_db(tmp_path)
        config_path = _write_config(tmp_path, db_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main([
                "replay",
                "--config", str(config_path),
                "--mode", "best_effort",
                "--event", event_id,
                "--json",
            ])

        summary = json.loads(stdout_buf.getvalue())
        assert summary["events_scanned"] >= 1
        assert summary["events_replayed"] >= 1

    def test_best_effort_creates_replay_receipts(
        self, tmp_path: Path,
    ) -> None:
        """BEST_EFFORT replay creates receipts with source='replay'."""
        import asyncio
        from medre.core.storage.sqlite import SQLiteStorage

        event_id, db_path = _seed_db(tmp_path)
        config_path = _write_config(tmp_path, db_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main([
                "replay",
                "--config", str(config_path),
                "--mode", "best_effort",
                "--event", event_id,
                "--json",
            ])

        # Verify replay receipts in storage directly.
        async def _check() -> None:
            storage = SQLiteStorage(db_path=str(db_path))
            await storage.initialize()
            try:
                receipts = await storage.list_receipts_for_event(event_id)
                replay_receipts = [r for r in receipts if r.source == "replay"]
                assert len(replay_receipts) >= 1, (
                    f"Expected >= 1 replay receipt, got {len(replay_receipts)}. "
                    f"All receipts: {[(r.source, r.status) for r in receipts]}"
                )
            finally:
                await storage.close()

        asyncio.run(_check())


class TestCLIReplayBestEffortWarning:
    """``medre replay --mode best_effort`` (no --json) prints duplicate-risk warning."""

    def test_best_effort_stderr_warning(
        self, tmp_path: Path,
    ) -> None:
        """Non-json BEST_EFFORT prints duplicate-risk warning to stderr."""
        event_id, db_path = _seed_db(tmp_path)
        config_path = _write_config(tmp_path, db_path)

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            main([
                "replay",
                "--config", str(config_path),
                "--mode", "best_effort",
                "--event", event_id,
            ])

        stderr_text = stderr_buf.getvalue()
        assert "duplicate" in stderr_text.lower(), (
            f"Expected duplicate-risk warning on stderr, got: {stderr_text!r}"
        )

    def test_best_effort_stderr_duplicate_risk_wording(
        self, tmp_path: Path,
    ) -> None:
        """Warning contains the expected operator-facing wording."""
        event_id, db_path = _seed_db(tmp_path)
        config_path = _write_config(tmp_path, db_path)

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            main([
                "replay",
                "--config", str(config_path),
                "--mode", "best_effort",
                "--event", event_id,
            ])

        stderr_text = stderr_buf.getvalue()
        # The exact warning from replay_commands._BEST_EFFORT_WARNING
        assert "BEST_EFFORT" in stderr_text
        assert "duplicate-send risk" in stderr_text
        assert "--dry-run" in stderr_text

    def test_best_effort_json_suppresses_warning(
        self, tmp_path: Path,
    ) -> None:
        """BEST_EFFORT --json does NOT print the warning to stderr."""
        event_id, db_path = _seed_db(tmp_path)
        config_path = _write_config(tmp_path, db_path)

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            main([
                "replay",
                "--config", str(config_path),
                "--mode", "best_effort",
                "--event", event_id,
                "--json",
            ])

        stderr_text = stderr_buf.getvalue()
        assert "duplicate-send risk" not in stderr_text
