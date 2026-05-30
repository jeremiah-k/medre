"""Replay delivery: envelope wrapping, adapter filtering, and capability filtering."""

from __future__ import annotations

import asyncio
import time
from typing import Any, NamedTuple

from medre.core.engine.replay.helpers import _elapsed_ms
from medre.core.engine.replay.types import ReplayMode, ReplayRequest, ReplayResult
from medre.core.events import CanonicalEvent
from medre.core.planning.capabilities import resolve_adapter_capabilities
from medre.core.planning.capability_decision import resolver as _resolver

# ---------------------------------------------------------------------------
# Capability filter result types
# ---------------------------------------------------------------------------


class _CapabilitySuppressedPlan(NamedTuple):
    """Evidence record for a single capability-suppressed delivery plan.

    Collected during ``_filter_plans_by_capability`` so that replay results
    can preserve the evidence linkage that live delivery produces via
    suppression receipts (delivery_plan_id, target_adapter, reason).
    """

    delivery_plan_id: str
    target_adapter: str | None
    capability_level: str
    capability_field: str | None
    reason: str | None


class _CapabilityFilterResult(NamedTuple):
    """Result of capability-aware plan filtering.

    Attributes
    ----------
    kept:
        Plans that passed capability checks (supported or passthrough).
    suppressed:
        Plans that were suppressed due to unsupported capabilities,
        with full evidence (delivery_plan_id, target_adapter, reason).
    """

    kept: list[Any]
    suppressed: list[_CapabilitySuppressedPlan]


# ---------------------------------------------------------------------------
# Suppressed-evidence builder
# ---------------------------------------------------------------------------


def _build_suppressed_evidence(
    suppressed: list[_CapabilitySuppressedPlan],
    run_id: str | None,
) -> dict[str, Any]:
    """Build a dict of suppressed-plan evidence for ReplayResult.output.

    Reused by both the all-suppressed (skipped) path and the mixed
    (passed) path so that evidence shaping stays consistent.
    """
    _suppressed_plan_ids = [s.delivery_plan_id for s in suppressed]
    _suppressed_evidence = [
        {
            "delivery_plan_id": s.delivery_plan_id,
            "target_adapter": s.target_adapter,
            "capability_level": s.capability_level,
            "capability_field": s.capability_field,
            "reason": s.reason,
        }
        for s in suppressed
    ]
    return {
        "capability_suppressed_plans": _suppressed_evidence,
        "delivery_plan_ids": _suppressed_plan_ids,
        "replay_run_id": run_id or None,
        "source": "replay",
    }


# ---------------------------------------------------------------------------
# Delivery envelope
# ---------------------------------------------------------------------------


def _replay_delivery_envelope(receipts: Any) -> dict[str, Any]:
    """Wrap adapter delivery results in a replay delivery envelope.

    The envelope marks the delivery as originating from replay and
    preserves the adapter's original results without promotion:
    queued/best-effort stays queued/best-effort.  Downstream consumers
    can inspect ``output["replay"]`` to distinguish replay deliveries
    from live ones.

    Parameters
    ----------
    receipts:
        The original adapter delivery results (list of receipts,
        :class:`AdapterDeliveryResult` instances, or any other
        pipeline output).

    Returns
    -------
    dict
        Envelope with ``"replay": True`` and ``"adapter_results"`` key.
    """
    return {
        "replay": True,
        "adapter_results": receipts,
    }


# ---------------------------------------------------------------------------
# Plan filtering
# ---------------------------------------------------------------------------


def _filter_plans_by_adapter(
    plans: list[Any],
    target_adapters: list[str],
) -> list[Any]:
    """Filter delivery plans to those targeting adapters in *target_adapters*.

    Accepts both bare plan lists and ``list[tuple[Route, DeliveryPlan]]``
    (as produced when the real pipeline is in use).  Plans that do not
    expose a ``target`` attribute with an ``adapter`` field are passed
    through (conservative: include rather than exclude when the plan
    structure is opaque).
    """
    allowed = set(target_adapters)
    result: list[Any] = []
    for item in plans:
        # Unwrap tuple (Route, DeliveryPlan) if present.
        if isinstance(item, tuple) and len(item) == 2:
            plan = item[1]
        else:
            plan = item
        target = getattr(plan, "target", None)
        adapter = getattr(target, "adapter", None) if target is not None else None
        if adapter is None:
            # Opaque plan structure -- include conservatively.
            result.append(item)
        elif adapter in allowed:
            result.append(item)
    return result


def _filter_plans_by_capability(
    event: CanonicalEvent,
    plans: list[Any],
    adapters: dict[str, Any] | None = None,
) -> _CapabilityFilterResult:
    """Filter delivery plans by target adapter capability.

    For each plan, resolves the target adapter's capabilities and checks
    whether the event kind is supported.  Plans with unsupported event
    kinds are excluded from ``kept`` but recorded in ``suppressed`` with
    full evidence (delivery_plan_id, target_adapter, capability_level,
    capability_field, reason).

    When *adapters* is ``None``, all plans are included conservatively
    and ``suppressed`` is empty.

    Only meaningful for BEST_EFFORT mode; the caller is responsible for
    gating on mode.

    Parameters
    ----------
    event:
        The canonical event being replayed.
    plans:
        Delivery plans to filter.
    adapters:
        Mapping of adapter ID to adapter instance, or ``None`` when
        unavailable (in which case all plans are included).

    Returns
    -------
    _CapabilityFilterResult
        Named tuple with ``kept`` (supported plans) and ``suppressed``
        (unsupported plan evidence records).
    """
    if adapters is None:
        return _CapabilityFilterResult(kept=list(plans), suppressed=[])

    # Cache capability resolution by adapter ID within this call scope.
    # Replay can process many plans targeting the same adapter; resolving
    # capabilities is deterministic per adapter so caching is safe.
    _caps_cache: dict[str, Any] = {}
    _decision_cache: dict[tuple[str, str], Any] = {}

    kept: list[Any] = []
    suppressed: list[_CapabilitySuppressedPlan] = []
    for item in plans:
        # Unwrap tuple (Route, DeliveryPlan) if present.
        if isinstance(item, tuple) and len(item) == 2:
            plan = item[1]
        else:
            plan = item

        target = getattr(plan, "target", None)
        if target is None:
            # Opaque plan structure -- include conservatively.
            kept.append(item)
            continue

        adapter_name = getattr(target, "adapter", None)
        if adapter_name is not None and adapter_name in _caps_cache:
            caps = _caps_cache[adapter_name]
        else:
            caps = resolve_adapter_capabilities(adapters, target)
            if adapter_name is not None:
                _caps_cache[adapter_name] = caps
        if caps is None:
            # Adapter is missing from the registry --- include conservatively
            # rather than suppressing based on default (all-false) caps.
            kept.append(item)
            continue

        # Cache capability decision by (adapter_name, event_kind) pair.
        decision_key = (adapter_name, event.event_kind)
        if decision_key in _decision_cache:
            decision = _decision_cache[decision_key]
        else:
            decision = _resolver.decide(event, caps, target_adapter=adapter_name)
            _decision_cache[decision_key] = decision
        if decision.supported:
            kept.append(item)
        else:
            # Capability-suppressed --- record evidence.
            plan_id = getattr(plan, "plan_id", "")
            suppressed.append(
                _CapabilitySuppressedPlan(
                    delivery_plan_id=plan_id,
                    target_adapter=adapter_name,
                    capability_level=decision.capability_level,
                    capability_field=decision.capability_field,
                    reason=decision.reason,
                )
            )
    return _CapabilityFilterResult(kept=kept, suppressed=suppressed)


# ---------------------------------------------------------------------------
# Replay delivery mixin
# ---------------------------------------------------------------------------


class _ReplayDeliveryMixin:
    """Mixin providing the replay delivery stage.

    Expects the host class (via MRO) to provide:

    * ``self._pipeline``           – the delivery pipeline (or ``None``)
    * ``self._diagnostician``      – diagnostic recorder (or ``None``)
    * ``self._capacity_controller`` – capacity / shutdown guard (or ``None``)
    * ``self._accounting``         – accounting recorder (or ``None``)
    """

    async def _stage_deliver(
        self,
        event: CanonicalEvent,
        plan_result: list[Any] | None,
        request: ReplayRequest,
    ) -> ReplayResult:
        """Execute delivery plans for *event*.

        This is the **only** stage with side effects -- it delivers to
        adapters.  Only executed in :attr:`ReplayMode.BEST_EFFORT` mode.
        In :attr:`ReplayMode.DRY_RUN` mode the delivery is suppressed
        and the result is ``"skipped"``.

        Delivery metadata honesty: the output wraps adapter results in a
        replay delivery envelope that marks the delivery as originating
        from replay.  The adapter's original result is preserved as-is;
        queued / best-effort results are **not** promoted to delivered /
        final.  Downstream consumers can inspect ``output["replay"]``
        to distinguish replay deliveries from live ones.
        """
        t0 = time.monotonic()
        mode = request.mode

        # DRY_RUN mode: suppress delivery, always skip.
        if mode is ReplayMode.DRY_RUN:
            return ReplayResult(
                event_id=event.event_id,
                stage="deliver",
                status="skipped",
                error="dry_run: delivery suppressed",
                duration_ms=_elapsed_ms(t0),
            )

        if plan_result is None:
            return ReplayResult(
                event_id=event.event_id,
                stage="deliver",
                status="skipped",
                error="No delivery plans available; planning may have errored",
                duration_ms=_elapsed_ms(t0),
            )
        if self._pipeline is None:
            return ReplayResult(
                event_id=event.event_id,
                stage="deliver",
                status="error",
                error="No pipeline configured; delivery requires a pipeline",
                duration_ms=_elapsed_ms(t0),
            )

        # Filter plans by target_adapters if specified.
        if request.target_adapters is not None:
            filtered = _filter_plans_by_adapter(
                plan_result,
                request.target_adapters,
            )
            if not filtered:
                if self._diagnostician is not None:
                    self._diagnostician.record_replay_skip(
                        event.event_id,
                        "No delivery plans matched target_adapters filter",
                    )
                return ReplayResult(
                    event_id=event.event_id,
                    stage="deliver",
                    status="skipped",
                    error="No delivery plans matched target_adapters filter",
                    duration_ms=_elapsed_ms(t0),
                )
            plan_result = filtered

        # Capability-aware skip: for BEST_EFFORT mode, check if the
        # event kind is supported by the target adapter.  Skip delivery
        # with a descriptive error when the adapter lacks the required
        # capability.  Non-BEST_EFFORT modes are not affected.
        _suppressed: list[_CapabilitySuppressedPlan] = []
        if mode is ReplayMode.BEST_EFFORT:
            # Extract adapters dict from the pipeline collaborator.
            _adapters: dict[str, Any] | None = None
            if self._pipeline is not None:
                _cfg = getattr(self._pipeline, "_config", None)
                if _cfg is not None:
                    _adapters = getattr(_cfg, "adapters", None)
            _cap_result = _filter_plans_by_capability(
                event,
                plan_result,
                _adapters,
            )
            plan_result = _cap_result.kept
            _suppressed = _cap_result.suppressed
            if _suppressed and self._accounting is not None:
                for _ in _suppressed:
                    self._accounting.record_capability_suppressed()
            if not plan_result:
                # Build enriched output with suppressed plan evidence
                # so that delivery_plan_id / replay_run_id / reason are
                # preserved (parity with live Phase 2.5 suppression
                # receipts).
                return ReplayResult(
                    event_id=event.event_id,
                    stage="deliver",
                    status="skipped",
                    error=(
                        f"capability_suppressed: {event.event_kind} "
                        f"not supported by target adapter(s)"
                    ),
                    output=_build_suppressed_evidence(_suppressed, request.run_id),
                    duration_ms=_elapsed_ms(t0),
                )

        # Capacity guard: acquire replay slot for BEST_EFFORT delivery.
        _capacity_acquired = False
        if self._capacity_controller is not None and mode is ReplayMode.BEST_EFFORT:
            acquired = await self._capacity_controller.acquire_replay()
            if not acquired:
                if self._accounting is not None:
                    self._accounting.record_capacity_rejection()
                replay_error = (
                    "replay_rejected_shutdown"
                    if not self._capacity_controller.accepting_work
                    else "replay_capacity_exceeded"
                )
                return ReplayResult(
                    event_id=event.event_id,
                    stage="deliver",
                    status="error",
                    error=replay_error,
                    duration_ms=_elapsed_ms(t0),
                )
            _capacity_acquired = True

        try:
            # Detect real pipeline by data format: if plan_result contains
            # (Route, DeliveryPlan) tuples, use deliver_to_targets.  This
            # avoids false-positives from AsyncMock which auto-creates every
            # attribute (making hasattr unreliable).
            _has_route_plan_pairs = (
                bool(plan_result)
                and isinstance(plan_result[0], tuple)
                and len(plan_result[0]) == 2
                and hasattr(plan_result[0][1], "target")
                and hasattr(plan_result[0][1], "plan_id")
            )
            if _has_route_plan_pairs:
                # Real pipeline: plan_result is list[tuple[Route, DeliveryPlan]].
                outcomes = await self._pipeline.deliver_to_targets(
                    event,
                    plan_result,
                    source="replay",
                    replay_run_id=request.run_id or None,
                )
                replay_output = _replay_delivery_envelope(outcomes)
            else:
                # Stub pipeline: plan_result is list[Any] (bare plans).
                receipts = await self._pipeline.deliver(event, plan_result)
                replay_output = _replay_delivery_envelope(receipts)

            # Mixed BEST_EFFORT outcome: some plans delivered, others
            # capability-suppressed.  Merge suppressed-plan evidence
            # into the replay output alongside adapter results.
            if _suppressed:
                replay_output.update(
                    _build_suppressed_evidence(_suppressed, request.run_id)
                )

            return ReplayResult(
                event_id=event.event_id,
                stage="deliver",
                status="passed",
                output=replay_output,
                duration_ms=_elapsed_ms(t0),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._diagnostician is not None:
                self._diagnostician.record_adapter_failure(
                    event.event_id,
                    "replay",
                    str(exc),
                )
            return ReplayResult(
                event_id=event.event_id,
                stage="deliver",
                status="error",
                error=str(exc),
                duration_ms=_elapsed_ms(t0),
            )
        finally:
            if _capacity_acquired and self._capacity_controller is not None:
                await self._capacity_controller.release_replay()
