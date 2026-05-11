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

# -- loader (guarded — available after Wave 2 deployment) --
try:
    from medre.config.loader import load_config, find_config, ConfigSource
except ImportError:
    load_config = None  # type: ignore[assignment]
    find_config = None  # type: ignore[assignment]
    ConfigSource = None  # type: ignore[assignment]

# -- env (guarded — available after Wave 2 deployment) --
try:
    from medre.config.env import apply_env_overrides, MedreEnvConfig
except ImportError:
    apply_env_overrides = None  # type: ignore[assignment]
    MedreEnvConfig = None  # type: ignore[assignment]

# -- sample (guarded — available after Wave 2 deployment) --
try:
    from medre.config.sample import generate_sample_config
except ImportError:
    generate_sample_config = None  # type: ignore[assignment]

__all__ = [
    # paths
    "MedrePaths",
    "MedrePathsError",
    # model
    "RuntimeConfig",
    "RuntimeOptions",
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
