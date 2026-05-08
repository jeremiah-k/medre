"""Core engine package for the meshnet framework.

This package provides the pipeline orchestration layer that wires together
storage, routing, planning, and adapter delivery:

* :class:`PipelineRunner` – orchestrates the full event pipeline.
* :class:`PipelineConfig` – configuration dataclass for the runner.
"""

from meshnet_framework.core.engine.pipeline import PipelineConfig, PipelineRunner

__all__ = [
    "PipelineConfig",
    "PipelineRunner",
]
