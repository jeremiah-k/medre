"""Pipeline package - orchestration and single-target delivery.

Submodules
----------
runner
    :class:`PipelineRunner`, :class:`PipelineConfig`, :class:`InflightDelivery`,
    :class:`PhaseSnapshot`, and all pipeline lifecycle / orchestration logic.
target_delivery
    :class:`TargetDeliveryService` — owns single-target delivery execution
    (rendering, adapter invocation, receipt creation).
delivery_lifecycle
    :class:`DeliveryLifecycleService` — owns retry decisions, dead-letter
    progression, attempt context, supplemental receipts, suppression receipts,
    and outbox finalization decisions.
"""

from medre.core.engine.pipeline.delivery_lifecycle import DeliveryLifecycleService
from medre.core.engine.pipeline.runner import (
    InflightDelivery,
    PhaseSnapshot,
    PipelineConfig,
    PipelineRunner,
)
from medre.core.engine.pipeline.target_delivery import TargetDeliveryService

__all__ = [
    "DeliveryLifecycleService",
    "InflightDelivery",
    "PhaseSnapshot",
    "PipelineConfig",
    "PipelineRunner",
    "TargetDeliveryService",
]
