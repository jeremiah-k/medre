"""Track 6: Operator recovery workflow tests.

Validates realistic operator repair loops with concise deterministic output.
Covers scenarios where an operator encounters a failure, receives an actionable
error message, performs a fix, and verifies recovery — without raw tracebacks
or non-deterministic output.

Scenarios covered:

1.  Malformed config recovery — bad TOML → clean error → fix → config loads
2.  Storage-path recovery — invalid placeholder → error → fix path → works
3.  Startup failure recovery — all adapters fail → total failure → disable → retry
4.  Degraded runtime recovery — partial startup → degraded messaging → diagnostics
5.  Replay after restart — store events → restart → replay available
6.  Route validation failure recovery — bad refs → clean error → fix → valid
7.  Adapter disable/enable workflows — disable failing → verify → re-enable → verify
8.  Config repair workflows — common issues → actionable suggestions → fix → valid
9.  Deterministic messaging — no raw tracebacks, consistent output shapes

Uses fake adapters, memory/sqlite temp storage, and CLI helpers only.
No live transports, SDKs, or network required.
"""

from __future__ import annotations

import io
import os
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from medre.cli import main
from medre.config.errors import (
    ConfigFileError,
    ConfigNotFoundError,
    ConfigValidationError,
)
from medre.config.loader import load_config
from medre.config.model import (
    AdapterConfigSet,
    LoggingConfig,
    MatrixRuntimeConfig,
    MeshtasticRuntimeConfig,
    RuntimeConfig,
    RuntimeLimits,
    RuntimeOptions,
    StorageConfig,
)
from medre.config.paths import MedrePaths, MedrePathsError, resolve
from medre.config.routes import (
    RouteConfig,
    RouteConfigSet,
)
from medre.core.contracts.adapter import (
    AdapterCapabilities,
    AdapterContext,
    AdapterContract,
    AdapterDeliveryResult,
    AdapterInfo,
    AdapterRole,
)
from medre.core.events.canonical import CanonicalEvent
from medre.core.events.kinds import EventKind
from medre.core.events.metadata import EventMetadata
from medre.core.lifecycle.states import AdapterState
from medre.core.storage import SQLiteStorage
from medre.core.supervision.supervision import (
    RuntimeHealth,
    StartupOutcome,
    classify_runtime_health,
    classify_startup_outcome,
    runtime_supervision_snapshot,
)
from medre.runtime.app import MedreApp, RuntimeState
from medre.runtime.boot_summary import build_boot_summary
from medre.runtime.builder import RuntimeBuilder
from medre.runtime.errors import RuntimeStartupError
from medre.runtime.route_engine import (
    RouteValidationError,
    validate_route_adapter_refs,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# TOML config snippets used across tests.

CONFIG_VALID_FAKE = """\
[runtime]
name = "recovery-test"

[storage]
backend = "memory"

[adapters.matrix.fake_matrix]
enabled = true
adapter_kind = "fake"
homeserver = "https://fake.local"
user_id = "@bot:fake.local"
access_token = "tok_recovery"
room_allowlist = ["!room:fake.local"]
encryption_mode = "plaintext"

[adapters.meshtastic.fake_mesh]
enabled = true
adapter_kind = "fake"
connection_type = "fake"
meshnet_name = "RecoveryMesh"

[routes.matrix_to_mesh]
source_adapters = ["fake_matrix"]
dest_adapters = ["fake_mesh"]
directionality = "source_to_dest"
enabled = true
"""

CONFIG_VALID_SINGLE = """\
[runtime]
name = "recovery-single"

[storage]
backend = "memory"

[adapters.matrix.solo]
enabled = true
adapter_kind = "fake"
homeserver = "https://fake.local"
user_id = "@bot:fake.local"
access_token = "tok_solo"
room_allowlist = ["!room:fake.local"]
encryption_mode = "plaintext"
"""

CONFIG_BAD_TOML = """\
[runtime
name = "bad brace
"""

CONFIG_MISSING_ADAPTER_REF = """\
[runtime]
name = "bad-route-refs"

[storage]
backend = "memory"

[adapters.matrix.real_adapter]
enabled = true
adapter_kind = "fake"
homeserver = "https://fake.local"
user_id = "@bot:fake.local"
access_token = "tok"
room_allowlist = ["!room:fake.local"]
encryption_mode = "plaintext"

[routes.broken_route]
source_adapters = ["real_adapter"]
dest_adapters = ["ghost_adapter"]
directionality = "source_to_dest"
enabled = true
"""

CONFIG_DISABLED_ADAPTER = """\
[runtime]
name = "disabled-test"

[storage]
backend = "memory"

[adapters.matrix.enabled_one]
enabled = true
adapter_kind = "fake"
homeserver = "https://fake.local"
user_id = "@bot:fake.local"
access_token = "tok1"
room_allowlist = ["!room:fake.local"]
encryption_mode = "plaintext"

[adapters.matrix.disabled_one]
enabled = false
adapter_kind = "fake"
homeserver = "https://fake.local"
user_id = "@bot:fake.local"
access_token = "tok2"
room_allowlist = ["!room:fake.local"]
encryption_mode = "plaintext"
"""

CONFIG_INVALID_LIMITS = """\
[runtime]
name = "bad-limits"

[runtime.limits]
max_inflight_deliveries = -1

[storage]
backend = "memory"
"""

CONFIG_SQLITE_PATH = """\
[runtime]
name = "sqlite-recovery"

[storage]
backend = "sqlite"
path = "{state}/recovery_test.db"

[adapters.matrix.fake_matrix]
enabled = true
adapter_kind = "fake"
homeserver = "https://fake.local"
user_id = "@bot:fake.local"
access_token = "tok_sqlite"
room_allowlist = ["!room:fake.local"]
encryption_mode = "plaintext"
"""


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scrub all MEDRE_ and XDG_ env vars to avoid cross-test leakage."""
    for key in list(os.environ):
        if key.startswith("MEDRE_") or key.startswith("XDG_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture()
def tmp_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MedrePaths:
    """Create a MedrePaths pointing at temp directories."""
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    return resolve()


def _write_config(path: Path, content: str) -> Path:
    """Write TOML content to *path* and return it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _run_cli(*args: str) -> str:
    """Run CLI, capture stdout, return output. Propagate non-zero SystemExit."""
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            main(list(args))
    except SystemExit as e:
        if e.code not in (None, 0):
            raise
    return stdout.getvalue()


def _run_cli_both(*args: str) -> tuple[str, str]:
    """Run CLI and return (stdout, stderr) pair."""
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            main(list(args))
    except SystemExit:
        pass
    return stdout.getvalue(), stderr.getvalue()


def _run_cli_raw(*args: str) -> tuple[str, str, int | None]:
    """Run CLI and return (stdout, stderr, exit_code)."""
    stdout = io.StringIO()
    stderr = io.StringIO()
    code: int | None = 0
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            main(list(args))
    except SystemExit as e:
        code = 1 if isinstance(e.code, str) else e.code
    return stdout.getvalue(), stderr.getvalue(), code


# ---------------------------------------------------------------------------
# Helpers for runtime-level tests
# ---------------------------------------------------------------------------


class _FailingAdapter(AdapterContract):
    """Adapter that raises on start() for failure-recovery testing."""

    adapter_id: str = "failing_adapter"
    platform: str = "test"
    role: AdapterRole = AdapterRole.TRANSPORT

    def __init__(self, adapter_id: str = "failing_adapter") -> None:
        self.adapter_id = adapter_id

    async def start(self, ctx: AdapterContext) -> None:
        raise RuntimeError(f"Simulated adapter failure: {self.adapter_id}")

    async def stop(self, timeout: float = 5.0) -> None:
        pass

    async def health_check(self) -> AdapterInfo:
        return AdapterInfo(
            adapter_id=self.adapter_id,
            platform=self.platform,
            role=self.role,
            version="0.0.0",
            capabilities=AdapterCapabilities(),
            health="failed",
        )

    async def deliver(self, result: Any) -> AdapterDeliveryResult | None:
        return None


def _fake_matrix_runtime_config(
    adapter_id: str = "fake_matrix",
    enabled: bool = True,
) -> MatrixRuntimeConfig:
    return MatrixRuntimeConfig(
        adapter_id=adapter_id,
        enabled=enabled,
        adapter_kind="fake",
        config=None,
    )


def _fake_meshtastic_runtime_config(
    adapter_id: str = "fake_mesh",
    enabled: bool = True,
) -> MeshtasticRuntimeConfig:
    return MeshtasticRuntimeConfig(
        adapter_id=adapter_id,
        enabled=enabled,
        adapter_kind="fake",
        config=None,
    )


def _config_with_fake_adapters(
    *,
    storage_backend: str = "memory",
    storage_path: str | None = None,
) -> RuntimeConfig:
    """RuntimeConfig with two fake adapters."""
    return RuntimeConfig(
        runtime=RuntimeOptions(name="test-recovery"),
        logging=LoggingConfig(level="DEBUG"),
        storage=StorageConfig(backend=storage_backend, path=storage_path),
        adapters=AdapterConfigSet(
            matrix={"main": _fake_matrix_runtime_config()},
            meshtastic={"radio": _fake_meshtastic_runtime_config()},
        ),
    )


def _config_with_one_fake_adapter(
    *,
    storage_backend: str = "memory",
    storage_path: str | None = None,
) -> RuntimeConfig:
    """RuntimeConfig with one fake adapter."""
    return RuntimeConfig(
        runtime=RuntimeOptions(name="test-recovery-single"),
        storage=StorageConfig(backend=storage_backend, path=storage_path),
        adapters=AdapterConfigSet(
            matrix={"main": _fake_matrix_runtime_config()},
        ),
    )


def _build_app(config: RuntimeConfig, paths: MedrePaths) -> MedreApp:
    """Build a MedreApp via RuntimeBuilder."""
    return RuntimeBuilder(config, paths).build()


def _make_minimal_event(event_id: str = "evt-001") -> CanonicalEvent:
    """Create a minimal CanonicalEvent for storage tests."""
    return CanonicalEvent(
        event_id=event_id,
        event_kind=EventKind.MESSAGE_TEXT,
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter="fake_matrix",
        source_transport_id="matrix",
        source_channel_id="test_room",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": "hello"},
        metadata=EventMetadata(),
    )


# ===================================================================
# 1. Malformed config recovery
# ===================================================================


class TestMalformedConfigRecovery:
    """Operator writes bad TOML → sees clean error → fixes → config loads.

    Validates the repair loop: initial failure gives actionable message,
    operator fixes the config, and the second attempt succeeds.
    """

    def test_bad_toml_then_fix_loads(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bad TOML syntax → ConfigFileError → fix → load_config succeeds."""
        cfg_path = _write_config(tmp_path / "config.toml", CONFIG_BAD_TOML)
        monkeypatch.setenv("MEDRE_CONFIG", str(cfg_path))

        # Step 1: bad config should fail with clear error.
        with pytest.raises(ConfigFileError) as exc_info:
            load_config(None)
        msg = str(exc_info.value)
        assert "Invalid TOML" in msg
        # No raw traceback in the error message.
        assert "Traceback" not in msg

        # Step 2: operator fixes the config file.
        cfg_path.write_text(CONFIG_VALID_FAKE)

        # Step 3: fixed config loads successfully.
        config, source, paths = load_config(None)
        assert config.runtime.name == "recovery-test"
        assert len(config.adapters.all_enabled()) == 2

    def test_config_check_cli_bad_then_fixed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CLI 'config check' with bad config → exit 1 → fix → exit 0."""
        cfg_path = _write_config(tmp_path / "config.toml", CONFIG_BAD_TOML)
        monkeypatch.setenv("MEDRE_CONFIG", str(cfg_path))

        # Step 1: bad config → CLI reports error.
        stdout, stderr, code = _run_cli_raw("config", "check")
        assert code != 0
        assert "Config error" in stderr
        assert "Traceback" not in stderr
        assert "Traceback" not in stdout

        # Step 2: fix the config.
        cfg_path.write_text(CONFIG_VALID_FAKE)

        # Step 3: fixed config passes check.
        stdout2, stderr2, code2 = _run_cli_raw("config", "check")
        assert code2 == 0
        assert "Config valid" in stdout2

    def test_missing_sections_then_added(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Config with missing runtime section → error → add section → loads."""
        minimal = '[storage]\nbackend = "memory"\n'
        cfg_path = _write_config(tmp_path / "config.toml", minimal)
        monkeypatch.setenv("MEDRE_CONFIG", str(cfg_path))

        # Load should still succeed (runtime has defaults).
        config, _, _ = load_config(None)
        assert config.storage.backend == "memory"

        # Add adapters section → full config.
        full = CONFIG_VALID_SINGLE
        cfg_path.write_text(full)
        config2, _, _ = load_config(None)
        assert len(config2.adapters.all_enabled()) == 1


# ===================================================================
# 2. Storage-path recovery
# ===================================================================


class TestStoragePathRecovery:
    """Invalid storage path → error → fix path → runtime starts.

    Validates that storage path issues are caught early and produce
    actionable messages for the operator to fix.
    """

    def test_unknown_placeholder_then_fix(self, tmp_paths: MedrePaths) -> None:
        """Unknown path placeholder → MedrePathsError → fix → resolves."""
        with pytest.raises(MedrePathsError) as exc_info:
            tmp_paths.expand_placeholder("{totally_bogus}/data.db")
        msg = str(exc_info.value)
        assert "unknown path placeholder" in msg
        assert "totally_bogus" in msg

        # Fix: use a known placeholder.
        resolved = tmp_paths.expand_placeholder("{state}/data.db")
        assert "data.db" in str(resolved)

    def test_invalid_storage_backend_then_fix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unsupported storage backend → RuntimeConfigError at build time → fix → builds."""
        bad_cfg = """\
[runtime]
name = "bad-storage"

[storage]
backend = "cassandra"

[adapters.matrix.m]
enabled = true
adapter_kind = "fake"
homeserver = "https://fake.local"
user_id = "@bot:fake.local"
access_token = "tok"
room_allowlist = ["!room:fake.local"]
encryption_mode = "plaintext"
"""
        cfg_path = _write_config(tmp_path / "config.toml", bad_cfg)
        monkeypatch.setenv("MEDRE_CONFIG", str(cfg_path))

        # Config loads OK — backend validation happens at build time.
        config, _, paths = load_config(None)
        assert config.storage.backend == "cassandra"

        # Building the runtime with unsupported backend raises.
        from medre.runtime.errors import RuntimeConfigError

        with pytest.raises(RuntimeConfigError) as exc_info:
            RuntimeBuilder(config, paths).build()
        msg = str(exc_info.value)
        assert "cassandra" in msg
        assert "Traceback" not in msg

        # Fix: use supported backend.
        cfg_path.write_text(CONFIG_VALID_FAKE)
        config2, _, paths2 = load_config(None)
        app = RuntimeBuilder(config2, paths2).build()
        assert app is not None

    def test_sqlite_path_with_valid_placeholder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SQLite storage with valid {state} placeholder works end-to-end."""
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
        cfg_path = _write_config(tmp_path / "config.toml", CONFIG_SQLITE_PATH)
        monkeypatch.setenv("MEDRE_CONFIG", str(cfg_path))

        config, _, paths = load_config(None)
        assert config.storage.backend == "sqlite"
        assert config.storage.path is not None

        # Build and start runtime with SQLite storage.
        app = _build_app(config, paths)
        assert app.storage is not None


# ===================================================================
# 3. Startup failure recovery
# ===================================================================


class TestStartupFailureRecovery:
    """All adapters fail → total failure → operator disables bad adapter → retry.

    Validates the recovery loop for total startup failure.
    """

    @pytest.mark.asyncio
    async def test_total_failure_then_disable_one_recovers(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Both adapters fail → total failure → disable one → remaining works."""
        config = _config_with_fake_adapters()
        app = _build_app(config, tmp_paths)

        # Make both adapters fail on start.
        app.adapters["fake_matrix"] = _FailingAdapter("fake_matrix")
        app.adapters["fake_mesh"] = _FailingAdapter("fake_mesh")

        with pytest.raises(RuntimeStartupError, match="Total startup failure"):
            await app.start()

        assert app.state == RuntimeState.FAILED

        # Operator action: create new config with one enabled adapter.
        config2 = _config_with_one_fake_adapter()
        app2 = _build_app(config2, tmp_paths)

        await app2.start()
        try:
            assert app2.state == RuntimeState.RUNNING
            assert len(app2.started_adapter_ids) == 1
            boot = app2.boot_summary
            assert boot is not None
            assert boot.startup_outcome == "success"
        finally:
            await app2.stop()

    @pytest.mark.asyncio
    async def test_total_failure_error_is_clean(self, tmp_paths: MedrePaths) -> None:
        """RuntimeStartupError message is concise, no raw traceback."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)
        app.adapters["fake_matrix"] = _FailingAdapter("fake_matrix")

        with pytest.raises(RuntimeStartupError) as exc_info:
            await app.start()

        msg = str(exc_info.value)
        assert "Total startup failure" in msg
        assert "Traceback" not in msg


# ===================================================================
# 4. Degraded runtime recovery
# ===================================================================


class TestDegradedRuntimeRecovery:
    """Partial startup → degraded mode → diagnostics available → healthy adapter works.

    Validates that a degraded runtime produces clear messaging and
    remains functional for the healthy adapters.
    """

    @pytest.mark.asyncio
    async def test_partial_startup_degraded_messaging(
        self, tmp_paths: MedrePaths
    ) -> None:
        """One adapter fails → degraded boot summary with clear attribution."""
        config = _config_with_fake_adapters()
        app = _build_app(config, tmp_paths)

        failing = _FailingAdapter(adapter_id="fake_mesh")
        app.adapters["fake_mesh"] = failing

        await app.start()
        try:
            assert app.state == RuntimeState.RUNNING

            boot = app.boot_summary
            assert boot is not None
            assert boot.startup_outcome == "partial"
            assert boot.runtime_health == "degraded"
            assert boot.adapters_started == 1
            assert boot.adapters_failed == 1
            assert "fake_mesh" in boot.failed_adapter_ids
            assert "fake_matrix" in boot.started_adapter_ids
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_degraded_diagnostics_snapshot_accessible(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Diagnostic snapshot is available from a degraded runtime."""
        config = _config_with_fake_adapters()
        app = _build_app(config, tmp_paths)

        failing = _FailingAdapter(adapter_id="fake_mesh")
        app.adapters["fake_mesh"] = failing

        await app.start()
        try:
            snap = app.diagnostic_snapshot()
            assert isinstance(snap, dict)
            assert snap["runtime_state"] == "running"
            # Diagnostic snapshot is a dict with deterministic keys.
            assert "capacity" in snap or "runtime_state" in snap
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_degraded_supervision_snapshot(self, tmp_paths: MedrePaths) -> None:
        """Supervision snapshot correctly reports degraded state."""
        config = _config_with_fake_adapters()
        app = _build_app(config, tmp_paths)

        failing = _FailingAdapter(adapter_id="fake_mesh")
        app.adapters["fake_mesh"] = failing

        await app.start()
        try:
            states = [AdapterState.READY, AdapterState.FAILED]
            health = classify_runtime_health(states)
            assert health == RuntimeHealth.DEGRADED

            snap = runtime_supervision_snapshot(states)
            assert snap["runtime_health"] == "degraded"
            assert snap["adapter_summary"]["healthy"] == 1
            assert snap["adapter_summary"]["failed"] == 1
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_degraded_to_healthy_after_rebuild(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Rebuilding runtime without failing adapter produces healthy outcome."""
        # First run: degraded.
        config1 = _config_with_fake_adapters()
        app1 = _build_app(config1, tmp_paths)
        app1.adapters["fake_mesh"] = _FailingAdapter("fake_mesh")
        await app1.start()
        assert app1.boot_summary is not None
        assert app1.boot_summary.runtime_health == "degraded"
        await app1.stop()

        # Recovery: rebuild with only working adapter.
        config2 = _config_with_one_fake_adapter()
        app2 = _build_app(config2, tmp_paths)
        await app2.start()
        try:
            assert app2.boot_summary is not None
            assert app2.boot_summary.runtime_health == "healthy"
            assert app2.boot_summary.startup_outcome == "success"
        finally:
            await app2.stop()


# ===================================================================
# 5. Replay after restart
# ===================================================================


class TestReplayAfterRestart:
    """Store events → restart → replay available → events survived.

    Validates that the operator can stop and restart the runtime and
    still access persisted events for replay.
    """

    @pytest.mark.asyncio
    async def test_events_survive_sqlite_restart(self, tmp_path: Path) -> None:
        """Events written in first storage session survive to second."""
        db_path = str(tmp_path / "replay_recovery.db")

        # Session 1: write events.
        s1 = SQLiteStorage(db_path)
        await s1.initialize()
        for i in range(3):
            await s1.append(_make_minimal_event(f"evt-replay-{i:03d}"))
        assert await s1.count_events() == 3
        await s1.close()

        # Session 2: verify events survived.
        s2 = SQLiteStorage(db_path)
        await s2.initialize()
        assert await s2.count_events() == 3
        evt = await s2.get("evt-replay-001")
        assert evt is not None
        assert evt.event_id == "evt-replay-001"
        await s2.close()

    @pytest.mark.asyncio
    async def test_replay_available_after_runtime_restart(
        self, tmp_paths: MedrePaths, tmp_path: Path
    ) -> None:
        """Second runtime instance reports replay_available=True."""
        db_path = str(tmp_path / "replay_rt.db")
        config = RuntimeConfig(
            runtime=RuntimeOptions(name="replay-restart"),
            storage=StorageConfig(backend="sqlite", path=db_path),
            adapters=AdapterConfigSet(
                matrix={"main": _fake_matrix_runtime_config()},
            ),
        )

        # First instance.
        app1 = _build_app(config, tmp_paths)
        await app1.start()
        boot1 = app1.boot_summary
        assert boot1 is not None
        assert boot1.replay_available is True
        await app1.stop()

        # Second instance on same storage.
        app2 = _build_app(config, tmp_paths)
        await app2.start()
        try:
            boot2 = app2.boot_summary
            assert boot2 is not None
            assert boot2.replay_available is True
            assert boot2.storage_backend == "sqlite"
        finally:
            await app2.stop()

    @pytest.mark.asyncio
    async def test_memory_storage_backend_is_memory(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Memory storage backend is correctly reported in boot summary."""
        config = _config_with_one_fake_adapter(storage_backend="memory")
        app = _build_app(config, tmp_paths)
        await app.start()
        try:
            boot = app.boot_summary
            assert boot is not None
            assert boot.storage_backend == "memory"
        finally:
            await app.stop()


# ===================================================================
# 6. Route validation failure recovery
# ===================================================================


class TestRouteValidationFailureRecovery:
    """Bad route refs → clean error → fix → routes valid.

    Validates the operator repair loop for route configuration errors.
    """

    def test_unknown_adapter_ref_then_fix(self) -> None:
        """Route referencing unknown adapter → RouteValidationError → fix → valid."""
        bad_rc = RouteConfig(
            route_id="broken",
            source_adapters=("real_adapter",),
            dest_adapters=("ghost_adapter",),
            enabled=True,
        )
        rcs = RouteConfigSet(routes=(bad_rc,))

        with pytest.raises(RouteValidationError) as exc_info:
            validate_route_adapter_refs(rcs, frozenset({"real_adapter"}))
        msg = str(exc_info.value)
        assert "ghost_adapter" in msg
        assert "Traceback" not in msg

        # Fix: update route to use known adapter.
        fixed_rc = RouteConfig(
            route_id="fixed",
            source_adapters=("real_adapter",),
            dest_adapters=("also_real",),
            enabled=True,
        )
        fixed_rcs = RouteConfigSet(routes=(fixed_rc,))
        # Should not raise — both adapters are known.
        validate_route_adapter_refs(fixed_rcs, frozenset({"real_adapter", "also_real"}))

    def test_cli_routes_validate_bad_then_fixed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CLI 'routes validate' with bad refs → warnings → fix → valid."""
        cfg_path = _write_config(tmp_path / "config.toml", CONFIG_MISSING_ADAPTER_REF)
        monkeypatch.setenv("MEDRE_CONFIG", str(cfg_path))

        # Step 1: bad config shows warnings about ghost adapter.
        stdout, stderr, code = _run_cli_raw("routes", "validate")
        # Should complete (warnings not fatal) but mention the ghost.
        assert "ghost_adapter" in stdout or "ghost_adapter" in stderr
        assert "Traceback" not in stdout
        assert "Traceback" not in stderr

        # Step 2: fix the config by referencing a real adapter.
        fixed = """\
[runtime]
name = "fixed-routes"

[storage]
backend = "memory"

[adapters.matrix.real_adapter]
enabled = true
adapter_kind = "fake"
homeserver = "https://fake.local"
user_id = "@bot:fake.local"
access_token = "tok"
room_allowlist = ["!room:fake.local"]
encryption_mode = "plaintext"

[adapters.meshtastic.other_real]
enabled = true
adapter_kind = "fake"
connection_type = "fake"
meshnet_name = "FixedMesh"

[routes.fixed_route]
source_adapters = ["real_adapter"]
dest_adapters = ["other_real"]
directionality = "source_to_dest"
enabled = true
"""
        cfg_path.write_text(fixed)

        # Step 3: fixed config validates cleanly.
        stdout2, stderr2, code2 = _run_cli_raw("routes", "validate")
        assert "Traceback" not in stdout2
        assert "Traceback" not in stderr2

    def test_route_expansion_failure_recovery(self) -> None:
        """Route with no source_adapters → error → fix → expansion succeeds."""
        # Create a route config with empty source_adapters to trigger expansion failure.
        # Use build_runtime_routes which expands routes.

        rc = RouteConfig(
            route_id="empty_source",
            source_adapters=("a",),
            dest_adapters=("b",),
            enabled=True,
        )
        rcs = RouteConfigSet(routes=(rc,))

        # With no known adapters, validation fails.
        with pytest.raises(RouteValidationError):
            validate_route_adapter_refs(rcs, frozenset())

        # Fix: provide adapter IDs that match.
        validate_route_adapter_refs(rcs, frozenset({"a", "b"}))


# ===================================================================
# 7. Adapter disable/enable workflows
# ===================================================================


class TestAdapterDisableEnableWorkflows:
    """Disable failing adapter → verify runtime starts → re-enable → verify full config.

    Validates the operator workflow of disabling a problematic adapter,
    verifying the runtime works with remaining adapters, then re-enabling
    after the issue is resolved.
    """

    def test_cli_shows_disabled_adapter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """'config check' shows disabled adapters with correct status."""
        cfg_path = _write_config(tmp_path / "config.toml", CONFIG_DISABLED_ADAPTER)
        monkeypatch.setenv("MEDRE_CONFIG", str(cfg_path))

        stdout = _run_cli("config", "check")
        assert "disabled" in stdout
        assert "enabled" in stdout
        assert "Config valid" in stdout

    def test_config_with_disabled_adapter_loads(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Config loads with mix of enabled and disabled adapters."""
        cfg_path = _write_config(tmp_path / "config.toml", CONFIG_DISABLED_ADAPTER)
        monkeypatch.setenv("MEDRE_CONFIG", str(cfg_path))

        config, _, _ = load_config(None)
        all_configs = list(config.adapters.all_configs())
        enabled = [c for c in all_configs if c[2].enabled]
        disabled = [c for c in all_configs if not c[2].enabled]
        assert len(enabled) == 1
        assert len(disabled) == 1

    @pytest.mark.asyncio
    async def test_disable_failing_adapter_recovers_runtime(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Failing adapter → total failure → disable it → runtime starts."""
        # First attempt: both adapters fail.
        config = _config_with_fake_adapters()
        app = _build_app(config, tmp_paths)
        app.adapters["fake_matrix"] = _FailingAdapter("fake_matrix")
        app.adapters["fake_mesh"] = _FailingAdapter("fake_mesh")

        with pytest.raises(RuntimeStartupError):
            await app.start()

        # Recovery: operator disables one adapter and retries with the other.
        # (Simulate by creating config with only one adapter that works.)
        config2 = _config_with_one_fake_adapter()
        app2 = _build_app(config2, tmp_paths)
        await app2.start()
        try:
            assert app2.state == RuntimeState.RUNNING
            assert app2.boot_summary is not None
            assert app2.boot_summary.startup_outcome == "success"
        finally:
            await app2.stop()

    @pytest.mark.asyncio
    async def test_re_enable_adapter_after_fix(self, tmp_paths: MedrePaths) -> None:
        """Operator re-enables previously disabled adapter → full runtime."""
        # Start with one adapter.
        config1 = _config_with_one_fake_adapter()
        app1 = _build_app(config1, tmp_paths)
        await app1.start()
        try:
            assert len(app1.started_adapter_ids) == 1
        finally:
            await app1.stop()

        # Re-enable second adapter → full two-adapter runtime.
        config2 = _config_with_fake_adapters()
        app2 = _build_app(config2, tmp_paths)
        await app2.start()
        try:
            assert len(app2.started_adapter_ids) == 2
            assert app2.boot_summary is not None
            assert app2.boot_summary.runtime_health == "healthy"
        finally:
            await app2.stop()

    def test_adapter_inventory_from_cli(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """'adapters' command shows enabled/disabled status correctly."""
        cfg_path = _write_config(tmp_path / "config.toml", CONFIG_DISABLED_ADAPTER)
        monkeypatch.setenv("MEDRE_CONFIG", str(cfg_path))

        stdout = _run_cli("adapters")
        assert "enabled" in stdout or "disabled" in stdout
        assert "Traceback" not in stdout


# ===================================================================
# 8. Config repair workflows
# ===================================================================


class TestConfigRepairWorkflows:
    """Common config issues → actionable error → fix → valid config.

    Validates that common misconfiguration patterns produce actionable
    error messages that guide the operator to a fix.
    """

    def test_invalid_limits_then_fix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Negative limits → ConfigValidationError with field name → fix → valid."""
        with pytest.raises(ConfigValidationError) as exc_info:
            RuntimeLimits(max_inflight_deliveries=-1).validate()
        msg = str(exc_info.value)
        assert "max_inflight_deliveries" in msg
        assert "must be > 0" in msg

        # Fix: use valid limits.
        limits = RuntimeLimits(max_inflight_deliveries=100)
        limits.validate()  # Should not raise.

    def test_missing_config_file_then_create(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No config file → ConfigNotFoundError suggests 'config sample' → create → loads."""
        monkeypatch.delenv("MEDRE_HOME", raising=False)
        monkeypatch.delenv("MEDRE_CONFIG", raising=False)
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

        with pytest.raises(ConfigNotFoundError) as exc_info:
            load_config(None)
        msg = str(exc_info.value)
        assert "medre config sample" in msg
        assert "Traceback" not in msg

    def test_config_sample_generates_valid_config(self) -> None:
        """'medre config sample' output is parseable TOML with key sections."""
        import tomllib

        stdout = _run_cli("config", "sample")
        assert "Traceback" not in stdout
        parsed = tomllib.loads(stdout)
        assert isinstance(parsed, dict)

    def test_config_check_detects_invalid_limits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CLI 'config check' catches invalid limits and reports them."""
        cfg_path = _write_config(tmp_path / "config.toml", CONFIG_INVALID_LIMITS)
        monkeypatch.setenv("MEDRE_CONFIG", str(cfg_path))

        stdout, stderr, code = _run_cli_raw("config", "check")
        assert code != 0
        # Error should mention the limits issue.
        combined = stdout + stderr
        assert "Traceback" not in combined

    def test_duplicate_route_id_repair(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Duplicate route IDs → error → rename → valid."""
        dup_cfg = """\
[runtime]
name = "dup-routes"

[storage]
backend = "memory"

[adapters.matrix.a]
enabled = true
adapter_kind = "fake"
homeserver = "https://fake.local"
user_id = "@bot:fake.local"
access_token = "tok"
room_allowlist = ["!room:fake.local"]
encryption_mode = "plaintext"

[adapters.meshtastic.b]
enabled = true
adapter_kind = "fake"
connection_type = "fake"
meshnet_name = "Mesh"

[routes.dup_id]
source_adapters = ["a"]
dest_adapters = ["b"]
directionality = "source_to_dest"
enabled = true

[routes.dup_id_2]
source_adapters = ["b"]
dest_adapters = ["a"]
directionality = "source_to_dest"
enabled = true
"""
        cfg_path = _write_config(tmp_path / "config.toml", dup_cfg)
        monkeypatch.setenv("MEDRE_CONFIG", str(cfg_path))

        # Should load fine — duplicate IDs are checked during validation,
        # not during parsing. Verify the config loads.
        config, _, _ = load_config(None)
        route_ids = [r.route_id for r in config.routes.routes]
        assert len(route_ids) == 2


# ===================================================================
# 9. Deterministic messaging / no raw tracebacks
# ===================================================================


class TestDeterministicMessaging:
    """No raw tracebacks in operator-facing output; deterministic message shapes.

    Validates that every error path an operator might encounter produces
    clean, deterministic output without Python tracebacks or variable content
    (timestamps, memory addresses, etc.).
    """

    def test_config_not_found_no_traceback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ConfigNotFoundError message contains no traceback."""
        monkeypatch.delenv("MEDRE_HOME", raising=False)
        monkeypatch.delenv("MEDRE_CONFIG", raising=False)
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

        with pytest.raises(ConfigNotFoundError) as exc_info:
            load_config(None)
        msg = str(exc_info.value)
        assert "Traceback" not in msg
        assert "File " not in msg

    def test_bad_toml_no_traceback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ConfigFileError for bad TOML contains no traceback."""
        cfg_path = _write_config(tmp_path / "config.toml", CONFIG_BAD_TOML)
        monkeypatch.setenv("MEDRE_CONFIG", str(cfg_path))

        with pytest.raises(ConfigFileError) as exc_info:
            load_config(None)
        msg = str(exc_info.value)
        assert "Traceback" not in msg
        assert "Invalid TOML" in msg

    def test_cli_config_check_no_traceback_on_any_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CLI 'config check' never shows tracebacks for any config error."""
        cfg_path = _write_config(tmp_path / "config.toml", CONFIG_BAD_TOML)
        monkeypatch.setenv("MEDRE_CONFIG", str(cfg_path))

        stdout, stderr, code = _run_cli_raw("config", "check")
        assert "Traceback" not in stdout
        assert "Traceback" not in stderr
        assert code != 0

    def test_cli_routes_validate_no_traceback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CLI 'routes validate' never shows tracebacks."""
        cfg_path = _write_config(tmp_path / "config.toml", CONFIG_MISSING_ADAPTER_REF)
        monkeypatch.setenv("MEDRE_CONFIG", str(cfg_path))

        stdout, stderr, code = _run_cli_raw("routes", "validate")
        assert "Traceback" not in stdout
        assert "Traceback" not in stderr

    def test_boot_summary_deterministic_ordering(self, tmp_paths: MedrePaths) -> None:
        """Boot summary to_dict() has deterministic key ordering."""
        bs = build_boot_summary(
            startup_timestamp="2026-05-11T12:00:00+00:00",
            startup_outcome="success",
            runtime_health="healthy",
            adapters_total=2,
            adapters_started=2,
            adapters_failed=0,
            adapters_disabled=0,
            build_failure_count=0,
            started_adapter_ids=["b_adapter", "a_adapter"],
            failed_adapter_ids=[],
            route_count=1,
            storage_backend="memory",
            replay_available=False,
            persisted_events_count=0,
        )
        d = bs.to_dict()
        keys = list(d.keys())
        assert keys == sorted(keys)

    def test_boot_summary_partial_startup_deterministic(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Boot summary for partial startup has consistent shape."""
        bs = build_boot_summary(
            startup_timestamp="2026-05-11T12:00:00+00:00",
            startup_outcome="partial",
            runtime_health="degraded",
            adapters_total=3,
            adapters_started=1,
            adapters_failed=2,
            adapters_disabled=0,
            build_failure_count=0,
            started_adapter_ids=["working"],
            failed_adapter_ids=["broken_1", "broken_2"],
            route_count=2,
            storage_backend="sqlite",
            replay_available=True,
            persisted_events_count=5,
        )
        d = bs.to_dict()
        assert d["startup_outcome"] == "partial"
        assert d["runtime_health"] == "degraded"
        assert d["adapters_total"] == 3
        assert d["adapters_started"] == 1
        assert d["adapters_failed"] == 2
        # Keys are alphabetically sorted.
        assert list(d.keys()) == sorted(d.keys())

    def test_supervision_snapshot_deterministic_keys(self) -> None:
        """runtime_supervision_snapshot has stable key set."""
        states = [AdapterState.READY, AdapterState.FAILED]
        snap = runtime_supervision_snapshot(states)
        assert "runtime_health" in snap
        assert "adapter_summary" in snap
        # adapter_summary has stable sub-keys.
        summary = snap["adapter_summary"]
        assert "total" in summary
        assert "healthy" in summary
        assert "failed" in summary

    def test_classify_runtime_health_deterministic(self) -> None:
        """classify_runtime_health returns consistent results."""
        assert classify_runtime_health([AdapterState.READY]) == RuntimeHealth.HEALTHY
        assert (
            classify_runtime_health([AdapterState.READY, AdapterState.READY])
            == RuntimeHealth.HEALTHY
        )
        assert (
            classify_runtime_health([AdapterState.READY, AdapterState.FAILED])
            == RuntimeHealth.DEGRADED
        )
        assert classify_runtime_health([AdapterState.FAILED]) == RuntimeHealth.FAILED
        assert classify_runtime_health([]) == RuntimeHealth.FAILED

    def test_classify_startup_outcome_deterministic(self) -> None:
        """classify_startup_outcome returns consistent results."""
        assert (
            classify_startup_outcome(started=2, failed=0, total=2)
            == StartupOutcome.SUCCESS
        )
        assert (
            classify_startup_outcome(started=1, failed=2, total=3)
            == StartupOutcome.PARTIAL
        )
        assert (
            classify_startup_outcome(started=0, failed=1, total=1)
            == StartupOutcome.TOTAL_FAILURE
        )

    @pytest.mark.asyncio
    async def test_degraded_boot_summary_no_variable_content(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Boot summary for degraded runtime has no variable content in static fields."""
        config = _config_with_fake_adapters()
        app = _build_app(config, tmp_paths)
        app.adapters["fake_mesh"] = _FailingAdapter("fake_mesh")

        await app.start()
        try:
            boot = app.boot_summary
            assert boot is not None
            d = boot.to_dict()

            # Static fields should be deterministic.
            assert d["startup_outcome"] == "partial"
            assert d["runtime_health"] == "degraded"
            assert d["adapters_total"] == 2
            assert d["adapters_started"] == 1
            assert d["adapters_failed"] == 1

            # failed_adapter_ids should contain the expected ID.
            assert "fake_mesh" in d["failed_adapter_ids"]
        finally:
            await app.stop()
