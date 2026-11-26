# medre-test-tier: compatibility
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
from types import SimpleNamespace

import pytest

from medre.core.startup_info import AdapterStartupInfo
from tests.helpers.async_utils import wait_until
from tests.helpers.cli import _run_cli, _run_cli_raw
from tests.helpers.compatibility_configs import CONFIG_FAKE_MULTI, CONFIG_SINGLE_ADAPTER


async def _trigger_shutdown_after_startup(
    run_mod, stdout: io.StringIO, wait_timeout: float = 10.0
) -> None:
    """Wait for startup completion marker, then trigger shutdown.

    The budget must tolerate slow CI runners: a loaded runner can take
    several wall-clock seconds to boot the runtime (SQLite open, adapter
    construction, banner) even though healthy runners finish in
    milliseconds — a 2s budget flaked exactly there. ``wait_until``
    returns as soon as the marker appears, so the larger default costs
    nothing on the happy path.
    """
    markers = (
        "All adapters ready — MEDRE runtime running",
        "MEDRE runtime running —",
    )
    ok = await wait_until(
        lambda: any(marker in stdout.getvalue() for marker in markers),
        timeout=wait_timeout,
    )
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
        output = _run_cli(
            "diagnostics", "--format", "json", "--config", str(config_fake_multi)
        )
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
runtime:
  name: degraded-test
storage:
  backend: memory
adapters:
  matrix:
    fm:
      enabled: true
      adapter_kind: fake
      homeserver: https://fake.local
      user_id: '@bot:fake.local'
      access_token: tok
      room_allowlist: ['!room:fake.local']
      encryption_mode: plaintext
  meshtastic:
    real_radio:
      enabled: true
      connection_type: serial
      serial_port: /dev/ttyNONEXISTENT
      origin_label: TestMesh
"""
        p = tmp_path / "config.yaml"
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
runtime:
  name: mixed-test
storage:
  backend: memory
adapters:
  matrix:
    active:
      enabled: true
      adapter_kind: fake
      homeserver: https://fake.local
      user_id: '@bot:fake.local'
      access_token: tok
      room_allowlist: ['!room:fake.local']
      encryption_mode: plaintext
  meshtastic:
    inactive:
      enabled: false
      connection_type: serial
      serial_port: /dev/ttyACM0
      origin_label: TestMesh
"""
        p = tmp_path / "config.yaml"
        p.write_text(config_mixed)
        output = _run_cli("config", "check", "--config", str(p))
        assert "active: enabled" in output
        assert "inactive: disabled" in output
        assert "1/2 adapter(s) enabled" in output
        assert "Config valid" in output

    def test_config_check_no_enabled_adapters(self, tmp_path: Path) -> None:
        """Config with all adapters disabled shows 0 enabled."""
        config_all_disabled = """\
runtime:
  name: all-disabled
storage:
  backend: memory
adapters:
  matrix:
    offline:
      enabled: false
      homeserver: https://fake.local
      user_id: '@bot:fake.local'
      access_token: tok
      room_allowlist: ['!room:fake.local']
      encryption_mode: plaintext
"""
        p = tmp_path / "config.yaml"
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

        p = tmp_path / "config.yaml"
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

        p = tmp_path / "config.yaml"
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

        p = tmp_path / "config.yaml"
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
            "snapshot_scope",
            "accounting",
            "adapters",
            "capacity",
            "content_collection",
            "diagnostics",
            "health",
            "identity",
            "lifecycle",
            "limits",
            "outbox",
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


async def test_status_file_tracks_owned_runtime_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import medre.cli.run_commands as run_mod

    config = tmp_path / "config.yaml"
    config.write_text(CONFIG_SINGLE_ADAPTER)
    status_path = tmp_path / "runtime.json"
    running = {}

    async def request_shutdown(app, _shutdown_wake: asyncio.Event) -> None:
        running.update(json.loads(status_path.read_text()))
        assert app.state.value == "running"

    monkeypatch.setattr(run_mod, "_wait_for_run_shutdown", request_shutdown)
    await run_mod._run(str(config), status_path=str(status_path))
    final = json.loads(status_path.read_text())
    assert running["phase"] == "running"
    assert running["adapters_started"] == 1
    assert final["phase"] == "stopped"
    assert final["run_id"] == running["run_id"]
    assert final["adapters_started"] is None


async def test_status_file_cannot_create_configured_database(tmp_path: Path) -> None:
    import medre.cli.run_commands as run_mod

    config = tmp_path / "config.yaml"
    database = tmp_path / "database.json"
    config.write_text(
        CONFIG_SINGLE_ADAPTER.replace(
            "backend: memory", f"backend: sqlite\n  path: '{database}'"
        )
    )
    with pytest.raises(SystemExit) as exc:
        await run_mod._run(str(config), status_path=str(database))
    assert exc.value.code == run_mod.EXIT_CONFIG
    assert not database.exists()


class TestRealSnapshotOnShutdown:
    """_run() lifecycle writes a valid snapshot to the specified path."""

    @pytest.mark.asyncio
    async def test_banner_failure_still_writes_shutdown_snapshot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import medre.cli.run_commands as run_mod

        config_path = tmp_path / "config.yaml"
        config_path.write_text(CONFIG_SINGLE_ADAPTER)
        snapshot_path = tmp_path / "snap.json"

        def broken_installation_lines() -> list[str]:
            raise RuntimeError("metadata unavailable token=syt_SecretFixture")

        async def request_shutdown(app, _shutdown_wake: asyncio.Event) -> None:
            assert app.state.value == "running"

        monkeypatch.setattr(run_mod, "installation_lines", broken_installation_lines)
        monkeypatch.setattr(run_mod, "_wait_for_run_shutdown", request_shutdown)
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            await run_mod._run(str(config_path), snapshot_path=str(snapshot_path))

        assert "Startup banner unavailable: metadata unavailable" in stdout.getvalue()
        assert "syt_SecretFixture" not in stdout.getvalue()
        lifecycle = json.loads(snapshot_path.read_text())["lifecycle"]
        assert lifecycle["runtime_state"] == "stopped"
        assert lifecycle["adapters"] == {"solo": "stopped"}

    @pytest.mark.asyncio
    async def test_snapshot_written_after_shutdown(self, tmp_path: Path) -> None:
        """Full _run() lifecycle: startup -> shutdown -> snapshot file exists and is valid."""
        import medre.cli.run_commands as run_mod

        p = tmp_path / "config.yaml"
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

        p = tmp_path / "config.yaml"
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

        p = tmp_path / "config.yaml"
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

        p = tmp_path / "config.yaml"
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
        assert "  Routing: " in output
        assert "live-eligible" in output
        assert "Route eligibility:" not in output

    @pytest.mark.asyncio
    async def test_shutdown_output_shows_accounting(self, tmp_path: Path) -> None:
        """Accounting counters appear in stdout during shutdown."""
        import medre.cli.run_commands as run_mod

        p = tmp_path / "config.yaml"
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

        p = tmp_path / "config.yaml"
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

    @pytest.mark.asyncio
    async def test_startup_output_shows_disabled_adapters_and_storage_path(
        self, tmp_path: Path
    ) -> None:
        """Startup output includes disabled adapter count and SQLite path."""
        import medre.cli.run_commands as run_mod

        db_path = tmp_path / "run-output.db"
        config = f"""\
runtime:
  name: run-output-storage
storage:
  backend: sqlite
  path: "{db_path}"
adapters:
  matrix:
    solo:
      enabled: true
      adapter_kind: fake
      homeserver: https://fake.local
      user_id: '@bot:fake.local'
      access_token: tok_single
      room_allowlist: ['!room:fake.local']
      encryption_mode: plaintext
  meshtastic:
    disabled:
      enabled: false
      adapter_kind: fake
      connection_type: fake
      origin_label: DisabledMesh
"""
        p = tmp_path / "config.yaml"
        p.write_text(config)

        run_mod.shutdown_requested = False

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            task = asyncio.create_task(run_mod._run(str(p)))
            trigger = asyncio.create_task(
                _trigger_shutdown_after_startup(run_mod, stdout)
            )
            await asyncio.gather(task, trigger)

        output = stdout.getvalue()
        assert "disabled, not started: meshtastic.disabled" in output
        assert f"Storage: sqlite ({db_path})" in output


# ===================================================================
# 17. Stale shutdown state — repeated _run() calls
# ===================================================================


class TestStaleShutdownState:
    """Repeated _run() calls do not inherit stale shutdown_requested."""

    @pytest.mark.asyncio
    async def test_repeated_run_no_stale_shutdown(self, tmp_path: Path) -> None:
        """shutdown_requested=True before first _run() is reset; second _run() starts clean."""
        import medre.cli.run_commands as run_mod

        p = tmp_path / "config.yaml"
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


@pytest.mark.asyncio
async def test_startup_installation_summary_survives_core_metadata_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import medre.cli.run_commands as run_mod
    from medre.runtime import installation_summary

    config = tmp_path / "config.yaml"
    config.write_text(CONFIG_SINGLE_ADAPTER)

    def broken_version(name: str) -> str:
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid metadata")

    # Patch only the summary's dependency lookups, not global provider discovery.
    provider = SimpleNamespace(
        metadata={"Name": "medre-adapter-matrix"},
        version="0.1.0",
        read_text=lambda filename: None,
    )
    not_found = installation_summary.importlib.metadata.PackageNotFoundError
    monkeypatch.setattr(
        installation_summary,
        "importlib",
        SimpleNamespace(
            metadata=SimpleNamespace(
                version=broken_version,
                distribution=broken_version,
                PackageNotFoundError=not_found,
                entry_points=lambda **kwargs: [
                    SimpleNamespace(name="matrix", dist=provider)
                ],
            )
        ),
    )
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        task = asyncio.create_task(run_mod._run(str(config)))
        trigger = asyncio.create_task(_trigger_shutdown_after_startup(run_mod, stdout))
        await asyncio.gather(task, trigger)
    output = stdout.getvalue()
    assert "Core:    medre ? (unknown)" in output
    assert "Python:  " in output
    assert "Adapter: matrix 0.1.0 (pypi)" in output
    assert (
        "All adapters ready — MEDRE runtime running" in output
        or "MEDRE runtime running —" in output
    )
    assert output.index("State:") < output.index("Core:") < output.index("Python:")


# ===================================================================
# 18. Consolidated startup banner — settle-time output
# ===================================================================


async def test_banner_follows_minimal_start_line(tmp_path: Path) -> None:
    """Early header is suppressed; identity facts render only at settle."""
    import medre.cli.run_commands as run_mod

    p = tmp_path / "config.yaml"
    p.write_text(CONFIG_SINGLE_ADAPTER)

    run_mod.shutdown_requested = False

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        task = asyncio.create_task(run_mod._run(str(p)))
        trigger = asyncio.create_task(_trigger_shutdown_after_startup(run_mod, stdout))
        await asyncio.gather(task, trigger)

    output = stdout.getvalue()
    medre_lines = [line for line in output.splitlines() if "[INFO] medre: " in line]
    assert medre_lines, output
    # Minimal start line only: no config/state/installation preview.
    assert (
        "[INFO] medre: MEDRE starting (single-adapter) with 1 adapter(s): solo"
        in medre_lines[0]
    )
    assert "  Config:" not in medre_lines[0]
    assert sum("  Config:" in line for line in medre_lines) == 1
    # Identity facts appear only after the runtime began starting.
    first_banner_fact = next(line for line in medre_lines if "  Config:" in line)
    all_lines = output.splitlines()
    app_start_index = next(
        i for i, line in enumerate(all_lines) if "Starting MEDRE runtime" in line
    )
    assert all_lines.index(first_banner_fact) > app_start_index


async def test_banner_blocks_are_consolidated_at_settle(tmp_path: Path) -> None:
    """One instance block with status and duration, stats, then outcome."""
    import medre.cli.run_commands as run_mod

    p = tmp_path / "config.yaml"
    p.write_text(CONFIG_SINGLE_ADAPTER)

    run_mod.shutdown_requested = False

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        task = asyncio.create_task(run_mod._run(str(p)))
        trigger = asyncio.create_task(_trigger_shutdown_after_startup(run_mod, stdout))
        await asyncio.gather(task, trigger)

    output = stdout.getvalue()
    assert "MEDRE single-adapter — startup (1 adapter(s))" in output
    assert "matrix.solo" in output
    assert "  ready" in output
    assert "  Events:" in output
    assert "(cached)" not in output
    # The trailing per-adapter "started X (Ns)" list is gone; durations
    # live in the per-instance banner blocks now.
    assert "  started matrix.solo" not in output
    assert "All adapters ready — MEDRE runtime running" in output


def test_settled_banner_reports_degraded_honestly() -> None:
    """A degraded adapter keeps its status tag and the N-of-M outcome line."""
    import medre.cli.run_commands as run_mod
    from medre.core.lifecycle.states import AdapterState

    app = SimpleNamespace(
        adapters={"main": object(), "radio": object()},
        build_failures=[],
        adapter_states={
            "main": AdapterState.READY,
            "radio": AdapterState.DEGRADED,
        },
        adapter_start_duration_ms={"main": 1500.0, "radio": 2200.0},
        startup_info={},
        adapter_start_errors={},
        adapter_start_health_reasons={"radio": "startup health check timed out"},
        route_eligibility=None,
        startup_readiness=None,
        boot_summary=SimpleNamespace(
            persisted_events_count=4,
            storage_backend="sqlite",
            storage_path="/tmp/medre.sqlite",
        ),
    )
    config = SimpleNamespace(
        runtime=SimpleNamespace(name="degraded-run"),
        routes=None,
        storage=SimpleNamespace(backend="sqlite"),
        adapters=SimpleNamespace(
            all_configs=lambda: [
                ("matrix", "main", SimpleNamespace(enabled=True)),
                ("meshcore", "radio", SimpleNamespace(enabled=True)),
            ]
        ),
        limits=SimpleNamespace(
            max_inflight_deliveries=10,
            max_inflight_replay_events=10,
            shutdown_drain_timeout_seconds=5,
        ),
    )
    paths = SimpleNamespace(config_file="/tmp/degraded.yaml", state_dir="/tmp/state")
    settled = run_mod._settled_startup_banner(app, config, paths)
    assert f"{'meshcore.radio':<14}  {'degraded':<8}  2.2s" in settled.text
    assert f"{'matrix.main':<14}  {'ready':<8}  1.5s" in settled.text
    assert "  reason startup health check timed out" in settled.text
    assert settled.all_ready is False
    assert settled.outcome_line == (
        "MEDRE runtime running — 1/2 adapter(s) ready, 1 degraded"
    )


def test_settled_banner_reports_failed_honestly() -> None:
    """A failed adapter carries its reason and the outcome line counts it."""
    import medre.cli.run_commands as run_mod
    from medre.core.lifecycle.states import AdapterState

    app = SimpleNamespace(
        adapters={"main": object(), "radio": object()},
        build_failures=[],
        adapter_states={
            "main": AdapterState.READY,
            "radio": AdapterState.FAILED,
        },
        adapter_start_duration_ms={"main": 1500.0, "radio": 312.0},
        startup_info={"radio": _cached_banner_info("meshcore")},
        adapter_start_errors={"radio": "connection refused"},
        route_eligibility=None,
        startup_readiness=None,
        boot_summary=SimpleNamespace(
            persisted_events_count=None,
            storage_backend="memory",
            storage_path=None,
        ),
    )
    config = SimpleNamespace(
        runtime=SimpleNamespace(name="partial-run"),
        routes=None,
        storage=SimpleNamespace(backend="memory"),
        adapters=SimpleNamespace(
            all_configs=lambda: [
                ("matrix", "main", SimpleNamespace(enabled=True)),
                ("meshcore", "radio", SimpleNamespace(enabled=True)),
            ]
        ),
        limits=SimpleNamespace(
            max_inflight_deliveries=10,
            max_inflight_replay_events=10,
            shutdown_drain_timeout_seconds=5,
        ),
    )
    paths = SimpleNamespace(config_file="/tmp/partial.yaml", state_dir="/tmp/state")
    settled = run_mod._settled_startup_banner(app, config, paths)
    assert "meshcore.radio  failed  312ms (cached)" in settled.text
    assert "  board OLD" in settled.text
    assert "  reason connection refused" in settled.text
    assert settled.all_ready is False
    assert settled.outcome_line == (
        "MEDRE runtime running — 1/2 adapter(s) ready, 1 failed"
    )


def test_settled_banner_includes_build_failures() -> None:
    """Build-failed instances render after started ones with their reason."""
    import medre.cli.run_commands as run_mod
    from medre.core.lifecycle.states import AdapterState

    app = SimpleNamespace(
        adapters={"main": object()},
        build_failures=[
            SimpleNamespace(
                transport="meshtastic",
                adapter_id="radio",
                error=RuntimeError("missing SDK token=private-fixture-token"),
            )
        ],
        adapter_states={"main": AdapterState.READY},
        adapter_start_duration_ms={"main": 1500.0},
        startup_info={"radio": _cached_banner_info("meshtastic")},
        adapter_start_errors={},
        route_eligibility=None,
        startup_readiness=None,
        boot_summary=SimpleNamespace(
            persisted_events_count=0,
            storage_backend="memory",
            storage_path=None,
        ),
    )
    config = SimpleNamespace(
        runtime=SimpleNamespace(name="build-fail-run"),
        routes=None,
        storage=SimpleNamespace(backend="memory"),
        adapters=SimpleNamespace(
            all_configs=lambda: [
                ("matrix", "main", SimpleNamespace(enabled=True)),
                ("meshtastic", "radio", SimpleNamespace(enabled=True)),
            ]
        ),
        limits=SimpleNamespace(
            max_inflight_deliveries=10,
            max_inflight_replay_events=10,
            shutdown_drain_timeout_seconds=5,
        ),
    )
    paths = SimpleNamespace(config_file="/tmp/build.yaml", state_dir="/tmp/state")
    settled = run_mod._settled_startup_banner(app, config, paths)
    assert "meshtastic.radio  build failed (cached)" in settled.text
    assert "  board OLD" in settled.text
    assert "  reason missing SDK" in settled.text
    assert "private-fixture-token" not in settled.text
    assert not settled.all_ready
    assert settled.outcome_line == (
        "MEDRE runtime running — 1/2 adapter(s) ready, 1 failed to build"
    )


def _cached_banner_info(transport: str) -> AdapterStartupInfo:
    from medre.core.startup_info import StartupInfoField

    return AdapterStartupInfo(
        adapter_id="radio",
        transport=transport,
        fields=(StartupInfoField(name="board", value="OLD", cached=True, row=0),),
        emitted_at="2026-11-20T00:00:00+00:00",
    )


@pytest.mark.parametrize(
    "state_name",
    [None, "INITIALIZING", "BACKPRESSURED", "DISCONNECTED", "STOPPING", "STOPPED"],
)
def test_settled_banner_never_counts_nonready_states(state_name: str | None) -> None:
    from medre.cli.run_commands import _settled_startup_banner
    from medre.core.lifecycle.states import AdapterState

    app = SimpleNamespace(
        adapters={"radio": object()}, build_failures=[], boot_summary=None
    )
    if state_name is not None:
        app.adapter_states = {"radio": AdapterState[state_name]}
    config = SimpleNamespace(
        runtime=SimpleNamespace(name="unknown-run"),
        routes=None,
        storage=None,
        adapters=SimpleNamespace(
            all_configs=lambda: [("meshcore", "radio", SimpleNamespace(enabled=True))]
        ),
        limits=SimpleNamespace(
            max_inflight_deliveries=10,
            max_inflight_replay_events=10,
            shutdown_drain_timeout_seconds=5,
        ),
    )
    paths = SimpleNamespace(config_file="/tmp/test.yaml", state_dir="/tmp/state")
    settled = _settled_startup_banner(app, config, paths)
    assert not settled.all_ready
    assert settled.outcome_line == "MEDRE runtime running — 0/1 adapter(s) ready"
    assert (state_name.lower() if state_name else "unknown") in settled.text


async def test_status_file_rejects_literal_tilde_snapshot_collision(
    tmp_path, monkeypatch
):
    import medre.cli.run_commands as run_mod

    monkeypatch.chdir(tmp_path)
    (tmp_path / "~").mkdir()
    config = tmp_path / "config.yaml"
    config.write_text(CONFIG_SINGLE_ADAPTER)
    status_path = tmp_path / "~" / "runtime.json"
    with pytest.raises(SystemExit) as exc:
        await run_mod._run(str(config), "~/runtime.json", str(status_path))
    assert exc.value.code == run_mod.EXIT_CONFIG
    assert not status_path.exists()


async def test_status_file_reports_no_enabled_adapters(tmp_path: Path) -> None:
    import medre.cli.run_commands as run_mod

    config = tmp_path / "config.yaml"
    config.write_text(CONFIG_SINGLE_ADAPTER.replace("enabled: true", "enabled: false"))
    status = tmp_path / "runtime.json"
    with pytest.raises(SystemExit) as exc:
        await run_mod._run(str(config), status_path=str(status))
    assert exc.value.code == run_mod.EXIT_CONFIG
    assert json.loads(status.read_text())["phase"] == "failed"
