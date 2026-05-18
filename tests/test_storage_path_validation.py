"""Track 6: Storage / Path Validation.

Tests that the storage and path model behaves correctly:
multiple Matrix stores are isolated, Meshtastic adapters don't get
Matrix stores, adapter state roots are correct, only one global DB,
and runtime cleanup preserves state directories.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from medre.config.model import (
    AdapterConfigSet,
    LoggingConfig,
    MatrixRuntimeConfig,
    MeshtasticRuntimeConfig,
    RuntimeConfig,
    RuntimeOptions,
    StorageConfig,
)
from medre.config.paths import MedrePaths, MedrePathsError, resolve
from medre.runtime.builder import RuntimeBuilder

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
    """Create a MedrePaths pointing at a temp directory via MEDRE_HOME."""
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    return resolve()


def _make_two_matrix_config() -> RuntimeConfig:
    """Two fake Matrix adapters."""
    return RuntimeConfig(
        runtime=RuntimeOptions(name="test-storage-matrix"),
        logging=LoggingConfig(level="DEBUG"),
        storage=StorageConfig(backend="memory"),
        adapters=AdapterConfigSet(
            matrix={
                "main": MatrixRuntimeConfig(
                    adapter_id="main",
                    enabled=True,
                    adapter_kind="fake",
                ),
                "secondary": MatrixRuntimeConfig(
                    adapter_id="secondary",
                    enabled=True,
                    adapter_kind="fake",
                ),
            },
        ),
    )


def _make_meshtastic_config() -> RuntimeConfig:
    """One fake Meshtastic adapter."""
    return RuntimeConfig(
        runtime=RuntimeOptions(name="test-storage-mesh"),
        logging=LoggingConfig(level="DEBUG"),
        storage=StorageConfig(backend="memory"),
        adapters=AdapterConfigSet(
            meshtastic={
                "radio": MeshtasticRuntimeConfig(
                    adapter_id="radio",
                    enabled=True,
                    adapter_kind="fake",
                ),
            },
        ),
    )


def _make_mixed_config() -> RuntimeConfig:
    """2 Matrix + 1 Meshtastic mixed runtime."""
    return RuntimeConfig(
        runtime=RuntimeOptions(name="test-storage-mixed"),
        logging=LoggingConfig(level="DEBUG"),
        storage=StorageConfig(backend="memory"),
        adapters=AdapterConfigSet(
            matrix={
                "mx_a": MatrixRuntimeConfig(
                    adapter_id="mx_a",
                    enabled=True,
                    adapter_kind="fake",
                ),
                "mx_b": MatrixRuntimeConfig(
                    adapter_id="mx_b",
                    enabled=True,
                    adapter_kind="fake",
                ),
            },
            meshtastic={
                "mt_1": MeshtasticRuntimeConfig(
                    adapter_id="mt_1",
                    enabled=True,
                    adapter_kind="fake",
                ),
            },
        ),
    )


# ===================================================================
# M) Multiple Matrix stores isolated
# ===================================================================


class TestMultipleMatrixStoresIsolated:
    """Two Matrix adapters → two separate store directories."""

    async def test_two_matrix_stores(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Two Matrix adapters get separate matrix/store directories."""
        config = _make_two_matrix_config()
        builder = RuntimeBuilder(config, tmp_paths)
        app = builder.build()
        await app.start()
        try:
            store_main = (
                tmp_paths.adapter_transport_state_dir("main", "matrix") / "store"
            )
            store_secondary = (
                tmp_paths.adapter_transport_state_dir("secondary", "matrix") / "store"
            )

            assert store_main.is_dir(), f"Expected {store_main} to exist"
            assert store_secondary.is_dir(), f"Expected {store_secondary} to exist"
            assert store_main != store_secondary
        finally:
            await app.stop()

    async def test_store_paths_are_under_adapter_roots(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Store paths are correct: {state}/adapters/{id}/matrix/store."""
        config = _make_two_matrix_config()
        builder = RuntimeBuilder(config, tmp_paths)
        app = builder.build()
        await app.start()
        try:
            state = tmp_paths.state_dir
            expected_main = state / "adapters" / "main" / "matrix" / "store"
            expected_secondary = state / "adapters" / "secondary" / "matrix" / "store"

            assert expected_main.is_dir()
            assert expected_secondary.is_dir()
        finally:
            await app.stop()


# ===================================================================
# N) Meshtastic adapters do NOT get Matrix stores
# ===================================================================


class TestMeshtasticNoMatrixStores:
    """Meshtastic adapters must not have matrix/store directories."""

    async def test_no_matrix_store_for_meshtastic(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Meshtastic adapter → no matrix/store directory created."""
        config = _make_meshtastic_config()
        builder = RuntimeBuilder(config, tmp_paths)
        app = builder.build()
        await app.start()
        try:
            matrix_store = (
                tmp_paths.adapter_transport_state_dir("radio", "matrix") / "store"
            )
            assert (
                not matrix_store.exists()
            ), f"Meshtastic adapter should not have Matrix store at {matrix_store}"
        finally:
            await app.stop()

    async def test_mixed_runtime_only_matrix_gets_stores(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """In mixed runtime, only Matrix adapters get matrix/store dirs."""
        config = _make_mixed_config()
        builder = RuntimeBuilder(config, tmp_paths)
        app = builder.build()
        await app.start()
        try:
            # Matrix adapters get matrix/store
            for aid in ("mx_a", "mx_b"):
                store = tmp_paths.adapter_transport_state_dir(aid, "matrix") / "store"
                assert store.is_dir(), f"Matrix adapter {aid} should have matrix/store"

            # Meshtastic does not
            store = tmp_paths.adapter_transport_state_dir("mt_1", "matrix") / "store"
            assert not store.exists(), "Meshtastic adapter should not have matrix/store"
        finally:
            await app.stop()


# ===================================================================
# O) Adapter state roots correct
# ===================================================================


class TestAdapterStateRootsCorrect:
    """Each adapter's state root resolves correctly."""

    def test_adapter_state_dir(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """adapter_state_dir returns {state}/adapters/{adapter_id}."""
        root = tmp_paths.adapter_state_dir("main")
        expected = tmp_paths.state_dir / "adapters" / "main"
        assert root == expected

    def test_adapter_transport_state_dir(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """adapter_transport_state_dir returns {state}/adapters/{id}/{transport}."""
        root = tmp_paths.adapter_transport_state_dir("main", "matrix")
        expected = tmp_paths.state_dir / "adapters" / "main" / "matrix"
        assert root == expected

    def test_adapter_state_dir_rejects_empty(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Empty adapter_id raises MedrePathsError."""
        with pytest.raises(MedrePathsError, match="non-empty"):
            tmp_paths.adapter_state_dir("")

    def test_adapter_state_dir_rejects_separators(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """adapter_id with path separators raises MedrePathsError."""
        with pytest.raises(MedrePathsError, match="path separators"):
            tmp_paths.adapter_state_dir("foo/bar")

    def test_adapter_transport_state_dir_rejects_empty_transport(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Empty transport raises MedrePathsError."""
        with pytest.raises(MedrePathsError, match="non-empty"):
            tmp_paths.adapter_transport_state_dir("main", "")

    def test_adapter_transport_state_dir_rejects_separator_in_transport(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Transport with path separators raises MedrePathsError."""
        with pytest.raises(MedrePathsError, match="path separators"):
            tmp_paths.adapter_transport_state_dir("main", "mat/rix")

    def test_medre_home_mode_paths(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """MEDRE_HOME mode resolves all paths under one root."""
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
        paths = resolve()
        assert paths.config_dir is None  # MEDRE_HOME has no config_dir
        assert paths.config_file == tmp_path / "config.toml"
        assert paths.state_dir == tmp_path / "state"
        assert paths.data_dir == tmp_path / "data"
        assert paths.cache_dir == tmp_path / "cache"
        assert paths.log_dir == tmp_path / "logs"
        assert paths.database_path == tmp_path / "state" / "medre.sqlite"

    def test_xdg_mode_paths(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """XDG mode resolves paths under XDG base directories."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
        monkeypatch.delenv("MEDRE_HOME", raising=False)
        paths = resolve()
        assert paths.config_dir == tmp_path / "config" / "medre"
        assert paths.state_dir == tmp_path / "state" / "medre"
        assert paths.data_dir == tmp_path / "data" / "medre"
        assert paths.cache_dir == tmp_path / "cache" / "medre"
        assert paths.database_path == tmp_path / "state" / "medre" / "medre.sqlite"


# ===================================================================
# P) No per-adapter MEDRE DBs
# ===================================================================


class TestNoPerAdapterMedreDBs:
    """Only one medre.sqlite at {state}/medre.sqlite."""

    async def test_single_db_at_state_root(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Only one medre.sqlite exists, at the global state root."""
        config = _make_mixed_config()
        # Use sqlite backend so the DB gets created
        config = RuntimeConfig(
            runtime=RuntimeOptions(name="test-single-db"),
            logging=LoggingConfig(level="DEBUG"),
            storage=StorageConfig(backend="sqlite"),
            adapters=AdapterConfigSet(
                matrix={
                    "mx_a": MatrixRuntimeConfig(
                        adapter_id="mx_a",
                        enabled=True,
                        adapter_kind="fake",
                    ),
                    "mx_b": MatrixRuntimeConfig(
                        adapter_id="mx_b",
                        enabled=True,
                        adapter_kind="fake",
                    ),
                },
                meshtastic={
                    "mt_1": MeshtasticRuntimeConfig(
                        adapter_id="mt_1",
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
            # The global DB should exist
            db_path = tmp_paths.database_path
            assert db_path.name == "medre.sqlite"
            # After start (with sqlite backend), the DB file should exist
            assert db_path.is_file(), f"Expected {db_path} to exist"
        finally:
            await app.stop()

    async def test_no_adapter_subdir_dbs(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """No medre.sqlite files in adapter subdirectories."""
        config = _make_mixed_config()
        builder = RuntimeBuilder(config, tmp_paths)
        app = builder.build()
        await app.start()
        try:
            adapters_dir = tmp_paths.state_dir / "adapters"
            if adapters_dir.exists():
                for root, _dirs, files in os.walk(adapters_dir):
                    assert (
                        "medre.sqlite" not in files
                    ), f"Found medre.sqlite in adapter subdir: {root}"
        finally:
            await app.stop()


# ===================================================================
# Q) Global DB shared
# ===================================================================


class TestGlobalDBShared:
    """The global DB path is consistent regardless of adapter count."""

    def test_db_path_consistent_one_adapter(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """DB path is the same with one adapter."""
        config = _make_meshtastic_config()
        builder = RuntimeBuilder(config, tmp_paths)
        builder.build()
        expected = tmp_paths.database_path
        assert expected == tmp_paths.state_dir / "medre.sqlite"

    def test_db_path_consistent_three_adapters(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """DB path is the same with three adapters."""
        config = _make_mixed_config()
        builder = RuntimeBuilder(config, tmp_paths)
        builder.build()
        expected = tmp_paths.database_path
        assert expected == tmp_paths.state_dir / "medre.sqlite"

    def test_db_path_constant_across_builds(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Same paths object always returns same database_path."""
        db1 = tmp_paths.database_path
        db2 = tmp_paths.database_path
        assert db1 == db2
        assert db1 == tmp_paths.state_dir / "medre.sqlite"


# ===================================================================
# R) Runtime cleanup preserves state dirs
# ===================================================================


class TestRuntimeCleanupPreservesStateDirs:
    """After runtime stop, state directories persist."""

    async def test_state_dirs_persist_after_stop(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """State directories are NOT cleaned up after stop."""
        config = _make_two_matrix_config()
        builder = RuntimeBuilder(config, tmp_paths)
        app = builder.build()
        await app.start()
        # Verify dirs exist during runtime
        root_main = tmp_paths.adapter_state_dir("main")
        root_secondary = tmp_paths.adapter_state_dir("secondary")
        store_main = tmp_paths.adapter_transport_state_dir("main", "matrix") / "store"
        store_secondary = (
            tmp_paths.adapter_transport_state_dir("secondary", "matrix") / "store"
        )

        assert root_main.is_dir()
        assert root_secondary.is_dir()
        assert store_main.is_dir()
        assert store_secondary.is_dir()

        await app.stop()

        # After stop, dirs should still exist (persistent state)
        assert root_main.is_dir(), "State dir should persist after stop"
        assert root_secondary.is_dir(), "State dir should persist after stop"
        assert store_main.is_dir(), "Matrix store should persist after stop"
        assert store_secondary.is_dir(), "Matrix store should persist after stop"

    async def test_db_persists_after_stop(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Database file persists after runtime stop."""
        config = RuntimeConfig(
            runtime=RuntimeOptions(name="test-db-persist"),
            logging=LoggingConfig(level="DEBUG"),
            storage=StorageConfig(backend="sqlite"),
            adapters=AdapterConfigSet(
                matrix={
                    "mx": MatrixRuntimeConfig(
                        adapter_id="mx",
                        enabled=True,
                        adapter_kind="fake",
                    ),
                },
            ),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        app = builder.build()
        await app.start()
        db_path = tmp_paths.database_path
        assert db_path.is_file()
        await app.stop()
        assert db_path.is_file(), "Database file should persist after stop"

    async def test_global_state_dir_persists(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Global state dir persists after runtime stop."""
        config = _make_mixed_config()
        builder = RuntimeBuilder(config, tmp_paths)
        app = builder.build()
        await app.start()
        await app.stop()
        assert tmp_paths.state_dir.is_dir(), "Global state dir should persist"
        assert tmp_paths.data_dir.is_dir(), "Data dir should persist"
