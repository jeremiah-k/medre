"""Public API surface for the medre.config package.

Re-exports all configuration types, loaders, and utilities from submodules.
Usage: ``from medre.config import RuntimeConfig, load_config, MedrePaths``
"""

from __future__ import annotations

# -- paths --
from medre.config.paths import MedrePaths, MedrePathsError

# -- model --
from medre.config.model import (
    RuntimeConfig,
    RuntimeOptions,
    RuntimeLimits,
    LoggingConfig,
    StorageConfig,
    AdapterConfigSet,
    MatrixRuntimeConfig,
    MeshtasticRuntimeConfig,
    MeshCoreRuntimeConfig,
    LxmfRuntimeConfig,
)

# -- routes (config-level route models) --
from medre.runtime.routes import (
    BridgePolicy,
    RouteConfig,
    RouteConfigSet,
    RouteDirectionality,
)

# -- errors --
from medre.config.errors import (
    ConfigError,
    ConfigNotFoundError,
    ConfigValidationError,
    ConfigFileError,
)

# -- loader --
from medre.config.loader import load_config, find_config, ConfigSource

# -- env --
from medre.config.env import apply_env_overrides, MedreEnvConfig

# -- sample --
from medre.config.sample import generate_sample_config

__all__ = [
    # paths
    "MedrePaths",
    "MedrePathsError",
    # model
    "RuntimeConfig",
    "RuntimeOptions",
    "RuntimeLimits",
    "LoggingConfig",
    "StorageConfig",
    "AdapterConfigSet",
    "MatrixRuntimeConfig",
    "MeshtasticRuntimeConfig",
    "MeshCoreRuntimeConfig",
    "LxmfRuntimeConfig",
    # routes
    "BridgePolicy",
    "RouteConfig",
    "RouteConfigSet",
    "RouteDirectionality",
    # errors
    "ConfigError",
    "ConfigNotFoundError",
    "ConfigValidationError",
    "ConfigFileError",
    # loader
    "load_config",
    "find_config",
    "ConfigSource",
    # env
    "apply_env_overrides",
    "MedreEnvConfig",
    # sample
    "generate_sample_config",
]
