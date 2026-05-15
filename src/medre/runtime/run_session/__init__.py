"""Run-session package: complete operate→send→inspect→stop→diagnose workflow.

Re-exports :func:`run_bridge_session` (the primary public API) and
:func:`_scenario_category` (used by tests for scenario classification).
"""

from .orchestration import run_bridge_session
from .scenario import _scenario_category

__all__ = ["run_bridge_session"]
