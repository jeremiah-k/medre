"""Alpha walkthrough CLI command-surface tests.

End-to-end tests for every documented CLI command in the alpha walkthrough.
Every test calls ``main([...])`` — the same entry point operators use —
proving the CLI command surface works without importing internal APIs.

Walkthrough sequence:
1. ``medre config check --config <path>``
2. ``medre smoke --config <path> --storage-path <db> --json``
3. ``medre inspect event --config <cfg> <event_id>``
4. ``medre inspect receipts --config <cfg> --event <event_id>``
5. ``medre trace event --config <cfg> <event_id> --json``
6. ``medre evidence --config <cfg> --json --event <event_id>``

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


def _write_inspect_config(tmp_path: Path, db_path: Path) -> Path:
    """Write a minimal TOML config pointing storage at *db_path*."""
    cfg = tmp_path / "walkthrough_config.toml"
    cfg.write_text(
        f'[runtime]\nname = "cli-walkthrough"\n\n'
        f'[storage]\nbackend = "sqlite"\npath = "{db_path}"\n'
    )
    return cfg


def _seed_via_smoke_cli(tmp_path: Path) -> tuple[str, Path, str]:
    """Run ``main(["smoke", ...])`` to create a populated DB.

    Returns (event_id, db_path, inspect_config_path).
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

    inspect_config = _write_inspect_config(tmp_path, db_path)
    return event_id, db_path, str(inspect_config)


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
# Tests: config check
# ---------------------------------------------------------------------------


class TestAlphaConfigCheckCLI:
    """``medre config check --config <path>`` via main()."""

    def test_cli_config_check_works(self) -> None:
        """config check --config <path> prints 'Config valid'."""
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(["config", "check", "--config", _smoke_config_path()])

        output = stdout_buf.getvalue()
        assert "Config valid" in output


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
# Tests: inspect event
# ---------------------------------------------------------------------------


class TestAlphaInspectEventCLI:
    """``medre inspect event --config <cfg> <event_id>`` via main()."""

    def test_inspect_event_returns_event_data(self, tmp_path: Path) -> None:
        """inspect event prints the stored canonical event."""
        event_id, db_path, config_path = _seed_via_smoke_cli(tmp_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main([
                "inspect", "event",
                "--config", config_path,
                event_id,
            ])

        output = stdout_buf.getvalue()
        assert event_id in output
        assert "fake_matrix" in output

    def test_inspect_event_exits_cleanly(self, tmp_path: Path) -> None:
        """inspect event does not call sys.exit on success."""
        event_id, db_path, config_path = _seed_via_smoke_cli(tmp_path)

        # Should NOT raise SystemExit.
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            main([
                "inspect", "event",
                "--config", config_path,
                event_id,
            ])


# ---------------------------------------------------------------------------
# Tests: inspect receipts
# ---------------------------------------------------------------------------


class TestAlphaInspectReceiptsCLI:
    """``medre inspect receipts --config <cfg> --event <id>`` via main()."""

    def test_inspect_receipts_lists_receipts(self, tmp_path: Path) -> None:
        """inspect receipts prints delivery receipts for the event."""
        event_id, db_path, config_path = _seed_via_smoke_cli(tmp_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main([
                "inspect", "receipts",
                "--config", config_path,
                "--event", event_id,
            ])

        output = stdout_buf.getvalue()
        assert "sent" in output

    def test_inspect_receipts_exits_cleanly(self, tmp_path: Path) -> None:
        """inspect receipts does not call sys.exit on success."""
        event_id, db_path, config_path = _seed_via_smoke_cli(tmp_path)

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            main([
                "inspect", "receipts",
                "--config", config_path,
                "--event", event_id,
            ])


# ---------------------------------------------------------------------------
# Tests: trace event
# ---------------------------------------------------------------------------


class TestAlphaTraceEventCLI:
    """``medre trace event --config <cfg> <event_id> --json`` via main()."""

    def test_trace_event_json_timeline(self, tmp_path: Path) -> None:
        """trace event --json returns a JSON timeline with entries."""
        event_id, db_path, config_path = _seed_via_smoke_cli(tmp_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main([
                "trace", "event",
                "--config", config_path,
                event_id,
                "--json",
            ])

        timeline = json.loads(stdout_buf.getvalue())
        assert isinstance(timeline, list)
        assert len(timeline) >= 1

    def test_trace_event_json_has_receipt_entries(self, tmp_path: Path) -> None:
        """Timeline includes at least one receipt entry."""
        event_id, db_path, config_path = _seed_via_smoke_cli(tmp_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main([
                "trace", "event",
                "--config", config_path,
                event_id,
                "--json",
            ])

        timeline = json.loads(stdout_buf.getvalue())
        entry_types = [e.get("entry_type") for e in timeline]
        assert "receipt" in entry_types, (
            f"Expected 'receipt' in timeline entry types, got: {entry_types}"
        )

    def test_trace_event_human_readable(self, tmp_path: Path) -> None:
        """trace event (no --json) prints human-readable timeline."""
        event_id, db_path, config_path = _seed_via_smoke_cli(tmp_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main([
                "trace", "event",
                "--config", config_path,
                event_id,
            ])

        output = stdout_buf.getvalue()
        assert event_id in output
        assert "Timeline" in output
        assert "Summary" in output


# ---------------------------------------------------------------------------
# Tests: evidence
# ---------------------------------------------------------------------------


class TestAlphaEvidenceCLI:
    """``medre evidence --config <cfg> --json`` via main()."""

    def test_evidence_json_bundle(self, tmp_path: Path) -> None:
        """evidence --json returns a valid evidence bundle."""
        event_id, db_path, config_path = _seed_via_smoke_cli(tmp_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main([
                "evidence",
                "--config", config_path,
                "--json",
            ])

        bundle = json.loads(stdout_buf.getvalue())
        assert "status" in bundle
        assert bundle["status"] in ("ok", "partial")
        assert "sections" in bundle

    def test_evidence_with_event_filter(self, tmp_path: Path) -> None:
        """evidence --event <id> --json includes event-specific data."""
        event_id, db_path, config_path = _seed_via_smoke_cli(tmp_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main([
                "evidence",
                "--config", config_path,
                "--json",
                "--event", event_id,
            ])

        bundle = json.loads(stdout_buf.getvalue())
        assert bundle["status"] in ("ok", "partial")

    def test_evidence_human_readable(self, tmp_path: Path) -> None:
        """evidence (no --json) prints human-readable summary."""
        event_id, db_path, config_path = _seed_via_smoke_cli(tmp_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main([
                "evidence",
                "--config", config_path,
            ])

        output = stdout_buf.getvalue()
        assert "Evidence:" in output


# ---------------------------------------------------------------------------
# Test: full walkthrough sequence
# ---------------------------------------------------------------------------


class TestAlphaFullWalkthroughCLI:
    """Full alpha walkthrough: smoke → inspect → trace → evidence via main()."""

    def test_full_walkthrough_sequence(self, tmp_path: Path) -> None:
        """Prove the documented operator walkthrough sequence works via main().

        Steps:
        1. medre config check --config <path>
        2. medre smoke --config <path> --storage-path <db> --json  → event_id
        3. medre inspect event --config <cfg> <event_id>
        4. medre inspect receipts --config <cfg> --event <event_id>
        5. medre trace event --config <cfg> <event_id> --json
        6. medre evidence --config <cfg> --json --event <event_id>
        """
        # Step 1: Config check
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(["config", "check", "--config", _smoke_config_path()])
        assert "Config valid" in stdout_buf.getvalue()

        # Step 2: Smoke seeds persistent DB
        db_path = tmp_path / "full_walkthrough.db"
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
        assert report["status"] == "passed"
        event_id = report["event_id"]

        # Build config for inspect/trace/evidence pointing at the DB
        inspect_config = _write_inspect_config(tmp_path, db_path)
        config_path = str(inspect_config)

        # Step 3: Inspect event
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main([
                "inspect", "event",
                "--config", config_path,
                event_id,
            ])
        assert event_id in stdout_buf.getvalue()

        # Step 4: Inspect receipts
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main([
                "inspect", "receipts",
                "--config", config_path,
                "--event", event_id,
            ])
        assert "sent" in stdout_buf.getvalue()

        # Step 5: Trace event
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main([
                "trace", "event",
                "--config", config_path,
                event_id,
                "--json",
            ])
        timeline = json.loads(stdout_buf.getvalue())
        assert len(timeline) >= 1
        entry_types = [e.get("entry_type") for e in timeline]
        assert "receipt" in entry_types

        # Step 6: Evidence
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main([
                "evidence",
                "--config", config_path,
                "--json",
                "--event", event_id,
            ])
        bundle = json.loads(stdout_buf.getvalue())
        assert bundle["status"] in ("ok", "partial")
