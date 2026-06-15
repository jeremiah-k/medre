"""Track 4: Deployment / Container Path Verification.

Tests that verify MEDRE's deployment path model behaves correctly for
container and non-container scenarios.  These tests are intentionally
narrow and non-overlapping with:

- test_config_paths.py        (XDG defaults, MEDRE_HOME mode, placeholders,
                                no-directory-creation)
- test_storage_path_validation.py  (Matrix store isolation, Meshtastic no
                                stores, adapter state roots, global DB,
                                runtime cleanup preserves state)
- test_runtime_builder.py     (builder construction, _ensure_dirs matrix
                                store, base directories, multi-adapter
                                isolation, store path derivation)

Focus areas:
  - Container layout correctness under MEDRE_HOME=/opt/medre
  - Simulated bind-mount: separate filesystem roots
  - Full directory tree verification after _ensure_dirs
  - Disabled adapters excluded from directory creation
  - Cross-mode path property differences (MEDRE_HOME vs XDG)
  - Multi-transport state tree depth and naming
  - Path non-overlap guarantees between adapter roots
  - Config file location differs between modes
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from medre.config.adapters.matrix import MatrixConfig
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.config.model import (
    AdapterConfigSet,
    LoggingConfig,
    LxmfRuntimeConfig,
    MatrixRuntimeConfig,
    MeshCoreRuntimeConfig,
    MeshtasticRuntimeConfig,
    RuntimeConfig,
    RuntimeOptions,
    StorageConfig,
)
from medre.config.paths import MedrePaths, resolve
from medre.runtime.builder import RuntimeBuilder

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_PATH_ENV_VARS: tuple[str, ...] = (
    "MEDRE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_STATE_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
)


@pytest.fixture(autouse=True)
def _clean_path_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure no path-related env vars leak between tests."""
    for var in _PATH_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture()
def container_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MedrePaths:
    """Simulate MEDRE_HOME=/opt/medre container layout using tmp_path."""
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    return resolve()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_matrix_config(adapter_id: str = "mx") -> MatrixConfig:
    return MatrixConfig(
        adapter_id=adapter_id,
        homeserver="https://matrix.test",
        user_id="@bot:test",
        access_token="tok",
        encryption_mode="plaintext",
    )


def _fake_meshtastic_config() -> MeshtasticConfig:
    return MeshtasticConfig(
        adapter_id="mesh",
        connection_type="fake",
    ).validate()


def _make_runtime_with(
    *,
    matrix_ids: list[str] | None = None,
    meshtastic_ids: list[str] | None = None,
    meshcore_ids: list[str] | None = None,
    lxmf_ids: list[str] | None = None,
    disabled_ids: list[str] | None = None,
    storage_backend: str = "memory",
) -> RuntimeConfig:
    """Build a RuntimeConfig with specified adapters."""
    adapters = AdapterConfigSet()

    for aid in matrix_ids or []:
        enabled = aid not in (disabled_ids or [])
        adapters.matrix[aid] = MatrixRuntimeConfig(
            adapter_id=aid,
            enabled=enabled,
            adapter_kind="fake",
        )

    for aid in meshtastic_ids or []:
        enabled = aid not in (disabled_ids or [])
        adapters.meshtastic[aid] = MeshtasticRuntimeConfig(
            adapter_id=aid,
            enabled=enabled,
            adapter_kind="fake",
        )

    for aid in meshcore_ids or []:
        enabled = aid not in (disabled_ids or [])
        adapters.meshcore[aid] = MeshCoreRuntimeConfig(
            adapter_id=aid,
            enabled=enabled,
            adapter_kind="fake",
        )

    for aid in lxmf_ids or []:
        enabled = aid not in (disabled_ids or [])
        adapters.lxmf[aid] = LxmfRuntimeConfig(
            adapter_id=aid,
            enabled=enabled,
            adapter_kind="fake",
        )

    return RuntimeConfig(
        runtime=RuntimeOptions(name="deploy-test"),
        logging=LoggingConfig(level="DEBUG"),
        storage=StorageConfig(backend=storage_backend),
        adapters=adapters,
    )


# ===================================================================
# A) Container layout matches docker.env.example expectations
# ===================================================================


class TestContainerLayoutMatchesDockerEnv:
    """Paths derived from MEDRE_HOME=/opt/medre match docker.env.example."""

    def test_state_dir_is_opt_medre_state(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """MEDRE_HOME=/opt/medre → state_dir = /opt/medre/state."""
        monkeypatch.setenv("MEDRE_HOME", "/opt/medre")
        paths = resolve()
        assert paths.state_dir == Path("/opt/medre/state")

    def test_config_file_is_opt_medre_config_toml(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Config file is at MEDRE_HOME/config.yaml (flat, no config_dir)."""
        monkeypatch.setenv("MEDRE_HOME", "/opt/medre")
        paths = resolve()
        assert paths.config_file == Path("/opt/medre/config.yaml")
        assert paths.config_dir is None

    def test_database_path_under_state(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Database is at {MEDRE_HOME}/state/medre.sqlite."""
        monkeypatch.setenv("MEDRE_HOME", "/opt/medre")
        paths = resolve()
        assert paths.database_path == Path("/opt/medre/state/medre.sqlite")
        assert paths.database_path.parent == paths.state_dir

    def test_logs_dir_is_medre_home_logs(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Log directory is {MEDRE_HOME}/logs/."""
        monkeypatch.setenv("MEDRE_HOME", "/opt/medre")
        paths = resolve()
        assert paths.log_dir == Path("/opt/medre/logs")

    def test_all_paths_are_absolute(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """All resolved paths are absolute in MEDRE_HOME mode."""
        monkeypatch.setenv("MEDRE_HOME", "/opt/medre")
        paths = resolve()
        for attr in (
            "config_file",
            "state_dir",
            "data_dir",
            "cache_dir",
            "log_dir",
            "database_path",
        ):
            p = getattr(paths, attr)
            assert p.is_absolute(), f"{attr}={p} is not absolute"


# ===================================================================
# B) Simulated bind-mount: state on separate root
# ===================================================================


class TestSimulatedBindMount:
    """Simulate bind-mount where MEDRE_HOME points to a different
    filesystem root than the host's default paths."""

    def test_bind_mount_separate_root(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """MEDRE_HOME can point to an arbitrary path (simulates bind mount)."""
        mount_point = tmp_path / "mounted" / "data"
        monkeypatch.setenv("MEDRE_HOME", str(mount_point))
        paths = resolve()

        assert paths.state_dir == mount_point / "state"
        assert paths.database_path == mount_point / "state" / "medre.sqlite"

    def test_bind_mount_deeply_nested(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """MEDRE_HOME can be a deeply nested path (realistic mount scenario)."""
        deep = tmp_path / "mnt" / "services" / "medre" / "prod"
        monkeypatch.setenv("MEDRE_HOME", str(deep))
        paths = resolve()

        assert paths.config_file == deep / "config.yaml"
        assert paths.state_dir == deep / "state"


# ===================================================================
# C) Full directory tree after _ensure_dirs
# ===================================================================


class TestFullDirectoryTreeAfterEnsureDirs:
    """_ensure_dirs creates the complete expected directory tree."""

    def test_full_tree_with_matrix_and_meshtastic(
        self,
        container_paths: MedrePaths,
    ) -> None:
        """Matrix + Meshtastic produces complete directory tree."""
        config = _make_runtime_with(
            matrix_ids=["mx_a"],
            meshtastic_ids=["mesh_b"],
        )
        builder = RuntimeBuilder(config, container_paths)
        app = builder.build()
        app._ensure_dirs()

        # Base dirs
        assert container_paths.state_dir.is_dir()
        assert container_paths.data_dir.is_dir()
        assert container_paths.cache_dir.is_dir()
        assert container_paths.log_dir.is_dir()

        # Adapter roots
        root_mx = container_paths.adapter_state_dir("mx_a")
        root_mesh = container_paths.adapter_state_dir("mesh_b")
        assert root_mx.is_dir()
        assert root_mesh.is_dir()

        # Matrix store
        mx_store = (
            container_paths.adapter_transport_state_dir("mx_a", "matrix") / "store"
        )
        assert mx_store.is_dir()

        # Database parent
        assert container_paths.database_path.parent.is_dir()

    def test_tree_with_all_four_transports(
        self,
        container_paths: MedrePaths,
    ) -> None:
        """All four transport types get adapter roots."""
        config = _make_runtime_with(
            matrix_ids=["mx"],
            meshtastic_ids=["mt"],
            meshcore_ids=["mc"],
            lxmf_ids=["lx"],
        )
        builder = RuntimeBuilder(config, container_paths)
        app = builder.build()
        app._ensure_dirs()

        for aid in ("mx", "mt", "mc", "lx"):
            root = container_paths.adapter_state_dir(aid)
            assert root.is_dir(), f"Adapter root for {aid} should exist"

        # Only Matrix gets matrix/store
        mx_store = container_paths.adapter_transport_state_dir("mx", "matrix") / "store"
        assert mx_store.is_dir()


# ===================================================================
# D) Disabled adapters excluded from _ensure_dirs
# ===================================================================


class TestDisabledAdaptersExcluded:
    """Disabled adapters do NOT get state directories."""

    def test_disabled_adapter_no_state_dir(
        self,
        container_paths: MedrePaths,
    ) -> None:
        """Disabled Matrix adapter does not get state dir created."""
        config = _make_runtime_with(
            matrix_ids=["active_mx", "disabled_mx"],
            disabled_ids=["disabled_mx"],
        )
        builder = RuntimeBuilder(config, container_paths)
        app = builder.build()
        app._ensure_dirs()

        active_root = container_paths.adapter_state_dir("active_mx")
        disabled_root = container_paths.adapter_state_dir("disabled_mx")

        assert active_root.is_dir(), "Enabled adapter should have state dir"
        assert not disabled_root.exists(), "Disabled adapter should NOT have state dir"

    def test_disabled_adapter_no_matrix_store(
        self,
        container_paths: MedrePaths,
    ) -> None:
        """Disabled Matrix adapter does not get matrix/store."""
        config = _make_runtime_with(
            matrix_ids=["on", "off"],
            disabled_ids=["off"],
        )
        builder = RuntimeBuilder(config, container_paths)
        app = builder.build()
        app._ensure_dirs()

        on_store = container_paths.adapter_transport_state_dir("on", "matrix") / "store"
        off_store = (
            container_paths.adapter_transport_state_dir("off", "matrix") / "store"
        )

        assert on_store.is_dir()
        assert not off_store.exists()

    def test_mixed_enabled_disabled(
        self,
        container_paths: MedrePaths,
    ) -> None:
        """Mix of enabled/disabled across transports."""
        config = _make_runtime_with(
            matrix_ids=["mx_on", "mx_off"],
            meshtastic_ids=["mt_on", "mt_off"],
            disabled_ids=["mx_off", "mt_off"],
        )
        builder = RuntimeBuilder(config, container_paths)
        app = builder.build()
        app._ensure_dirs()

        for aid in ("mx_on", "mt_on"):
            assert container_paths.adapter_state_dir(aid).is_dir()

        for aid in ("mx_off", "mt_off"):
            assert not container_paths.adapter_state_dir(aid).exists()


# ===================================================================
# E) Cross-mode property differences (MEDRE_HOME vs XDG)
# ===================================================================


class TestCrossModePropertyDifferences:
    """MEDRE_HOME and XDG modes have distinct structural properties."""

    def test_config_dir_none_in_medre_home_mode(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """MEDRE_HOME mode has config_dir=None (no config directory)."""
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
        paths = resolve()
        assert paths.config_dir is None

    def test_config_dir_set_in_xdg_mode(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """XDG mode has a non-None config_dir."""
        paths = resolve()
        assert paths.config_dir is not None
        assert paths.config_dir.name == "medre"

    def test_database_path_relation_to_state_dir(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """database_path is always a direct child of state_dir."""
        # MEDRE_HOME mode
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path / "home"))
        paths_home = resolve()
        assert paths_home.database_path.parent == paths_home.state_dir

        # XDG mode
        monkeypatch.delenv("MEDRE_HOME", raising=False)
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
        paths_xdg = resolve()
        assert paths_xdg.database_path.parent == paths_xdg.state_dir

    def test_log_dir_under_state_in_xdg_mode(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """In XDG mode, log_dir is a child of state_dir."""
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
        paths = resolve()
        assert str(paths.log_dir).startswith(str(paths.state_dir))

    def test_log_dir_not_under_state_in_medre_home_mode(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """In MEDRE_HOME mode, log_dir is a sibling of state_dir (both under
        MEDRE_HOME), not a child of state_dir."""
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
        paths = resolve()
        # log_dir is MEDRE_HOME/logs, state_dir is MEDRE_HOME/state
        assert paths.log_dir == tmp_path / "logs"
        assert paths.state_dir == tmp_path / "state"
        # They are siblings, not parent/child
        assert paths.log_dir.parent == paths.state_dir.parent


# ===================================================================
# F) Multi-transport state tree naming
# ===================================================================


class TestMultiTransportStateTreeNaming:
    """Verify naming conventions for multi-transport adapter state."""

    def test_matrix_store_path_components(
        self,
        container_paths: MedrePaths,
    ) -> None:
        """Matrix store path has correct components:
        state/adapters/{id}/matrix/store."""
        store = container_paths.adapter_transport_state_dir("mx1", "matrix") / "store"
        parts = store.parts

        # Find 'state' in parts and verify structure after it
        state_idx = parts.index("state")
        assert parts[state_idx + 1] == "adapters"
        assert parts[state_idx + 2] == "mx1"
        assert parts[state_idx + 3] == "matrix"
        assert parts[state_idx + 4] == "store"

    def test_meshtastic_transport_dir_components(
        self,
        container_paths: MedrePaths,
    ) -> None:
        """Meshtastic transport dir: state/adapters/{id}/meshtastic."""
        transport_dir = container_paths.adapter_transport_state_dir(
            "radio", "meshtastic"
        )
        parts = transport_dir.parts

        state_idx = parts.index("state")
        assert parts[state_idx + 1] == "adapters"
        assert parts[state_idx + 2] == "radio"
        assert parts[state_idx + 3] == "meshtastic"


# ===================================================================
# G) Adapter root non-overlap guarantee
# ===================================================================


class TestAdapterRootNonOverlap:
    """No adapter state root is a prefix of another."""

    def test_no_prefix_overlap(
        self,
        container_paths: MedrePaths,
    ) -> None:
        """Similar adapter IDs don't produce overlapping roots."""
        ids = ["alpha", "alpha_beta", "alpha_beta_gamma", "alpha1"]
        roots = [str(container_paths.adapter_state_dir(aid)) for aid in ids]

        # No root is a prefix of another (except exact match)
        for i, a in enumerate(roots):
            for j, b in enumerate(roots):
                if i != j:
                    assert not b.startswith(a + os.sep), f"{a} is a prefix of {b}"

    def test_no_prefix_overlap_with_separator(
        self,
        container_paths: MedrePaths,
    ) -> None:
        """Adapter IDs that share a prefix don't share a root path."""
        root_a = str(container_paths.adapter_state_dir("mx"))
        root_b = str(container_paths.adapter_state_dir("mx_other"))

        assert root_a != root_b
        assert not root_b.startswith(root_a + os.sep)


# ===================================================================
# H) Config file location differs between modes
# ===================================================================


class TestConfigFileLocationDiffersBetweenModes:
    """MEDRE_HOME and XDG modes place config files differently."""

    def test_xdg_config_in_config_dir(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """XDG: config_file is inside config_dir."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
        paths = resolve()

        assert paths.config_dir is not None
        assert paths.config_file.parent == paths.config_dir
        assert paths.config_file.name == "config.yaml"

    def test_medre_home_config_flat(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """MEDRE_HOME: config_file is at MEDRE_HOME/config.yaml (no config_dir)."""
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
        paths = resolve()

        assert paths.config_dir is None
        assert paths.config_file == tmp_path / "config.yaml"
        assert paths.config_file.parent == tmp_path


# ===================================================================
# I) No writes outside MEDRE_HOME during _ensure_dirs
# ===================================================================


class TestNoWritesOutsideMedreHome:
    """_ensure_dirs only writes under the configured paths."""

    def test_no_writes_outside_home(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """After _ensure_dirs, no directories exist outside MEDRE_HOME."""
        medre_home = tmp_path / "home"
        monkeypatch.setenv("MEDRE_HOME", str(medre_home))

        config = _make_runtime_with(matrix_ids=["mx"])
        paths = resolve()
        builder = RuntimeBuilder(config, paths)
        app = builder.build()

        app._ensure_dirs()

        # Collect all directories under tmp_path
        created_dirs: list[Path] = []
        for root, dirs, _files in os.walk(tmp_path):
            for d in dirs:
                created_dirs.append(Path(root) / d)

        # Every created directory should be under medre_home
        for d in created_dirs:
            assert str(d).startswith(
                str(medre_home)
            ), f"Directory {d} created outside MEDRE_HOME {medre_home}"

    def test_no_writes_outside_xdg_roots(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """XDG mode: no writes outside configured XDG roots."""
        xdg_state = tmp_path / "xdg" / "state"
        xdg_data = tmp_path / "xdg" / "data"
        xdg_cache = tmp_path / "xdg" / "cache"

        monkeypatch.setenv("XDG_STATE_HOME", str(xdg_state))
        monkeypatch.setenv("XDG_DATA_HOME", str(xdg_data))
        monkeypatch.setenv("XDG_CACHE_HOME", str(xdg_cache))

        config = _make_runtime_with(meshtastic_ids=["radio"])
        paths = resolve()
        builder = RuntimeBuilder(config, paths)
        app = builder.build()

        app._ensure_dirs()

        # Verify all created dirs are under tmp_path/xdg
        for root, dirs, _files in os.walk(tmp_path):
            for d in dirs:
                full = Path(root) / d
                if full.exists():
                    assert str(full).startswith(
                        str(tmp_path / "xdg")
                    ), f"Directory {full} created outside XDG roots"


# ===================================================================
# J) to_diagnostics snapshot includes all deployment-relevant paths
# ===================================================================


class TestDiagnosticsSnapshot:
    """to_diagnostics() provides deployment-relevant path information."""

    def test_medre_home_diagnostics(
        self,
        container_paths: MedrePaths,
    ) -> None:
        """Diagnostics snapshot includes all key paths."""
        diag = container_paths.to_diagnostics()

        assert "config_dir" in diag
        assert "config_file" in diag
        assert "state_dir" in diag
        assert "data_dir" in diag
        assert "cache_dir" in diag
        assert "log_dir" in diag
        assert "database_path" in diag
        assert "adapter_state_root" in diag

        # In MEDRE_HOME mode, config_dir is "(none)"
        assert diag["config_dir"] == "(none)"

        # adapter_state_root is state_dir/adapters
        assert diag["adapter_state_root"] == str(container_paths.state_dir / "adapters")

    def test_xdg_diagnostics(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Diagnostics in XDG mode shows config_dir."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "st"))
        paths = resolve()
        diag = paths.to_diagnostics()

        # In XDG mode, config_dir is a real path
        assert diag["config_dir"] != "(none)"
        assert "cfg" in diag["config_dir"]
