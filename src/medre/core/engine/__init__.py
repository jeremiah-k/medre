"""Core engine package for the medre.

This package provides the pipeline orchestration layer that wires together
storage, routing, planning, and adapter delivery:

* :class:`PipelineRunner` -- orchestrates the full event pipeline.
* :class:`PipelineConfig` -- configuration dataclass for the runner.
* :class:`PipelinePhase` -- enumeration of the six implemented pipeline stages.

Replay runtime lives in :mod:`medre.core.engine.replay` (now a package).
Import explicitly from submodules, e.g.
``from medre.core.engine.replay.engine import ReplayEngine``.
"""

from medre.core.engine.phases import PipelinePhase
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner

__all__ = [
    "PipelineConfig",
    "PipelinePhase",
    "PipelineRunner",
]
