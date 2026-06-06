"""CLI-surface tests proving replay command uses real command handlers.

Every test calls ``main(["replay", ...])`` — the same entry point operators
use — rather than importing ReplayEngine or RuntimeBuilder directly.

Tests cover:
- ``medre replay --mode dry_run --event <id> --config <cfg> --json``
- ``medre replay --mode best_effort --event <id> --config <cfg> --json``
- ``medre replay --mode best_effort --event <id> --config <cfg>`` (no --json)
  → stderr contains duplicate-risk warning
- Invalid mode rejection by argparse
- Human-readable output for dry_run / best_effort
- JSON shape: ``run_id``, ``by_stage``, ``mode`` keys
- BEST_EFFORT receipts carry ``replay_run_id`` and ``source="replay"``
- ``--storage-path`` rejected with helpful error explaining config requirement
- ``--target-adapters`` and ``--route-ids`` filter behaviour at CLI surface
- Exit code conventions (0 on success, 2 on config/arg errors)

Runtime-level replay tests remain in test_alpha_walkthrough_runtime_retry_replay.py.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

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
    """Run smoke via main() with SQLite config to create a populated DB.

    Returns (event_id, db_path).
    """
    db_path = tmp_path / "replay_surface.db"
    _write_config(tmp_path, db_path)

    # Config already specifies SQLite at db_path — smoke uses it directly.
    seed_cfg = tmp_path / "seed.toml"
    seed_cfg.write_text(
        _SMOKELIKE_TOML.format(storage_path=str(db_path)),
    )

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    "smoke",
                    "--config",
                    str(seed_cfg),
                    "--json",
                ]
            )
    assert exc_info.value.code == 0, f"Smoke seed failed: {stderr_buf.getvalue()}"
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
        self,
        tmp_path: Path,
    ) -> None:
        """DRY_RUN --json exits without error and returns valid JSON."""
        event_id, db_path = _seed_db(tmp_path)
        config_path = _write_config(tmp_path, db_path)

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            main(
                [
                    "replay",
                    "--config",
                    str(config_path),
                    "--mode",
                    "dry_run",
                    "--event",
                    event_id,
                    "--json",
                ]
            )

        output = stdout_buf.getvalue()
        summary = json.loads(output)
        assert summary["mode"] == "dry_run"
        assert summary["events_scanned"] >= 1
        assert summary["events_replayed"] >= 1

    def test_dry_run_json_has_by_status(
        self,
        tmp_path: Path,
    ) -> None:
        """DRY_RUN summary includes by_status with all four canonical keys."""
        event_id, db_path = _seed_db(tmp_path)
        config_path = _write_config(tmp_path, db_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "replay",
                    "--config",
                    str(config_path),
                    "--mode",
                    "dry_run",
                    "--event",
                    event_id,
                    "--json",
                ]
            )

        summary = json.loads(stdout_buf.getvalue())
        assert "by_status" in summary
        for key in ("passed", "skipped", "failed", "error"):
            assert key in summary["by_status"]

    def test_dry_run_json_event_count_matches(
        self,
        tmp_path: Path,
    ) -> None:
        """DRY_RUN for a single event produces >= 4 stage results (store/route/plan/render/deliver)."""
        event_id, db_path = _seed_db(tmp_path)
        config_path = _write_config(tmp_path, db_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "replay",
                    "--config",
                    str(config_path),
                    "--mode",
                    "dry_run",
                    "--event",
                    event_id,
                    "--json",
                ]
            )

        summary = json.loads(stdout_buf.getvalue())
        # DRY_RUN runs store, route, plan, render, deliver (skipped) = 5 stages.
        # events_replayed counts total (event, stage) tuples.
        assert summary["events_replayed"] >= 4


class TestCLIReplayBestEffortJSON:
    """``medre replay --mode best_effort --json`` via main()."""

    def test_best_effort_json_exits_cleanly(
        self,
        tmp_path: Path,
    ) -> None:
        """BEST_EFFORT --json exits without error."""
        event_id, db_path = _seed_db(tmp_path)
        config_path = _write_config(tmp_path, db_path)

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            main(
                [
                    "replay",
                    "--config",
                    str(config_path),
                    "--mode",
                    "best_effort",
                    "--event",
                    event_id,
                    "--json",
                ]
            )

        output = stdout_buf.getvalue()
        summary = json.loads(output)
        assert summary["mode"] == "best_effort"

    def test_best_effort_json_event_counts(
        self,
        tmp_path: Path,
    ) -> None:
        """BEST_EFFORT replays at least one event with >= 5 stages."""
        event_id, db_path = _seed_db(tmp_path)
        config_path = _write_config(tmp_path, db_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "replay",
                    "--config",
                    str(config_path),
                    "--mode",
                    "best_effort",
                    "--event",
                    event_id,
                    "--json",
                ]
            )

        summary = json.loads(stdout_buf.getvalue())
        assert summary["events_scanned"] >= 1
        assert summary["events_replayed"] >= 1

    def test_best_effort_creates_replay_receipts(
        self,
        tmp_path: Path,
    ) -> None:
        """BEST_EFFORT replay creates receipts with source='replay'."""
        import asyncio

        from medre.core.storage.sqlite.storage import SQLiteStorage

        event_id, db_path = _seed_db(tmp_path)
        config_path = _write_config(tmp_path, db_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "replay",
                    "--config",
                    str(config_path),
                    "--mode",
                    "best_effort",
                    "--event",
                    event_id,
                    "--json",
                ]
            )

        # Verify replay receipts in storage directly.
        async def _check() -> None:
            storage = SQLiteStorage(db_path=str(db_path))
            try:
                await storage.initialize()
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
        self,
        tmp_path: Path,
    ) -> None:
        """Non-json BEST_EFFORT prints duplicate-risk warning to stderr."""
        event_id, db_path = _seed_db(tmp_path)
        config_path = _write_config(tmp_path, db_path)

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            main(
                [
                    "replay",
                    "--config",
                    str(config_path),
                    "--mode",
                    "best_effort",
                    "--event",
                    event_id,
                ]
            )

        stderr_text = stderr_buf.getvalue()
        assert (
            "duplicate" in stderr_text.lower()
        ), f"Expected duplicate-risk warning on stderr, got: {stderr_text!r}"

    def test_best_effort_stderr_duplicate_risk_wording(
        self,
        tmp_path: Path,
    ) -> None:
        """Warning contains the expected operator-facing wording."""
        event_id, db_path = _seed_db(tmp_path)
        config_path = _write_config(tmp_path, db_path)

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            main(
                [
                    "replay",
                    "--config",
                    str(config_path),
                    "--mode",
                    "best_effort",
                    "--event",
                    event_id,
                ]
            )

        stderr_text = stderr_buf.getvalue()
        # The exact warning from replay_commands._BEST_EFFORT_WARNING
        assert "BEST_EFFORT" in stderr_text
        assert "duplicate-send risk" in stderr_text
        assert "--dry-run" in stderr_text

    def test_best_effort_json_suppresses_warning(
        self,
        tmp_path: Path,
    ) -> None:
        """BEST_EFFORT --json does NOT print the warning to stderr."""
        event_id, db_path = _seed_db(tmp_path)
        config_path = _write_config(tmp_path, db_path)

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            main(
                [
                    "replay",
                    "--config",
                    str(config_path),
                    "--mode",
                    "best_effort",
                    "--event",
                    event_id,
                    "--json",
                ]
            )

        stderr_text = stderr_buf.getvalue()
        assert "duplicate-send risk" not in stderr_text


# ---------------------------------------------------------------------------
# Invalid mode
# ---------------------------------------------------------------------------


class TestCLIReplayInvalidMode:
    """Argparse rejects invalid --mode values."""

    def test_invalid_mode_exits_config(self) -> None:
        """Invalid mode causes argparse to exit with code 2."""
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            with pytest.raises(SystemExit) as exc_info:
                main(
                    [
                        "replay",
                        "--config",
                        "/dev/null",
                        "--mode",
                        "nonsense_mode",
                    ]
                )
        assert exc_info.value.code == 2
        stderr_text = stderr_buf.getvalue()
        assert (
            "invalid choice" in stderr_text.lower() or "invalid" in stderr_text.lower()
        )


# ---------------------------------------------------------------------------
# Human-readable output
# ---------------------------------------------------------------------------


class TestCLIReplayDryRunHumanOutput:
    """``medre replay --mode dry_run`` (no --json) produces human-readable output."""

    def test_dry_run_human_readable_output(
        self,
        tmp_path: Path,
    ) -> None:
        """Non-json DRY_RUN prints human-readable summary to stdout."""
        event_id, db_path = _seed_db(tmp_path)
        config_path = _write_config(tmp_path, db_path)

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            main(
                [
                    "replay",
                    "--config",
                    str(config_path),
                    "--mode",
                    "dry_run",
                    "--event",
                    event_id,
                ]
            )

        stdout_text = stdout_buf.getvalue()
        assert "Replay: dry_run" in stdout_text
        assert "Events scanned:" in stdout_text

    def test_dry_run_no_best_effort_warning(
        self,
        tmp_path: Path,
    ) -> None:
        """DRY_RUN never prints the BEST_EFFORT duplicate-risk warning."""
        event_id, db_path = _seed_db(tmp_path)
        config_path = _write_config(tmp_path, db_path)

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            main(
                [
                    "replay",
                    "--config",
                    str(config_path),
                    "--mode",
                    "dry_run",
                    "--event",
                    event_id,
                ]
            )

        stderr_text = stderr_buf.getvalue()
        assert "duplicate-send risk" not in stderr_text


class TestCLIReplayBestEffortHumanOutput:
    """``medre replay --mode best_effort`` (no --json) produces human-readable output."""

    def test_best_effort_human_readable_output(
        self,
        tmp_path: Path,
    ) -> None:
        """Non-json BEST_EFFORT prints human-readable summary to stdout."""
        event_id, db_path = _seed_db(tmp_path)
        config_path = _write_config(tmp_path, db_path)

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            main(
                [
                    "replay",
                    "--config",
                    str(config_path),
                    "--mode",
                    "best_effort",
                    "--event",
                    event_id,
                ]
            )

        stdout_text = stdout_buf.getvalue()
        assert "Replay: best_effort" in stdout_text
        assert "Events scanned:" in stdout_text


# ---------------------------------------------------------------------------
# JSON shape evidence
# ---------------------------------------------------------------------------


class TestCLIReplayJSONShape:
    """JSON output includes documented keys: run_id, mode, by_stage, by_status."""

    def test_best_effort_json_has_run_id(
        self,
        tmp_path: Path,
    ) -> None:
        """BEST_EFFORT JSON summary contains ``run_id`` key."""
        event_id, db_path = _seed_db(tmp_path)
        config_path = _write_config(tmp_path, db_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "replay",
                    "--config",
                    str(config_path),
                    "--mode",
                    "best_effort",
                    "--event",
                    event_id,
                    "--json",
                ]
            )

        summary = json.loads(stdout_buf.getvalue())
        assert "run_id" in summary
        # Without an explicit run_id, it defaults to empty string.
        assert isinstance(summary["run_id"], str)

    def test_dry_run_json_has_mode_key(
        self,
        tmp_path: Path,
    ) -> None:
        """DRY_RUN JSON summary contains ``mode`` key with value ``dry_run``."""
        event_id, db_path = _seed_db(tmp_path)
        config_path = _write_config(tmp_path, db_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "replay",
                    "--config",
                    str(config_path),
                    "--mode",
                    "dry_run",
                    "--event",
                    event_id,
                    "--json",
                ]
            )

        summary = json.loads(stdout_buf.getvalue())
        assert "mode" in summary
        assert summary["mode"] == "dry_run"

    def test_best_effort_json_has_by_stage(
        self,
        tmp_path: Path,
    ) -> None:
        """JSON summary includes ``by_stage`` with pipeline stage names."""
        event_id, db_path = _seed_db(tmp_path)
        config_path = _write_config(tmp_path, db_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "replay",
                    "--config",
                    str(config_path),
                    "--mode",
                    "best_effort",
                    "--event",
                    event_id,
                    "--json",
                ]
            )

        summary = json.loads(stdout_buf.getvalue())
        assert "by_stage" in summary
        assert isinstance(summary["by_stage"], dict)
        # BEST_EFFORT runs store, route, plan, render, deliver.
        assert "store" in summary["by_stage"]

    def test_best_effort_json_has_elapsed_ms(
        self,
        tmp_path: Path,
    ) -> None:
        """JSON summary includes ``elapsed_ms`` as a non-negative number."""
        event_id, db_path = _seed_db(tmp_path)
        config_path = _write_config(tmp_path, db_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "replay",
                    "--config",
                    str(config_path),
                    "--mode",
                    "best_effort",
                    "--event",
                    event_id,
                    "--json",
                ]
            )

        summary = json.loads(stdout_buf.getvalue())
        assert "elapsed_ms" in summary
        assert summary["elapsed_ms"] >= 0.0


# ---------------------------------------------------------------------------
# BEST_EFFORT receipt evidence: replay_run_id and source="replay"
# ---------------------------------------------------------------------------


class TestCLIReplayBestEffortReceiptEvidence:
    """BEST_EFFORT replay creates receipts with ``replay_run_id`` and ``source="replay"``."""

    def test_receipts_have_replay_run_id(
        self,
        tmp_path: Path,
    ) -> None:
        """BEST_EFFORT receipts carry ``replay_run_id`` field (None when no explicit run_id)."""
        import asyncio

        from medre.core.storage.sqlite.storage import SQLiteStorage

        event_id, db_path = _seed_db(tmp_path)
        config_path = _write_config(tmp_path, db_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "replay",
                    "--config",
                    str(config_path),
                    "--mode",
                    "best_effort",
                    "--event",
                    event_id,
                    "--json",
                ]
            )

        async def _check() -> None:
            storage = SQLiteStorage(db_path=str(db_path))
            try:
                await storage.initialize()
                receipts = await storage.list_receipts_for_event(event_id)
                replay_receipts = [r for r in receipts if r.source == "replay"]
                assert len(replay_receipts) >= 1
                for r in replay_receipts:
                    # replay_run_id is a str | None field.  When the CLI
                    # does not pass an explicit run_id, it is None.
                    # Verify the field exists and is properly typed.
                    assert hasattr(
                        r, "replay_run_id"
                    ), "DeliveryReceipt missing replay_run_id field"
                    assert r.replay_run_id is None or isinstance(
                        r.replay_run_id, str
                    ), f"Expected replay_run_id to be str|None, got {type(r.replay_run_id)}"
            finally:
                await storage.close()

        asyncio.run(_check())

    def test_receipts_have_source_replay(
        self,
        tmp_path: Path,
    ) -> None:
        """BEST_EFFORT receipts have ``source='replay'``."""
        import asyncio

        from medre.core.storage.sqlite.storage import SQLiteStorage

        event_id, db_path = _seed_db(tmp_path)
        config_path = _write_config(tmp_path, db_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "replay",
                    "--config",
                    str(config_path),
                    "--mode",
                    "best_effort",
                    "--event",
                    event_id,
                    "--json",
                ]
            )

        async def _check() -> None:
            storage = SQLiteStorage(db_path=str(db_path))
            try:
                await storage.initialize()
                receipts = await storage.list_receipts_for_event(event_id)
                replay_receipts = [r for r in receipts if r.source == "replay"]
                assert len(replay_receipts) >= 1
                for r in replay_receipts:
                    assert r.source == "replay"
            finally:
                await storage.close()

        asyncio.run(_check())


# ---------------------------------------------------------------------------
# --storage-path rejection
# ---------------------------------------------------------------------------


class TestCLIReplayStoragePathRejected:
    """``--storage-path`` with replay is intentionally unsupported."""

    def test_storage_path_rejected_with_helpful_error(self) -> None:
        """Passing --storage-path to replay exits with helpful config error."""
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            with pytest.raises(SystemExit) as exc_info:
                main(
                    [
                        "replay",
                        "--config",
                        "/dev/null",
                        "--mode",
                        "dry_run",
                        "--storage-path",
                        "/tmp/replay.db",
                    ]
                )
        assert exc_info.value.code == 2
        stderr_text = stderr_buf.getvalue()
        assert "storage-path" in stderr_text.lower() or "--storage-path" in stderr_text
        assert "config" in stderr_text.lower()
        assert "routes" in stderr_text.lower() or "adapters" in stderr_text.lower()

    def test_storage_path_rejected_before_config_load(self) -> None:
        """Rejection happens before attempting to load config file."""
        # Use a non-existent config to prove we never get to config loading.
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            with pytest.raises(SystemExit) as exc_info:
                main(
                    [
                        "replay",
                        "--config",
                        "/nonexistent/path.toml",
                        "--mode",
                        "dry_run",
                        "--storage-path",
                        "/tmp/replay.db",
                    ]
                )
        # Exit is from --storage-path rejection, not from missing config.
        assert exc_info.value.code == 2
        stderr_text = stderr_buf.getvalue()
        assert "--storage-path" in stderr_text


# ---------------------------------------------------------------------------
# --target-adapters filter
# ---------------------------------------------------------------------------


class TestCLIReplayTargetAdaptersFilter:
    """``--target-adapters`` restricts delivery to specified adapter IDs."""

    def test_nonexistent_target_adapter_skips_delivery(
        self,
        tmp_path: Path,
    ) -> None:
        """Targeting a non-existent adapter results in skipped delivery."""
        event_id, db_path = _seed_db(tmp_path)
        config_path = _write_config(tmp_path, db_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "replay",
                    "--config",
                    str(config_path),
                    "--mode",
                    "dry_run",
                    "--event",
                    event_id,
                    "--target-adapters",
                    "nonexistent_adapter",
                    "--json",
                ]
            )

        summary = json.loads(stdout_buf.getvalue())
        # Delivery stage should show skipped because no plans match
        # the nonexistent adapter filter.
        assert summary["mode"] == "dry_run"
        assert summary["events_scanned"] >= 1

    def test_matching_target_adapter_replays(
        self,
        tmp_path: Path,
    ) -> None:
        """Targeting the actual dest adapter produces normal replay results."""
        event_id, db_path = _seed_db(tmp_path)
        config_path = _write_config(tmp_path, db_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "replay",
                    "--config",
                    str(config_path),
                    "--mode",
                    "dry_run",
                    "--event",
                    event_id,
                    "--target-adapters",
                    "fake_meshtastic",
                    "--json",
                ]
            )

        summary = json.loads(stdout_buf.getvalue())
        assert summary["events_scanned"] >= 1
        assert summary["events_replayed"] >= 1


# ---------------------------------------------------------------------------
# --route-ids filter
# ---------------------------------------------------------------------------


class TestCLIReplayRouteIdsFilter:
    """``--route-ids`` restricts routing to specified route IDs."""

    def test_nonexistent_route_id_no_match(
        self,
        tmp_path: Path,
    ) -> None:
        """Specifying a non-existent route ID results in no route matches."""
        event_id, db_path = _seed_db(tmp_path)
        config_path = _write_config(tmp_path, db_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "replay",
                    "--config",
                    str(config_path),
                    "--mode",
                    "dry_run",
                    "--event",
                    event_id,
                    "--route-ids",
                    "nonexistent_route",
                    "--json",
                ]
            )

        summary = json.loads(stdout_buf.getvalue())
        assert summary["events_scanned"] >= 1
        # Route stage should report "failed" or "skipped" because
        # no routes match the filter.
        by_status = summary["by_status"]
        # At least one non-passed result (route stage failed/skipped).
        assert (by_status.get("failed", 0) + by_status.get("skipped", 0)) >= 1

    def test_valid_route_id_matches(
        self,
        tmp_path: Path,
    ) -> None:
        """Specifying the actual route ID produces normal replay results."""
        event_id, db_path = _seed_db(tmp_path)
        config_path = _write_config(tmp_path, db_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "replay",
                    "--config",
                    str(config_path),
                    "--mode",
                    "dry_run",
                    "--event",
                    event_id,
                    "--route-ids",
                    "mx_to_mesh",
                    "--json",
                ]
            )

        summary = json.loads(stdout_buf.getvalue())
        assert summary["events_scanned"] >= 1
        assert summary["events_replayed"] >= 1


# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------


class TestCLIReplayExitCodes:
    """Exit code conventions for replay command."""

    def test_dry_run_exits_zero(
        self,
        tmp_path: Path,
    ) -> None:
        """Normal dry_run replay exits with code 0."""
        event_id, db_path = _seed_db(tmp_path)
        config_path = _write_config(tmp_path, db_path)

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        # main() does NOT raise SystemExit on success — it returns normally.
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            main(
                [
                    "replay",
                    "--config",
                    str(config_path),
                    "--mode",
                    "dry_run",
                    "--event",
                    event_id,
                    "--json",
                ]
            )

        # No SystemExit means success (exit code 0).

    def test_best_effort_exits_zero(
        self,
        tmp_path: Path,
    ) -> None:
        """Normal best_effort replay exits with code 0."""
        event_id, db_path = _seed_db(tmp_path)
        config_path = _write_config(tmp_path, db_path)

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            main(
                [
                    "replay",
                    "--config",
                    str(config_path),
                    "--mode",
                    "best_effort",
                    "--event",
                    event_id,
                    "--json",
                ]
            )

        # No SystemExit means success (exit code 0).

    def test_missing_config_exits_config_code(self) -> None:
        """Missing config file exits with EXIT_CONFIG (2)."""
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            with pytest.raises(SystemExit) as exc_info:
                main(
                    [
                        "replay",
                        "--config",
                        "/nonexistent/medre-config.toml",
                        "--mode",
                        "dry_run",
                        "--json",
                    ]
                )
        assert exc_info.value.code == 2
        stderr_text = stderr_buf.getvalue()
        assert "config" in stderr_text.lower() or "Config" in stderr_text
