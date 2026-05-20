"""Runtime diagnostics, health, supervision, and accounting subsystem.

Re-exported symbols
-------------------
* From :mod:`~medre.core.runtime.accounting`:
  ``RuntimeAccounting``, ``RuntimeCounters``.
* From :mod:`~medre.core.runtime.capacity`:
  ``CapacityController``.
* From :mod:`~medre.core.runtime.health`:
  ``VALID_HEALTH_STRINGS``, ``normalize_adapter_health``.
* From :mod:`~medre.core.runtime.supervision`:
  ``RuntimeHealth``, ``AdapterFailureSeverity``, ``StartupOutcome``,
  ``classify_runtime_health``, ``classify_adapter_failure_severity``,
  ``classify_startup_outcome``, ``runtime_supervision_snapshot``.
"""

from .accounting import RuntimeAccounting, RuntimeCounters
from .capacity import CapacityController
from .health import (
    VALID_HEALTH_STRINGS,
    normalize_adapter_health,
)
from .supervision import (
    AdapterFailureSeverity,
    RuntimeHealth,
    StartupOutcome,
    classify_adapter_failure_severity,
    classify_runtime_health,
    classify_startup_outcome,
    runtime_supervision_snapshot,
)

__all__ = [
    # accounting
    "RuntimeAccounting",
    "RuntimeCounters",
    # capacity
    "CapacityController",
    # health
    "VALID_HEALTH_STRINGS",
    "normalize_adapter_health",
    # supervision
    "AdapterFailureSeverity",
    "RuntimeHealth",
    "StartupOutcome",
    "classify_adapter_failure_severity",
    "classify_runtime_health",
    "classify_startup_outcome",
    "runtime_supervision_snapshot",
]
