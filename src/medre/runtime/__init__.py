"""Runtime construction layer for the MEDRE framework.

Public symbols
--------------
* :class:`~medre.runtime.errors.RuntimeError` — base runtime exception
* :class:`~medre.runtime.errors.RuntimeConfigError` — config issues
* :class:`~medre.runtime.errors.RuntimeStartupError` — startup failures
* :class:`~medre.runtime.errors.RuntimeShutdownError` — shutdown failures
* :class:`~medre.runtime.errors.AdapterStartupError` — adapter start failures
* :class:`~medre.runtime.app.MedreApp` — runtime container with lifecycle
* :class:`~medre.runtime.builder.RuntimeBuilder` — constructs MedreApp from config
* :class:`~medre.runtime.builder.AdapterBuildFailure` — adapter construction failure record
* :class:`~medre.runtime.routes.RouteDirectionality` — route flow direction
* :class:`~medre.runtime.routes.BridgePolicy` — static route allowlist policy
* :class:`~medre.runtime.routes.RouteConfig` — single named route definition
* :class:`~medre.runtime.routes.RouteConfigSet` — ordered, validated route collection
"""

from medre.runtime.app import MedreApp
from medre.runtime.builder import AdapterBuildFailure, RuntimeBuilder
from medre.runtime.errors import (
    AdapterStartupError,
    RuntimeConfigError,
    RuntimeError,
    RuntimeShutdownError,
    RuntimeStartupError,
)
from medre.runtime.routes import (
    BridgePolicy,
    RouteConfig,
    RouteConfigSet,
    RouteDirectionality,
)

__all__ = [
    "RuntimeError",
    "RuntimeConfigError",
    "RuntimeStartupError",
    "RuntimeShutdownError",
    "AdapterStartupError",
    "MedreApp",
    "RuntimeBuilder",
    "AdapterBuildFailure",
    "BridgePolicy",
    "RouteConfig",
    "RouteConfigSet",
    "RouteDirectionality",
]
