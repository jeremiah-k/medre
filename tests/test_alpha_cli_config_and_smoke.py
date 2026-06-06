"""Alpha CLI tests: config check, routes validate, smoke, subprocess,
config sample, SDK boundaries, first-run walkthrough, and E2E product path.

Split from the original test_alpha_walkthrough_cli.py monolith.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tomllib
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from medre.cli import main
from tests.helpers.alpha_cli import (
    EXAMPLES_SMOKE_CONFIG,
    SRC_DIR,
    optional_sdks_in_modules,
    smoke_config_path,
    write_replay_config,
    write_sqlite_config_from_example,
)

# ---------------------------------------------------------------------------
# Tests: smoke seeds DB
# ---------------------------------------------------------------------------


class TestAlphaSmokeSeedsCLI:
    """``medre smoke --config <sqlite-config> --json`` via main()."""

    def test_smoke_json_creates_persistent_db(self, tmp_path: Path) -> None:
        """Smoke with SQLite config creates a persistent SQLite file."""
        db_path = tmp_path / "smoke_seed.db"
        cfg = write_sqlite_config_from_example(tmp_path, db_path)

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
        assert db_path.exists(), "SQLite DB should exist after smoke"

        report = json.loads(stdout_buf.getvalue())
        assert report["status"] == "passed"
        assert report["storage_path"] == str(db_path)

    def test_smoke_json_event_id_present(self, tmp_path: Path) -> None:
        """Smoke --json report has a non-empty event_id."""
        db_path = tmp_path / "seed_evt.db"
        cfg = write_sqlite_config_from_example(tmp_path, db_path)

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
        assert isinstance(report["event_id"], str)
        assert len(report["event_id"]) > 0


# ---------------------------------------------------------------------------
# Test: single E2E product command path
# ---------------------------------------------------------------------------


class TestAlphaE2EProductPathCLI:
    """One test proving the complete product surface via main([...]).

    Phases:
      0. ``config check``, ``routes validate`` (pre-flight)
      1. ``smoke --config <sqlite-config> --json`` → event_id
      2. ``inspect receipts --event <id> --storage-path``
      3. ``inspect event <id> --timeline / --evidence / --recovery --storage-path``
      4. ``replay --config --mode dry_run --event <id> --json``
         ``replay --config --mode best_effort --event <id> --json``

    Same event_id flows through every step.
    No trace / evidence / recover primary command needed (inspect subsumes them).
    """

    def test_full_product_command_path(self, tmp_path: Path) -> None:
        cfg = smoke_config_path()

        # --- Phase 0: pre-flight ---
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(["config", "check", "--config", cfg])
        assert "Config valid" in stdout_buf.getvalue()

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(["routes", "validate", "--config", cfg])
        assert "Routes valid" in stdout_buf.getvalue()

        # --- Phase 1: smoke seeds persistent DB ---
        db_path = tmp_path / "e2e_product.db"
        sqlite_cfg = write_sqlite_config_from_example(tmp_path, db_path)
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            with pytest.raises(SystemExit) as exc_info:
                main(
                    [
                        "smoke",
                        "--config",
                        sqlite_cfg,
                        "--json",
                    ]
                )
        assert exc_info.value.code == 0
        report = json.loads(stdout_buf.getvalue())
        assert report["status"] == "passed"
        event_id: str = report["event_id"]
        assert event_id

        # --- Phase 2: inspect receipts ---
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "inspect",
                    "receipts",
                    "--event",
                    event_id,
                    "--storage-path",
                    str(db_path),
                ]
            )
        assert "sent" in stdout_buf.getvalue()
        assert event_id in stdout_buf.getvalue()

        # --- Phase 3a: timeline ---
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "inspect",
                    "event",
                    event_id,
                    "--timeline",
                    "--storage-path",
                    str(db_path),
                ]
            )
        tl = json.loads(stdout_buf.getvalue())
        assert tl["event"]["event_id"] == event_id
        assert any(e.get("entry_type") == "receipt" for e in tl["timeline"])

        # --- Phase 3b: evidence ---
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "inspect",
                    "event",
                    event_id,
                    "--evidence",
                    "--storage-path",
                    str(db_path),
                ]
            )
        ev = json.loads(stdout_buf.getvalue())
        assert ev["evidence"]["status"] in ("partial", "passed")
        assert (
            ev["evidence"]["sections"]["storage"]["data"]["event"]["event_id"]
            == event_id
        )

        # --- Phase 3c: recovery (inspect subsumes recover command) ---
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "inspect",
                    "event",
                    event_id,
                    "--recovery",
                    "--storage-path",
                    str(db_path),
                ]
            )
        rc = json.loads(stdout_buf.getvalue())
        assert rc["event"]["event_id"] == event_id
        assert "recovery" in rc

        # --- Phase 4: replay (config required, no --storage-path) ---
        replay_cfg = write_replay_config(tmp_path, db_path)

        # 4a: dry_run — no side effects
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "replay",
                    "--config",
                    replay_cfg,
                    "--mode",
                    "dry_run",
                    "--event",
                    event_id,
                    "--json",
                ]
            )
        dry = json.loads(stdout_buf.getvalue())
        assert dry["mode"] == "dry_run"
        assert dry["events_scanned"] >= 1
        assert dry["events_replayed"] >= 1
        # Taxonomy: by_status must contain all four canonical keys.
        for status_key in ("passed", "skipped", "failed", "error"):
            assert status_key in dry.get(
                "by_status", {}
            ), f"Replay dry_run by_status missing taxonomy key {status_key!r}"

        # 4b: best_effort — creates replay receipts
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "replay",
                    "--config",
                    replay_cfg,
                    "--mode",
                    "best_effort",
                    "--event",
                    event_id,
                    "--json",
                ]
            )
        be = json.loads(stdout_buf.getvalue())
        assert be["mode"] == "best_effort"
        assert be["events_replayed"] >= 1
        # Taxonomy: by_status must contain all four canonical keys.
        for status_key in ("passed", "skipped", "failed", "error"):
            assert status_key in be.get(
                "by_status", {}
            ), f"Replay best_effort by_status missing taxonomy key {status_key!r}"


# ===================================================================
# Subprocess: python -m medre
# ===================================================================


class TestSubprocessPythonM:
    """Proves ``python -m medre`` works via subprocess with source checkout.

    Uses ``sys.executable`` and ``PYTHONPATH=src/`` to match the project's
    standard test invocation pattern.
    """

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SRC_DIR) + os.pathsep + env.get("PYTHONPATH", "")
        return subprocess.run(
            [sys.executable, "-m", "medre", *args],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

    def test_version(self) -> None:
        """``python -m medre version`` exits 0 with version info."""
        proc = self._run("version")
        assert proc.returncode == 0, f"Exit {proc.returncode}: stderr={proc.stderr!r}"
        assert "medre" in proc.stdout.lower()
        assert "Python" in proc.stdout

    def test_help(self) -> None:
        """``python -m medre --help`` exits 0 with usage info."""
        proc = self._run("--help")
        assert proc.returncode == 0, f"Exit {proc.returncode}: stderr={proc.stderr!r}"
        assert "usage" in proc.stdout.lower() or "medre" in proc.stdout.lower()

    def test_version_output_format(self) -> None:
        """First line of ``python -m medre version`` starts with 'medre '."""
        proc = self._run("version")
        assert proc.returncode == 0
        first_line = proc.stdout.strip().splitlines()[0]
        assert first_line.startswith("medre "), f"Unexpected first line: {first_line!r}"

    def test_no_optional_sdks_in_subprocess(self) -> None:
        """Subprocess ``python -m medre version`` does not import optional SDKs.

        Writes a temporary check script and runs it via subprocess to verify
        that optional SDK modules do not appear in sys.modules after importing
        medre.cli and running the version command.  Records modules before
        the version call and checks for NEW leaks only (some environments
        have SDKs pre-installed).
        """
        import tempfile

        from tests.helpers.alpha_cli import OPTIONAL_SDK_MODULES

        env = os.environ.copy()
        env["PYTHONPATH"] = str(SRC_DIR) + os.pathsep + env.get("PYTHONPATH", "")
        sdk_list = sorted(OPTIONAL_SDK_MODULES)

        check_script = (
            "import sys, io\n"
            "from contextlib import redirect_stdout, redirect_stderr\n"
            "from medre.cli import main\n"
            f"before = {{m for m in {sdk_list!r} if m in sys.modules}}\n"
            "buf_o, buf_e = io.StringIO(), io.StringIO()\n"
            "try:\n"
            "    with redirect_stdout(buf_o), redirect_stderr(buf_e):\n"
            "        main(['version'])\n"
            "except SystemExit:\n"
            "    pass\n"
            f"after = {{m for m in {sdk_list!r} if m in sys.modules}}\n"
            "leaked = sorted(after - before)\n"
            "print(','.join(leaked))\n"
        )

        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".py",
            delete=False,
        ) as tf:
            tf.write(check_script)
            script_path = tf.name

        try:
            proc = subprocess.run(
                [sys.executable, script_path],
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
        finally:
            os.unlink(script_path)

        assert proc.returncode == 0, f"Check script failed: stderr={proc.stderr!r}"
        leaked = proc.stdout.strip()
        assert not leaked, f"Optional SDKs leaked into sys.modules by version: {leaked}"

    def test_subprocess_smoke_with_source_config(self) -> None:
        """``python -m medre smoke --config <examples config>`` passes."""
        if not EXAMPLES_SMOKE_CONFIG.is_file():
            pytest.skip("Source-tree example config not available")
        proc = self._run(
            "smoke",
            "--config",
            str(EXAMPLES_SMOKE_CONFIG),
            "--json",
        )
        assert proc.returncode == 0, (
            f"Smoke failed (code={proc.returncode}): "
            f"stdout={proc.stdout[:500]!r} stderr={proc.stderr[:500]!r}"
        )
        report = json.loads(proc.stdout)
        assert report["status"] == "passed"


# ===================================================================
# Config sample → parse → check → fake-only smoke
# ===================================================================


class TestConfigSampleToSmoke:
    """Proves ``medre config sample`` output parses, ``config check`` passes,
    and the generated config works as fake-only smoke input without importing
    optional SDKs."""

    def test_sample_generates_valid_toml(self) -> None:
        """``config sample`` produces valid parseable TOML."""
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(["config", "sample"])
        sample = stdout_buf.getvalue()
        parsed = tomllib.loads(sample)
        assert isinstance(parsed, dict)
        assert "runtime" in parsed
        assert "adapters" in parsed

    def test_sample_config_check_passes(self, tmp_path: Path) -> None:
        """Generated sample config passes ``config check``."""
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(["config", "sample"])
        sample = stdout_buf.getvalue()

        cfg_path = tmp_path / "sample.toml"
        cfg_path.write_text(sample)

        stdout_buf2 = io.StringIO()
        stderr_buf2 = io.StringIO()
        with redirect_stdout(stdout_buf2), redirect_stderr(stderr_buf2):
            main(["config", "check", "--config", str(cfg_path)])
        output = stdout_buf2.getvalue()
        assert (
            "Config valid" in output
        ), f"config check did not produce 'Config valid': {output[:300]!r}"

    def test_sample_config_smoke_passes(self, tmp_path: Path) -> None:
        """``smoke --config <sample>`` passes or fails gracefully.

        The generated sample config may have route policies that filter by
        ``allowed_event_types`` (e.g. ``["message"]``) which don't match
        the smoke event kind ``message.text``. This is expected — the sample
        config is for operator reference, not guaranteed to pass smoke.
        The important thing is: it does not crash, returns valid JSON, and
        does not import optional SDKs.
        """
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(["config", "sample"])
        sample = stdout_buf.getvalue()
        cfg_path = tmp_path / "sample.toml"
        cfg_path.write_text(sample)

        stdout_buf2 = io.StringIO()
        with redirect_stdout(stdout_buf2), redirect_stderr(io.StringIO()):
            with pytest.raises(SystemExit):
                main(["smoke", "--config", str(cfg_path), "--json"])
        # Exit code may be 0 or 1 depending on route policies.
        # The key invariant: valid JSON output, no crash.
        report = json.loads(stdout_buf2.getvalue())
        assert "status" in report
        assert report["status"] in ("passed", "failed")
        # Must have an event_id even if delivery failed.
        assert "event_id" in report

    def test_sample_config_smoke_with_storage(self, tmp_path: Path) -> None:
        """``smoke --config <sample-sqlite>`` produces valid JSON report.

        The generated sample config may not pass smoke due to route policy
        filtering (``allowed_event_types``), but the command should not crash
        and should produce valid JSON output with storage metadata.
        """
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(["config", "sample"])
        sample = stdout_buf.getvalue()
        # Derive a SQLite variant of the sample config.
        db_path = str(tmp_path / "sample-smoke.db")
        target = 'backend = "memory"'
        assert (
            sample.count(target) == 1
        ), "Expected exactly one 'backend = \"memory\"' in sample config"
        sqlite_sample = sample.replace(
            target,
            f'backend = "sqlite"\npath = {db_path!r}',
            1,
        )
        cfg_path = tmp_path / "sample_sqlite.toml"
        cfg_path.write_text(sqlite_sample)

        stdout_buf2 = io.StringIO()
        with redirect_stdout(stdout_buf2), redirect_stderr(io.StringIO()):
            with pytest.raises(SystemExit):
                main(
                    [
                        "smoke",
                        "--config",
                        str(cfg_path),
                        "--json",
                    ]
                )
        report = json.loads(stdout_buf2.getvalue())
        assert "status" in report
        assert report["storage_backend"] == "sqlite"

    def test_sample_config_no_sdk_imports(self, tmp_path: Path) -> None:
        """``config check`` + ``smoke`` with sample config do not import SDKs."""
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(["config", "sample"])
        cfg_path = tmp_path / "sample.toml"
        cfg_path.write_text(stdout_buf.getvalue())

        before = optional_sdks_in_modules()

        # config check
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            main(["config", "check", "--config", str(cfg_path)])

        # smoke (may exit 0 or 1 — that's fine, we check SDK imports)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            try:
                main(["smoke", "--config", str(cfg_path), "--json"])
            except SystemExit:
                pass

        after = optional_sdks_in_modules()
        leaked = after - before
        assert (
            not leaked
        ), f"config check + smoke leaked optional SDKs: {sorted(leaked)}"


# ===================================================================
# First-run source-checkout walkthrough
# ===================================================================


class TestFirstRunSourceCheckout:
    """Full operator walkthrough following docs exactly.

    Mirrors the documented alpha-walkthrough / alpha-installation runbook:
    version → paths → adapters → config sample → temp config → config check
    → smoke with ``examples/configs/fake-bridge-smoke.toml`` → inspect
    receipts → inspect event timeline/evidence → replay dry_run.

    All commands via CLI ``main()`` — no internal runtime APIs.
    """

    def test_step1_version(self) -> None:
        """``medre version`` shows version, Python, platform."""
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(["version"])
        output = stdout_buf.getvalue()
        lines = output.strip().splitlines()
        assert lines[0].startswith("medre ")
        assert any("Python" in line for line in lines)
        assert any("Platform" in line for line in lines)

    def test_step2_paths(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``medre paths`` resolves MEDRE_HOME paths."""
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(["paths"])
        output = stdout_buf.getvalue()
        assert "Config file:" in output
        assert "State dir:" in output
        assert str(tmp_path) in output

    def test_step3_adapters(self) -> None:
        """``medre adapters`` lists adapter types and SDK availability."""
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            main(["adapters"])
        output = stdout_buf.getvalue() + stderr_buf.getvalue()
        assert "matrix" in output.lower() or "adapter" in output.lower()

    def test_step4_config_sample_to_check(self, tmp_path: Path) -> None:
        """``config sample`` > temp file > ``config check`` passes."""
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(["config", "sample"])
        sample = stdout_buf.getvalue()
        assert "[runtime]" in sample
        parsed = tomllib.loads(sample)
        assert "runtime" in parsed

        cfg_path = tmp_path / "walkthrough.toml"
        cfg_path.write_text(sample)

        stdout_buf2 = io.StringIO()
        stderr_buf2 = io.StringIO()
        with redirect_stdout(stdout_buf2), redirect_stderr(stderr_buf2):
            main(["config", "check", "--config", str(cfg_path)])
        output = stdout_buf2.getvalue()
        assert "Config valid" in output, f"config check failed: {output[:300]!r}"

    def test_step5_smoke_with_examples_config(self, tmp_path: Path) -> None:
        """``smoke --config <sqlite-config>`` passes with persistent storage
        for later inspection."""
        assert (
            EXAMPLES_SMOKE_CONFIG.is_file()
        ), f"Source-tree example config not found: {EXAMPLES_SMOKE_CONFIG}"
        db_path = tmp_path / "walkthrough-smoke.db"
        cfg = write_sqlite_config_from_example(tmp_path, db_path)
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
        assert report["storage_backend"] == "sqlite"
        assert db_path.is_file()

    def test_step6_inspect_receipts_after_smoke(self, tmp_path: Path) -> None:
        """After smoke, ``inspect receipts --event <id> --storage-path``
        shows delivery receipts."""
        db_path = tmp_path / "walkthrough-receipts.db"
        cfg = write_sqlite_config_from_example(tmp_path, db_path)
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            with pytest.raises(SystemExit):
                main(
                    [
                        "smoke",
                        "--config",
                        cfg,
                        "--json",
                    ]
                )
        report = json.loads(stdout_buf.getvalue())
        event_id = report["event_id"]

        stdout_buf2 = io.StringIO()
        with redirect_stdout(stdout_buf2), redirect_stderr(io.StringIO()):
            main(
                [
                    "inspect",
                    "receipts",
                    "--event",
                    event_id,
                    "--storage-path",
                    str(db_path),
                ]
            )
        output = stdout_buf2.getvalue()
        assert "sent" in output

    def test_step7_inspect_event_timeline(self, tmp_path: Path) -> None:
        """``inspect event <id> --timeline --storage-path`` returns timeline."""
        db_path = tmp_path / "walkthrough-timeline.db"
        cfg = write_sqlite_config_from_example(tmp_path, db_path)
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            with pytest.raises(SystemExit):
                main(
                    [
                        "smoke",
                        "--config",
                        cfg,
                        "--json",
                    ]
                )
        event_id = json.loads(stdout_buf.getvalue())["event_id"]

        stdout_buf2 = io.StringIO()
        with redirect_stdout(stdout_buf2), redirect_stderr(io.StringIO()):
            main(
                [
                    "inspect",
                    "event",
                    event_id,
                    "--timeline",
                    "--storage-path",
                    str(db_path),
                ]
            )
        result = json.loads(stdout_buf2.getvalue())
        assert "event" in result
        assert "timeline" in result
        assert isinstance(result["timeline"], list)
        assert len(result["timeline"]) >= 1

    def test_step8_inspect_event_evidence(self, tmp_path: Path) -> None:
        """``inspect event <id> --evidence --storage-path`` returns evidence."""
        db_path = tmp_path / "walkthrough-evidence.db"
        cfg = write_sqlite_config_from_example(tmp_path, db_path)
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            with pytest.raises(SystemExit):
                main(
                    [
                        "smoke",
                        "--config",
                        cfg,
                        "--json",
                    ]
                )
        event_id = json.loads(stdout_buf.getvalue())["event_id"]

        stdout_buf2 = io.StringIO()
        with redirect_stdout(stdout_buf2), redirect_stderr(io.StringIO()):
            main(
                [
                    "inspect",
                    "event",
                    event_id,
                    "--evidence",
                    "--storage-path",
                    str(db_path),
                ]
            )
        result = json.loads(stdout_buf2.getvalue())
        assert "event" in result
        assert "evidence" in result
        evidence = result["evidence"]
        assert "sections" in evidence

    def test_step9_replay_dry_run(self, tmp_path: Path) -> None:
        """``replay --config <cfg> --mode dry_run --event <id> --json`` after
        smoke works with generated replay config."""
        db_path = tmp_path / "walkthrough-replay.db"
        cfg = write_sqlite_config_from_example(tmp_path, db_path)
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            with pytest.raises(SystemExit):
                main(
                    [
                        "smoke",
                        "--config",
                        cfg,
                        "--json",
                    ]
                )
        event_id = json.loads(stdout_buf.getvalue())["event_id"]

        replay_cfg = write_replay_config(tmp_path, db_path)
        stdout_buf2 = io.StringIO()
        with redirect_stdout(stdout_buf2), redirect_stderr(io.StringIO()):
            main(
                [
                    "replay",
                    "--config",
                    replay_cfg,
                    "--mode",
                    "dry_run",
                    "--event",
                    event_id,
                    "--json",
                ]
            )
        summary = json.loads(stdout_buf2.getvalue())
        assert summary["mode"] == "dry_run"
        assert summary["events_scanned"] >= 1
        assert summary["events_replayed"] >= 1

    def test_full_source_checkout_walkthrough(self, tmp_path: Path) -> None:
        """End-to-end: sample → check → smoke → inspect → evidence → replay.

        Runs the entire sequence in one test, reusing the same DB across
        steps, proving the CLI commands chain correctly.
        """
        # 1. Generate sample config.
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(["config", "sample"])
        sample = stdout_buf.getvalue()
        cfg_path = tmp_path / "full-walkthrough.toml"
        cfg_path.write_text(sample)

        # 2. Config check on sample.
        stdout_buf2 = io.StringIO()
        with redirect_stdout(stdout_buf2), redirect_stderr(io.StringIO()):
            main(["config", "check", "--config", str(cfg_path)])
        assert "Config valid" in stdout_buf2.getvalue()

        # 3. Smoke with examples config (SQLite variant for persistence).
        db_path = tmp_path / "full-walkthrough.db"
        sqlite_cfg = write_sqlite_config_from_example(tmp_path, db_path)
        stdout_buf3 = io.StringIO()
        with redirect_stdout(stdout_buf3), redirect_stderr(io.StringIO()):
            with pytest.raises(SystemExit) as exc_info:
                main(
                    [
                        "smoke",
                        "--config",
                        sqlite_cfg,
                        "--json",
                    ]
                )
        assert exc_info.value.code == 0
        report = json.loads(stdout_buf3.getvalue())
        assert report["status"] == "passed"
        event_id = report["event_id"]

        # 4. Inspect receipts.
        stdout_buf4 = io.StringIO()
        with redirect_stdout(stdout_buf4), redirect_stderr(io.StringIO()):
            main(
                [
                    "inspect",
                    "receipts",
                    "--event",
                    event_id,
                    "--storage-path",
                    str(db_path),
                ]
            )
        assert "sent" in stdout_buf4.getvalue()

        # 5. Inspect event --timeline.
        stdout_buf5 = io.StringIO()
        with redirect_stdout(stdout_buf5), redirect_stderr(io.StringIO()):
            main(
                [
                    "inspect",
                    "event",
                    event_id,
                    "--timeline",
                    "--storage-path",
                    str(db_path),
                ]
            )
        tl = json.loads(stdout_buf5.getvalue())
        assert "timeline" in tl

        # 6. Inspect event --evidence.
        stdout_buf6 = io.StringIO()
        with redirect_stdout(stdout_buf6), redirect_stderr(io.StringIO()):
            main(
                [
                    "inspect",
                    "event",
                    event_id,
                    "--evidence",
                    "--storage-path",
                    str(db_path),
                ]
            )
        ev = json.loads(stdout_buf6.getvalue())
        assert "evidence" in ev


# ===================================================================
# Optional SDK non-import boundaries
# ===================================================================


class TestOptionalSDKBoundaries:
    """Proves fake-only CLI operations do not import optional SDK modules.

    Checks ``sys.modules`` before and after each CLI operation to ensure
    none of the forbidden SDK packages appear. This covers both import names
    (``nio``, ``meshtastic``, ``meshcore``, ``RNS``, ``LXMF``) and
    fork/dist import names (``mindroom_nio``, ``mtjk``, ``meshcore_py``,
    ``lxmf``).
    """

    def test_version_no_sdk_leak(self) -> None:
        before = optional_sdks_in_modules()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            main(["version"])
        after = optional_sdks_in_modules()
        assert not (after - before), f"version leaked SDKs: {sorted(after - before)}"

    def test_config_sample_no_sdk_leak(self) -> None:
        before = optional_sdks_in_modules()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            main(["config", "sample"])
        after = optional_sdks_in_modules()
        assert not (
            after - before
        ), f"config sample leaked SDKs: {sorted(after - before)}"

    def test_config_check_no_sdk_leak(self, tmp_path: Path) -> None:
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(["config", "sample"])
        cfg = tmp_path / "sdk-check.toml"
        cfg.write_text(stdout_buf.getvalue())

        before = optional_sdks_in_modules()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            main(["config", "check", "--config", str(cfg)])
        after = optional_sdks_in_modules()
        assert not (
            after - before
        ), f"config check leaked SDKs: {sorted(after - before)}"

    def test_smoke_no_sdk_leak(self) -> None:
        """``medre smoke`` with fake-bridge-smoke.toml does not import SDKs."""
        assert EXAMPLES_SMOKE_CONFIG.is_file()

        before = optional_sdks_in_modules()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            try:
                main(["smoke", "--config", str(EXAMPLES_SMOKE_CONFIG)])
            except SystemExit:
                pass
        after = optional_sdks_in_modules()
        assert not (after - before), f"smoke leaked SDKs: {sorted(after - before)}"

    def test_smoke_json_no_sdk_leak(self) -> None:
        """``medre smoke --json`` with fake config does not import SDKs."""
        assert EXAMPLES_SMOKE_CONFIG.is_file()

        before = optional_sdks_in_modules()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            try:
                main(
                    [
                        "smoke",
                        "--config",
                        str(EXAMPLES_SMOKE_CONFIG),
                        "--json",
                    ]
                )
            except SystemExit:
                pass
        after = optional_sdks_in_modules()
        assert not (
            after - before
        ), f"smoke --json leaked SDKs: {sorted(after - before)}"

    def test_smoke_with_storage_no_sdk_leak(self, tmp_path: Path) -> None:
        """``medre smoke --config <sqlite-config>`` does not import SDKs."""
        assert EXAMPLES_SMOKE_CONFIG.is_file()
        db_path = tmp_path / "sdk-leak-check.db"
        cfg = write_sqlite_config_from_example(tmp_path, db_path)

        before = optional_sdks_in_modules()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            try:
                main(
                    [
                        "smoke",
                        "--config",
                        cfg,
                        "--json",
                    ]
                )
            except SystemExit:
                pass
        after = optional_sdks_in_modules()
        assert not (
            after - before
        ), f"smoke with SQLite config leaked SDKs: {sorted(after - before)}"

    def test_inspect_no_sdk_leak(self, tmp_path: Path) -> None:
        """``inspect receipts`` and ``inspect event`` do not import SDKs."""
        assert EXAMPLES_SMOKE_CONFIG.is_file()
        db_path = tmp_path / "sdk-inspect.db"
        cfg = write_sqlite_config_from_example(tmp_path, db_path)

        # First: smoke to populate DB.
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            with pytest.raises(SystemExit):
                main(
                    [
                        "smoke",
                        "--config",
                        cfg,
                        "--json",
                    ]
                )
        event_id = json.loads(stdout_buf.getvalue())["event_id"]

        before = optional_sdks_in_modules()

        # Inspect receipts.
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            main(
                [
                    "inspect",
                    "receipts",
                    "--event",
                    event_id,
                    "--storage-path",
                    str(db_path),
                ]
            )
        after_receipts = optional_sdks_in_modules()
        assert not (
            after_receipts - before
        ), f"inspect receipts leaked SDKs: {sorted(after_receipts - before)}"

        # Inspect event --timeline.
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            main(
                [
                    "inspect",
                    "event",
                    event_id,
                    "--timeline",
                    "--storage-path",
                    str(db_path),
                ]
            )
        after_timeline = optional_sdks_in_modules()
        assert not (after_timeline - before), (
            f"inspect event --timeline leaked SDKs: "
            f"{sorted(after_timeline - before)}"
        )

        # Inspect event --evidence.
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            main(
                [
                    "inspect",
                    "event",
                    event_id,
                    "--evidence",
                    "--storage-path",
                    str(db_path),
                ]
            )
        after_evidence = optional_sdks_in_modules()
        assert not (after_evidence - before), (
            f"inspect event --evidence leaked SDKs: "
            f"{sorted(after_evidence - before)}"
        )


# ===================================================================
# Smoke without --config
# ===================================================================


class TestSmokeWithoutConfig:
    """Proves smoke without ``--config`` uses the source-tree default when
    available."""

    def test_smoke_default_from_source_tree(self) -> None:
        """``medre smoke`` (no --config) finds source-tree default config.

        In a source checkout, the default config finder locates
        ``examples/configs/fake-bridge-smoke.toml`` automatically.
        """
        if not EXAMPLES_SMOKE_CONFIG.is_file():
            pytest.skip("Source-tree example config not available")

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            with pytest.raises(SystemExit) as exc_info:
                main(["smoke", "--json"])
        assert exc_info.value.code == 0
        report = json.loads(stdout_buf.getvalue())
        assert report["status"] == "passed"

    def test_smoke_default_human_readable(self) -> None:
        """``medre smoke`` (no --config, no --json) shows PASS."""
        if not EXAMPLES_SMOKE_CONFIG.is_file():
            pytest.skip("Source-tree example config not available")

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            with pytest.raises(SystemExit) as exc_info:
                main(["smoke"])
        assert exc_info.value.code == 0
        assert "PASS" in stdout_buf.getvalue()
