"""Track 3/4: Deterministic container-runtime validation tests.

Validates MEDRE's container deployment model behaves deterministically
without requiring Docker, Kubernetes, or any container runtime.  Every test
in this module:

- Simulates container behaviour using temp directories and monkeypatched
  environment variables (MEDRE_HOME, XDG_*).
- Exercises **state persistence, isolation, and restart semantics** only —
  no live network, no hardware, no actual Docker or podman.
- Produces **deterministic** pass/fail results.
- Does **not** duplicate ``test_deployment_paths.py`` (which covers path
  layout, bind-mount roots, _ensure_dirs tree, disabled adapters, cross-mode
  differences, non-overlap, and diagnostics).

Focus areas:

  1. Bind-mounted state persistence via temp dirs.
  2. SQLite persistence across simulated container restarts.
  3. Matrix store path persistence across re-resolution.
  4. Adapter-state isolation between container instances.
  5. _ensure_dirs idempotency (repeated calls, no errors).
  6. Non-root assumptions as documentation checks.
  7. Container restart behaviour simulation (build → start → stop → rebuild).
  8. docker.env.example structural validation.

NOT EXECUTED: No real Docker containers are created.  These tests verify the
*application logic* that would run inside a container, not the container
platform itself.
"""

from __future__ import annotations

import os
import re
import sqlite3
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
from medre.config.paths import MedrePaths, resolve
from medre.runtime.app import MedreApp
from medre.runtime.builder import RuntimeBuilder

# ---------------------------------------------------------------------------
# Repo paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DOCKER_ENV = _REPO_ROOT / "examples" / "env" / "docker.env.example"

# ---------------------------------------------------------------------------
# Path-related env vars to clean
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_runtime_with(
    *,
    matrix_ids: list[str] | None = None,
    meshtastic_ids: list[str] | None = None,
    storage_backend: str = "memory",
    storage_path: str | None = None,
) -> RuntimeConfig:
    """Build a RuntimeConfig with specified adapters."""
    adapters = AdapterConfigSet()

    for aid in matrix_ids or []:
        adapters.matrix[aid] = MatrixRuntimeConfig(
            adapter_id=aid,
            enabled=True,
            adapter_kind="fake",
        )

    for aid in meshtastic_ids or []:
        adapters.meshtastic[aid] = MeshtasticRuntimeConfig(
            adapter_id=aid,
            enabled=True,
            adapter_kind="fake",
        )

    storage = StorageConfig(backend=storage_backend)
    if storage_path is not None:
        storage = StorageConfig(backend=storage_backend, path=storage_path)

    return RuntimeConfig(
        runtime=RuntimeOptions(name="container-test"),
        logging=LoggingConfig(level="DEBUG"),
        storage=storage,
        adapters=adapters,
    )


def _build_and_ensure_dirs(
    config: RuntimeConfig,
    paths: MedrePaths,
) -> MedreApp:
    """Build a MedreApp and call _ensure_dirs (without starting)."""
    builder = RuntimeBuilder(config, paths)
    app = builder.build()
    app._ensure_dirs()
    return app


# ===================================================================
# 1. Bind-mounted state persistence via temp dirs
# ===================================================================


class TestBindMountStatePersistence:
    """Files written to simulated bind-mount paths survive re-resolution.

    Simulates the pattern where a Docker volume is mounted at MEDRE_HOME
    and the application is restarted (re-resolves paths from same env).
    """

    def test_state_file_survives_reresolution(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A file written to state_dir persists after re-resolve."""
        vol = tmp_path / "volume"
        monkeypatch.setenv("MEDRE_HOME", str(vol))

        paths1 = resolve()
        marker = paths1.state_dir / "marker.txt"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("persisted")

        # Simulate container restart — re-resolve paths from same env
        paths2 = resolve()
        assert (paths2.state_dir / "marker.txt").read_text() == "persisted"
        assert paths1.state_dir == paths2.state_dir

    def test_database_file_survives_reresolution(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """SQLite database file persists after re-resolve."""
        vol = tmp_path / "volume"
        monkeypatch.setenv("MEDRE_HOME", str(vol))

        paths1 = resolve()
        db = paths1.database_path
        db.parent.mkdir(parents=True, exist_ok=True)
        # Write a minimal SQLite database
        conn = sqlite3.connect(str(db))
        try:
            conn.execute("CREATE TABLE t (x TEXT)")
            conn.execute("INSERT INTO t VALUES ('hello')")
            conn.commit()
        finally:
            conn.close()

        paths2 = resolve()
        conn2 = sqlite3.connect(str(paths2.database_path))
        try:
            rows = conn2.execute("SELECT x FROM t").fetchall()
        finally:
            conn2.close()
        assert rows == [("hello",)]

    def test_adapter_state_survives_reresolution(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Adapter state directory contents persist after re-resolve."""
        vol = tmp_path / "volume"
        monkeypatch.setenv("MEDRE_HOME", str(vol))

        paths1 = resolve()
        adapter_state = paths1.adapter_state_dir("mx_a")
        adapter_state.mkdir(parents=True, exist_ok=True)
        (adapter_state / "session.dat").write_bytes(b"\x00\x01\x02")

        paths2 = resolve()
        assert (
            paths2.adapter_state_dir("mx_a") / "session.dat"
        ).read_bytes() == b"\x00\x01\x02"


# ===================================================================
# 2. SQLite persistence across simulated container restarts
# ===================================================================


class TestSQLitePersistence:
    """SQLite storage retains data across simulated container restarts.

    Uses raw sqlite3 (no aiosqlite needed) to verify on-disk persistence.
    """

    def test_sqlite_data_survives_rebuild(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Build app with sqlite backend, insert data, rebuild, data persists."""
        vol = tmp_path / "vol"
        monkeypatch.setenv("MEDRE_HOME", str(vol))

        config = _make_runtime_with(
            matrix_ids=["mx"],
            storage_backend="sqlite",
            storage_path="{state}/medre.sqlite",
        )
        paths = resolve()

        # _ensure_dirs creates parent dir, but not the database file itself.
        # The database file is created by storage.initialize() on start().
        # Here we create it manually to simulate a previous run.
        _build_and_ensure_dirs(config, paths)
        assert paths.database_path.parent.is_dir()

        # Write data directly to the SQLite file (simulates previous run)
        conn = sqlite3.connect(str(paths.database_path))
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
            conn.execute("INSERT INTO kv VALUES ('test', 'value')")
            conn.commit()
        finally:
            conn.close()

        # Simulate container restart: rebuild app with same paths
        _build_and_ensure_dirs(config, paths)

        # Data must still be there
        conn2 = sqlite3.connect(str(paths.database_path))
        try:
            rows = conn2.execute("SELECT v FROM kv WHERE k = 'test'").fetchall()
        finally:
            conn2.close()
        assert rows == [("value",)]

    def test_sqlite_wal_mode_compatible(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """SQLite WAL mode can be enabled on database under MEDRE_HOME."""
        vol = tmp_path / "vol"
        monkeypatch.setenv("MEDRE_HOME", str(vol))
        paths = resolve()
        paths.database_path.parent.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(str(paths.database_path))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
        assert mode == "wal"


# ===================================================================
# 3. Matrix store path persistence
# ===================================================================


class TestMatrixStorePathPersistence:
    """Matrix crypto store path is stable and deterministic."""

    def test_matrix_store_path_identical_across_resolves(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Re-resolving paths produces identical Matrix store path."""
        vol = tmp_path / "vol"
        monkeypatch.setenv("MEDRE_HOME", str(vol))

        paths1 = resolve()
        store1 = paths1.adapter_transport_state_dir("mx1", "matrix") / "store"

        paths2 = resolve()
        store2 = paths2.adapter_transport_state_dir("mx1", "matrix") / "store"

        assert store1 == store2

    def test_matrix_store_files_persist(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Files written to Matrix store dir survive re-resolution."""
        vol = tmp_path / "vol"
        monkeypatch.setenv("MEDRE_HOME", str(vol))

        paths = resolve()
        store = paths.adapter_transport_state_dir("mx1", "matrix") / "store"
        store.mkdir(parents=True, exist_ok=True)
        (store / "crypto.pkl").write_bytes(b"fake-crypto-state")

        paths2 = resolve()
        store2 = paths2.adapter_transport_state_dir("mx1", "matrix") / "store"
        assert (store2 / "crypto.pkl").read_bytes() == b"fake-crypto-state"


# ===================================================================
# 4. Adapter-state isolation between container instances
# ===================================================================


class TestContainerInstanceIsolation:
    """Two different MEDRE_HOME values produce completely isolated state."""

    def test_separate_homes_no_shared_state(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Container A and Container B have no shared state directories."""
        home_a = tmp_path / "container_a"
        home_b = tmp_path / "container_b"

        # Container A
        monkeypatch.setenv("MEDRE_HOME", str(home_a))
        config = _make_runtime_with(matrix_ids=["mx"])
        paths_a = resolve()
        _build_and_ensure_dirs(config, paths_a)

        # Write marker in container A
        (paths_a.adapter_state_dir("mx") / "a_marker.txt").write_text("A")

        # Container B
        monkeypatch.setenv("MEDRE_HOME", str(home_b))
        paths_b = resolve()
        _build_and_ensure_dirs(config, paths_b)

        # Container B has its own state dir
        assert paths_b.state_dir != paths_a.state_dir
        assert paths_b.adapter_state_dir("mx") != paths_a.adapter_state_dir("mx")

        # Container B's adapter dir exists but doesn't have A's marker
        assert paths_b.adapter_state_dir("mx").is_dir()
        assert not (paths_b.adapter_state_dir("mx") / "a_marker.txt").exists()

        # Container A's marker still exists
        assert (paths_a.adapter_state_dir("mx") / "a_marker.txt").exists()

    def test_database_isolation(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Container A and Container B have separate databases."""
        home_a = tmp_path / "container_a"
        home_b = tmp_path / "container_b"

        monkeypatch.setenv("MEDRE_HOME", str(home_a))
        paths_a = resolve()

        monkeypatch.setenv("MEDRE_HOME", str(home_b))
        paths_b = resolve()

        assert paths_a.database_path != paths_b.database_path
        assert not str(paths_b.database_path).startswith(str(home_a))


# ===================================================================
# 5. _ensure_dirs idempotency
# ===================================================================


class TestEnsureDirsIdempotency:
    """Calling _ensure_dirs multiple times produces no errors."""

    def test_ensure_dirs_twice_no_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_ensure_dirs can be called twice without error."""
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
        config = _make_runtime_with(matrix_ids=["mx"], meshtastic_ids=["mt"])
        paths = resolve()

        app = _build_and_ensure_dirs(config, paths)
        # Second call should not raise
        app._ensure_dirs()

        # Verify directories still exist
        assert paths.state_dir.is_dir()
        assert paths.adapter_state_dir("mx").is_dir()

    def test_ensure_dirs_thrice_idempotent(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Three calls to _ensure_dirs produce same directory tree."""
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
        config = _make_runtime_with(matrix_ids=["mx"])
        paths = resolve()

        app = _build_and_ensure_dirs(config, paths)
        app._ensure_dirs()
        app._ensure_dirs()

        # Count directories
        dirs_before = set()
        for root, ds, _ in os.walk(tmp_path):
            for d in ds:
                dirs_before.add(Path(root) / d)

        app._ensure_dirs()

        dirs_after = set()
        for root, ds, _ in os.walk(tmp_path):
            for d in ds:
                dirs_after.add(Path(root) / d)

        assert dirs_before == dirs_after


# ===================================================================
# 6. Non-root assumptions as documentation checks
# ===================================================================


class TestNonRootAssumptions:
    """Verify documentation reflects non-root container assumptions.

    These tests check that the docker.env.example and related docs
    communicate non-root practices, volume ownership expectations, and
    path permissions.  They do NOT verify actual file permissions.
    """

    def test_docker_env_mentions_medre_home_volume(
        self,
    ) -> None:
        """docker.env.example must mention volume mounting for MEDRE_HOME."""
        assert _DOCKER_ENV.is_file(), f"Missing: {_DOCKER_ENV}"
        text = _DOCKER_ENV.read_text()
        assert "MEDRE_HOME" in text, "docker.env.example missing MEDRE_HOME"
        # Must mention /opt/medre (the canonical container path)
        assert (
            "/opt/medre" in text
        ), "docker.env.example must specify MEDRE_HOME=/opt/medre"

    def test_docker_env_mentions_volume_mount(
        self,
    ) -> None:
        """docker.env.example must reference volume mount or persistence."""
        text = _DOCKER_ENV.read_text()
        # Should mention mount/volume/persistent somewhere
        assert any(
            kw in text.lower() for kw in ("mount", "volume", "persistent", "data")
        ), "docker.env.example should mention volume mounting or data persistence"

    def test_docker_env_no_hardcoded_uid_gid(
        self,
    ) -> None:
        """docker.env.example should not hardcode UID/GID values."""
        text = _DOCKER_ENV.read_text()
        # Should not have explicit UID/GID like 1000:1000
        assert not re.search(
            r"\bUID\b\s*=\s*\d{4}", text
        ), "docker.env.example should not hardcode UID"
        assert not re.search(
            r"\bGID\b\s*=\s*\d{4}", text
        ), "docker.env.example should not hardcode GID"

    def test_docker_env_comments_mention_persistence(
        self,
    ) -> None:
        """docker.env.example header comments must mention persistence."""
        text = _DOCKER_ENV.read_text()
        # First few lines should mention persistent state / data
        header = "\n".join(text.splitlines()[:10])
        has_persistence_keyword = any(
            kw in header.lower() for kw in ("persistent", "volume", "state", "data")
        )
        assert (
            has_persistence_keyword
        ), "docker.env.example header should mention persistence/volume/state"


# ===================================================================
# 7. Container restart behaviour simulation
# ===================================================================


class TestContainerRestartSimulation:
    """Simulate container restart: build → _ensure_dirs → rebuild.

    Verifies that state created in the first "container run" survives
    into the second "container run" when using the same MEDRE_HOME.
    """

    def test_dirs_survive_restart_cycle(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Directories created in first run survive rebuild."""
        vol = tmp_path / "vol"
        monkeypatch.setenv("MEDRE_HOME", str(vol))
        config = _make_runtime_with(
            matrix_ids=["mx"],
            meshtastic_ids=["mt"],
        )
        paths = resolve()

        # First "container run"
        _build_and_ensure_dirs(config, paths)
        assert paths.adapter_state_dir("mx").is_dir()

        # Second "container run" (same volume)
        _build_and_ensure_dirs(config, paths)
        assert paths.adapter_state_dir("mx").is_dir()
        assert paths.adapter_state_dir("mt").is_dir()

    def test_data_survives_restart_cycle(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Data files created in first run survive rebuild."""
        vol = tmp_path / "vol"
        monkeypatch.setenv("MEDRE_HOME", str(vol))
        config = _make_runtime_with(matrix_ids=["mx"])
        paths = resolve()

        # First run — write data
        _build_and_ensure_dirs(config, paths)
        data_file = paths.data_dir / "runtime_state.json"
        data_file.parent.mkdir(parents=True, exist_ok=True)
        data_file.write_text('{"status": "ok"}')

        # Second run — data still there
        _build_and_ensure_dirs(config, paths)
        assert (paths.data_dir / "runtime_state.json").read_text() == '{"status": "ok"}'

    def test_matrix_store_survives_restart(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Matrix crypto store survives container restart."""
        vol = tmp_path / "vol"
        monkeypatch.setenv("MEDRE_HOME", str(vol))
        config = _make_runtime_with(matrix_ids=["mx"])
        paths = resolve()

        # First run
        _build_and_ensure_dirs(config, paths)
        store = paths.adapter_transport_state_dir("mx", "matrix") / "store"
        (store / "keys.db").write_bytes(b"fake-keys")

        # Second run
        _build_and_ensure_dirs(config, paths)
        assert (store / "keys.db").read_bytes() == b"fake-keys"


# ===================================================================
# 8. docker.env.example structural validation
# ===================================================================


class TestDockerEnvExampleStructure:
    """docker.env.example has correct structure and known keys."""

    def test_file_exists(self) -> None:
        assert _DOCKER_ENV.is_file()

    def test_is_commented_template(self) -> None:
        """File should be mostly comments (template style)."""
        text = _DOCKER_ENV.read_text()
        lines = text.splitlines()
        non_empty = [line for line in lines if line.strip()]
        comment_lines = [line for line in non_empty if line.strip().startswith("#")]
        # Most lines should be comments
        assert (
            len(comment_lines) > len(non_empty) // 2
        ), "docker.env.example should be predominantly comments"

    def test_contains_medre_home_assignment(self) -> None:
        """MEDRE_HOME must be assigned a value."""
        text = _DOCKER_ENV.read_text()
        assert re.search(
            r"^MEDRE_HOME\s*=", text, re.MULTILINE
        ), "docker.env.example missing MEDRE_HOME=<value> assignment"

    def test_contains_log_level(self) -> None:
        """MEDRE_LOG_LEVEL should be present."""
        text = _DOCKER_ENV.read_text()
        assert "MEDRE_LOG_LEVEL" in text, "docker.env.example missing MEDRE_LOG_LEVEL"

    def test_matrix_adapter_keys_present(self) -> None:
        """Matrix adapter env vars should be documented."""
        text = _DOCKER_ENV.read_text()
        for key in (
            "MEDRE_ADAPTER__MAIN__ENABLED",
            "MEDRE_ADAPTER__MAIN__HOMESERVER",
            "MEDRE_ADAPTER__MAIN__USER_ID",
            "MEDRE_ADAPTER__MAIN__ACCESS_TOKEN",
        ):
            assert key in text, f"docker.env.example missing {key}"

    def test_meshtastic_adapter_keys_present(self) -> None:
        """Meshtastic adapter env vars should be documented."""
        text = _DOCKER_ENV.read_text()
        assert "MEDRE_ADAPTER__RADIO__ENABLED" in text
        assert "MEDRE_ADAPTER__RADIO__CONNECTION_TYPE" in text

    def test_no_real_secrets(self) -> None:
        """docker.env.example must not contain real secret patterns."""
        text = _DOCKER_ENV.read_text()
        # Real Matrix access tokens: syt_ followed by 10+ alphanumeric chars
        assert not re.search(
            r"syt_[a-zA-Z0-9]{10,}", text
        ), "docker.env.example contains a real-looking access token"
        # No private keys
        assert "-----BEGIN" not in text, "docker.env.example contains a private key"

    def test_placeholder_token_is_safe(self) -> None:
        """Access token placeholder should be clearly fake."""
        text = _DOCKER_ENV.read_text()
        match = re.search(r"MEDRE_ADAPTER__MAIN__ACCESS_TOKEN\s*=\s*(.+)", text)
        if match:
            value = match.group(1).strip()
            # Should not be a real-looking token
            assert (
                len(value) < 50
                or "secret" in value.lower()
                or "placeholder" in value.lower()
            ), f"Access token value looks potentially real: {value!r}"
