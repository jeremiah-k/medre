"""Scenario injection for run-session failure simulation.

Provides functions to inject, classify, and describe failure scenarios
used by :func:`~medre.runtime.run_session.orchestration.run_bridge_session`.
Fake injection stays scoped to this module.  No adapter-level publish
callback or public injection API is exposed.
"""

from __future__ import annotations

from typing import Any

from medre.core.contracts.adapter import (
    AdapterPermanentError,
    AdapterSendError,
)
from medre.runtime.app import MedreApp

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SUPPORTED_SCENARIOS: tuple[str, ...] = (
    "happy_path",
    "renderer_failure",
    "adapter_permanent_failure",
    "adapter_transient_failure",
    "capacity_rejection",
    "degraded_live_health",
)

# ---------------------------------------------------------------------------
# Scenario classification helpers
# ---------------------------------------------------------------------------


def _expected_failure_kind(scenario: str) -> str | None:
    """Return the expected failure classification for a failure-kind scenario."""
    return {
        "renderer_failure": "renderer_failure",
        "adapter_permanent_failure": "adapter_permanent",
        "adapter_transient_failure": "adapter_transient",
        "capacity_rejection": "capacity_rejection",
    }.get(scenario)


def scenario_category(scenario: str) -> str:
    """Return the scenario category for operator categorization.

    Public API for use by tests and report consumers.  Maps scenario
    names to one of: ``"happy_path"``, ``"delivery_failure"``,
    ``"capacity"``, ``"health"``, or ``"unknown"``.
    """
    return {
        "happy_path": "happy_path",
        "renderer_failure": "delivery_failure",
        "adapter_permanent_failure": "delivery_failure",
        "adapter_transient_failure": "delivery_failure",
        "capacity_rejection": "capacity",
        "degraded_live_health": "health",
    }.get(scenario, "unknown")


def _simulation_method(scenario: str) -> str | None:
    """Return the simulation method description for a scenario."""
    return {
        "renderer_failure": "cleared rendering pipeline renderers",
        "adapter_permanent_failure": "monkeypatched adapter deliver method",
        "adapter_transient_failure": "monkeypatched adapter deliver method",
        "capacity_rejection": "exhausted capacity semaphore",
        "degraded_live_health": "monkeypatched adapter health_check",
    }.get(scenario)


def _operator_interpretation(scenario: str) -> str:
    """Return operator-facing guidance for interpreting a scenario result."""
    return {
        "happy_path": (
            "All steps should succeed. If any step fails, investigate "
            "the specific failing component."
        ),
        "renderer_failure": (
            "Event injection should produce a 'failed' receipt with "
            "failure_kind=renderer_failure. The event is stored but "
            "never delivered. No native refs should be created."
        ),
        "adapter_permanent_failure": (
            "Delivery should fail with permanent_failure classification. "
            "A 'failed' receipt is persisted. No native ref for the "
            "failed adapter. Outbound_failed accounting should increment."
        ),
        "adapter_transient_failure": (
            "Delivery should fail with transient_failure classification "
            "and is_retryable=True. A 'failed' receipt is persisted. "
            "No native ref for the failed adapter."
        ),
        "capacity_rejection": (
            "Delivery should be rejected with capacity_rejection "
            "classification. No receipt should be persisted for the "
            "rejected delivery. Capacity_rejections accounting should "
            "increment."
        ),
        "degraded_live_health": (
            "Runtime should observe degraded adapter health without "
            "crashing. Event delivery may still succeed. The snapshot "
            "should reflect degraded health for the target adapter."
        ),
    }.get(scenario, "Unknown scenario.")


# ---------------------------------------------------------------------------
# Scenario injection
# ---------------------------------------------------------------------------


async def _inject_scenario(
    app: MedreApp,
    scenario: str,
    source_aid: str,
    *,
    ingress_mode: str = "direct_pipeline",
) -> str | None:
    """Inject failure conditions for the given scenario.

    Modifies runtime state in-place before event injection.  Returns
    ``None`` on success, or an error description if scenario setup failed.

    The *ingress_mode* parameter is recorded for reference but does not
    affect scenario setup — scenario preparation (clearing renderers,
    monkeypatching adapters, exhausting capacity) is ingress-mode-
    agnostic.  The mode determines how the event is subsequently
    injected (see :func:`run_bridge_session`).
    """
    if scenario == "renderer_failure":
        pipeline = app.pipeline_runner
        if pipeline is None or not hasattr(pipeline, "_rendering_pipeline"):
            return "No rendering pipeline to clear"
        pipeline._rendering_pipeline._renderers.clear()

    elif scenario == "adapter_permanent_failure":
        target_aid: str | None = None
        for aid in sorted(app.adapters.keys()):
            if aid != source_aid:
                target_aid = aid
                break
        if target_aid is None:
            return "No target adapter to patch for permanent failure"
        adapter = app.adapters[target_aid]
        original_deliver = adapter.deliver

        async def _failing_deliver(
            result: Any,
            _orig: Any = original_deliver,
        ) -> Any:
            raise AdapterPermanentError("run-session: simulated permanent failure")

        adapter.deliver = _failing_deliver  # type: ignore[assignment]

    elif scenario == "adapter_transient_failure":
        target_aid = None
        for aid in sorted(app.adapters.keys()):
            if aid != source_aid:
                target_aid = aid
                break
        if target_aid is None:
            return "No target adapter to patch for transient failure"
        adapter = app.adapters[target_aid]
        original_deliver = adapter.deliver

        async def _transient_deliver(
            result: Any,
            _orig: Any = original_deliver,
        ) -> Any:
            raise AdapterSendError(
                "run-session: simulated transient failure",
                transient=True,
            )

        adapter.deliver = _transient_deliver  # type: ignore[assignment]

    elif scenario == "capacity_rejection":
        cc = app._capacity_controller
        if cc is None:
            return "No capacity controller wired"
        # Exhaust the delivery semaphore.
        from medre.config.model import RuntimeLimits
        from medre.runtime.capacity import CapacityController

        small_cc = CapacityController(
            RuntimeLimits(
                max_inflight_deliveries=1,
                max_inflight_replay_events=1,
                delivery_acquire_timeout_seconds=0.1,
            ),
        )
        await small_cc._delivery_sem.acquire()
        small_cc._delivery_current = 1
        app._capacity_controller = small_cc
        app.pipeline_runner._capacity_controller = small_cc

    elif scenario == "degraded_live_health":
        target_aid = None
        for aid in sorted(app.adapters.keys()):
            target_aid = aid
            break
        if target_aid is None:
            return "No adapter available for health patch"
        from medre.core.contracts.adapter import (
            AdapterCapabilities,
            AdapterInfo,
            AdapterRole,
        )

        adapter = app.adapters[target_aid]

        # Preserve original capabilities.
        orig_caps: AdapterCapabilities | None = None
        try:
            info = await adapter.health_check()
            orig_caps = info.capabilities
        except Exception:
            orig_caps = AdapterCapabilities(text=True)

        async def _degraded_health() -> AdapterInfo:
            raw_role = getattr(adapter, "role", "unknown")
            role = (
                raw_role
                if isinstance(raw_role, AdapterRole)
                else AdapterRole.PRESENTATION
            )
            return AdapterInfo(
                adapter_id=adapter.adapter_id,
                platform=getattr(adapter, "platform", "unknown"),
                role=role,
                version="0.1.0",
                capabilities=orig_caps,
                health="degraded",
            )

        adapter.health_check = _degraded_health  # type: ignore[assignment]
        await app.refresh_live_health()

    return None


def _observed_failure_kind(
    outcomes_or_receipts: list[Any],
    use_receipts: bool = False,
) -> str | None:
    """Derive the observed failure classification.

    When ``use_receipts=True``, inspects ``failure_kind`` on receipt
    objects (which store the kind as a string).  Otherwise inspects
    ``DeliveryOutcome`` objects where ``failure_kind`` is an enum with
    a ``.value`` attribute.
    """
    for item in outcomes_or_receipts:
        fk = item.failure_kind
        if fk is not None:
            if use_receipts:
                return str(fk)
            return fk.value if hasattr(fk, "value") else str(fk)
    return None
