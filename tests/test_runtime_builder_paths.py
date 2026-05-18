"""Tests for medre.runtime.builder: Matrix store_path derivation
and _ensure_dirs directory creation."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from medre.config.adapters.matrix import MatrixConfig
from medre.config.model import (
    AdapterConfigSet,
    MatrixRuntimeConfig,
    MeshtasticRuntimeConfig,
    RuntimeConfig,
    StorageConfig,
)
from medre.config.paths import MedrePaths, resolve
from medre.core.contracts.adapter import AdapterContract
from medre.runtime.builder import RuntimeBuilder
from tests.helpers.runtime_builder import (
    clean_path_env,
    make_fake_matrix_config,
    make_fake_meshtastic_config,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_path_env(monkeypatch: pytest.MonkeyPatch) -> None:
    clean_path_env(monkeypatch)


@pytest.fixture()
def tmp_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MedrePaths:
    """Create a MedrePaths pointing at a temp directory."""
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    return resolve()


# ---------------------------------------------------------------------------
# Matrix store_path derivation (Blocker 1)
# ---------------------------------------------------------------------------


class TestMatrixStorePathDerivation:
    """Builder injects per-adapter store_path from resolved state dir."""

    def _make_matrix_rt(
        self,
        adapter_id: str = "main",
        store_path: str | None = None,
    ) -> MatrixRuntimeConfig:
        cfg = MatrixConfig(
            adapter_id=adapter_id,
            homeserver="https://matrix.test",
            user_id="@bot:test",
            access_token="tok",
            store_path=store_path,
            encryption_mode="plaintext",
        )
        return MatrixRuntimeConfig(
            adapter_id=adapter_id,
            enabled=True,
            config=cfg,
        )

    def test_store_path_derived_when_unset(self, tmp_paths: MedrePaths) -> None:
        """MatrixConfig without store_path gets derived state store path."""
        rt = self._make_matrix_rt(adapter_id="mybot")
        config = RuntimeConfig(
            adapters=AdapterConfigSet(matrix={"mybot": rt}),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        injected_store_path = self._capture_store_path(builder, rt, "mybot")

        # The adapter was constructed with a derived store_path.
        expected = tmp_paths.state_dir / "adapters" / "mybot" / "matrix" / "store"
        assert injected_store_path == str(expected)

    def test_store_path_derived_medre_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MEDRE_HOME produces $MEDRE_HOME/state/adapters/<adapter_id>/matrix/store."""
        medre_home = tmp_path / "medre-root"
        monkeypatch.setenv("MEDRE_HOME", str(medre_home))
        paths = resolve()

        rt = self._make_matrix_rt(adapter_id="e2ee-bot")
        config = RuntimeConfig(
            adapters=AdapterConfigSet(matrix={"e2ee-bot": rt}),
        )
        builder = RuntimeBuilder(config, paths)

        expected_store = (
            medre_home / "state" / "adapters" / "e2ee-bot" / "matrix" / "store"
        )
        injected_store_path = self._capture_store_path(builder, rt, "e2ee-bot")
        assert injected_store_path == str(expected_store)

    def test_store_path_derived_xdg_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """XDG state produces $XDG_STATE_HOME/medre/adapters/<adapter_id>/matrix/store."""
        xdg_state = tmp_path / "xdg-state"
        monkeypatch.setenv("XDG_STATE_HOME", str(xdg_state))
        monkeypatch.delenv("MEDRE_HOME", raising=False)
        paths = resolve()

        rt = self._make_matrix_rt(adapter_id="xdg-bot")
        config = RuntimeConfig(
            adapters=AdapterConfigSet(matrix={"xdg-bot": rt}),
        )
        builder = RuntimeBuilder(config, paths)

        expected_store = (
            xdg_state / "medre" / "adapters" / "xdg-bot" / "matrix" / "store"
        )
        injected_store_path = self._capture_store_path(builder, rt, "xdg-bot")
        assert injected_store_path == str(expected_store)

    def test_explicit_store_path_preserved(self, tmp_paths: MedrePaths) -> None:
        """Explicit store_path on MatrixConfig is not overridden."""
        explicit = "/custom/store/path"
        rt = self._make_matrix_rt(adapter_id="explicit", store_path=explicit)
        config = RuntimeConfig(
            adapters=AdapterConfigSet(matrix={"explicit": rt}),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        injected_store_path = self._capture_store_path(builder, rt, "explicit")
        assert injected_store_path == explicit

    def test_multiple_matrix_adapters_distinct_paths(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Multiple Matrix adapters get distinct store paths."""
        rt1 = self._make_matrix_rt(adapter_id="bot_a")
        rt2 = self._make_matrix_rt(adapter_id="bot_b")
        config = RuntimeConfig(
            adapters=AdapterConfigSet(
                matrix={"a": rt1, "b": rt2},
            ),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        path_a = self._capture_store_path(builder, rt1, "bot_a")
        path_b = self._capture_store_path(builder, rt2, "bot_b")
        assert path_a != path_b
        assert path_a is not None and "bot_a" in path_a
        assert path_b is not None and "bot_b" in path_b

    def test_no_tempdir_in_derived_path(self, tmp_paths: MedrePaths) -> None:
        """Derived store path does not use the old tempdir pattern."""
        rt = self._make_matrix_rt(adapter_id="notmp")
        config = RuntimeConfig(
            adapters=AdapterConfigSet(matrix={"notmp": rt}),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        injected_store_path = self._capture_store_path(builder, rt, "notmp")

        assert injected_store_path is not None
        # The old pattern was {tempdir}/medre-matrix-store/{adapter_id}
        # The new pattern is {state_dir}/adapters/{adapter_id}/matrix/store
        assert "medre-matrix-store" not in injected_store_path
        assert injected_store_path.endswith("adapters/notmp/matrix/store")

    def _capture_store_path(
        self,
        builder: RuntimeBuilder,
        rt: MatrixRuntimeConfig,
        adapter_id: str,
    ) -> str | None:
        """Build a single adapter via _build_single_adapter and capture the
        store_path that was injected into the config before it reached the
        factory."""
        import medre.runtime.builder as builder_mod
        from medre.runtime.builder import _AdapterFactory

        captured: list[str | None] = []
        original_factory = builder_mod._ADAPTER_BUILDERS.get("matrix")

        def _capture_factory_build(cfg: Any) -> AdapterContract:
            captured.append(getattr(cfg, "store_path", None))
            return MagicMock(spec=AdapterContract)

        capture_factory = MagicMock(spec=_AdapterFactory)
        capture_factory.build = MagicMock(side_effect=_capture_factory_build)
        builder_mod._ADAPTER_BUILDERS["matrix"] = capture_factory
        try:
            builder._build_single_adapter("matrix", adapter_id, rt)
        finally:
            if original_factory is not None:
                builder_mod._ADAPTER_BUILDERS["matrix"] = original_factory

        return captured[0] if captured else None


class TestEnsureDirsMatrixStore:
    """Runtime start creates the derived Matrix store directory."""

    def test_ensure_dirs_creates_matrix_store(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
        paths = resolve()

        rt = MatrixRuntimeConfig(
            adapter_id="dirtest",
            enabled=True,
            adapter_kind="fake",
            config=make_fake_matrix_config(),
        )
        config = RuntimeConfig(
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(matrix={"dirtest": rt}),
        )
        builder = RuntimeBuilder(config, paths)
        app = builder.build()

        app._ensure_dirs()

        expected = paths.state_dir / "adapters" / "dirtest" / "matrix" / "store"
        assert expected.is_dir()


class TestEnsureDirsBaseDirectories:
    """_ensure_dirs creates state, data, cache, log, and database parent dirs."""

    def test_creates_all_base_directories(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
        paths = resolve()

        config = RuntimeConfig(
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(),
        )
        builder = RuntimeBuilder(config, paths)
        app = builder.build()

        app._ensure_dirs()

        assert paths.state_dir.is_dir()
        assert paths.data_dir.is_dir()
        assert paths.cache_dir.is_dir()
        assert paths.log_dir.is_dir()
        assert paths.database_path.parent.is_dir()

    def test_idempotent_rerun(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
        paths = resolve()

        config = RuntimeConfig(
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(),
        )
        builder = RuntimeBuilder(config, paths)
        app = builder.build()

        app._ensure_dirs()
        app._ensure_dirs()  # second call must not raise

        assert paths.state_dir.is_dir()


class TestEnsureDirsMultiAdapterIsolation:
    """Multiple adapters get isolated state roots; no directory collision."""

    def test_two_adapters_isolated_roots(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
        paths = resolve()

        rt_a = MatrixRuntimeConfig(
            adapter_id="alpha",
            enabled=True,
            adapter_kind="fake",
            config=make_fake_matrix_config(),
        )
        rt_b = MeshtasticRuntimeConfig(
            adapter_id="beta",
            enabled=True,
            adapter_kind="fake",
            config=make_fake_meshtastic_config(),
        )
        config = RuntimeConfig(
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(
                matrix={"alpha": rt_a},
                meshtastic={"beta": rt_b},
            ),
        )
        builder = RuntimeBuilder(config, paths)
        app = builder.build()

        app._ensure_dirs()

        root_a = paths.adapter_state_dir("alpha")
        root_b = paths.adapter_state_dir("beta")

        assert root_a.is_dir()
        assert root_b.is_dir()
        assert root_a != root_b
        # Verify no overlap: neither is a parent of the other
        assert not str(root_a).startswith(str(root_b))
        assert not str(root_b).startswith(str(root_a))
