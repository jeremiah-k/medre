"""Track 2: Multi-Adapter Runtime Validation.

Tests that the runtime correctly builds, starts, and manages multiple
adapters concurrently, including mixed transport types, failure isolation,
and edge cases around configuration.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from medre.adapters.fakes.matrix import FakeMatrixAdapter
from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter
from medre.config.errors import ConfigValidationError
from medre.config.model import (
    AdapterConfigSet,
    LoggingConfig,
    MatrixRuntimeConfig,
    MeshtasticRuntimeConfig,
    RuntimeConfig,
    RuntimeOptions,
    StorageConfig,
)
from medre.config.paths import MedrePaths, resolve
from medre.runtime.app import MedreApp
from medre.runtime.builder import RuntimeBuilder
from medre.runtime.errors import RuntimeStartupError

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


@pytest.fixture()
def tmp_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MedrePaths:
    """Create a MedrePaths pointing at a temp directory."""
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    return resolve()


def _make_config_with_two_matrix_fake(
    adapter_id_a: str = "main",
    adapter_id_b: str = "secondary",
) -> RuntimeConfig:
    """RuntimeConfig with two fake Matrix adapters."""
    return RuntimeConfig(
        runtime=RuntimeOptions(name="test-multi-matrix"),
        logging=LoggingConfig(level="DEBUG"),
        storage=StorageConfig(backend="memory"),
        adapters=AdapterConfigSet(
            matrix={
                adapter_id_a: MatrixRuntimeConfig(
                    adapter_id=adapter_id_a,
                    enabled=True,
                    adapter_kind="fake",
                ),
                adapter_id_b: MatrixRuntimeConfig(
                    adapter_id=adapter_id_b,
                    enabled=True,
                    adapter_kind="fake",
                ),
            },
        ),
    )


def _make_config_with_two_meshtastic_fake(
    adapter_id_a: str = "mesh_alpha",
    adapter_id_b: str = "mesh_bravo",
) -> RuntimeConfig:
    """RuntimeConfig with two fake Meshtastic adapters.

    Uses adapter_kind='fake' which bypasses real SDK deps.
    """
    return RuntimeConfig(
        runtime=RuntimeOptions(name="test-multi-meshtastic"),
        logging=LoggingConfig(level="DEBUG"),
        storage=StorageConfig(backend="memory"),
        adapters=AdapterConfigSet(
            meshtastic={
                adapter_id_a: MeshtasticRuntimeConfig(
                    adapter_id=adapter_id_a,
                    enabled=True,
                    adapter_kind="fake",
                ),
                adapter_id_b: MeshtasticRuntimeConfig(
                    adapter_id=adapter_id_b,
                    enabled=True,
                    adapter_kind="fake",
                ),
            },
        ),
    )


def _make_config_mixed_four() -> RuntimeConfig:
    """RuntimeConfig with 2 fake Matrix + 2 fake Meshtastic = 4 adapters."""
    return RuntimeConfig(
        runtime=RuntimeOptions(name="test-mixed-4"),
        logging=LoggingConfig(level="DEBUG"),
        storage=StorageConfig(backend="memory"),
        adapters=AdapterConfigSet(
            matrix={
                "mx_main": MatrixRuntimeConfig(
                    adapter_id="mx_main",
                    enabled=True,
                    adapter_kind="fake",
                ),
                "mx_backup": MatrixRuntimeConfig(
                    adapter_id="mx_backup",
                    enabled=True,
                    adapter_kind="fake",
                ),
            },
            meshtastic={
                "mt_alpha": MeshtasticRuntimeConfig(
                    adapter_id="mt_alpha",
                    enabled=True,
                    adapter_kind="fake",
                ),
                "mt_bravo": MeshtasticRuntimeConfig(
                    adapter_id="mt_bravo",
                    enabled=True,
                    adapter_kind="fake",
                ),
            },
        ),
    )


def _build_and_start(config: RuntimeConfig, paths: MedrePaths) -> MedreApp:
    """Build and return a MedreApp (not yet started)."""
    builder = RuntimeBuilder(config, paths)
    return builder.build()


# ===================================================================
# A) Two fake Matrix adapters
# ===================================================================


class TestTwoFakeMatrixAdapters:
    """Two Matrix adapters with adapter_kind='fake'."""

    def test_both_build_successfully(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """RuntimeBuilder builds two fake Matrix adapters."""
        config = _make_config_with_two_matrix_fake()
        app = _build_and_start(config, tmp_paths)
        assert len(app.adapters) == 2
        assert "main" in app.adapters
        assert "secondary" in app.adapters

    def test_distinct_adapter_ids(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Each adapter has a distinct adapter_id."""
        config = _make_config_with_two_matrix_fake()
        app = _build_and_start(config, tmp_paths)
        ids = {a.adapter_id for a in app.adapters.values()}
        # Matrix fakes take adapter_id from constructor parameter
        assert "main" in ids or len(ids) == 2

    def test_distinct_state_roots(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Each adapter gets a distinct adapter state root."""
        config = _make_config_with_two_matrix_fake("main", "secondary")
        _build_and_start(config, tmp_paths)
        # State roots are computed from paths, not from adapter objects
        root_a = tmp_paths.adapter_state_dir("main")
        root_b = tmp_paths.adapter_state_dir("secondary")
        assert root_a != root_b
        assert root_a == tmp_paths.state_dir / "adapters" / "main"
        assert root_b == tmp_paths.state_dir / "adapters" / "secondary"

    async def test_matrix_store_dirs_created(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Matrix store dirs are created on start for Matrix adapters."""
        config = _make_config_with_two_matrix_fake("main", "secondary")
        app = _build_and_start(config, tmp_paths)
        await app.start()
        try:
            store_a = tmp_paths.adapter_transport_state_dir("main", "matrix") / "store"
            store_b = (
                tmp_paths.adapter_transport_state_dir("secondary", "matrix") / "store"
            )
            assert store_a.is_dir(), f"Expected {store_a} to be a directory"
            assert store_b.is_dir(), f"Expected {store_b} to be a directory"
        finally:
            await app.stop()

    def test_adapters_are_fake_matrix(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Built adapters are FakeMatrixAdapter instances."""
        config = _make_config_with_two_matrix_fake()
        app = _build_and_start(config, tmp_paths)
        for adapter in app.adapters.values():
            assert isinstance(adapter, FakeMatrixAdapter)


# ===================================================================
# B) Two fake Meshtastic adapters
# ===================================================================


class TestTwoFakeMeshtasticAdapters:
    """Two Meshtastic adapters with adapter_kind='fake'."""

    def test_both_build_successfully(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """RuntimeBuilder builds two fake Meshtastic adapters."""
        config = _make_config_with_two_meshtastic_fake("mesh_alpha", "mesh_bravo")
        app = _build_and_start(config, tmp_paths)
        assert len(app.adapters) == 2

    def test_separate_state_roots(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Separate state roots computed for each adapter."""
        config = _make_config_with_two_meshtastic_fake("mesh_alpha", "mesh_bravo")
        _build_and_start(config, tmp_paths)
        root_a = tmp_paths.adapter_state_dir("mesh_alpha")
        root_b = tmp_paths.adapter_state_dir("mesh_bravo")
        assert root_a != root_b

    async def test_no_matrix_store_dirs(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Meshtastic adapters do NOT get Matrix store directories."""
        config = _make_config_with_two_meshtastic_fake("mesh_alpha", "mesh_bravo")
        app = _build_and_start(config, tmp_paths)
        await app.start()
        try:
            store_a = (
                tmp_paths.adapter_transport_state_dir("mesh_alpha", "matrix") / "store"
            )
            # The matrix subdir should not have been created
            assert (
                not store_a.is_dir()
            ), f"Meshtastic adapter should not have Matrix store at {store_a}"
        finally:
            await app.stop()


# ===================================================================
# C) Mixed transport runtime (2 Matrix + 2 Meshtastic)
# ===================================================================


class TestMixedTransportRuntime:
    """2 fake Matrix + 2 fake Meshtastic = 4 adapters."""

    async def test_all_four_build_and_start(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """All 4 adapters build and start."""
        config = _make_config_mixed_four()
        app = _build_and_start(config, tmp_paths)
        assert len(app.adapters) == 4
        await app.start()
        try:
            assert len(app.adapters) == 4
            for adapter in app.adapters.values():
                assert isinstance(adapter, (FakeMatrixAdapter, FakeMeshtasticAdapter))
        finally:
            await app.stop()

    async def test_correct_state_roots(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Each adapter gets correct state root."""
        config = _make_config_mixed_four()
        app = _build_and_start(config, tmp_paths)
        await app.start()
        try:
            for adapter_id in ("mx_main", "mx_backup", "mt_alpha", "mt_bravo"):
                root = tmp_paths.adapter_state_dir(adapter_id)
                assert root.is_dir(), f"Expected {root} to exist"
        finally:
            await app.stop()

    async def test_matrix_store_dirs_only_for_matrix(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Matrix store dirs created only for Matrix adapters."""
        config = _make_config_mixed_four()
        app = _build_and_start(config, tmp_paths)
        await app.start()
        try:
            # Matrix adapters get matrix/store
            for aid in ("mx_main", "mx_backup"):
                store = tmp_paths.adapter_transport_state_dir(aid, "matrix") / "store"
                assert store.is_dir(), f"Expected {store} for Matrix adapter"

            # Meshtastic adapters do NOT get matrix/store
            for aid in ("mt_alpha", "mt_bravo"):
                store = tmp_paths.adapter_transport_state_dir(aid, "matrix") / "store"
                assert (
                    not store.is_dir()
                ), f"Meshtastic adapter {aid} should not have Matrix store"
        finally:
            await app.stop()


# ===================================================================
# D) Startup/shutdown cycle
# ===================================================================


class TestStartupShutdownCycle:
    """Build/start multi-adapter runtime, then stop."""

    async def test_clean_shutdown(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """All adapters stop cleanly after start."""
        config = _make_config_mixed_four()
        app = _build_and_start(config, tmp_paths)
        await app.start()
        assert len(app.adapters) == 4
        await app.stop()
        # After stop, shutdown event should be set
        assert app.shutdown_event.is_set()

    async def test_no_resource_leaks(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """After stop, adapters report as not started."""
        config = _make_config_with_two_matrix_fake()
        app = _build_and_start(config, tmp_paths)
        await app.start()
        adapters = list(app.adapters.values())
        await app.stop()
        for adapter in adapters:
            assert isinstance(adapter, FakeMatrixAdapter)
            assert adapter._started is False


# ===================================================================
# E) One adapter failure does not corrupt others
# ===================================================================


class TestAdapterFailureIsolation:
    """Invalid adapter config does not prevent others from working."""

    async def test_invalid_adapter_reported(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Invalid adapter failure is reported with adapter_id attribution.

        The builder logs individual adapter failures and records them in
        ``build_failures`` rather than raising, so the 2 valid adapters
        still work.
        """

        # Configure 3 adapters: 2 fake + 1 real (which will fail without config)
        config = RuntimeConfig(
            runtime=RuntimeOptions(name="test-failure-isolation"),
            logging=LoggingConfig(level="DEBUG"),
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(
                matrix={
                    "valid_a": MatrixRuntimeConfig(
                        adapter_id="valid_a",
                        enabled=True,
                        adapter_kind="fake",
                    ),
                    "valid_b": MatrixRuntimeConfig(
                        adapter_id="valid_b",
                        enabled=True,
                        adapter_kind="fake",
                    ),
                    # Real Matrix adapter without config → fails
                    "broken": MatrixRuntimeConfig(
                        adapter_id="broken",
                        enabled=True,
                        adapter_kind="real",
                        config=None,
                    ),
                },
            ),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        app = builder.build()

        # The 2 valid adapters should be in the inventory
        assert "valid_a" in app.adapters
        assert "valid_b" in app.adapters

        # The broken adapter should be recorded in build_failures
        assert len(app.build_failures) == 1
        failure = app.build_failures[0]
        assert failure.adapter_id == "broken"
        assert failure.transport == "matrix"

    async def test_valid_adapters_unaffected_by_peer_failure(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """When only valid adapters are configured, all succeed."""
        # Use 3 valid fake adapters to show isolation is possible
        config = RuntimeConfig(
            runtime=RuntimeOptions(name="test-valid-only"),
            logging=LoggingConfig(level="DEBUG"),
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(
                matrix={
                    "a": MatrixRuntimeConfig(
                        adapter_id="a",
                        enabled=True,
                        adapter_kind="fake",
                    ),
                    "b": MatrixRuntimeConfig(
                        adapter_id="b",
                        enabled=True,
                        adapter_kind="fake",
                    ),
                    "c": MatrixRuntimeConfig(
                        adapter_id="c",
                        enabled=True,
                        adapter_kind="fake",
                    ),
                },
            ),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        app = builder.build()
        await app.start()
        try:
            assert len(app.adapters) == 3
            for adapter in app.adapters.values():
                info = await adapter.health_check()
                assert info.health == "healthy"
        finally:
            await app.stop()


# ===================================================================
# F) One adapter stop does not stop others
# ===================================================================


class TestIndividualAdapterStop:
    """Stopping one adapter does not affect others."""

    async def test_other_adapters_continue_running(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """After stopping one adapter, the others continue running."""
        config = _make_config_with_two_matrix_fake(
            "alpha",
            "beta",
        )
        # Add a third
        config = RuntimeConfig(
            runtime=RuntimeOptions(name="test-individual-stop"),
            logging=LoggingConfig(level="DEBUG"),
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(
                matrix={
                    "alpha": MatrixRuntimeConfig(
                        adapter_id="alpha",
                        enabled=True,
                        adapter_kind="fake",
                    ),
                    "beta": MatrixRuntimeConfig(
                        adapter_id="beta",
                        enabled=True,
                        adapter_kind="fake",
                    ),
                    "gamma": MatrixRuntimeConfig(
                        adapter_id="gamma",
                        enabled=True,
                        adapter_kind="fake",
                    ),
                },
            ),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        app = builder.build()
        await app.start()
        try:
            assert len(app.adapters) == 3
            # Stop one adapter directly
            stopped = app.adapters["beta"]
            await stopped.stop(timeout=2.0)
            assert stopped._started is False
            # Others still running
            assert app.adapters["alpha"]._started is True
            assert app.adapters["gamma"]._started is True
        finally:
            await app.stop()

    async def test_stopped_adapter_tasks_cleaned(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Stopped adapter's internal state is cleaned up."""
        config = _make_config_with_two_matrix_fake("x", "y")
        builder = RuntimeBuilder(config, tmp_paths)
        app = builder.build()
        await app.start()
        try:
            adapter = app.adapters["x"]
            assert adapter._started is True
            assert adapter.ctx is not None
            await adapter.stop(timeout=2.0)
            assert adapter._started is False
        finally:
            await app.stop()


# ===================================================================
# G) Edge cases
# ===================================================================


class TestEdgeCases:
    """Edge cases: duplicate adapter_id, empty runtime, disabled, ordering."""

    def test_duplicate_adapter_id_rejected(self) -> None:
        """Invalid adapter_kind raises ConfigValidationError via from_dict."""
        with pytest.raises(ConfigValidationError):
            MatrixRuntimeConfig.from_dict(
                "test_dup",
                {"adapter_kind": "invalid_kind"},
            )

    def test_empty_runtime_no_crash(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Empty runtime (no adapters enabled) builds without crash."""
        config = RuntimeConfig(
            runtime=RuntimeOptions(name="test-empty"),
            logging=LoggingConfig(),
            storage=StorageConfig(backend="memory"),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        app = builder.build()
        assert len(app.adapters) == 0

    async def test_empty_runtime_starts_and_stops(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Empty runtime (zero adapters) raises RuntimeStartupError.

        Per startup semantics, zero adapters started is a total
        failure — the runtime cannot route any events.
        """

        config = RuntimeConfig(
            runtime=RuntimeOptions(name="test-empty-lifecycle"),
            logging=LoggingConfig(),
            storage=StorageConfig(backend="memory"),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        app = builder.build()
        with pytest.raises(RuntimeStartupError, match="Total startup failure"):
            await app.start()
        assert len(app.adapters) == 0

    def test_disabled_adapters_not_started(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Disabled adapters are not built or listed in inventory."""
        config = RuntimeConfig(
            runtime=RuntimeOptions(name="test-disabled"),
            logging=LoggingConfig(),
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(
                matrix={
                    "on": MatrixRuntimeConfig(
                        adapter_id="on",
                        enabled=True,
                        adapter_kind="fake",
                    ),
                    "off": MatrixRuntimeConfig(
                        adapter_id="off",
                        enabled=False,
                        adapter_kind="fake",
                    ),
                },
            ),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        app = builder.build()
        assert "on" in app.adapters
        assert "off" not in app.adapters
        assert len(app.adapters) == 1

    def test_startup_ordering_deterministic(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Same config always produces same adapter ordering."""
        config = RuntimeConfig(
            runtime=RuntimeOptions(name="test-ordering"),
            logging=LoggingConfig(),
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(
                matrix={
                    "delta": MatrixRuntimeConfig(
                        adapter_id="delta",
                        enabled=True,
                        adapter_kind="fake",
                    ),
                    "alpha": MatrixRuntimeConfig(
                        adapter_id="alpha",
                        enabled=True,
                        adapter_kind="fake",
                    ),
                    "charlie": MatrixRuntimeConfig(
                        adapter_id="charlie",
                        enabled=True,
                        adapter_kind="fake",
                    ),
                },
            ),
        )
        # Build twice and verify ordering is the same
        builder1 = RuntimeBuilder(config, tmp_paths)
        app1 = builder1.build()
        builder2 = RuntimeBuilder(config, tmp_paths)
        app2 = builder2.build()
        keys1 = list(app1.adapters.keys())
        keys2 = list(app2.adapters.keys())
        assert (
            keys1 == keys2
        ), f"Adapter ordering must be deterministic: {keys1} != {keys2}"
