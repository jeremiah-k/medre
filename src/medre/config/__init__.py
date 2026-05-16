"""Public API surface for the medre.config package.

Re-exports all configuration types, loaders, and utilities from submodules.
Usage: ``from medre.config import RuntimeConfig, load_config, MedrePaths``

Heavy imports (model, routes, loader, env) are deferred via ``__getattr__``
so that lightweight CLI paths (``--help``, ``version``, ``config sample``)
do not transitively import optional SDK packages (nio, meshtastic, RNS,
LXMF).  The public API is unchanged — ``from medre.config import
RuntimeConfig`` still works, the import is just deferred to first access.
"""
from __future__ import annotations

# -- Lightweight imports (no transitive SDK dependencies) --
from medre.config.paths import MedrePaths, MedrePathsError

# -- errors --
from medre.config.errors import (
    ConfigError,
    ConfigNotFoundError,
    ConfigValidationError,
    ConfigFileError,
)

# -- sample (pure string generation, no SDKs) --
from medre.config.sample import generate_sample_config

# -- Deferred imports via __getattr__ (PEP 562) --
# These modules transitively import adapter configs which trigger optional
# SDK compat guards.  Deferring them keeps lightweight CLI paths SDK-free.

_DEFERRED: dict[str, tuple[str, str]] = {
    # model
    "RuntimeConfig": ("medre.config.model", "RuntimeConfig"),
    "RuntimeOptions": ("medre.config.model", "RuntimeOptions"),
    "RuntimeLimits": ("medre.config.model", "RuntimeLimits"),
    "LoggingConfig": ("medre.config.model", "LoggingConfig"),
    "StorageConfig": ("medre.config.model", "StorageConfig"),
    "AdapterConfigSet": ("medre.config.model", "AdapterConfigSet"),
    "MatrixRuntimeConfig": ("medre.config.model", "MatrixRuntimeConfig"),
    "MeshtasticRuntimeConfig": ("medre.config.model", "MeshtasticRuntimeConfig"),
    "MeshCoreRuntimeConfig": ("medre.config.model", "MeshCoreRuntimeConfig"),
    "LxmfRuntimeConfig": ("medre.config.model", "LxmfRuntimeConfig"),
    # routes
    "BridgePolicy": ("medre.runtime.routes", "BridgePolicy"),
    "RouteConfig": ("medre.runtime.routes", "RouteConfig"),
    "RouteConfigSet": ("medre.runtime.routes", "RouteConfigSet"),
    "RouteDirectionality": ("medre.runtime.routes", "RouteDirectionality"),
    # loader
    "load_config": ("medre.config.loader", "load_config"),
    "find_config": ("medre.config.loader", "find_config"),
    "ConfigSource": ("medre.config.loader", "ConfigSource"),
    # env
    "apply_env_overrides": ("medre.config.env", "apply_env_overrides"),
    "MedreEnvConfig": ("medre.config.env", "MedreEnvConfig"),
}


def __getattr__(name: str) -> object:
    """Lazy-import deferred symbols on first access (PEP 562)."""
    spec = _DEFERRED.get(name)
    if spec is not None:
        import importlib

        mod = importlib.import_module(spec[0])
        value = getattr(mod, spec[1])
        # Cache in module globals so __getattr__ is only called once.
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
