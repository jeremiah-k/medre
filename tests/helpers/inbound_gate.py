"""Shared doubles for inbound admission gate tests.

Extracted from ``test_inbound_admission_gate.py`` so other suites (durable
pressure evidence) can reuse the exact harness without test modules
importing each other.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from medre.core.supervision.accounting import RuntimeAccounting
from medre.core.supervision.capacity import CapacityController
from medre.runtime.app import MedreApp as _MedreApp


@dataclass
class LimitsDouble:
    """Minimal limits provider matching the controller's protocol."""

    max_inflight_deliveries: int = 4
    max_inflight_replay_events: int = 4
    delivery_acquire_timeout_seconds: float = 0.05
    max_inflight_inbound_admissions: int = 2
    inbound_admission_timeout_seconds: float = 0.05


class FakeAdmitRunner:
    """Pipeline runner double recording admit_ingress calls."""

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.admitted: list[Any] = []

    async def admit_ingress(
        self,
        event: Any,
        provenance: Any,
        attachment: Any = None,
        attachment_limits: Any = None,
    ) -> Any:
        if self.delay:
            await asyncio.sleep(self.delay)
        self.admitted.append(event)
        return {"admitted": len(self.admitted)}


class GateAppDouble:
    """Minimal MedreApp double reusing the production seam methods."""

    pipeline_runner: Any
    storage: Any = object()
    _capacity_controller: CapacityController | None
    _runtime_accounting: RuntimeAccounting | None = None
    _attachment_limits: Any = None

    # Reuse the production methods so the double cannot drift from the
    # real wiring shape.
    _gate_inbound_admission = _MedreApp._gate_inbound_admission
    _record_inbound_pressure_loss = _MedreApp._record_inbound_pressure_loss
    _flush_inbound_pressure = _MedreApp._flush_inbound_pressure
    _drain_inbound_pressure_flush = _MedreApp._drain_inbound_pressure_flush
    _make_admit_inbound = _MedreApp._make_admit_inbound
    _make_publish_inbound = _MedreApp._make_publish_inbound
