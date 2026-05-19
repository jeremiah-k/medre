"""Shutdown/restart, signal safety, snapshot-on-shutdown, run output, and stale shutdown state.

Shutdown lifecycle tests use ``_trigger_shutdown_after_startup()`` which waits
for a deterministic startup marker in stdout before requesting shutdown,
avoiding races where shutdown fires before startup completes.
"""

from __future__ import annotations

import asyncio
import io
import json
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.helpers.async_utils import wait_until
from tests.helpers.cli import (
    CONFIG_FAKE_MULTI,
    CONFIG_SINGLE_ADAPTER,
    _run_cli,
    _run_cli_raw,
)


async def _trigger_shutdown_after_startup(
    run_mod, stdout: io.StringIO, wait_timeout: float = 2.0
) -> None:
    """Wait for startup completion marker, then trigger shutdown."""
    marker = "Run `medre diagnostics --refresh-health` for live adapter health"
    ok = await wait_until(lambda: marker in stdout.getvalue(), timeout=wait_timeout)
    assert ok, (
        f"runtime did not reach post-startup marker within {wait_timeout}s; "
        f"output was:\n{stdout.getvalue()}"
    )
    run_mod.shutdown_requested = True


# ===================================================================
# 9. Shutdown/restart workflow with fake runtime
# ===================================================================


class TestShutdownRestartWorkflow:
    """Operators start and stop the runtime with fake adapters."""

    def test_run_exits_on_no_enabled_adapters(self, config_minimal: Path) -> None:
        """Run with no adapters exits cleanly with clear message."""
        _, stderr, code = _run_cli_raw("run", "--config", str(config_minimal))
        assert code != 0
        assert "Traceback" not in stderr
        assert "adapter" in stderr.lower()

    def test_config_check_before_run(self, config_fake_multi: Path) -> None:
        """Operator workflow: check config before attempting run."""
        output = _run_cli("config", "check", "--config", str(config_fake_multi))
        assert "Config valid" in output
        assert "2/2 adapter(s) enabled" in output

    def test_routes_validate_before_run(self, config_fake_multi: Path) -> None:
        """Operator workflow: validate routes before attempting run."""
        output = _run_cli("routes", "validate", "--config", str(config_fake_multi))
        assert "Routes valid" in output

    def test_diagnostics_before_run(self, config_fake_multi: Path) -> None:
        """Operator workflow: check diagnostics before attempting run."""
        output = _run_cli("diagnostics", "--config", str(config_fake_multi))
        parsed = json.loads(output)
        assert "schema_version" in parsed

    def test_fake_runtime_build_and_snapshot(self, config_fake_multi: Path) -> None:
        """RuntimeBuilder can build from fake config and produce a snapshot."""
        from medre.config.loader import load_config
        from medre.runtime.builder import RuntimeBuilder
        from medre.runtime.snapshot import build_runtime_snapshot

        config, _source, paths = load_config(str(config_fake_multi))
        builder = RuntimeBuilder(config, paths)
        app = builder.build()
        assert app is not None
        assert len(app.adapters) >= 1

        snapshot = build_runtime_snapshot(
            app,
            now_fn=lambda: datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
            monotonic_fn=lambda: 0.0,
        )
        assert isinstance(snapshot, dict)
        assert "schema_version" in snapshot
        assert snapshot["schema_version"] == 1
        assert "adapters" in snapshot


# ===================================================================
# 10. Degraded-state messaging
# ===================================================================


class TestDegradedStateMessaging:
    """Operators see clear 'degraded' messaging when adapters partially fail."""

    def test_degraded_build_failure_in_snapshot(self, tmp_path: Path) -> None:
        """Build failures appear in diagnostics snapshot."""
        config_with_real = """\
[runtime]
name = "degraded-test"

[storage]
backend = "memory"

[adapters.matrix.fm]
enabled = true
adapter_kind = "fake"
homeserver = "https://fake.local"
user_id = "@bot:fake.local"
access_token = "tok"
room_allowlist = ["!room:fake.local"]
encryption_mode = "plaintext"

[adapters.meshtastic.real_radio]
enabled = true
connection_type = "serial"
serial_port = "/dev/ttyNONEXISTENT"
meshnet_name = "TestMesh"
"""
        p = tmp_path / "config.toml"
        p.write_text(config_with_real)

        from medre.config.loader import load_config
        from medre.runtime.builder import RuntimeBuilder
        from medre.runtime.snapshot import build_runtime_snapshot

        config, _source, paths = load_config(str(p))
        builder = RuntimeBuilder(config, paths)
        app = builder.build()

        assert len(app.adapters) >= 1

        if app.build_failures:
            snapshot = build_runtime_snapshot(
                app,
                now_fn=lambda: datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
                monotonic_fn=lambda: 0.0,
            )
            assert "build_failures" in snapshot["startup"]
            assert len(snapshot["startup"]["build_failures"]) > 0

    def test_config_check_with_disabled_adapter(self, tmp_path: Path) -> None:
        """Config check shows disabled adapters clearly."""
        config_mixed = """\
[runtime]
name = "mixed-test"

[storage]
backend = "memory"

[adapters.matrix.active]
enabled = true
adapter_kind = "fake"
homeserver = "https://fake.local"
user_id = "@bot:fake.local"
access_token = "tok"
room_allowlist = ["!room:fake.local"]
encryption_mode = "plaintext"

[adapters.meshtastic.inactive]
enabled = false
connection_type = "serial"
serial_port = "/dev/ttyACM0"
meshnet_name = "TestMesh"
"""
        p = tmp_path / "config.toml"
        p.write_text(config_mixed)
        output = _run_cli("config", "check", "--config", str(p))
        assert "active: enabled" in output
        assert "inactive: disabled" in output
        assert "1/2 adapter(s) enabled" in output
        assert "Config valid" in output

    def test_config_check_no_enabled_adapters(self, tmp_path: Path) -> None:
        """Config with all adapters disabled shows 0 enabled."""
        config_all_disabled = """\
[runtime]
name = "all-disabled"

[storage]
backend = "memory"

[adapters.matrix.offline]
enabled = false
homeserver = "https://fake.local"
user_id = "@bot:fake.local"
access_token = "tok"
room_allowlist = ["!room:fake.local"]
encryption_mode = "plaintext"
"""
        p = tmp_path / "config.toml"
        p.write_text(config_all_disabled)
        output = _run_cli("config", "check", "--config", str(p))
        assert "0/1 adapter(s) enabled" in output
        assert "Config valid" in output


# ===================================================================
# 13. Signal safety and shutdown request
# ===================================================================


class TestSignalSafety:
    """Signal handler triggers clean shutdown via _request_shutdown."""

    @pytest.mark.asyncio
    async def test_request_shutdown_sets_flag_and_clean_stop(
        self, tmp_path: Path
    ) -> None:
        """Calling _request_shutdown simulates SIGTERM; app stops cleanly."""
        import signal as signal_mod

        import medre.cli.run_commands as run_mod
        from medre.cli.run_commands import _request_shutdown
        from medre.config.loader import load_config
        from medre.runtime.builder import RuntimeBuilder

        p = tmp_path / "config.toml"
        p.write_text(CONFIG_SINGLE_ADAPTER)
        config, _source, paths = load_config(str(p))
        app = RuntimeBuilder(config, paths).build()
        await app.start()
        assert app.state.value == "running"

        run_mod.shutdown_requested = False
        _request_shutdown(signal_mod.SIGTERM, None)
        assert run_mod.shutdown_requested is True

        await app.stop()
        assert app.state.value == "stopped"

        run_mod.shutdown_requested = False

    @pytest.mark.asyncio
    async def test_request_shutdown_sigint(self) -> None:
        """SIGINT also sets shutdown_requested."""
        import signal as signal_mod

        import medre.cli.run_commands as run_mod
        from medre.cli.run_commands import _request_shutdown

        run_mod.shutdown_requested = False
        _request_shutdown(signal_mod.SIGINT, None)
        assert run_mod.shutdown_requested is True
        run_mod.shutdown_requested = False


# ===================================================================
# 14. Snapshot-on-shutdown end-to-end
# ===================================================================


class TestSnapshotOnShutdown:
    """--snapshot-on-shutdown writes a valid JSON snapshot on graceful stop."""

    @pytest.mark.asyncio
    async def test_snapshot_written_on_graceful_stop(self, tmp_path: Path) -> None:
        """Runtime builds snapshot and writes JSON to the specified path."""
        from medre.config.loader import load_config
        from medre.runtime.builder import RuntimeBuilder
        from medre.runtime.snapshot import build_runtime_snapshot

        p = tmp_path / "config.toml"
        p.write_text(CONFIG_SINGLE_ADAPTER)
        config, _source, paths = load_config(str(p))
        app = RuntimeBuilder(config, paths).build()
        await app.start()

        snap = build_runtime_snapshot(app)
        snap_path = tmp_path / "shutdown.json"
        snap_path.write_text(json.dumps(snap, indent=2, sort_keys=True) + "\n")

        await app.stop()

        assert snap_path.exists()
        data = json.loads(snap_path.read_text())
        assert "schema_version" in data
        assert data["schema_version"] == 1
        assert "adapters" in data
        assert "lifecycle" in data

    @pytest.mark.asyncio
    async def test_snapshot_has_expected_keys(self, tmp_path: Path) -> None:
        """Snapshot dict contains all required top-level sections."""
        from medre.config.loader import load_config
        from medre.runtime.builder import RuntimeBuilder
        from medre.runtime.snapshot import build_runtime_snapshot

        p = tmp_path / "config.toml"
        p.write_text(CONFIG_SINGLE_ADAPTER)
        config, _source, paths = load_config(str(p))
        app = RuntimeBuilder(config, paths).build()

        snap = build_runtime_snapshot(
            app,
            now_fn=lambda: datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
            monotonic_fn=lambda: 0.0,
        )

        expected_sections = {
            "schema_version",
            "snapshot_at",
            "accounting",
            "adapters",
            "capacity",
            "diagnostics",
            "health",
            "identity",
            "lifecycle",
            "limits",
            "persistence",
            "replay",
            "retry",
            "routes",
            "startup",
            "unstable",
        }
        assert set(snap.keys()) == expected_sections


# ===================================================================
# 15. Real CLI snapshot test — _run() writes snapshot via CLI lifecycle
# ===================================================================


class TestRealSnapshotOnShutdown:
    """_run() lifecycle writes a valid snapshot to the specified path."""

    @pytest.mark.asyncio
    async def test_snapshot_written_after_shutdown(self, tmp_path: Path) -> None:
        """Full _run() lifecycle: startup -> shutdown -> snapshot file exists and is valid."""
        import medre.cli.run_commands as run_mod

        p = tmp_path / "config.toml"
        p.write_text(CONFIG_SINGLE_ADAPTER)
        snap_path = tmp_path / "snap.json"

        run_mod.shutdown_requested = False

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            task = asyncio.create_task(
                run_mod._run(str(p), snapshot_path=str(snap_path))
            )
            trigger = asyncio.create_task(
                _trigger_shutdown_after_startup(run_mod, stdout)
            )
            await asyncio.gather(task, trigger)

        assert snap_path.exists(), "Snapshot file was not written"
        data = json.loads(snap_path.read_text())
        assert data["schema_version"] == 1
        assert "lifecycle" in data
        assert data["lifecycle"]["runtime_state"] == "stopped"
        assert "accounting" in data
        assert "routes" in data

    @pytest.mark.asyncio
    async def test_snapshot_json_is_valid(self, tmp_path: Path) -> None:
        """Snapshot written by _run() is valid, parseable JSON."""
        import medre.cli.run_commands as run_mod

        p = tmp_path / "config.toml"
        p.write_text(CONFIG_SINGLE_ADAPTER)
        snap_path = tmp_path / "snap.json"

        run_mod.shutdown_requested = False

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            task = asyncio.create_task(
                run_mod._run(str(p), snapshot_path=str(snap_path))
            )
            trigger = asyncio.create_task(
                _trigger_shutdown_after_startup(run_mod, stdout)
            )
            await asyncio.gather(task, trigger)

        raw = snap_path.read_text()
        data = json.loads(raw)
        assert isinstance(data, dict)
        top_keys = list(data.keys())
        assert top_keys == sorted(top_keys)


# ===================================================================
# 16. Run output assertions — startup and shutdown stdout
# ===================================================================


class TestRunOutput:
    """Assert specific text appears in stdout during _run() lifecycle."""

    @pytest.mark.asyncio
    async def test_startup_output_lists_adapters(self, tmp_path: Path) -> None:
        """Startup output lists adapter IDs."""
        import medre.cli.run_commands as run_mod

        p = tmp_path / "config.toml"
        p.write_text(CONFIG_SINGLE_ADAPTER)

        run_mod.shutdown_requested = False

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            task = asyncio.create_task(run_mod._run(str(p)))
            trigger = asyncio.create_task(
                _trigger_shutdown_after_startup(run_mod, stdout)
            )
            await asyncio.gather(task, trigger)

        output = stdout.getvalue()
        assert "solo" in output

    @pytest.mark.asyncio
    async def test_startup_output_shows_route_eligibility(self, tmp_path: Path) -> None:
        """Route eligibility text appears when routes are configured."""
        import medre.cli.run_commands as run_mod

        p = tmp_path / "config.toml"
        p.write_text(CONFIG_FAKE_MULTI)

        run_mod.shutdown_requested = False

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            task = asyncio.create_task(run_mod._run(str(p)))
            trigger = asyncio.create_task(
                _trigger_shutdown_after_startup(run_mod, stdout)
            )
            await asyncio.gather(task, trigger)

        output = stdout.getvalue()
        assert "Route eligibility:" in output

    @pytest.mark.asyncio
    async def test_shutdown_output_shows_accounting(self, tmp_path: Path) -> None:
        """Accounting counters appear in stdout during shutdown."""
        import medre.cli.run_commands as run_mod

        p = tmp_path / "config.toml"
        p.write_text(CONFIG_SINGLE_ADAPTER)

        run_mod.shutdown_requested = False

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            task = asyncio.create_task(run_mod._run(str(p)))
            trigger = asyncio.create_task(
                _trigger_shutdown_after_startup(run_mod, stdout)
            )
            await asyncio.gather(task, trigger)

        output = stdout.getvalue()
        assert "Accounting:" in output

    @pytest.mark.asyncio
    async def test_shutdown_output_shows_snapshot_path(self, tmp_path: Path) -> None:
        """When snapshot_path is provided, 'Final snapshot written' appears."""
        import medre.cli.run_commands as run_mod

        p = tmp_path / "config.toml"
        p.write_text(CONFIG_SINGLE_ADAPTER)
        snap_path = tmp_path / "snap.json"

        run_mod.shutdown_requested = False

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            task = asyncio.create_task(
                run_mod._run(str(p), snapshot_path=str(snap_path))
            )
            trigger = asyncio.create_task(
                _trigger_shutdown_after_startup(run_mod, stdout)
            )
            await asyncio.gather(task, trigger)

        output = stdout.getvalue()
        assert "Final snapshot written to:" in output


# ===================================================================
# 17. Stale shutdown state — repeated _run() calls
# ===================================================================


class TestStaleShutdownState:
    """Repeated _run() calls do not inherit stale shutdown_requested."""

    @pytest.mark.asyncio
    async def test_repeated_run_no_stale_shutdown(self, tmp_path: Path) -> None:
        """shutdown_requested=True before first _run() is reset; second _run() starts clean."""
        import medre.cli.run_commands as run_mod

        p = tmp_path / "config.toml"
        p.write_text(CONFIG_SINGLE_ADAPTER)

        # Pre-set stale shutdown state.
        run_mod.shutdown_requested = True

        # First run: should reset shutdown_requested to False at start,
        # then exit when we trigger shutdown.
        stdout1 = io.StringIO()
        with redirect_stdout(stdout1):
            task1 = asyncio.create_task(run_mod._run(str(p)))
            trigger1 = asyncio.create_task(
                _trigger_shutdown_after_startup(run_mod, stdout1)
            )
            await asyncio.gather(task1, trigger1)

        # After first run, shutdown_requested is True (set by trigger).
        # Second run should reset it to False.
        stdout2 = io.StringIO()
        with redirect_stdout(stdout2):
            task2 = asyncio.create_task(run_mod._run(str(p)))
            trigger2 = asyncio.create_task(
                _trigger_shutdown_after_startup(run_mod, stdout2)
            )
            await asyncio.gather(task2, trigger2)

        # Both runs should have completed successfully (not hung).
        output2 = stdout2.getvalue()
        assert "Runtime shutting down" in output2
