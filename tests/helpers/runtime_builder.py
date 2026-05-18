"""Shared config-builder helpers for runtime builder tests."""

from __future__ import annotations

import pytest

from medre.config.adapters.lxmf import LxmfConfig
from medre.config.adapters.matrix import MatrixConfig
from medre.config.adapters.meshcore import MeshCoreConfig
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


# ---------------------------------------------------------------------------
# Path-env cleanup (call from an autouse fixture in each test module)
# ---------------------------------------------------------------------------


def clean_path_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove MEDRE_HOME and XDG_* env vars so tests start clean."""
    for var in (
        "MEDRE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
    ):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# Config factory helpers
# ---------------------------------------------------------------------------


def make_fake_matrix_config() -> MatrixConfig:
    return MatrixConfig(
        adapter_id="matrix_main",
        homeserver="https://matrix.test",
        user_id="@bot:test",
        access_token="test-tok",
        encryption_mode="plaintext",
    )


def make_fake_meshtastic_config() -> MeshtasticConfig:
    return MeshtasticConfig(
        adapter_id="mesh_radio",
        connection_type="fake",
    ).validate()


def make_fake_meshcore_config() -> MeshCoreConfig:
    return MeshCoreConfig(
        adapter_id="meshcore_node",
        connection_type="fake",
    ).validate()


def make_fake_lxmf_config() -> LxmfConfig:
    return LxmfConfig(
        adapter_id="lxmf_local",
        connection_type="fake",
    ).validate()


def make_all_enabled_config() -> RuntimeConfig:
    """RuntimeConfig with all adapter types enabled (fake connections)."""
    matrix_rt = MatrixRuntimeConfig(
        adapter_id="matrix_main",
        enabled=True,
        config=make_fake_matrix_config(),
    )
    meshtastic_rt = MeshtasticRuntimeConfig(
        adapter_id="mesh_radio",
        enabled=True,
        config=make_fake_meshtastic_config(),
    )
    meshcore_rt = MeshCoreRuntimeConfig(
        adapter_id="meshcore_node",
        enabled=True,
        config=make_fake_meshcore_config(),
    )
    lxmf_rt = LxmfRuntimeConfig(
        adapter_id="lxmf_local",
        enabled=True,
        config=make_fake_lxmf_config(),
    )
    return RuntimeConfig(
        runtime=RuntimeOptions(name="test-builder"),
        logging=LoggingConfig(level="DEBUG"),
        storage=StorageConfig(backend="sqlite"),
        adapters=AdapterConfigSet(
            matrix={"main": matrix_rt},
            meshtastic={"radio": meshtastic_rt},
            meshcore={"node": meshcore_rt},
            lxmf={"local": lxmf_rt},
        ),
    )


def make_disabled_config() -> RuntimeConfig:
    """RuntimeConfig where all adapters are disabled."""
    matrix_rt = MatrixRuntimeConfig(
        adapter_id="matrix_off",
        enabled=False,
        config=make_fake_matrix_config(),
    )
    meshtastic_rt = MeshtasticRuntimeConfig(
        adapter_id="mesh_off",
        enabled=False,
        config=make_fake_meshtastic_config(),
    )
    return RuntimeConfig(
        runtime=RuntimeOptions(),
        logging=LoggingConfig(),
        storage=StorageConfig(),
        adapters=AdapterConfigSet(
            matrix={"off": matrix_rt},
            meshtastic={"off": meshtastic_rt},
        ),
    )


def make_empty_config() -> RuntimeConfig:
    """RuntimeConfig with no adapters at all."""
    return RuntimeConfig(
        runtime=RuntimeOptions(),
        logging=LoggingConfig(),
        storage=StorageConfig(),
    )
