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
"""

from medre.runtime.errors import (
    AdapterStartupError,
    RuntimeConfigError,
    RuntimeError,
    RuntimeShutdownError,
    RuntimeStartupError,
)
from medre.runtime.app import MedreApp
from medre.runtime.builder import RuntimeBuilder

__all__ = [
    "RuntimeError",
    "RuntimeConfigError",
    "RuntimeStartupError",
    "RuntimeShutdownError",
    "AdapterStartupError",
    "MedreApp",
    "RuntimeBuilder",
]
