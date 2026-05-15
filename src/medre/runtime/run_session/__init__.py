"""Run-session package: complete operate→send→inspect→stop→diagnose workflow.

Re-exports :func:`run_bridge_session` (the primary public API),
:func:`scenario_category` (public scenario classification helper),
and :data:`DEFAULT_INGRESS_MODE` (default ingress mode constant).
"""

from .orchestration import DEFAULT_INGRESS_MODE, run_bridge_session
from .scenario import scenario_category

__all__ = [
    "run_bridge_session",
    "scenario_category",
    "DEFAULT_INGRESS_MODE",
]
