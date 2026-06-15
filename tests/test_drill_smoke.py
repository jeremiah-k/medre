"""Tests for operator failure drills and smoke persistence.

Proves that:

- ``medre smoke --config <sqlite-config>`` persists evidence to SQLite.
- ``run_drill()`` produces valid PASS reports for each drill.
- Drill reports have the correct shape and evidence fields.
- No Docker, network, or SDK dependencies.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

import pytest

from medre.runtime.drill import AVAILABLE_DRILLS, run_drill
from medre.runtime.smoke import run_fake_bridge_smoke

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


def _smoke_config_path() -> str:
    """Return path to the shipped fake-bridge-smoke.yaml."""
    from medre.runtime.smoke import _default_smoke_config_path

    path = _default_smoke_config_path()
    assert path is not None, "examples/configs/fake-bridge-smoke.yaml not found"
    return path


def _write_sqlite_smoke_config(tmp_path: Path, db_path: str) -> str:
    """Write a YAML config with SQLite storage at *db_path* for smoke CLI tests."""
    cfg = tmp_path / "smoke_sqlite.yaml"
    cfg.write_text(f"""\
runtime:
  name: fake-bridge-smoke-persist
  shutdown_timeout_seconds: 10
logging:
  level: WARNING
  format: text
storage:
  backend: sqlite
  path: {db_path!r}
adapters:
  matrix:
    fake_matrix:
      enabled: true
      adapter_kind: fake
      homeserver: https://fake.local
      user_id: "@bridge-bot:fake.local"
      access_token: fake_token_bridge_smoke
      room_allowlist:
        - "!bridge-room:fake.local"
      encryption_mode: plaintext
  meshtastic:
    fake_meshtastic:
      enabled: true
      adapter_kind: fake
      connection_type: fake
      origin_label: smoke-radio
routes:
  mx_to_mesh:
    source_adapters:
      - fake_matrix
    dest_adapters:
      - fake_meshtastic
    directionality: source_to_dest
    enabled: true
""")
    return str(cfg)


# ---------------------------------------------------------------------------
# Storage persistence tests
# ---------------------------------------------------------------------------


class TestSmokeStoragePath:
    """Proves config-driven SQLite storage persists smoke evidence."""

    @pytest.mark.asyncio
    async def test_storage_path_creates_sqlite(self, tmp_path: Path) -> None:
        """Storage path creates a real SQLite file."""
        db_path = str(tmp_path / "smoke-test.db")
        config_path = _smoke_config_path()
        report = await run_fake_bridge_smoke(
            config_path,
            storage_path=db_path,
        )
        assert report["status"] == "passed", report.get("fail_reasons", [])
        assert Path(db_path).is_file()

    @pytest.mark.asyncio
    async def test_storage_path_report_includes_path(self, tmp_path: Path) -> None:
        """Report includes storage_path and storage_backend when set."""
        db_path = str(tmp_path / "smoke-test.db")
        report = await run_fake_bridge_smoke(
            _smoke_config_path(),
            storage_path=db_path,
        )
        assert report["storage_path"] == db_path
        assert report["storage_backend"] == "sqlite"

    @pytest.mark.asyncio
    async def test_default_stays_memory(self) -> None:
        """Without storage_path, report has memory backend and no storage_path key."""
        report = await run_fake_bridge_smoke(_smoke_config_path())
        assert report["storage_backend"] == "memory"
        assert "storage_path" not in report

    @pytest.mark.asyncio
    async def test_storage_path_data_persists(self, tmp_path: Path) -> None:
        """Stored event is retrievable from the SQLite file after smoke."""
        db_path = str(tmp_path / "smoke-test.db")
        report = await run_fake_bridge_smoke(
            _smoke_config_path(),
            storage_path=db_path,
        )
        event_id = report["event_id"]
        # Direct SQLite query to prove persistence.
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                "SELECT count(*) FROM canonical_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            assert rows is not None and rows[0] >= 1
        finally:
            conn.close()

    @pytest.mark.asyncio
    async def test_storage_path_receipts_persist(self, tmp_path: Path) -> None:
        """Delivery receipts are persisted to SQLite."""
        db_path = str(tmp_path / "smoke-test.db")
        report = await run_fake_bridge_smoke(
            _smoke_config_path(),
            storage_path=db_path,
        )
        event_id = report["event_id"]
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                "SELECT count(*) FROM delivery_receipts WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            assert rows is not None and rows[0] >= 1
        finally:
            conn.close()

    def test_cli_sqlite_config_persists(self, tmp_path: Path) -> None:
        """CLI medre smoke --config <sqlite-config> persists evidence."""
        import io

        from medre.cli import main

        db_path = str(tmp_path / "cli-test.db")
        config_path = _write_sqlite_smoke_config(tmp_path, db_path)

        stdout_capture = io.StringIO()
        stderr_capture = io.StringIO()
        with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
            with pytest.raises(SystemExit) as exc_info:
                main(
                    [
                        "smoke",
                        "--config",
                        config_path,
                        "--json",
                    ]
                )

        assert exc_info.value.code == 0
        report = json.loads(stdout_capture.getvalue())
        assert report["status"] == "passed"
        assert report["storage_path"] == db_path
        assert report["storage_backend"] == "sqlite"

    def test_cli_sqlite_config_human_readable(self, tmp_path: Path) -> None:
        """CLI medre smoke --config <sqlite-config> (no --json) shows storage info."""
        import io

        from medre.cli import main

        db_path = str(tmp_path / "cli-human.db")
        config_path = _write_sqlite_smoke_config(tmp_path, db_path)

        stdout_capture = io.StringIO()
        stderr_capture = io.StringIO()
        with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
            with pytest.raises(SystemExit) as exc_info:
                main(
                    [
                        "smoke",
                        "--config",
                        config_path,
                    ]
                )

        assert exc_info.value.code == 0
        output = stdout_capture.getvalue()
        assert "Storage:" in output


# ---------------------------------------------------------------------------
# Drill infrastructure tests
# ---------------------------------------------------------------------------


class TestDrillInfrastructure:
    """Proves drill dispatch and report shape."""

    @pytest.mark.asyncio
    async def test_unknown_drill_returns_fail(self) -> None:
        """Unknown drill name returns FAIL report."""
        report = await run_drill("nonexistent_drill")
        assert report["status"] == "failed"
        assert report["evidence_level"] == "drill"
        assert "Unknown drill" in report["fail_reasons"][0]

    @pytest.mark.asyncio
    async def test_available_drills_list(self) -> None:
        """AVAILABLE_DRILLS contains all expected drill names."""
        expected = {
            "renderer_failure",
            "adapter_permanent_failure",
            "adapter_transient_failure",
            "capacity_rejection",
            "shutdown_rejection",
            "replay_duplicate_risk",
            "degraded_live_health",
            "bad_route_config",
            "all_adapters_build_fail",
            "partial_degraded_startup",
            "all_adapters_start_fail",
        }
        assert set(AVAILABLE_DRILLS) == expected

    def test_cli_drill_dispatch(self) -> None:
        """CLI medre smoke --drill renderer_failure --json works."""
        import io

        from medre.cli import main

        config_path = _smoke_config_path()
        stdout_capture = io.StringIO()
        stderr_capture = io.StringIO()
        with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
            with pytest.raises(SystemExit) as exc_info:
                main(
                    [
                        "smoke",
                        "--config",
                        config_path,
                        "--drill",
                        "renderer_failure",
                        "--json",
                    ]
                )

        assert exc_info.value.code == 0
        report = json.loads(stdout_capture.getvalue())
        assert report["status"] == "passed"
        assert report["drill_name"] == "renderer_failure"

    def test_cli_drill_human_readable(self) -> None:
        """CLI medre smoke --drill shows human-readable output."""
        import io

        from medre.cli import main

        config_path = _smoke_config_path()
        stdout_capture = io.StringIO()
        stderr_capture = io.StringIO()
        with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
            with pytest.raises(SystemExit) as exc_info:
                main(
                    [
                        "smoke",
                        "--config",
                        config_path,
                        "--drill",
                        "renderer_failure",
                    ]
                )

        assert exc_info.value.code == 0
        output = stdout_capture.getvalue()
        assert "PASS" in output

    def test_cli_drill_unknown_name_fails(self) -> None:
        """CLI medre smoke --drill nonexistent exits 1."""
        import io

        from medre.cli import main

        config_path = _smoke_config_path()
        stdout_capture = io.StringIO()
        stderr_capture = io.StringIO()
        with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
            with pytest.raises(SystemExit) as exc_info:
                main(
                    [
                        "smoke",
                        "--config",
                        config_path,
                        "--drill",
                        "nonexistent_drill",
                        "--json",
                    ]
                )

        assert exc_info.value.code == 1
        report = json.loads(stdout_capture.getvalue())
        assert report["status"] == "failed"


# ---------------------------------------------------------------------------
# Individual drill tests
# ---------------------------------------------------------------------------


class TestRendererFailureDrill:
    """Proves renderer_failure drill exercises RENDERER_FAILURE path."""

    @pytest.mark.asyncio
    async def test_pass(self) -> None:
        report = await run_drill("renderer_failure", config_path=_smoke_config_path())
        assert report["status"] == "passed", report.get("fail_reasons", [])
        assert report["drill_name"] == "renderer_failure"
        assert report["evidence_level"] == "drill"
        assert "event_id" in report
        assert "drill_steps" in report
        assert len(report["drill_steps"]) >= 3

    @pytest.mark.asyncio
    async def test_json_safe(self) -> None:
        report = await run_drill("renderer_failure", config_path=_smoke_config_path())
        serialized = json.dumps(report, sort_keys=True)
        assert isinstance(serialized, str)

    @pytest.mark.asyncio
    async def test_has_failed_receipt(self) -> None:
        """Renderer failure produces a 'failed' receipt."""
        report = await run_drill("renderer_failure", config_path=_smoke_config_path())
        steps = report["drill_steps"]
        verify_step = next(
            (s for s in steps if s["step"] == "verify_failed_receipt"),
            None,
        )
        assert verify_step is not None
        assert verify_step["result"] == "ok"
        assert verify_step["has_failed_receipt"] is True


class TestAdapterPermanentFailureDrill:
    """Proves adapter_permanent_failure drill exercises ADAPTER_PERMANENT path."""

    @pytest.mark.asyncio
    async def test_pass(self) -> None:
        report = await run_drill(
            "adapter_permanent_failure",
            config_path=_smoke_config_path(),
        )
        assert report["status"] == "passed", report.get("fail_reasons", [])

    @pytest.mark.asyncio
    async def test_outcome_is_permanent(self) -> None:
        report = await run_drill(
            "adapter_permanent_failure",
            config_path=_smoke_config_path(),
        )
        steps = report["drill_steps"]
        verify_step = next(
            (s for s in steps if s["step"] == "verify_permanent_failure"),
            None,
        )
        assert verify_step is not None
        assert verify_step["observed"] == "permanent_failure"

    @pytest.mark.asyncio
    async def test_json_safe(self) -> None:
        report = await run_drill(
            "adapter_permanent_failure",
            config_path=_smoke_config_path(),
        )
        json.dumps(report, sort_keys=True)


class TestAdapterTransientFailureDrill:
    """Proves adapter_transient_failure drill exercises ADAPTER_TRANSIENT path."""

    @pytest.mark.asyncio
    async def test_pass(self) -> None:
        report = await run_drill(
            "adapter_transient_failure",
            config_path=_smoke_config_path(),
        )
        assert report["status"] == "passed", report.get("fail_reasons", [])

    @pytest.mark.asyncio
    async def test_outcome_is_transient(self) -> None:
        report = await run_drill(
            "adapter_transient_failure",
            config_path=_smoke_config_path(),
        )
        steps = report["drill_steps"]
        verify_step = next(
            (s for s in steps if s["step"] == "verify_transient_failure"),
            None,
        )
        assert verify_step is not None
        assert verify_step["observed"] == "transient_failure"

    @pytest.mark.asyncio
    async def test_failure_is_retryable(self) -> None:
        report = await run_drill(
            "adapter_transient_failure",
            config_path=_smoke_config_path(),
        )
        steps = report["drill_steps"]
        retry_step = next(
            (s for s in steps if s["step"] == "verify_retryable"),
            None,
        )
        assert retry_step is not None
        assert retry_step["is_retryable"] is True

    @pytest.mark.asyncio
    async def test_recovery_path_in_report(self) -> None:
        """Recovery simulation produces recovery_path evidence."""
        report = await run_drill(
            "adapter_transient_failure",
            config_path=_smoke_config_path(),
        )
        assert "recovery_path" in report
        rp = report["recovery_path"]
        assert rp["failure_kind"] == "ADAPTER_TRANSIENT"
        assert rp["is_retryable"] is True
        assert rp["recovery_simulated"] is True
        assert rp["receipt_before_recovery"]["status"] == "transient_failure"
        assert rp["receipt_after_recovery"]["status"] == "success"

    @pytest.mark.asyncio
    async def test_recovery_step_in_drill_steps(self) -> None:
        """Recovery step appears in drill_steps."""
        report = await run_drill(
            "adapter_transient_failure",
            config_path=_smoke_config_path(),
        )
        steps = report["drill_steps"]
        recovery_step = next(
            (s for s in steps if s["step"] == "simulate_manual_recovery"),
            None,
        )
        assert recovery_step is not None
        assert recovery_step["result"] == "ok"

    @pytest.mark.asyncio
    async def test_json_safe(self) -> None:
        report = await run_drill(
            "adapter_transient_failure",
            config_path=_smoke_config_path(),
        )
        json.dumps(report, sort_keys=True)


class TestCapacityRejectionDrill:
    """Proves capacity_rejection drill exercises CAPACITY_REJECTION path."""

    @pytest.mark.asyncio
    async def test_pass(self) -> None:
        report = await run_drill(
            "capacity_rejection",
            config_path=_smoke_config_path(),
        )
        assert report["status"] == "passed", report.get("fail_reasons", [])

    @pytest.mark.asyncio
    async def test_suppressed_receipt_for_rejected(self) -> None:
        """Capacity rejection produces suppressed evidence receipt."""
        report = await run_drill(
            "capacity_rejection",
            config_path=_smoke_config_path(),
        )
        steps = report["drill_steps"]
        suppressed_rcpt = next(
            (s for s in steps if s["step"] == "verify_suppressed_receipts"),
            None,
        )
        assert suppressed_rcpt is not None
        assert suppressed_rcpt["result"] == "ok"
        assert suppressed_rcpt["all_suppressed"] is True

    @pytest.mark.asyncio
    async def test_json_safe(self) -> None:
        report = await run_drill(
            "capacity_rejection",
            config_path=_smoke_config_path(),
        )
        json.dumps(report, sort_keys=True)


class TestShutdownRejectionDrill:
    """Proves shutdown_rejection drill exercises SHUTDOWN_REJECTION path."""

    @pytest.mark.asyncio
    async def test_pass(self) -> None:
        report = await run_drill(
            "shutdown_rejection",
            config_path=_smoke_config_path(),
        )
        assert report["status"] == "passed", report.get("fail_reasons", [])

    @pytest.mark.asyncio
    async def test_suppressed_receipt_for_rejected(self) -> None:
        """Shutdown rejection produces suppressed evidence receipt."""
        report = await run_drill(
            "shutdown_rejection",
            config_path=_smoke_config_path(),
        )
        steps = report["drill_steps"]
        suppressed_rcpt = next(
            (s for s in steps if s["step"] == "verify_suppressed_receipts"),
            None,
        )
        assert suppressed_rcpt is not None
        assert suppressed_rcpt["result"] == "ok"
        assert suppressed_rcpt["all_suppressed"] is True

    @pytest.mark.asyncio
    async def test_rejection_timeline_in_report(self) -> None:
        """Rejection timeline shows stop/inject timestamps."""
        report = await run_drill(
            "shutdown_rejection",
            config_path=_smoke_config_path(),
        )
        assert "rejection_timeline" in report
        tl = report["rejection_timeline"]
        assert tl["accepting_work_at_rejection"] is False
        assert tl["suppressed_receipts_created"] >= 1
        assert tl["shutdown_rejections"] >= 1
        assert "stop_accepting_at" in tl
        assert "inject_at" in tl

    @pytest.mark.asyncio
    async def test_json_safe(self) -> None:
        report = await run_drill(
            "shutdown_rejection",
            config_path=_smoke_config_path(),
        )
        json.dumps(report, sort_keys=True)


class TestReplayDuplicateRiskDrill:
    """Proves replay_duplicate_risk drill shows duplicate receipt creation."""

    @pytest.mark.asyncio
    async def test_pass(self) -> None:
        report = await run_drill(
            "replay_duplicate_risk",
            config_path=_smoke_config_path(),
        )
        assert report["status"] == "passed", report.get("fail_reasons", [])

    @pytest.mark.asyncio
    async def test_duplicate_receipts_created(self) -> None:
        """Replay creates new receipts beyond the initial delivery."""
        report = await run_drill(
            "replay_duplicate_risk",
            config_path=_smoke_config_path(),
        )
        steps = report["drill_steps"]
        dup_step = next(
            (s for s in steps if s["step"] == "verify_duplicate_receipts"),
            None,
        )
        assert dup_step is not None
        assert dup_step["new_receipts"] > 0

    @pytest.mark.asyncio
    async def test_receipt_timeline_in_report(self) -> None:
        """Receipt timeline shows live vs replay counts."""
        report = await run_drill(
            "replay_duplicate_risk",
            config_path=_smoke_config_path(),
        )
        assert "receipt_timeline" in report
        tl = report["receipt_timeline"]
        assert tl["live_receipt_count"] > 0
        assert tl["replay_receipt_count"] > 0
        assert tl["total_receipt_count"] > tl["live_receipt_count"]
        assert tl["replay_run_id"] is not None
        assert tl["timeline_verified"] is True

    @pytest.mark.asyncio
    async def test_json_safe(self) -> None:
        report = await run_drill(
            "replay_duplicate_risk",
            config_path=_smoke_config_path(),
        )
        json.dumps(report, sort_keys=True)


class TestDegradedLiveHealthDrill:
    """Proves degraded_live_health drill observes degraded adapter health."""

    @pytest.mark.asyncio
    async def test_pass(self) -> None:
        report = await run_drill(
            "degraded_live_health",
            config_path=_smoke_config_path(),
        )
        assert report["status"] == "passed", report.get("fail_reasons", [])

    @pytest.mark.asyncio
    async def test_degraded_observed(self) -> None:
        """Drill observes 'degraded' health status."""
        report = await run_drill(
            "degraded_live_health",
            config_path=_smoke_config_path(),
        )
        steps = report["drill_steps"]
        verify_step = next(
            (s for s in steps if s["step"] == "verify_degraded"),
            None,
        )
        assert verify_step is not None
        assert verify_step["observed_health"] == "degraded"

    @pytest.mark.asyncio
    async def test_health_timeline_in_report(self) -> None:
        """Health timeline shows before/after and correlation."""
        report = await run_drill(
            "degraded_live_health",
            config_path=_smoke_config_path(),
        )
        assert "health_timeline" in report
        tl = report["health_timeline"]
        assert tl["health_after"] == "degraded"
        assert tl["health_before"] in ("healthy", "unknown")
        assert tl["correlation_verified"] is True
        assert "patched_at" in tl
        assert "refresh_at" in tl
        assert "snapshot_at" in tl
        assert tl["target_adapter"] is not None

    @pytest.mark.asyncio
    async def test_json_safe(self) -> None:
        report = await run_drill(
            "degraded_live_health",
            config_path=_smoke_config_path(),
        )
        json.dumps(report, sort_keys=True)


# ---------------------------------------------------------------------------
# Pre-runtime drill tests
# ---------------------------------------------------------------------------


class TestBadRouteConfigDrill:
    """Proves bad_route_config drill exercises route validation failure."""

    @pytest.mark.asyncio
    async def test_pass(self) -> None:
        report = await run_drill("bad_route_config")
        assert report["status"] == "passed", report.get("fail_reasons", [])
        assert report["drill_name"] == "bad_route_config"
        assert report["evidence_level"] == "drill"

    @pytest.mark.asyncio
    async def test_route_id_in_report(self) -> None:
        report = await run_drill("bad_route_config")
        assert report["route_id"] == "bad_route"

    @pytest.mark.asyncio
    async def test_unknown_adapter_in_report(self) -> None:
        report = await run_drill("bad_route_config")
        assert report["unknown_adapter_id"] == "ghost_adapter"

    @pytest.mark.asyncio
    async def test_known_adapter_ids(self) -> None:
        report = await run_drill("bad_route_config")
        assert report["known_adapter_ids"] == ["fake_matrix"]

    @pytest.mark.asyncio
    async def test_error_type_is_route_validation(self) -> None:
        report = await run_drill("bad_route_config")
        assert report["error_type"] == "RouteValidationError"
        assert "ghost_adapter" in report["error_message"]

    @pytest.mark.asyncio
    async def test_runtime_not_started(self) -> None:
        report = await run_drill("bad_route_config")
        assert report["runtime_started"] is False
        assert report["build_succeeded"] is False

    @pytest.mark.asyncio
    async def test_json_safe(self) -> None:
        report = await run_drill("bad_route_config")
        serialized = json.dumps(report, sort_keys=True)
        assert isinstance(serialized, str)

    def test_cli_dispatch(self) -> None:
        """CLI medre smoke --drill bad_route_config --json works."""
        import io

        from medre.cli import main

        config_path = _smoke_config_path()
        stdout_capture = io.StringIO()
        stderr_capture = io.StringIO()
        with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
            with pytest.raises(SystemExit) as exc_info:
                main(
                    [
                        "smoke",
                        "--config",
                        config_path,
                        "--drill",
                        "bad_route_config",
                        "--json",
                    ]
                )

        assert exc_info.value.code == 0
        report = json.loads(stdout_capture.getvalue())
        assert report["status"] == "passed"
        assert report["drill_name"] == "bad_route_config"


class TestAllAdaptersBuildFailDrill:
    """Proves all_adapters_build_fail drill exercises build failure recording."""

    @pytest.mark.asyncio
    async def test_pass(self) -> None:
        report = await run_drill("all_adapters_build_fail")
        assert report["status"] == "passed", report.get("fail_reasons", [])
        assert report["drill_name"] == "all_adapters_build_fail"

    @pytest.mark.asyncio
    async def test_build_failures_count(self) -> None:
        report = await run_drill("all_adapters_build_fail")
        assert report["failed_adapter_count"] == 2
        assert report["build_failure_count"] == 2

    @pytest.mark.asyncio
    async def test_build_failures_have_attribution(self) -> None:
        report = await run_drill("all_adapters_build_fail")
        failures = report["build_failures"]
        assert len(failures) == 2
        adapter_ids = {f["adapter_id"] for f in failures}
        assert adapter_ids == {"broken1", "broken2"}
        transports = {f["transport"] for f in failures}
        assert "matrix" in transports
        assert "meshtastic" in transports

    @pytest.mark.asyncio
    async def test_no_adapters_built(self) -> None:
        report = await run_drill("all_adapters_build_fail")
        assert report["known_adapter_ids"] == []

    @pytest.mark.asyncio
    async def test_runtime_not_started(self) -> None:
        report = await run_drill("all_adapters_build_fail")
        assert report["runtime_started"] is False

    @pytest.mark.asyncio
    async def test_json_safe(self) -> None:
        report = await run_drill("all_adapters_build_fail")
        serialized = json.dumps(report, sort_keys=True)
        assert isinstance(serialized, str)

    def test_cli_dispatch(self) -> None:
        """CLI medre smoke --drill all_adapters_build_fail --json works."""
        import io

        from medre.cli import main

        config_path = _smoke_config_path()
        stdout_capture = io.StringIO()
        stderr_capture = io.StringIO()
        with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
            with pytest.raises(SystemExit) as exc_info:
                main(
                    [
                        "smoke",
                        "--config",
                        config_path,
                        "--drill",
                        "all_adapters_build_fail",
                        "--json",
                    ]
                )

        assert exc_info.value.code == 0
        report = json.loads(stdout_capture.getvalue())
        assert report["status"] == "passed"


class TestPartialDegradedStartupDrill:
    """Proves partial_degraded_startup drill exercises degraded startup."""

    @pytest.mark.asyncio
    async def test_pass(self) -> None:
        report = await run_drill("partial_degraded_startup")
        assert report["status"] == "passed", report.get("fail_reasons", [])

    @pytest.mark.asyncio
    async def test_outcome_is_partial(self) -> None:
        report = await run_drill("partial_degraded_startup")
        assert report["startup_outcome"] == "partial"

    @pytest.mark.asyncio
    async def test_health_is_degraded(self) -> None:
        report = await run_drill("partial_degraded_startup")
        assert report["runtime_health"] == "degraded"

    @pytest.mark.asyncio
    async def test_started_adapters(self) -> None:
        report = await run_drill("partial_degraded_startup")
        assert report["started_adapters"] == ["alpha", "gamma"]

    @pytest.mark.asyncio
    async def test_failed_adapters(self) -> None:
        report = await run_drill("partial_degraded_startup")
        assert report["failed_adapters"] == ["beta"]

    @pytest.mark.asyncio
    async def test_boot_summary_counts(self) -> None:
        report = await run_drill("partial_degraded_startup")
        boot = report["boot_summary"]
        assert boot is not None
        assert boot["adapters_started"] == 2
        assert boot["adapters_failed"] == 1
        assert boot["adapters_total"] == 3

    @pytest.mark.asyncio
    async def test_json_safe(self) -> None:
        report = await run_drill("partial_degraded_startup")
        serialized = json.dumps(report, sort_keys=True)
        assert isinstance(serialized, str)

    def test_cli_dispatch(self) -> None:
        """CLI medre smoke --drill partial_degraded_startup --json works."""
        import io

        from medre.cli import main

        config_path = _smoke_config_path()
        stdout_capture = io.StringIO()
        stderr_capture = io.StringIO()
        with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
            with pytest.raises(SystemExit) as exc_info:
                main(
                    [
                        "smoke",
                        "--config",
                        config_path,
                        "--drill",
                        "partial_degraded_startup",
                        "--json",
                    ]
                )

        assert exc_info.value.code == 0
        report = json.loads(stdout_capture.getvalue())
        assert report["status"] == "passed"


class TestAllAdaptersStartFailDrill:
    """Proves all_adapters_start_fail drill exercises total startup failure."""

    @pytest.mark.asyncio
    async def test_pass(self) -> None:
        report = await run_drill("all_adapters_start_fail")
        assert report["status"] == "passed", report.get("fail_reasons", [])

    @pytest.mark.asyncio
    async def test_outcome_is_total_failure(self) -> None:
        report = await run_drill("all_adapters_start_fail")
        assert report["startup_outcome"] == "total_failure"

    @pytest.mark.asyncio
    async def test_no_started_adapters(self) -> None:
        report = await run_drill("all_adapters_start_fail")
        assert report["started_adapters"] == []

    @pytest.mark.asyncio
    async def test_all_adapters_failed(self) -> None:
        report = await run_drill("all_adapters_start_fail")
        assert sorted(report["failed_adapters"]) == ["alpha", "beta"]

    @pytest.mark.asyncio
    async def test_cleanup_pipeline_stopped(self) -> None:
        report = await run_drill("all_adapters_start_fail")
        assert report["cleanup_evidence"]["pipeline_stopped"] is True

    @pytest.mark.asyncio
    async def test_cleanup_storage_closed(self) -> None:
        report = await run_drill("all_adapters_start_fail")
        assert report["cleanup_evidence"]["storage_closed"] is True

    @pytest.mark.asyncio
    async def test_cleanup_adapters_stopped(self) -> None:
        report = await run_drill("all_adapters_start_fail")
        assert report["cleanup_evidence"]["adapters_stopped"] is True

    @pytest.mark.asyncio
    async def test_app_state_is_failed(self) -> None:
        report = await run_drill("all_adapters_start_fail")
        assert report["cleanup_evidence"]["app_state"] == "failed"

    @pytest.mark.asyncio
    async def test_json_safe(self) -> None:
        report = await run_drill("all_adapters_start_fail")
        serialized = json.dumps(report, sort_keys=True)
        assert isinstance(serialized, str)

    def test_cli_dispatch(self) -> None:
        """CLI medre smoke --drill all_adapters_start_fail --json works."""
        import io

        from medre.cli import main

        config_path = _smoke_config_path()
        stdout_capture = io.StringIO()
        stderr_capture = io.StringIO()
        with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
            with pytest.raises(SystemExit) as exc_info:
                main(
                    [
                        "smoke",
                        "--config",
                        config_path,
                        "--drill",
                        "all_adapters_start_fail",
                        "--json",
                    ]
                )

        assert exc_info.value.code == 0
        report = json.loads(stdout_capture.getvalue())
        assert report["status"] == "passed"


class TestDrillCrossCutting:
    """Proves all drills share correct report shape."""

    @pytest.mark.parametrize("drill_name", AVAILABLE_DRILLS)
    @pytest.mark.asyncio
    async def test_report_has_required_fields(self, drill_name: str) -> None:
        """Every drill report has the required top-level fields."""
        report = await run_drill(drill_name, config_path=_smoke_config_path())
        for field in (
            "status",
            "evidence_level",
            "drill_name",
            "timestamp",
            "config_source",
            "storage_backend",
            "drill_steps",
            "limitations",
        ):
            assert field in report, f"Missing field {field!r} in {drill_name}"

    @pytest.mark.parametrize("drill_name", AVAILABLE_DRILLS)
    @pytest.mark.asyncio
    async def test_report_passes(self, drill_name: str) -> None:
        """Every drill produces a PASS report."""
        report = await run_drill(drill_name, config_path=_smoke_config_path())
        assert (
            report["status"] == "passed"
        ), f"Drill {drill_name} failed: {report.get('fail_reasons', [])}"

    @pytest.mark.parametrize("drill_name", AVAILABLE_DRILLS)
    @pytest.mark.asyncio
    async def test_report_json_safe(self, drill_name: str) -> None:
        """Every drill report is JSON-serializable."""
        report = await run_drill(drill_name, config_path=_smoke_config_path())
        serialized = json.dumps(report, sort_keys=True)
        parsed = json.loads(serialized)
        assert parsed["drill_name"] == drill_name

    @pytest.mark.parametrize("drill_name", AVAILABLE_DRILLS)
    @pytest.mark.asyncio
    async def test_report_has_drill_steps(self, drill_name: str) -> None:
        """Every drill report has non-empty drill_steps."""
        report = await run_drill(drill_name, config_path=_smoke_config_path())
        assert isinstance(report["drill_steps"], list)
        assert len(report["drill_steps"]) >= 2

    @pytest.mark.parametrize("drill_name", AVAILABLE_DRILLS)
    @pytest.mark.asyncio
    async def test_evidence_level_is_drill(self, drill_name: str) -> None:
        """Every drill report has evidence_level = 'drill'."""
        report = await run_drill(drill_name, config_path=_smoke_config_path())
        assert report["evidence_level"] == "drill"

    @pytest.mark.parametrize("drill_name", AVAILABLE_DRILLS)
    @pytest.mark.asyncio
    async def test_drill_with_storage_path(
        self, drill_name: str, tmp_path: Path
    ) -> None:
        """Every drill works with --storage-path."""
        db_path = str(tmp_path / f"drill-{drill_name}.db")
        report = await run_drill(
            drill_name,
            config_path=_smoke_config_path(),
            storage_path=db_path,
        )
        assert report["status"] == "passed", (
            f"Drill {drill_name} with storage_path failed: "
            f"{report.get('fail_reasons', [])}"
        )
        assert report["storage_backend"] == "sqlite"
        assert report.get("storage_path") == db_path

    _TIMELINE_DRILLS = (
        "replay_duplicate_risk",
        "adapter_transient_failure",
        "shutdown_rejection",
        "degraded_live_health",
    )

    @pytest.mark.parametrize("drill_name", _TIMELINE_DRILLS)
    @pytest.mark.asyncio
    async def test_timeline_drills_have_timestamps(self, drill_name: str) -> None:
        """Timeline-expanded drills have timestamps on every step."""
        report = await run_drill(drill_name, config_path=_smoke_config_path())
        for step in report["drill_steps"]:
            assert (
                "timestamp" in step
            ), f"Step {step['step']} in {drill_name} missing timestamp"


# ---------------------------------------------------------------------------
# Drill → storage cross-check tests
# ---------------------------------------------------------------------------


_CROSSCHECK_DRILLS = (
    "renderer_failure",
    "adapter_permanent_failure",
    "adapter_transient_failure",
    "capacity_rejection",
    "shutdown_rejection",
    "replay_duplicate_risk",
)

# Drills that produce at least one delivery receipt.
_RECEIPT_DRILLS = (
    "renderer_failure",
    "adapter_permanent_failure",
    "adapter_transient_failure",
    "replay_duplicate_risk",
)

# Drills where all deliveries are rejected (suppressed evidence receipts expected).
_REJECTION_DRILLS = (
    "capacity_rejection",
    "shutdown_rejection",
)


def _crosscheck_config_text(db_path: str) -> str:
    """Return a minimal YAML config pointing at *db_path*."""
    return (
        "runtime:\n"
        "  name: drill-crosscheck\n"
        "\n"
        "storage:\n"
        f"  path: '{db_path}'\n"
    )


def _write_crosscheck_config(tmp_path: Path, db_path: str) -> str:
    """Write a config file pointing at *db_path* and return its string path."""
    cfg = tmp_path / "crosscheck.yaml"
    cfg.write_text(_crosscheck_config_text(db_path))
    return str(cfg)


def _capture_cli(*args: str) -> str:
    """Run a CLI command, capture stdout, and return it.

    Exits with code 0 are swallowed; non-zero exits are re-raised.
    """
    import io
    from contextlib import redirect_stderr, redirect_stdout

    from medre.cli import main

    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            main(list(args))
    except SystemExit as exc:
        if exc.code not in (None, 0):
            raise
    return stdout.getvalue()


class TestDrillStorageCrossCheck:
    """Proves drill outputs can be traced through storage using CLI commands.

    Each test runs a drill with ``--storage-path`` to a temp SQLite DB,
    then verifies that ``medre trace event``, ``medre inspect receipts``,
    and ``medre evidence`` can inspect the stored data.

    Tests are synchronous because CLI dispatch uses ``asyncio.run()``
    internally, which cannot be called from a running event loop.
    The drill execution is wrapped in ``asyncio.run()`` at the test level.
    """

    @staticmethod
    def _run_drill_sync(
        drill_name: str,
        *,
        storage_path: str,
    ) -> dict[str, Any]:
        """Run a drill synchronously via ``asyncio.run()``."""
        import asyncio as _asyncio

        return _asyncio.run(
            run_drill(
                drill_name,
                config_path=_smoke_config_path(),
                storage_path=storage_path,
            )
        )

    # -- trace event ---------------------------------------------------------

    @pytest.mark.parametrize("drill_name", _CROSSCHECK_DRILLS)
    def test_trace_event_after_drill(
        self,
        drill_name: str,
        tmp_path: Path,
    ) -> None:
        """``medre trace event`` finds the drill-generated event in storage."""
        db_path = str(tmp_path / f"{drill_name}-trace.db")
        report = self._run_drill_sync(drill_name, storage_path=db_path)
        assert report["status"] == "passed", report.get("fail_reasons", [])
        event_id = report["event_id"]

        output = _capture_cli(
            "trace",
            "event",
            event_id,
            "--storage-path",
            db_path,
            "--json",
        )
        timeline = json.loads(output)
        assert isinstance(timeline, list)
        assert len(timeline) >= 1
        types = [e["entry_type"] for e in timeline]
        assert "event" in types
        event_entry = next(e for e in timeline if e["entry_type"] == "event")
        assert event_entry["data"]["event_id"] == event_id

    # -- inspect receipts (receipt-producing drills) -------------------------

    @pytest.mark.parametrize("drill_name", _RECEIPT_DRILLS)
    def test_inspect_receipts_after_drill(
        self,
        drill_name: str,
        tmp_path: Path,
    ) -> None:
        """``medre inspect receipts`` finds at least one receipt."""
        db_path = str(tmp_path / f"{drill_name}-rcpt.db")
        report = self._run_drill_sync(drill_name, storage_path=db_path)
        assert report["status"] == "passed", report.get("fail_reasons", [])
        event_id = report["event_id"]

        output = _capture_cli(
            "inspect",
            "receipts",
            "--event",
            event_id,
            "--storage-path",
            db_path,
        )
        receipts = json.loads(output)
        assert isinstance(receipts, list)
        assert (
            len(receipts) >= 1
        ), f"Expected at least 1 receipt for {drill_name}, got {len(receipts)}"

    # -- inspect receipts (rejection drills: zero receipts expected) ---------

    @pytest.mark.parametrize("drill_name", _REJECTION_DRILLS)
    def test_inspect_suppressed_receipts_for_rejection_drills(
        self,
        drill_name: str,
        tmp_path: Path,
    ) -> None:
        """Rejection drills produce suppressed evidence receipts (event stored)."""
        db_path = str(tmp_path / f"{drill_name}-reject.db")
        report = self._run_drill_sync(drill_name, storage_path=db_path)
        assert report["status"] == "passed", report.get("fail_reasons", [])
        event_id = report["event_id"]

        # Event is still traceable.
        trace_output = _capture_cli(
            "trace",
            "event",
            event_id,
            "--storage-path",
            db_path,
            "--json",
        )
        timeline = json.loads(trace_output)
        types = [e["entry_type"] for e in timeline]
        assert "event" in types

        # Receipts list contains suppressed receipts for rejection drills.
        rcpt_output = _capture_cli(
            "inspect",
            "receipts",
            "--event",
            event_id,
            "--storage-path",
            db_path,
        )
        receipts = json.loads(rcpt_output)
        assert isinstance(receipts, list)
        assert (
            len(receipts) >= 1
        ), f"Expected suppressed receipts for {drill_name}, got {len(receipts)}"
        # All receipts for rejection drills should be suppressed.
        suppressed_kinds = {"capacity_rejection", "shutdown_rejection"}
        for r in receipts:
            assert (
                r["status"] == "suppressed"
            ), f"Expected suppressed status for {drill_name}, got {r['status']}"
            assert r["failure_kind"] in suppressed_kinds, (
                f"Expected rejection failure_kind for {drill_name}, "
                f"got {r['failure_kind']}"
            )

    # -- evidence --event ----------------------------------------------------

    @pytest.mark.parametrize("drill_name", _CROSSCHECK_DRILLS)
    def test_evidence_after_drill(
        self,
        drill_name: str,
        tmp_path: Path,
    ) -> None:
        """``medre evidence --event`` retrieves the drill event from storage."""
        db_path = str(tmp_path / f"{drill_name}-ev.db")
        report = self._run_drill_sync(drill_name, storage_path=db_path)
        assert report["status"] == "passed", report.get("fail_reasons", [])
        event_id = report["event_id"]

        output = _capture_cli(
            "evidence",
            "--storage-path",
            db_path,
            "--json",
            "--event",
            event_id,
        )
        evidence = json.loads(output)
        assert evidence["schema_version"] == 1
        storage_section = evidence["sections"]["storage"]
        assert storage_section["status"] == "passed"
        assert storage_section["data"]["db_exists"] is True
        assert storage_section["data"]["event"] is not None
        assert storage_section["data"]["event"]["event_id"] == event_id

    # -- comprehensive cross-check (all 6 drills) ---------------------------

    def test_all_drills_produce_traceable_evidence(self, tmp_path: Path) -> None:
        """All 6 drills produce events traceable via CLI storage commands."""
        for drill_name in _CROSSCHECK_DRILLS:
            db_path = str(tmp_path / f"all-{drill_name}.db")
            report = self._run_drill_sync(drill_name, storage_path=db_path)
            assert (
                report["status"] == "passed"
            ), f"Drill {drill_name} failed: {report.get('fail_reasons', [])}"
            event_id = report["event_id"]
            assert event_id is not None

            # 1. trace event — event must be found.
            trace_output = _capture_cli(
                "trace",
                "event",
                event_id,
                "--storage-path",
                db_path,
                "--json",
            )
            timeline = json.loads(trace_output)
            types = [e["entry_type"] for e in timeline]
            assert "event" in types, f"Event missing from timeline for {drill_name}"

            # 2. inspect receipts — must return a valid list.
            rcpt_output = _capture_cli(
                "inspect",
                "receipts",
                "--event",
                event_id,
                "--storage-path",
                db_path,
            )
            receipts = json.loads(rcpt_output)
            assert isinstance(receipts, list), f"Receipts not a list for {drill_name}"
            if drill_name in _RECEIPT_DRILLS:
                assert (
                    len(receipts) >= 1
                ), f"No receipts for receipt-producing drill {drill_name}"
            if drill_name in _REJECTION_DRILLS:
                assert (
                    len(receipts) >= 1
                ), f"No suppressed receipts for rejection drill {drill_name}"
                for r in receipts:
                    assert r["status"] == "suppressed", (
                        f"Expected suppressed receipt for {drill_name}, "
                        f"got {r['status']}"
                    )

            # 3. evidence — must find the event.
            ev_output = _capture_cli(
                "evidence",
                "--storage-path",
                db_path,
                "--json",
                "--event",
                event_id,
            )
            evidence = json.loads(ev_output)
            assert evidence["sections"]["storage"]["status"] == "passed"
            assert evidence["sections"]["storage"]["data"]["db_exists"] is True
