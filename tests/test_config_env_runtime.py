"""Tests for env-created adapters building through RuntimeBuilder.

Verifies that adapters created via MEDRE_ADAPTER__<TOKEN>__TRANSPORT=meshcore/lxmf
can be built by RuntimeBuilder in fake/no-SDK mode.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from medre.config.env import apply_env_overrides
from medre.config.model import (
    AdapterConfigSet,
    LoggingConfig,
    RuntimeConfig,
    RuntimeOptions,
    StorageConfig,
)
from medre.config.paths import MedrePaths, resolve
from medre.runtime.builder import RuntimeBuilder
from tests.helpers.runtime_builder import clean_path_env


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


@pytest.fixture(autouse=True)
def _clean_medre_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove all MEDRE_* env vars between tests."""
    import os

    for key in list(os.environ.keys()):
        if key.startswith("MEDRE_"):
            monkeypatch.delenv(key, raising=False)


def _make_minimal_config() -> RuntimeConfig:
    """Return a minimal RuntimeConfig with storage for builder tests."""
    return RuntimeConfig(
        runtime=RuntimeOptions(name="env-runtime-test"),
        logging=LoggingConfig(level="WARNING"),
        storage=StorageConfig(backend="memory"),
        adapters=AdapterConfigSet(),
    )


# ---------------------------------------------------------------------------
# MeshCore env-created adapter runtime tests
# ---------------------------------------------------------------------------


class TestMeshCoreEnvCreatedRuntime:
    """MeshCore adapters created from env can be built by RuntimeBuilder."""

    def test_two_meshcore_env_adapters_build(
        self, monkeypatch: pytest.MonkeyPatch, tmp_paths: MedrePaths,
    ) -> None:
        """Two env-created MeshCore adapters build through RuntimeBuilder."""
        monkeypatch.setenv("MEDRE_ADAPTER__MESHCORE_TBEAM__TRANSPORT", "meshcore")
        monkeypatch.setenv("MEDRE_ADAPTER__MESHCORE_TBEAM__CONNECTION_TYPE", "fake")
        monkeypatch.setenv("MEDRE_ADAPTER__MESHCORE_TBEAM__ADAPTER_KIND", "fake")

        monkeypatch.setenv("MEDRE_ADAPTER__MESHCORE_LAB__TRANSPORT", "meshcore")
        monkeypatch.setenv("MEDRE_ADAPTER__MESHCORE_LAB__CONNECTION_TYPE", "fake")
        monkeypatch.setenv("MEDRE_ADAPTER__MESHCORE_LAB__ADAPTER_KIND", "fake")

        config = apply_env_overrides(_make_minimal_config())
        builder = RuntimeBuilder(config, tmp_paths)
        adapters: dict[str, object] = {}
        builder._build_adapters(adapters)

        assert "meshcore-tbeam" in adapters
        assert "meshcore-lab" in adapters
        assert adapters["meshcore-tbeam"].adapter_id == "meshcore-tbeam"
        assert adapters["meshcore-lab"].adapter_id == "meshcore-lab"
        assert adapters["meshcore-tbeam"].platform == "meshcore"
        assert adapters["meshcore-lab"].platform == "meshcore"

    def test_meshcore_env_adapter_distinct_instances(
        self, monkeypatch: pytest.MonkeyPatch, tmp_paths: MedrePaths,
    ) -> None:
        """Two env-created MeshCore adapters produce distinct instances."""
        monkeypatch.setenv("MEDRE_ADAPTER__MESHCORE_A__TRANSPORT", "meshcore")
        monkeypatch.setenv("MEDRE_ADAPTER__MESHCORE_A__CONNECTION_TYPE", "fake")
        monkeypatch.setenv("MEDRE_ADAPTER__MESHCORE_A__ADAPTER_KIND", "fake")

        monkeypatch.setenv("MEDRE_ADAPTER__MESHCORE_B__TRANSPORT", "meshcore")
        monkeypatch.setenv("MEDRE_ADAPTER__MESHCORE_B__CONNECTION_TYPE", "fake")
        monkeypatch.setenv("MEDRE_ADAPTER__MESHCORE_B__ADAPTER_KIND", "fake")

        config = apply_env_overrides(_make_minimal_config())
        builder = RuntimeBuilder(config, tmp_paths)
        adapters: dict[str, object] = {}
        builder._build_adapters(adapters)

        # Distinct adapter IDs and distinct objects
        assert adapters["meshcore-a"].adapter_id == "meshcore-a"
        assert adapters["meshcore-b"].adapter_id == "meshcore-b"
        assert adapters["meshcore-a"] is not adapters["meshcore-b"]


# ---------------------------------------------------------------------------
# LXMF env-created adapter runtime tests
# ---------------------------------------------------------------------------


class TestLxmfEnvCreatedRuntime:
    """LXMF adapters created from env can be built by RuntimeBuilder."""

    def test_two_lxmf_env_adapters_build(
        self, monkeypatch: pytest.MonkeyPatch, tmp_paths: MedrePaths,
    ) -> None:
        """Two env-created LXMF adapters build through RuntimeBuilder."""
        monkeypatch.setenv("MEDRE_ADAPTER__LXMF_SENDER__TRANSPORT", "lxmf")
        monkeypatch.setenv("MEDRE_ADAPTER__LXMF_SENDER__CONNECTION_TYPE", "fake")
        monkeypatch.setenv("MEDRE_ADAPTER__LXMF_SENDER__ADAPTER_KIND", "fake")
        monkeypatch.setenv("MEDRE_ADAPTER__LXMF_SENDER__DISPLAY_NAME", "sender")

        monkeypatch.setenv("MEDRE_ADAPTER__LXMF_RECEIVER__TRANSPORT", "lxmf")
        monkeypatch.setenv("MEDRE_ADAPTER__LXMF_RECEIVER__CONNECTION_TYPE", "fake")
        monkeypatch.setenv("MEDRE_ADAPTER__LXMF_RECEIVER__ADAPTER_KIND", "fake")
        monkeypatch.setenv("MEDRE_ADAPTER__LXMF_RECEIVER__DISPLAY_NAME", "receiver")

        config = apply_env_overrides(_make_minimal_config())
        builder = RuntimeBuilder(config, tmp_paths)
        adapters: dict[str, object] = {}
        builder._build_adapters(adapters)

        assert "lxmf-sender" in adapters
        assert "lxmf-receiver" in adapters
        assert adapters["lxmf-sender"].adapter_id == "lxmf-sender"
        assert adapters["lxmf-receiver"].adapter_id == "lxmf-receiver"
        assert adapters["lxmf-sender"].platform == "lxmf"
        assert adapters["lxmf-receiver"].platform == "lxmf"

    def test_lxmf_env_adapter_distinct_instances(
        self, monkeypatch: pytest.MonkeyPatch, tmp_paths: MedrePaths,
    ) -> None:
        """Two env-created LXMF adapters produce distinct instances."""
        monkeypatch.setenv("MEDRE_ADAPTER__LXMF_ALPHA__TRANSPORT", "lxmf")
        monkeypatch.setenv("MEDRE_ADAPTER__LXMF_ALPHA__CONNECTION_TYPE", "fake")
        monkeypatch.setenv("MEDRE_ADAPTER__LXMF_ALPHA__ADAPTER_KIND", "fake")

        monkeypatch.setenv("MEDRE_ADAPTER__LXMF_BETA__TRANSPORT", "lxmf")
        monkeypatch.setenv("MEDRE_ADAPTER__LXMF_BETA__CONNECTION_TYPE", "fake")
        monkeypatch.setenv("MEDRE_ADAPTER__LXMF_BETA__ADAPTER_KIND", "fake")

        config = apply_env_overrides(_make_minimal_config())
        builder = RuntimeBuilder(config, tmp_paths)
        adapters: dict[str, object] = {}
        builder._build_adapters(adapters)

        assert adapters["lxmf-alpha"].adapter_id == "lxmf-alpha"
        assert adapters["lxmf-beta"].adapter_id == "lxmf-beta"
        assert adapters["lxmf-alpha"] is not adapters["lxmf-beta"]
