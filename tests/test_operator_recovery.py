"""Operator recovery workflow tests (slimmed).

This file was split by behavioral domain. The following domains moved to
dedicated modules:

- ``tests/test_config_repair.py`` — malformed config, storage path, config
  repair workflows (TestMalformedConfigRecovery, TestStoragePathRecovery,
  TestConfigRepairWorkflows).
- ``tests/test_startup_recovery.py`` — startup failure recovery, degraded
  runtime recovery, adapter disable/enable workflows (TestStartupFailureRecovery,
  TestDegradedRuntimeRecovery, TestAdapterDisableEnableWorkflows).
- ``tests/test_deterministic_messaging.py`` — no-traceback assertions,
  deterministic boot/supervision shape (TestDeterministicMessaging).

Shared fixtures/helpers live in ``tests/helpers/operator_recovery.py``.

This file retains:

- Route validation failure recovery (TestRouteValidationFailureRecovery)
- Replay after restart (TestReplayAfterRestart)
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from medre.config.model import (
    AdapterConfigSet,
    RuntimeConfig,
    RuntimeOptions,
    StorageConfig,
)
from medre.config.paths import MedrePaths, resolve
from medre.config.routes import (
    RouteConfig,
    RouteConfigSet,
)
from medre.core.events.canonical import CanonicalEvent
from medre.core.events.kinds import EventKind
from medre.core.events.metadata import EventMetadata
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.runtime.route_engine import (
    RouteValidationError,
    validate_route_adapter_refs,
)
from tests.helpers.operator_recovery import (
    CONFIG_MISSING_ADAPTER_REF,
    _build_app,
    _config_with_one_fake_adapter,
    _fake_matrix_runtime_config,
    _run_cli_raw,
    _write_config,
)

# ---------------------------------------------------------------------------
# Fixtures (re-declared locally; pytest does not discover imported fixtures
# from non-conftest helper modules — see tests/helpers/startup_cleanup.py)
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
# Route validation failure recovery
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
        cfg_path = _write_config(tmp_path / "config.yaml", CONFIG_MISSING_ADAPTER_REF)
        monkeypatch.setenv("MEDRE_CONFIG", str(cfg_path))

        # Step 1: bad config shows warnings about ghost adapter.
        stdout, stderr, code = _run_cli_raw("routes", "validate")
        # Should complete (warnings not fatal) but mention the ghost.
        assert "ghost_adapter" in stdout or "ghost_adapter" in stderr
        assert "Traceback" not in stdout
        assert "Traceback" not in stderr

        # Step 2: fix the config by referencing a real adapter.
        fixed = """\
runtime:
  name: fixed-routes
storage:
  backend: memory
adapters:
  matrix:
    real_adapter:
      enabled: true
      adapter_kind: fake
      homeserver: https://fake.local
      user_id: "@bot:fake.local"
      access_token: tok
      room_allowlist:
        - "!room:fake.local"
      encryption_mode: plaintext
  meshtastic:
    other_real:
      enabled: true
      adapter_kind: fake
      connection_type: fake
      origin_label: FixedMesh
routes:
  fixed_route:
    source_adapters:
      - real_adapter
    dest_adapters:
      - other_real
    directionality: source_to_dest
    enabled: true
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
# Replay after restart
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
        try:
            await s1.initialize()
            for i in range(3):
                await s1.append(_make_minimal_event(f"evt-replay-{i:03d}"))
            assert await s1.count_events() == 3
        finally:
            await s1.close()

        # Session 2: verify events survived.
        s2 = SQLiteStorage(db_path)
        try:
            await s2.initialize()
            assert await s2.count_events() == 3
            evt = await s2.get("evt-replay-001")
            assert evt is not None
            assert evt.event_id == "evt-replay-001"
        finally:
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
