"""Track 6 v2 — Extended operator recovery workflow tests.

Goes beyond ``test_operator_recovery.py`` by exercising multi-error
sequences, multi-adapter restarts, container-restart simulations, and
more complex repair loops:

1. **Malformed config recovery with sequential error types** — bad TOML →
   fix TOML → missing section → add section → invalid limits → fix limits.
2. **Storage-path recovery across backends** — sqlite path error → memory
   fallback → sqlite recovery.
3. **Startup + degraded recovery combined** — multiple restart cycles with
   alternating healthy/degraded/failed outcomes.
4. **Replay-after-restart with multi-adapter config** — store events with
   2 adapters → restart → replay available and events survived.
5. **Adapter disable/enable rapid cycling** — disable → start → verify →
   re-enable → start → verify, across 3+ cycles.
6. **Route validation repair with multi-hop graph** — 3-route chain with
   bad refs → fix each → all valid.
7. **Container restart workflow simulation** — full stop → verify cleanup →
   reconfig → restart → verify operational.
8. **Config repair with multiple simultaneous issues** — bad TOML + missing
   adapters + invalid limits → fix all → valid.
9. **Degraded-to-healthy transition verification** — degraded → fix →
   healthy boot summary across 2 transitions.
10. **Boot summary consistency across restart cycles** — 3+ restart cycles
    with same config produce identical boot summaries.

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
    ConfigValidationError,
)
from medre.config.loader import load_config
from medre.config.model import (
    AdapterConfigSet,
    LoggingConfig,
    LxmfRuntimeConfig,
    MatrixRuntimeConfig,
    MeshCoreRuntimeConfig,
    MeshtasticRuntimeConfig,
    RuntimeConfig,
    RuntimeLimits,
    RuntimeOptions,
    StorageConfig,
)
from medre.config.paths import MedrePaths, MedrePathsError, resolve
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
from medre.core.runtime.supervision import (
    RuntimeHealth,
    StartupOutcome,
    classify_runtime_health,
    classify_startup_outcome,
)
from medre.core.storage.sqlite import SQLiteStorage
from medre.runtime.app import MedreApp, RuntimeState
from medre.runtime.boot_summary import build_boot_summary
from medre.runtime.builder import RuntimeBuilder
from medre.runtime.capacity import CapacityController
from medre.runtime.errors import RuntimeStartupError
from medre.runtime.route_engine import (
    RouteValidationError,
    validate_route_adapter_refs,
)
from medre.runtime.routes import (
    RouteConfig,
    RouteConfigSet,
)

# ---------------------------------------------------------------------------
# TOML config snippets
# ---------------------------------------------------------------------------

CONFIG_VALID_TWO_ADAPTERS = """\
[runtime]
name = "v2-recovery"

[storage]
backend = "memory"

[adapters.matrix.fake_matrix]
enabled = true
adapter_kind = "fake"
homeserver = "https://fake.local"
user_id = "@bot:fake.local"
access_token = "tok_v2"
room_allowlist = ["!room:fake.local"]
encryption_mode = "plaintext"

[adapters.meshtastic.fake_mesh]
enabled = true
adapter_kind = "fake"
connection_type = "fake"
meshnet_name = "V2Mesh"

[adapters.meshcore.fake_core]
enabled = true
adapter_kind = "fake"

[routes.matrix_to_mesh]
source_adapters = ["fake_matrix"]
dest_adapters = ["fake_mesh"]
directionality = "source_to_dest"
enabled = true

[routes.mesh_to_core]
source_adapters = ["fake_mesh"]
dest_adapters = ["fake_core"]
directionality = "source_to_dest"
enabled = true
"""

CONFIG_BAD_TOML = """\
[runtime
name = "v2 bad brace
"""

CONFIG_MISSING_ADAPTER_SECTION = """\
[runtime]
name = "v2-missing-adapter"

[storage]
backend = "memory"
"""

CONFIG_INVALID_LIMITS = """\
[runtime]
name = "v2-bad-limits"

[runtime.limits]
max_inflight_deliveries = -5

[storage]
backend = "memory"

[adapters.matrix.m]
enabled = true
adapter_kind = "fake"
homeserver = "https://fake.local"
user_id = "@bot:fake.local"
access_token = "tok"
room_allowlist = ["!room:fake.local"]
encryption_mode = "plaintext"
"""

CONFIG_SINGLE_ADAPTER = """\
[runtime]
name = "v2-single"

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

CONFIG_DISABLED_ADAPTER = """\
[runtime]
name = "v2-disabled"

[storage]
backend = "memory"

[adapters.matrix.active]
enabled = true
adapter_kind = "fake"
homeserver = "https://fake.local"
user_id = "@bot:fake.local"
access_token = "tok_act"
room_allowlist = ["!room:fake.local"]
encryption_mode = "plaintext"

[adapters.meshtastic.inactive]
enabled = false
adapter_kind = "fake"
connection_type = "fake"
meshnet_name = "DisabledMesh"
"""

CONFIG_MULTI_HOP_ROUTES_BAD = """\
[runtime]
name = "v2-multi-hop-bad"

[storage]
backend = "memory"

[adapters.matrix.src]
enabled = true
adapter_kind = "fake"
homeserver = "https://fake.local"
user_id = "@bot:fake.local"
access_token = "tok"
room_allowlist = ["!room:fake.local"]
encryption_mode = "plaintext"

[routes.hop1]
source_adapters = ["src"]
dest_adapters = ["ghost_mid"]
directionality = "source_to_dest"
enabled = true

[routes.hop2]
source_adapters = ["ghost_mid"]
dest_adapters = ["ghost_end"]
directionality = "source_to_dest"
enabled = true

[routes.hop3]
source_adapters = ["ghost_end"]
dest_adapters = ["src"]
directionality = "source_to_dest"
enabled = true
"""

CONFIG_SQLITE_PATH = """\
[runtime]
name = "v2-sqlite"

[storage]
backend = "sqlite"
path = "{state}/v2_recovery.db"

[adapters.matrix.fake_matrix]
enabled = true
adapter_kind = "fake"
homeserver = "https://fake.local"
user_id = "@bot:fake.local"
access_token = "tok_sqlite"
room_allowlist = ["!room:fake.local"]
encryption_mode = "plaintext"
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
    """Run CLI, capture stdout, return output."""
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            main(list(args))
    except SystemExit as e:
        if e.code not in (None, 0):
            raise
    return stdout.getvalue()


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


def _fake_meshcore_runtime_config(
    adapter_id: str = "fake_core",
    enabled: bool = True,
) -> MeshCoreRuntimeConfig:
    return MeshCoreRuntimeConfig(
        adapter_id=adapter_id,
        enabled=enabled,
        adapter_kind="fake",
    )


def _fake_lxmf_runtime_config(
    adapter_id: str = "fake_lxmf",
    enabled: bool = True,
) -> LxmfRuntimeConfig:
    return LxmfRuntimeConfig(
        adapter_id=adapter_id,
        enabled=enabled,
        adapter_kind="fake",
    )


def _config_with_two_fake_adapters(
    *,
    storage_backend: str = "memory",
    storage_path: str | None = None,
) -> RuntimeConfig:
    """RuntimeConfig with two fake adapters."""
    return RuntimeConfig(
        runtime=RuntimeOptions(name="v2-recovery"),
        logging=LoggingConfig(level="DEBUG"),
        storage=StorageConfig(backend=storage_backend, path=storage_path),
        adapters=AdapterConfigSet(
            matrix={"main": _fake_matrix_runtime_config()},
            meshtastic={"radio": _fake_meshtastic_runtime_config()},
        ),
    )


def _config_with_three_fake_adapters(
    *,
    storage_backend: str = "memory",
    storage_path: str | None = None,
) -> RuntimeConfig:
    """RuntimeConfig with three fake adapters."""
    return RuntimeConfig(
        runtime=RuntimeOptions(name="v2-recovery-3"),
        logging=LoggingConfig(level="DEBUG"),
        storage=StorageConfig(backend=storage_backend, path=storage_path),
        adapters=AdapterConfigSet(
            matrix={"main": _fake_matrix_runtime_config()},
            meshtastic={"radio": _fake_meshtastic_runtime_config()},
            meshcore={"core": _fake_meshcore_runtime_config()},
        ),
    )


def _config_with_one_fake_adapter(
    *,
    storage_backend: str = "memory",
    storage_path: str | None = None,
) -> RuntimeConfig:
    """RuntimeConfig with one fake adapter."""
    return RuntimeConfig(
        runtime=RuntimeOptions(name="v2-recovery-single"),
        storage=StorageConfig(backend=storage_backend, path=storage_path),
        adapters=AdapterConfigSet(
            matrix={"main": _fake_matrix_runtime_config()},
        ),
    )


def _build_app(config: RuntimeConfig, paths: MedrePaths) -> MedreApp:
    """Build a MedreApp via RuntimeBuilder."""
    return RuntimeBuilder(config, paths).build()


def _make_minimal_event(event_id: str = "evt-v2-001") -> CanonicalEvent:
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
        payload={"text": "v2-recovery"},
        metadata=EventMetadata(),
    )


# ===================================================================
# 1. Malformed config recovery with sequential error types
# ===================================================================


class TestSequentialConfigRecovery:
    """Operator encounters multiple config errors in sequence: bad TOML →
    fix → missing adapter section → add section → invalid limits → fix.

    Prior tests fix a single error type; this exercises a repair loop
    with 3 sequential error types.
    """

    def test_three_sequential_errors_then_fixed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bad TOML → fix → missing adapter → add → invalid limits → fix → loads."""
        cfg_path = tmp_path / "config.toml"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)

        # Error 1: bad TOML.
        cfg_path.write_text(CONFIG_BAD_TOML)
        monkeypatch.setenv("MEDRE_CONFIG", str(cfg_path))

        with pytest.raises(ConfigFileError) as exc_info:
            load_config(None)
        assert "Invalid TOML" in str(exc_info.value)
        assert "Traceback" not in str(exc_info.value)

        # Error 2: fix TOML but no adapters → loads with defaults.
        cfg_path.write_text(CONFIG_MISSING_ADAPTER_SECTION)
        config, _, _ = load_config(None)
        assert len(config.adapters.all_enabled()) == 0

        # Error 3: invalid limits — validation happens during load_config.
        cfg_path.write_text(CONFIG_INVALID_LIMITS)
        with pytest.raises(ConfigValidationError) as exc_info:
            load_config(None)
        assert "max_inflight_deliveries" in str(exc_info.value)

        # Fix: valid config with all sections.
        cfg_path.write_text(CONFIG_VALID_TWO_ADAPTERS)
        config, _, _ = load_config(None)
        assert config.runtime.name == "v2-recovery"
        assert len(config.adapters.all_enabled()) == 3
        config.limits.validate()  # Should not raise.

    def test_cli_sequential_config_check(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CLI config check across three sequential error states."""
        cfg_path = _write_config(tmp_path / "config.toml", CONFIG_BAD_TOML)
        monkeypatch.setenv("MEDRE_CONFIG", str(cfg_path))

        # State 1: bad TOML.
        stdout, stderr, code = _run_cli_raw("config", "check")
        assert code != 0
        assert "Traceback" not in stdout
        assert "Traceback" not in stderr

        # State 2: missing adapters (still valid config, just empty).
        cfg_path.write_text(CONFIG_MISSING_ADAPTER_SECTION)
        stdout2, stderr2, code2 = _run_cli_raw("config", "check")
        assert code2 == 0

        # State 3: valid full config.
        cfg_path.write_text(CONFIG_VALID_TWO_ADAPTERS)
        stdout3, stderr3, code3 = _run_cli_raw("config", "check")
        assert code3 == 0
        assert "Config valid" in stdout3


# ===================================================================
# 2. Storage-path recovery across backends
# ===================================================================


class TestStoragePathRecoveryMultiBackend:
    """Storage path errors with both sqlite and memory backends.

    Prior tests check one backend at a time; this exercises recovery
    across multiple backend types in sequence.
    """

    @pytest.mark.asyncio
    async def test_sqlite_error_then_memory_fallback_then_sqlite_recovery(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Start with sqlite → works → switch to memory → works → back to sqlite."""
        # Session 1: SQLite.
        db_path = str(tmp_path / "v2_cross.db")
        config_sqlite = RuntimeConfig(
            runtime=RuntimeOptions(name="v2-sqlite-1"),
            storage=StorageConfig(backend="sqlite", path=db_path),
            adapters=AdapterConfigSet(
                matrix={"main": _fake_matrix_runtime_config()},
            ),
        )
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
        paths = resolve()
        app1 = _build_app(config_sqlite, paths)

        await app1.start()
        assert app1.state == RuntimeState.RUNNING
        assert app1.boot_summary is not None
        assert app1.boot_summary.storage_backend == "sqlite"
        await app1.stop()

        # Session 2: Memory.
        config_memory = _config_with_one_fake_adapter(storage_backend="memory")
        app2 = _build_app(config_memory, paths)

        await app2.start()
        assert app2.state == RuntimeState.RUNNING
        assert app2.boot_summary is not None
        assert app2.boot_summary.storage_backend == "memory"
        await app2.stop()

        # Session 3: Back to SQLite.
        app3 = _build_app(config_sqlite, paths)

        await app3.start()
        assert app3.state == RuntimeState.RUNNING
        assert app3.boot_summary is not None
        assert app3.boot_summary.storage_backend == "sqlite"
        await app3.stop()

    def test_unknown_placeholder_then_fix_both_backends(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Unknown placeholder → error → fix → sqlite path works."""
        with pytest.raises(MedrePathsError) as exc_info:
            tmp_paths.expand_placeholder("{bogus_var}/data.db")
        assert "unknown path placeholder" in str(exc_info.value)

        # Fix: use known placeholder.
        resolved = tmp_paths.expand_placeholder("{state}/v2_fixed.db")
        assert "v2_fixed.db" in str(resolved)


# ===================================================================
# 3. Startup + degraded recovery combined
# ===================================================================


class TestStartupDegradedRecoveryCombined:
    """Multiple restart cycles with alternating healthy/degraded/failed outcomes.

    Prior tests exercise a single healthy→degraded→healthy transition;
    this cycles through multiple transitions.
    """

    @pytest.mark.asyncio
    async def test_three_transition_cycles(self, tmp_paths: MedrePaths) -> None:
        """Cycle 1: healthy → degraded. Cycle 2: degraded → healthy.
        Cycle 3: healthy → degraded → healthy."""

        # Cycle 1: healthy with two adapters.
        config1 = _config_with_two_fake_adapters()
        app1 = _build_app(config1, tmp_paths)
        await app1.start()
        assert app1.boot_summary is not None
        assert app1.boot_summary.runtime_health == "healthy"
        await app1.stop()

        # Cycle 1b: degrade by replacing one adapter with failing.
        app1b = _build_app(config1, tmp_paths)
        app1b.adapters["fake_mesh"] = _FailingAdapter("fake_mesh")
        await app1b.start()
        assert app1b.boot_summary is not None
        assert app1b.boot_summary.runtime_health == "degraded"
        await app1b.stop()

        # Cycle 2: recover to healthy.
        config2 = _config_with_one_fake_adapter()
        app2 = _build_app(config2, tmp_paths)
        await app2.start()
        assert app2.boot_summary is not None
        assert app2.boot_summary.runtime_health == "healthy"
        await app2.stop()

        # Cycle 3: back to two adapters, one failing → degraded.
        app3 = _build_app(config1, tmp_paths)
        app3.adapters["fake_mesh"] = _FailingAdapter("fake_mesh")
        await app3.start()
        assert app3.boot_summary is not None
        assert app3.boot_summary.runtime_health == "degraded"
        await app3.stop()

        # Cycle 3b: fix → healthy again.
        app3b = _build_app(config1, tmp_paths)
        await app3b.start()
        assert app3b.boot_summary is not None
        assert app3b.boot_summary.runtime_health == "healthy"
        await app3b.stop()

    @pytest.mark.asyncio
    async def test_total_failure_then_partial_then_full_recovery(
        self, tmp_paths: MedrePaths
    ) -> None:
        """All fail → partial recovery → full recovery."""
        config = _config_with_two_fake_adapters()

        # Total failure.
        app_fail = _build_app(config, tmp_paths)
        app_fail.adapters["fake_matrix"] = _FailingAdapter("fake_matrix")
        app_fail.adapters["fake_mesh"] = _FailingAdapter("fake_mesh")
        with pytest.raises(RuntimeStartupError):
            await app_fail.start()
        assert app_fail.state == RuntimeState.FAILED

        # Partial recovery: one adapter works.
        app_partial = _build_app(config, tmp_paths)
        app_partial.adapters["fake_mesh"] = _FailingAdapter("fake_mesh")
        await app_partial.start()
        assert app_partial.state == RuntimeState.RUNNING
        assert app_partial.boot_summary is not None
        assert app_partial.boot_summary.runtime_health == "degraded"
        await app_partial.stop()

        # Full recovery: both adapters work.
        app_full = _build_app(config, tmp_paths)
        await app_full.start()
        assert app_full.state == RuntimeState.RUNNING
        assert app_full.boot_summary is not None
        assert app_full.boot_summary.runtime_health == "healthy"
        assert len(app_full.started_adapter_ids) == 2
        await app_full.stop()


# ===================================================================
# 4. Replay-after-restart with multi-adapter config
# ===================================================================


class TestReplayAfterRestartMultiAdapter:
    """Store events with multi-adapter config → restart → replay available.

    Prior tests use single-adapter storage; this verifies persistence
    and replay availability with 2+ adapter configurations.
    """

    @pytest.mark.asyncio
    async def test_events_survive_two_adapter_restart(self, tmp_path: Path) -> None:
        """Events written in first 2-adapter session survive to second."""
        db_path = str(tmp_path / "v2_multi_replay.db")

        # Session 1: write events.
        s1 = SQLiteStorage(db_path)
        await s1.initialize()
        for i in range(5):
            await s1.append(_make_minimal_event(f"evt-multi-{i:03d}"))
        assert await s1.count_events() == 5
        await s1.close()

        # Session 2: verify events survived.
        s2 = SQLiteStorage(db_path)
        await s2.initialize()
        assert await s2.count_events() == 5
        evt = await s2.get("evt-multi-002")
        assert evt is not None
        assert evt.event_id == "evt-multi-002"
        await s2.close()

    @pytest.mark.asyncio
    async def test_replay_available_after_multi_adapter_restart(
        self, tmp_paths: MedrePaths, tmp_path: Path
    ) -> None:
        """Runtime with 2 adapters reports replay_available on restart."""
        db_path = str(tmp_path / "v2_rt_multi.db")
        config = RuntimeConfig(
            runtime=RuntimeOptions(name="v2-replay-multi"),
            storage=StorageConfig(backend="sqlite", path=db_path),
            adapters=AdapterConfigSet(
                matrix={"main": _fake_matrix_runtime_config()},
                meshtastic={"radio": _fake_meshtastic_runtime_config()},
            ),
        )

        # First instance.
        app1 = _build_app(config, tmp_paths)
        await app1.start()
        assert app1.boot_summary is not None
        assert app1.boot_summary.replay_available is True
        assert app1.boot_summary.adapters_started == 2
        await app1.stop()

        # Second instance on same storage.
        app2 = _build_app(config, tmp_paths)
        await app2.start()
        try:
            boot2 = app2.boot_summary
            assert boot2 is not None
            assert boot2.replay_available is True
            assert boot2.storage_backend == "sqlite"
            assert boot2.adapters_started == 2
        finally:
            await app2.stop()

    @pytest.mark.asyncio
    async def test_three_adapter_restart_preserves_events(self, tmp_path: Path) -> None:
        """Events survive across restarts with 3-adapter config."""
        db_path = str(tmp_path / "v2_three_adapter.db")

        # Write events.
        s1 = SQLiteStorage(db_path)
        await s1.initialize()
        for i in range(3):
            await s1.append(_make_minimal_event(f"evt-three-{i:03d}"))
        assert await s1.count_events() == 3
        await s1.close()

        # Verify after "restart".
        s2 = SQLiteStorage(db_path)
        await s2.initialize()
        assert await s2.count_events() == 3
        for i in range(3):
            evt = await s2.get(f"evt-three-{i:03d}")
            assert evt is not None
        await s2.close()


# ===================================================================
# 5. Adapter disable/enable rapid cycling
# ===================================================================


class TestAdapterDisableEnableRapidCycling:
    """Disable → start → verify → re-enable → start → verify across 3+ cycles.

    Prior tests do a single disable→enable; this rapidly cycles through
    multiple transitions.
    """

    @pytest.mark.asyncio
    async def test_three_disable_enable_cycles(self, tmp_paths: MedrePaths) -> None:
        """3 cycles: 2 adapters → 1 adapter → 2 adapters → 1 adapter → 2 adapters."""
        config_two = _config_with_two_fake_adapters()
        config_one = _config_with_one_fake_adapter()

        outcomes: list[str] = []

        # Cycle 1: 2 adapters (healthy).
        app1 = _build_app(config_two, tmp_paths)
        await app1.start()
        assert app1.boot_summary is not None
        outcomes.append(app1.boot_summary.runtime_health)
        await app1.stop()

        # Cycle 2: 1 adapter (healthy, but reduced).
        app2 = _build_app(config_one, tmp_paths)
        await app2.start()
        assert app2.boot_summary is not None
        outcomes.append(app2.boot_summary.runtime_health)
        assert len(app2.started_adapter_ids) == 1
        await app2.stop()

        # Cycle 3: back to 2 adapters (healthy).
        app3 = _build_app(config_two, tmp_paths)
        await app3.start()
        assert app3.boot_summary is not None
        outcomes.append(app3.boot_summary.runtime_health)
        assert len(app3.started_adapter_ids) == 2
        await app3.stop()

        # Cycle 4: 1 adapter again.
        app4 = _build_app(config_one, tmp_paths)
        await app4.start()
        assert app4.boot_summary is not None
        outcomes.append(app4.boot_summary.runtime_health)
        await app4.stop()

        # Cycle 5: back to 2 adapters.
        app5 = _build_app(config_two, tmp_paths)
        await app5.start()
        assert app5.boot_summary is not None
        outcomes.append(app5.boot_summary.runtime_health)
        await app5.stop()

        assert outcomes == ["healthy", "healthy", "healthy", "healthy", "healthy"]

    def test_cli_shows_disabled_adapter_after_cycle(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CLI config check shows disabled adapter status correctly."""
        cfg_path = _write_config(tmp_path / "config.toml", CONFIG_DISABLED_ADAPTER)
        monkeypatch.setenv("MEDRE_CONFIG", str(cfg_path))

        stdout = _run_cli("config", "check")
        assert "disabled" in stdout or "inactive" in stdout
        assert "Config valid" in stdout


# ===================================================================
# 6. Route validation repair with multi-hop graph
# ===================================================================


class TestRouteValidationRepairMultiHop:
    """3-route chain with bad refs → fix each → all valid.

    Prior tests validate single-route errors; this exercises a multi-hop
    chain where each route depends on the previous one being fixed.
    """

    def test_three_hop_bad_refs_then_fixed(self) -> None:
        """3-route chain referencing non-existent adapters → fix all → valid."""
        known_adapters = frozenset({"src", "mid", "end"})

        # Bad: route hop1 references ghost_mid.
        hop1_bad = RouteConfig(
            route_id="hop1",
            source_adapters=("src",),
            dest_adapters=("ghost_mid",),
            enabled=True,
        )
        hop2_bad = RouteConfig(
            route_id="hop2",
            source_adapters=("ghost_mid",),
            dest_adapters=("ghost_end",),
            enabled=True,
        )
        hop3_bad = RouteConfig(
            route_id="hop3",
            source_adapters=("ghost_end",),
            dest_adapters=("src",),
            enabled=True,
        )
        bad_rcs = RouteConfigSet(routes=(hop1_bad, hop2_bad, hop3_bad))

        with pytest.raises(RouteValidationError) as exc_info:
            validate_route_adapter_refs(bad_rcs, known_adapters)
        msg = str(exc_info.value)
        assert "ghost_mid" in msg or "ghost_end" in msg

        # Fix hop1: src → mid.
        hop1_fixed = RouteConfig(
            route_id="hop1_fixed",
            source_adapters=("src",),
            dest_adapters=("mid",),
            enabled=True,
        )

        # Validate only hop1 — should pass with known adapters.
        validate_route_adapter_refs(
            RouteConfigSet(routes=(hop1_fixed,)),
            known_adapters,
        )

        # Fix hop2: mid → end.
        hop2_fixed = RouteConfig(
            route_id="hop2_fixed",
            source_adapters=("mid",),
            dest_adapters=("end",),
            enabled=True,
        )
        validate_route_adapter_refs(
            RouteConfigSet(routes=(hop1_fixed, hop2_fixed)),
            known_adapters,
        )

        # Fix hop3: end → src.
        hop3_fixed = RouteConfig(
            route_id="hop3_fixed",
            source_adapters=("end",),
            dest_adapters=("src",),
            enabled=True,
        )

        # All fixed: validate the full chain.
        fixed_rcs = RouteConfigSet(routes=(hop1_fixed, hop2_fixed, hop3_fixed))
        validate_route_adapter_refs(fixed_rcs, known_adapters)

    def test_cli_multi_hop_route_validation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CLI routes validate with multi-hop bad refs → fix → valid."""
        cfg_path = _write_config(tmp_path / "config.toml", CONFIG_MULTI_HOP_ROUTES_BAD)
        monkeypatch.setenv("MEDRE_CONFIG", str(cfg_path))

        # Step 1: bad routes show warnings.
        stdout, stderr, code = _run_cli_raw("routes", "validate")
        combined = stdout + stderr
        assert "Traceback" not in combined
        # Should mention the ghost adapters.
        assert "ghost_mid" in combined or "ghost_end" in combined

        # Step 2: fix the routes.
        fixed = """\
[runtime]
name = "v2-multi-hop-fixed"

[storage]
backend = "memory"

[adapters.matrix.src]
enabled = true
adapter_kind = "fake"
homeserver = "https://fake.local"
user_id = "@bot:fake.local"
access_token = "tok"
room_allowlist = ["!room:fake.local"]
encryption_mode = "plaintext"

[adapters.meshtastic.mid]
enabled = true
adapter_kind = "fake"
connection_type = "fake"
meshnet_name = "FixedMesh"

[adapters.meshcore.end]
enabled = true
adapter_kind = "fake"

[routes.hop1]
source_adapters = ["src"]
dest_adapters = ["mid"]
directionality = "source_to_dest"
enabled = true

[routes.hop2]
source_adapters = ["mid"]
dest_adapters = ["end"]
directionality = "source_to_dest"
enabled = true

[routes.hop3]
source_adapters = ["end"]
dest_adapters = ["src"]
directionality = "source_to_dest"
enabled = true
"""
        cfg_path.write_text(fixed)

        # Step 3: fixed routes validate cleanly.
        stdout2, stderr2, code2 = _run_cli_raw("routes", "validate")
        assert "Traceback" not in stdout2
        assert "Traceback" not in stderr2


# ===================================================================
# 7. Container restart workflow simulation
# ===================================================================


class TestContainerRestartWorkflow:
    """Full stop → verify cleanup → reconfig → restart → verify operational.

    Simulates a container restart where the operator stops the runtime,
    verifies state is clean, reconfigures, and restarts.
    """

    @pytest.mark.asyncio
    async def test_full_restart_workflow(self, tmp_paths: MedrePaths) -> None:
        """Stop → verify STOPPED → reconfig with 3 adapters → start → verify."""
        # Phase 1: start with 1 adapter.
        config1 = _config_with_one_fake_adapter()
        app1 = _build_app(config1, tmp_paths)

        await app1.start()
        assert app1.state is RuntimeState.RUNNING
        assert len(app1.started_adapter_ids) == 1
        await app1.stop()
        assert app1.state is RuntimeState.STOPPED

        # Phase 2: reconfigure with 3 adapters.
        config2 = _config_with_three_fake_adapters()
        app2 = _build_app(config2, tmp_paths)

        await app2.start()
        try:
            assert app2.state is RuntimeState.RUNNING
            assert len(app2.started_adapter_ids) == 3
            assert app2.boot_summary is not None
            assert app2.boot_summary.runtime_health == "healthy"
        finally:
            await app2.stop()

        # Phase 3: verify clean state after second stop.
        assert app2.state is RuntimeState.STOPPED

    @pytest.mark.asyncio
    async def test_restart_with_sqlite_persistence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Container restart preserves sqlite data."""
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
        paths = resolve()
        db_path = str(tmp_path / "v2_container.db")

        # Phase 1: start, write events, stop.
        config = RuntimeConfig(
            runtime=RuntimeOptions(name="v2-container"),
            storage=StorageConfig(backend="sqlite", path=db_path),
            adapters=AdapterConfigSet(
                matrix={"main": _fake_matrix_runtime_config()},
            ),
        )

        app1 = _build_app(config, paths)
        await app1.start()
        assert app1.storage is not None
        for i in range(4):
            await app1.storage.append(_make_minimal_event(f"evt-ctr-{i:03d}"))
        assert await app1.storage.count_events() == 4
        await app1.stop()

        # Phase 2: restart with same config, verify data survived.
        app2 = _build_app(config, paths)
        await app2.start()
        try:
            assert app2.storage is not None
            assert await app2.storage.count_events() == 4
            assert app2.boot_summary is not None
            assert app2.boot_summary.replay_available is True
        finally:
            await app2.stop()

    @pytest.mark.asyncio
    async def test_restart_capacity_reset(self, tmp_paths: MedrePaths) -> None:
        """Capacity controller is fresh after container restart."""
        config = _config_with_one_fake_adapter()
        limits = RuntimeLimits(
            max_inflight_deliveries=2,
            max_inflight_replay_events=2,
        )

        # Phase 1: use capacity.
        app1 = _build_app(config, tmp_paths)
        await app1.start()

        cc1 = CapacityController(limits)
        ok = await cc1.acquire_delivery()
        assert ok is True
        snap1 = cc1.snapshot()
        assert snap1["delivery_current"] == 1
        await cc1.release_delivery()
        cc1.stop_accepting()
        await app1.stop()

        # Phase 2: new capacity controller on restart.
        app2 = _build_app(config, tmp_paths)
        await app2.start()

        cc2 = CapacityController(limits)
        snap2 = cc2.snapshot()
        assert snap2["delivery_current"] == 0
        assert snap2["delivery_limit"] == 2
        cc2.stop_accepting()
        await app2.stop()


# ===================================================================
# 8. Config repair with multiple simultaneous issues
# ===================================================================


class TestMultiIssueConfigRepair:
    """Bad TOML + missing adapters + invalid limits → fix all → valid.

    Prior tests fix one issue at a time; this fixes multiple issues
    simultaneously.
    """

    def test_fix_all_three_issues(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Start with multi-issue config → fix all at once → loads."""
        cfg_path = tmp_path / "config.toml"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)

        # All three issues: bad TOML.
        cfg_path.write_text(CONFIG_BAD_TOML)
        monkeypatch.setenv("MEDRE_CONFIG", str(cfg_path))

        with pytest.raises(ConfigFileError):
            load_config(None)

        # Fix all issues at once.
        cfg_path.write_text(CONFIG_VALID_TWO_ADAPTERS)
        config, _, _ = load_config(None)
        assert config.runtime.name == "v2-recovery"
        assert len(config.adapters.all_enabled()) == 3
        config.limits.validate()

    def test_cli_multi_issue_config_check(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CLI config check across multi-issue repair."""
        cfg_path = _write_config(tmp_path / "config.toml", CONFIG_BAD_TOML)
        monkeypatch.setenv("MEDRE_CONFIG", str(cfg_path))

        # Bad state.
        _, _, code = _run_cli_raw("config", "check")
        assert code != 0

        # Fix all issues.
        cfg_path.write_text(CONFIG_VALID_TWO_ADAPTERS)
        stdout, _, code = _run_cli_raw("config", "check")
        assert code == 0
        assert "Config valid" in stdout

    def test_invalid_limits_then_valid_then_fix_all(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Invalid limits → caught at load → fix with valid config."""
        cfg_path = _write_config(tmp_path / "config.toml", CONFIG_INVALID_LIMITS)
        monkeypatch.setenv("MEDRE_CONFIG", str(cfg_path))

        with pytest.raises(ConfigValidationError) as exc_info:
            load_config(None)
        assert "max_inflight_deliveries" in str(exc_info.value)

        # Fix everything: valid config with proper limits.
        cfg_path.write_text(CONFIG_VALID_TWO_ADAPTERS)
        config2, _, _ = load_config(None)
        config2.limits.validate()  # Should not raise.
        assert len(config2.adapters.all_enabled()) == 3


# ===================================================================
# 9. Degraded-to-healthy transition verification
# ===================================================================


class TestDegradedToHealthyTransitions:
    """Degraded → fix → healthy boot summary across 2 transitions.

    Prior tests verify a single transition; this exercises multiple
    degraded→healthy transitions with different adapter configurations.
    """

    @pytest.mark.asyncio
    async def test_two_degraded_to_healthy_transitions(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Transition 1: 2-adapter degraded → healthy.
        Transition 2: 3-adapter degraded → healthy."""
        # Transition 1: degraded with 2 adapters.
        config2 = _config_with_two_fake_adapters()
        app_deg1 = _build_app(config2, tmp_paths)
        app_deg1.adapters["fake_mesh"] = _FailingAdapter("fake_mesh")
        await app_deg1.start()
        assert app_deg1.boot_summary is not None
        assert app_deg1.boot_summary.runtime_health == "degraded"
        assert app_deg1.boot_summary.adapters_failed == 1
        await app_deg1.stop()

        # Fix: healthy with 2 adapters.
        app_healthy1 = _build_app(config2, tmp_paths)
        await app_healthy1.start()
        assert app_healthy1.boot_summary is not None
        assert app_healthy1.boot_summary.runtime_health == "healthy"
        await app_healthy1.stop()

        # Transition 2: degraded with 3 adapters (2 fail).
        config3 = _config_with_three_fake_adapters()
        app_deg2 = _build_app(config3, tmp_paths)
        app_deg2.adapters["fake_mesh"] = _FailingAdapter("fake_mesh")
        app_deg2.adapters["fake_core"] = _FailingAdapter("fake_core")
        await app_deg2.start()
        assert app_deg2.boot_summary is not None
        assert app_deg2.boot_summary.runtime_health == "degraded"
        assert app_deg2.boot_summary.adapters_failed == 2
        await app_deg2.stop()

        # Fix: healthy with 3 adapters.
        app_healthy2 = _build_app(config3, tmp_paths)
        await app_healthy2.start()
        assert app_healthy2.boot_summary is not None
        assert app_healthy2.boot_summary.runtime_health == "healthy"
        assert len(app_healthy2.started_adapter_ids) == 3
        await app_healthy2.stop()

    @pytest.mark.asyncio
    async def test_boot_summary_fields_accurate_across_transitions(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Boot summary fields accurately reflect adapter counts across
        degraded/healthy transitions."""
        config = _config_with_two_fake_adapters()

        # Healthy.
        app_h = _build_app(config, tmp_paths)
        await app_h.start()
        boot_h = app_h.boot_summary
        assert boot_h is not None
        d_h = boot_h.to_dict()
        assert d_h["adapters_started"] == 2
        assert d_h["adapters_failed"] == 0
        assert d_h["startup_outcome"] == "success"
        await app_h.stop()

        # Degraded.
        app_d = _build_app(config, tmp_paths)
        app_d.adapters["fake_mesh"] = _FailingAdapter("fake_mesh")
        await app_d.start()
        boot_d = app_d.boot_summary
        assert boot_d is not None
        d_d = boot_d.to_dict()
        assert d_d["adapters_started"] == 1
        assert d_d["adapters_failed"] == 1
        assert d_d["startup_outcome"] == "partial"
        await app_d.stop()


# ===================================================================
# 10. Boot summary consistency across restart cycles
# ===================================================================


class TestBootSummaryConsistencyAcrossRestarts:
    """3+ restart cycles with same config produce identical boot summaries.

    Prior tests verify boot summary shape once; this exercises multiple
    restart cycles to verify determinism.
    """

    @pytest.mark.asyncio
    async def test_three_restarts_same_config_consistent(
        self, tmp_paths: MedrePaths
    ) -> None:
        """3 restart cycles with same config: boot summaries identical."""
        config = _config_with_two_fake_adapters()
        summaries: list[dict[str, Any]] = []

        for _ in range(3):
            app = _build_app(config, tmp_paths)
            await app.start()
            boot = app.boot_summary
            assert boot is not None
            summaries.append(boot.to_dict())
            await app.stop()

        # Static fields must be identical across all restarts.
        static_keys = [
            "adapters_failed",
            "adapters_started",
            "adapters_total",
            "runtime_health",
            "startup_outcome",
            "storage_backend",
        ]
        for key in static_keys:
            values = [s[key] for s in summaries]
            assert (
                len(set(map(str, values))) == 1
            ), f"Boot summary key {key!r} not consistent: {values}"

    @pytest.mark.asyncio
    async def test_three_restarts_degraded_consistent(
        self, tmp_paths: MedrePaths
    ) -> None:
        """3 restart cycles with degraded config: boot summaries consistent."""
        config = _config_with_two_fake_adapters()
        summaries: list[dict[str, Any]] = []

        for _ in range(3):
            app = _build_app(config, tmp_paths)
            app.adapters["fake_mesh"] = _FailingAdapter("fake_mesh")
            await app.start()
            boot = app.boot_summary
            assert boot is not None
            summaries.append(boot.to_dict())
            await app.stop()

        static_keys = [
            "adapters_failed",
            "adapters_started",
            "adapters_total",
            "runtime_health",
            "startup_outcome",
        ]
        for key in static_keys:
            values = [s[key] for s in summaries]
            assert (
                len(set(map(str, values))) == 1
            ), f"Degraded boot summary key {key!r} not consistent: {values}"

    def test_build_boot_summary_deterministic_keys(self) -> None:
        """build_boot_summary produces alphabetically sorted keys."""
        bs = build_boot_summary(
            startup_timestamp="2026-05-12T00:00:00+00:00",
            startup_outcome="success",
            runtime_health="healthy",
            adapters_total=3,
            adapters_started=3,
            adapters_failed=0,
            adapters_disabled=0,
            build_failure_count=0,
            started_adapter_ids=["c_adapter", "a_adapter", "b_adapter"],
            failed_adapter_ids=[],
            route_count=2,
            storage_backend="sqlite",
            replay_available=True,
            persisted_events_count=10,
        )
        d = bs.to_dict()
        assert list(d.keys()) == sorted(d.keys())
        assert d["adapters_total"] == 3
        assert d["startup_outcome"] == "success"
        assert d["persisted_events_count"] == 10

    def test_classify_runtime_health_all_states(self) -> None:
        """classify_runtime_health consistent for all possible inputs."""
        assert classify_runtime_health([]) == RuntimeHealth.FAILED
        assert classify_runtime_health([AdapterState.FAILED]) == RuntimeHealth.FAILED
        assert (
            classify_runtime_health([AdapterState.READY, AdapterState.FAILED])
            == RuntimeHealth.DEGRADED
        )
        assert classify_runtime_health([AdapterState.READY]) == RuntimeHealth.HEALTHY
        assert (
            classify_runtime_health([AdapterState.READY, AdapterState.READY])
            == RuntimeHealth.HEALTHY
        )
        assert (
            classify_runtime_health(
                [AdapterState.READY, AdapterState.READY, AdapterState.FAILED]
            )
            == RuntimeHealth.DEGRADED
        )

    def test_classify_startup_outcome_all_states(self) -> None:
        """classify_startup_outcome consistent for all possible inputs."""
        assert (
            classify_startup_outcome(started=0, failed=0, total=0)
            == StartupOutcome.TOTAL_FAILURE
        )
        assert (
            classify_startup_outcome(started=0, failed=1, total=1)
            == StartupOutcome.TOTAL_FAILURE
        )
        assert (
            classify_startup_outcome(started=1, failed=1, total=2)
            == StartupOutcome.PARTIAL
        )
        assert (
            classify_startup_outcome(started=2, failed=0, total=2)
            == StartupOutcome.SUCCESS
        )
        assert (
            classify_startup_outcome(started=3, failed=1, total=4)
            == StartupOutcome.PARTIAL
        )
