"""Target delivery service - owns one-target execution.

This module extracts the single-target delivery logic from
:class:`~medre.core.engine.pipeline.runner.PipelineRunner` into a focused
service class.  :class:`TargetDeliveryService` owns:

* Rendering invocation.
* Adapter lookup / invocation.
* Adapter response normalisation.
* Rendering / adapter failure normalisation.
* Primary single-attempt receipt construction.
* Rendering evidence attachment.
* ``adapter_message_id`` extraction.
* Receipt status determination.

It does **not** own outbox creation, capacity acquisition / release, lease
ownership, retry scheduling, replay processing, route planning, relation
enrichment, or delivery lifecycle management.  Retry decisions, dead-letter
progression, attempt context, and retry lineage are delegated to
:class:`~medre.core.engine.pipeline.delivery_lifecycle.DeliveryLifecycleService`.

Relation enrichment ownership
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Per-target relation enrichment (resolving ``target_event_id`` -> target-adapter
native refs) is owned by :class:`~medre.core.engine.pipeline.runner.PipelineRunner`.
The runner enriches the event before calling this service and passes the
enriched event as the ``render_event`` parameter.  This service receives a
pre-enriched render event and does **not** depend on
:class:`~medre.core.planning.relation_enricher.RelationEnricher`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from typing import (
    Any,
    Callable,
    Literal,
    get_args,
)

from medre.core.contracts.adapter import (
    AdapterCapabilities,
    AdapterContract,
    AdapterDeliveryResult,
)
from medre.core.engine.pipeline.delivery_lifecycle import DeliveryLifecycleService
from medre.core.events.canonical import (
    CanonicalEvent,
    DeliveryReceipt,
    NativeMessageRef,
)
from medre.core.observability.metrics import Diagnostician
from medre.core.planning.capabilities import resolve_adapter_capabilities
from medre.core.planning.capability_decision import resolver as _resolver
from medre.core.planning.delivery_plan import (
    DeliveryFailureKind,
    DeliveryPlan,
)
from medre.core.rendering.renderer import CapabilityLevel as _CapLevel
from medre.core.rendering.renderer import (
    DeliveryStrategyMethod,
    RenderingPipeline,
)
from medre.core.routing.models import Route, RouteTarget
from medre.core.storage.backend import StorageBackend

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Delivery strategy method validation
# ---------------------------------------------------------------------------

#: Mapping from raw strategy strings to their :data:`DeliveryStrategyMethod`
#: typed values.  Used by :func:`_validate_strategy_method` to validate and
#: narrow ``DeliveryStrategy.method`` (typed ``str``) to the strict
#: ``DeliveryStrategyMethod`` literal accepted by
#: :meth:`RenderingPipeline.render`.
_VALID_DELIVERY_STRATEGIES: dict[str, DeliveryStrategyMethod] = {
    m: m for m in get_args(DeliveryStrategyMethod)
}


def _validate_strategy_method(method: str) -> DeliveryStrategyMethod:
    """Validate *method* against known delivery strategy literals.

    Returns the :data:`DeliveryStrategyMethod`-typed value on success so
    callers can pass it directly to :meth:`RenderingPipeline.render`.

    Raises :class:`ValueError` for unknown strategy strings.
    """
    try:
        return _VALID_DELIVERY_STRATEGIES[method]
    except KeyError:
        raise ValueError(f"Unknown delivery strategy method: {method!r}") from None


# ---------------------------------------------------------------------------
# Delivery errors
# ---------------------------------------------------------------------------


class _AdapterDeliveryError(Exception):
    """Raised by ``deliver_to_target`` after persisting a failed receipt.

    Carries the adapter ID, error string, the original exception,
    an optional pre-classified ``failure_kind``, and the persisted
    ``receipt`` so that callers can produce a deterministic
    :class:`DeliveryOutcome` without re-inspecting the exception type
    and can correlate the outbox row with the actual receipt.
    """

    def __init__(
        self,
        adapter_id: str,
        error: str,
        original: Exception | None = None,
        *,
        failure_kind: DeliveryFailureKind | None = None,
        receipt: DeliveryReceipt | None = None,
    ) -> None:
        self.adapter_id = adapter_id
        self.error = error
        self.original = original
        self.failure_kind = failure_kind
        self.receipt = receipt
        super().__init__(error)


class _RendererDeliveryError(Exception):
    """Raised by ``deliver_to_target`` when rendering fails before delivery.

    Carries the adapter ID, error string, and optional persisted
    ``receipt`` so callers can produce a deterministic
    :class:`DeliveryOutcome` and correlate the outbox row.
    """

    def __init__(
        self,
        adapter_id: str,
        error: str,
        *,
        receipt: DeliveryReceipt | None = None,
        failure_kind: DeliveryFailureKind | None = None,
    ) -> None:
        self.adapter_id = adapter_id
        self.error = error
        self.receipt = receipt
        self.failure_kind = failure_kind
        super().__init__(error)


# ---------------------------------------------------------------------------
# Rendering evidence serialisation
# ---------------------------------------------------------------------------


def _serialize_rendering_evidence_for_receipt(
    raw_evidence: object,
) -> str | None:
    """Serialize rendering evidence for attachment to a delivery receipt.

    Accepts:
    - ``str`` - passed through as-is (already serialized).
    - ``dict`` - serialized via ``json.dumps(sort_keys=True)``.
    - Objects with a ``.to_dict()`` method (e.g. :class:`RenderingEvidence`)
      - called and the result serialized.
    - Any other type - returns ``None`` (unsupported).

    Returns ``None`` if serialization fails (e.g. ``to_dict()`` raises),
    so the receipt is persisted without evidence rather than crashing.

    Raises :class:`asyncio.CancelledError` if caught during serialization,
    so task cancellation propagates correctly.
    """
    try:
        if isinstance(raw_evidence, str):
            return raw_evidence
        if isinstance(raw_evidence, dict):
            return json.dumps(raw_evidence, sort_keys=True)
        to_dict = getattr(raw_evidence, "to_dict", None)
        if callable(to_dict):
            return json.dumps(to_dict(), sort_keys=True)
        # Unsupported type - return None without stringifying.
        return None
    except Exception as exc:
        if isinstance(exc, asyncio.CancelledError):
            raise
        # Serialization failed - return None rather than crashing.
        _logger.warning(
            "Failed to serialize rendering evidence of type %s: %s",
            type(raw_evidence).__name__,
            exc,
        )
        return None


# ---------------------------------------------------------------------------
# Target delivery service
# ---------------------------------------------------------------------------


class TargetDeliveryService:
    """Owns single-target delivery execution.

    Coordinates rendering, adapter invocation, receipt creation, and
    native-ref persistence for a single delivery target.  Created and
    called by :class:`~medre.core.engine.pipeline.runner.PipelineRunner`.

    Relation enrichment is performed by the runner *before* calling
    this service.  The caller passes the enriched event as
    ``render_event``; this service does not depend on
    :class:`~medre.core.planning.relation_enricher.RelationEnricher`.

    Lifecycle decisions (retry, dead-letter, attempt context) are
    delegated to
    :class:`~medre.core.engine.pipeline.delivery_lifecycle.DeliveryLifecycleService`.

    Parameters
    ----------
    adapters:
        Mapping of adapter ID to adapter instance.
    rendering_pipeline:
        The rendering pipeline for converting events before delivery.
    storage:
        Storage backend for receipts and native refs.
    diagnostician:
        Failure diagnostic recorder.
    lifecycle:
        The delivery lifecycle service for retry/dead-letter/attempt decisions.
    logger:
        Logger instance.
    """

    def __init__(
        self,
        *,
        adapters: dict[str, AdapterContract],
        rendering_pipeline: RenderingPipeline,
        storage: StorageBackend,
        diagnostician: Diagnostician,
        lifecycle: DeliveryLifecycleService,
        logger: logging.Logger,
    ) -> None:
        self._adapters = adapters
        self._rendering_pipeline = rendering_pipeline
        self._storage = storage
        self._diagnostician = diagnostician
        self._lifecycle = lifecycle
        self._log = logger

    # -- Public API ---------------------------------------------------------

    async def deliver_to_target(
        self,
        event: CanonicalEvent,
        route: Route,
        plan: DeliveryPlan,
        *,
        render_event: CanonicalEvent | None = None,
        previous_receipt: DeliveryReceipt | None = None,
        source: str = "live",
        replay_run_id: str | None = None,
    ) -> DeliveryReceipt:
        """Deliver *event* to a single target adapter and record the receipt.

        Steps:

        1. Look up the target adapter from the config.
        2. Render the event via the rendering pipeline.
        3. Call the adapter's ``deliver`` method.
        4. Record a :class:`DeliveryReceipt` in storage with receipt
           lineage (``attempt_number``, ``parent_receipt_id``).
        5. Store a :class:`NativeMessageRef` mapping.
        6. If the delivery fails and a :class:`RetryPolicy` is configured,
           compute the next retry state.  If retries are exhausted, record
           a ``dead_lettered`` receipt.

        When no retry scheduler is enabled, retry is receipt-level only:
        this method records the failure receipt with ``next_retry_at`` populated.
        A scheduler or manual replay re-invokes this method with the
        ``previous_receipt`` parameter.

        Parameters
        ----------
        event:
            The canonical event to deliver.  Used for receipt identity
            (``event_id``) - the original (non-enriched) event.
        route:
            The route that matched the event.
        plan:
            The delivery plan for this target.
        render_event:
            The pre-enriched event to use for rendering.  When ``None``,
            *event* is used directly (no enrichment applied).  The runner
            is responsible for per-target relation enrichment before
            calling this method.
        previous_receipt:
            The receipt from the previous delivery attempt, if this is a
            retry.  ``None`` for the first attempt.

        Returns
        -------
        DeliveryReceipt
            The receipt recording the delivery outcome.
        """
        target = plan.target
        adapter_id = target.adapter
        receipt_id = f"rcpt-{uuid.uuid4()}"

        # Compute attempt number and parent receipt for lineage.
        attempt_number, parent_receipt_id = self._lifecycle.compute_attempt_context(
            previous_receipt
        )

        adapter = self._adapters.get(adapter_id) if adapter_id else None

        if adapter is None:
            self._log.warning(
                "Target adapter %r not found; event_id=%s",
                adapter_id,
                event.event_id,
            )
            now = datetime.now(tz=timezone.utc)
            receipt = DeliveryReceipt(
                sequence=0,
                receipt_id=receipt_id,
                event_id=event.event_id,
                delivery_plan_id=plan.plan_id,
                target_adapter=adapter_id or "",
                target_channel=target.channel,
                route_id=route.id,
                status="failed",
                error=f"Adapter {adapter_id!r} is not registered in the runtime "
                f"- the adapter may have failed to build or was not configured. "
                f"Check build logs for {adapter_id!r}",
                failure_kind=DeliveryFailureKind.ADAPTER_MISSING.value,
                next_retry_at=None,
                created_at=now,
                attempt_number=attempt_number,
                parent_receipt_id=parent_receipt_id,
                source=source,
                replay_run_id=replay_run_id,
                **self._lifecycle.extract_retry_fields(plan),
            )
            await self._storage.append_receipt(receipt)
            raise _AdapterDeliveryError(
                adapter_id or "",
                f"Adapter {adapter_id!r} is not registered - "
                f"check if the adapter was configured and built successfully",
                failure_kind=DeliveryFailureKind.ADAPTER_MISSING,
                receipt=receipt,
            ) from None

        # Check delivery plan deadline.
        now = datetime.now(tz=timezone.utc)
        if plan.deadline is not None and now > plan.deadline:
            receipt = DeliveryReceipt(
                sequence=0,
                receipt_id=receipt_id,
                event_id=event.event_id,
                delivery_plan_id=plan.plan_id,
                target_adapter=adapter_id or "",
                target_channel=target.channel,
                route_id=route.id,
                status="failed",
                error="Delivery deadline exceeded",
                failure_kind=DeliveryFailureKind.DEADLINE_EXCEEDED.value,
                next_retry_at=None,
                created_at=now,
                attempt_number=attempt_number,
                parent_receipt_id=parent_receipt_id,
                source=source,
                replay_run_id=replay_run_id,
                **self._lifecycle.extract_retry_fields(plan),
            )
            await self._storage.append_receipt(receipt)
            raise _AdapterDeliveryError(
                adapter_id or "",
                "Delivery deadline exceeded",
                failure_kind=DeliveryFailureKind.DEADLINE_EXCEEDED,
                receipt=receipt,
            ) from None

        # Render the event into a RenderingResult before adapter delivery.
        # Pass the adapter's platform so renderers can match on platform
        # identity instead of adapter-name heuristics.
        #
        # The caller (PipelineRunner) is responsible for per-target
        # relation enrichment.  When render_event is provided, it
        # carries target-adapter native refs for structured replies /
        # reactions.  When None, the original event is used as-is.
        _render_event = render_event if render_event is not None else event
        target_platform = getattr(adapter, "platform", None)
        if isinstance(target_platform, str):
            platform_param: str | None = target_platform
        else:
            platform_param = None
        # Resolve adapter capabilities to pass text budgets to renderers.
        _caps = self._get_adapter_capabilities(target)
        _max_text_chars = _caps.max_text_chars
        _max_text_bytes = _caps.max_text_bytes

        # Resolve capability level for rendering context from the
        # capability decision model.  Uses the same resolver as Phase 2.5
        # and replay so live/replay rendering evidence shares one source.
        _cap_decision = _resolver.decide(event, _caps, target_adapter=adapter_id)
        if _cap_decision.capability_level not in ("native", "fallback", "unsupported"):
            raise ValueError(
                f"Unexpected capability_level "
                f"{_cap_decision.capability_level!r} from resolver "
                f"(expected 'native', 'fallback', or 'unsupported')"
            )
        _capability_level: _CapLevel = _cap_decision.capability_level

        # Honor the delivery plan's strategy: validate and narrow the
        # method string to a typed DeliveryStrategyMethod before passing
        # it to the rendering pipeline.
        _strategy_method = plan.primary_strategy.method

        if _strategy_method == "skip":
            # Defense-in-depth only: the canonical skip path is in
            # _deliver_one() Phase 2.75 which runs BEFORE outbox
            # creation, capacity acquisition, and rendering.  This block
            # handles edge cases where deliver_to_target() is called
            # directly (e.g. via _deliver_all).  A plan-level skip is
            # NOT a renderer failure - it is a suppressed delivery.
            _skip_error = (
                f"delivery_skipped: plan strategy is 'skip' "
                f"(event_kind={event.event_kind})"
            )
            now = datetime.now(tz=timezone.utc)
            _skip_receipt = DeliveryReceipt(
                sequence=0,
                receipt_id=receipt_id,
                event_id=event.event_id,
                delivery_plan_id=plan.plan_id,
                target_adapter=adapter_id or "",
                target_channel=target.channel,
                route_id=route.id,
                status="suppressed",
                error=_skip_error,
                failure_kind=DeliveryFailureKind.CAPABILITY_SUPPRESSED.value,
                next_retry_at=None,
                created_at=now,
                attempt_number=attempt_number,
                parent_receipt_id=parent_receipt_id,
                source=source,
                replay_run_id=replay_run_id,
                **self._lifecycle.extract_retry_fields(plan),
            )
            await self._storage.append_receipt(_skip_receipt)
            return _skip_receipt

        # Validate the strategy method against the strict
        # DeliveryStrategyMethod literal type accepted by
        # RenderingPipeline.render().  Unknown methods are pipeline
        # configuration errors - the strategy string is invalid before
        # any rendering is attempted.
        try:
            _validated_strategy: DeliveryStrategyMethod = _validate_strategy_method(
                _strategy_method
            )
        except ValueError:
            _invalid_error = (
                f"Invalid delivery strategy method "
                f"{_strategy_method!r}: not a known strategy"
            )
            self._diagnostician.record_planner_failure(event.event_id, _invalid_error)
            now = datetime.now(tz=timezone.utc)
            receipt = DeliveryReceipt(
                sequence=0,
                receipt_id=receipt_id,
                event_id=event.event_id,
                delivery_plan_id=plan.plan_id,
                target_adapter=adapter_id or "",
                target_channel=target.channel,
                route_id=route.id,
                status="failed",
                error=_invalid_error,
                failure_kind=DeliveryFailureKind.PLANNER_FAILURE.value,
                next_retry_at=None,
                created_at=now,
                attempt_number=attempt_number,
                parent_receipt_id=parent_receipt_id,
                source=source,
                replay_run_id=replay_run_id,
                **self._lifecycle.extract_retry_fields(plan),
            )
            await self._storage.append_receipt(receipt)
            raise _RendererDeliveryError(
                adapter_id or "",
                _invalid_error,
                receipt=receipt,
                failure_kind=DeliveryFailureKind.PLANNER_FAILURE,
            ) from None

        try:
            rendering_result = await self._rendering_pipeline.render(
                _render_event,
                adapter_id or "",
                target.channel,
                target_platform=platform_param,
                max_text_chars=_max_text_chars,
                max_text_bytes=_max_text_bytes,
                delivery_strategy=_validated_strategy,
                capability_level=_capability_level,
            )
        except Exception as exc:
            rendering_error = f"Rendering failed: {type(exc).__name__}: {exc}"
            self._diagnostician.record_renderer_failure(
                event.event_id, adapter_id or "", rendering_error
            )
            now = datetime.now(tz=timezone.utc)
            receipt = DeliveryReceipt(
                sequence=0,
                receipt_id=receipt_id,
                event_id=event.event_id,
                delivery_plan_id=plan.plan_id,
                target_adapter=adapter_id or "",
                target_channel=target.channel,
                route_id=route.id,
                status="failed",
                error=rendering_error,
                failure_kind=DeliveryFailureKind.RENDERER_FAILURE.value,
                next_retry_at=None,
                created_at=now,
                attempt_number=attempt_number,
                parent_receipt_id=parent_receipt_id,
                source=source,
                replay_run_id=replay_run_id,
                **self._lifecycle.extract_retry_fields(plan),
            )
            await self._storage.append_receipt(receipt)
            raise _RendererDeliveryError(
                adapter_id or "",
                rendering_error,
                receipt=receipt,
                failure_kind=DeliveryFailureKind.RENDERER_FAILURE,
            ) from None

        # Stamp the delivery_plan_id onto the rendering result so that
        # queue-based adapters can propagate it through their queue into
        # OutboundNativeRefRecord for deterministic queued→sent receipt
        # correlation.  RenderingResult is frozen; use dataclass replace().
        rendering_result = replace(rendering_result, delivery_plan_id=plan.plan_id)

        # Guard: adapter must expose a callable deliver() method.
        deliver_fn: Callable[..., Any] | None = getattr(adapter, "deliver", None)
        if deliver_fn is None or not callable(deliver_fn):
            no_deliver_error = "Adapter has no deliver() method"
            self._log.warning(
                "Adapter %r has no deliver() method; event_id=%s",
                adapter_id,
                event.event_id,
            )
            now = datetime.now(tz=timezone.utc)
            receipt = DeliveryReceipt(
                sequence=0,
                receipt_id=receipt_id,
                event_id=event.event_id,
                delivery_plan_id=plan.plan_id,
                target_adapter=adapter_id or "",
                target_channel=target.channel,
                route_id=route.id,
                status="failed",
                error=no_deliver_error,
                failure_kind=DeliveryFailureKind.ADAPTER_PERMANENT.value,
                next_retry_at=None,
                created_at=now,
                attempt_number=attempt_number,
                parent_receipt_id=parent_receipt_id,
                source=source,
                replay_run_id=replay_run_id,
                **self._lifecycle.extract_retry_fields(plan),
            )
            await self._storage.append_receipt(receipt)
            raise _AdapterDeliveryError(
                adapter_id or "",
                no_deliver_error,
                failure_kind=DeliveryFailureKind.ADAPTER_PERMANENT,
                receipt=receipt,
            ) from None

        # Deliver the rendered result via adapter.
        delivery_exc: Exception | None = None
        adapter_result: AdapterDeliveryResult | None = None
        try:
            adapter_result = await deliver_fn(rendering_result)

            # Respect the adapter's declared delivery lifecycle state.
            # Queue-based adapters return
            # delivery_status="enqueued" to indicate local acceptance
            # only; synchronous adapters use the default "sent".
            _adapter_delivery_status = (
                getattr(adapter_result, "delivery_status", "sent")
                if adapter_result
                else "sent"
            )
            status: Literal["sent", "failed", "queued"] = (
                "queued" if _adapter_delivery_status == "enqueued" else "sent"
            )
            error: str | None = None
            _log_status = _adapter_delivery_status
            self._log.info(
                "Delivered: event_id=%s -> adapter=%s plan=%s attempt=%d " "status=%s",
                event.event_id,
                adapter_id,
                plan.plan_id,
                attempt_number,
                _log_status,
            )
        except asyncio.CancelledError:
            # CancelledError must propagate directly - never caught and
            # classified as a delivery failure.
            raise
        except Exception as exc:
            status = "failed"
            error = f"{type(exc).__name__}: {exc}"
            delivery_exc = exc
            self._log.exception(
                "Delivery failed: event_id=%s -> adapter=%s attempt=%d",
                event.event_id,
                adapter_id,
                attempt_number,
            )

        # Determine if we need to record a retry or dead-letter receipt.
        # This happens AFTER the main receipt is persisted (below) to
        # maintain correct append ordering. We capture the decision here
        # and execute after the primary receipt.
        _needs_dead_letter = self._lifecycle.should_dead_letter(
            status, plan, attempt_number
        )

        # Record receipt.
        # Classify the failure kind as an enum so we can propagate the same
        # value to both the receipt (as .value string) and the re-raised
        # _AdapterDeliveryError (as the typed enum).
        _classified_failure_kind: DeliveryFailureKind | None = None
        if status == "failed" and delivery_exc is not None:
            _classified_failure_kind = self._lifecycle.classify_failure(
                delivery_exc,
                adapter_registered=True,
            )
        _receipt_failure_kind: str | None = (
            _classified_failure_kind.value if _classified_failure_kind else None
        )

        # Use a fresh persistence-time timestamp for created_at and
        # next_retry_at on the main receipt, rather than the stale
        # execution-start ``now`` captured before enrichment / rendering /
        # adapter I/O.  The start time is no longer used for receipt
        # timestamps in this path.
        now_persist = datetime.now(tz=timezone.utc)

        # Compute next_retry_at for retryable transient failures.
        # Only set when the plan declares an explicit retry_policy.
        _next_retry_at: datetime | None = self._lifecycle.compute_next_retry_at(
            status,
            _classified_failure_kind,
            plan,
            attempt_number,
            now_persist,
        )

        # Populate adapter_message_id only when delivery succeeded and
        # the adapter returned a native_message_id.  Never fabricate IDs.
        _adapter_message_id: str | None = None
        if (
            status == "sent"
            and adapter_result is not None
            and adapter_result.native_message_id is not None
        ):
            _adapter_message_id = adapter_result.native_message_id

        # Serialize rendering evidence from the rendering result into the
        # receipt.  Only attached on successful deliveries (sent / queued);
        # suppressed, skipped, rendering-failure, and adapter-failure receipts
        # naturally leave rendering_evidence=None.
        _rendering_evidence: str | None = None
        if status in ("sent", "queued"):
            _raw_evidence = getattr(rendering_result, "rendering_evidence", None)
            if _raw_evidence is not None:
                _rendering_evidence = _serialize_rendering_evidence_for_receipt(
                    _raw_evidence
                )
                if _rendering_evidence is None and _raw_evidence is not None:
                    self._log.warning(
                        "rendering_evidence is unsupported type %s; "
                        "persisting receipt without evidence",
                        type(_raw_evidence).__name__,
                    )

        receipt = DeliveryReceipt(
            sequence=0,
            receipt_id=receipt_id,
            event_id=event.event_id,
            delivery_plan_id=plan.plan_id,
            target_adapter=adapter_id or "",
            target_channel=target.channel,
            route_id=route.id,
            status=status,
            error=error,
            failure_kind=_receipt_failure_kind,
            adapter_message_id=_adapter_message_id,
            next_retry_at=_next_retry_at,
            created_at=now_persist,
            attempt_number=attempt_number,
            parent_receipt_id=parent_receipt_id,
            source=source,
            replay_run_id=replay_run_id,
            **self._lifecycle.extract_retry_fields(plan),
            rendering_evidence=_rendering_evidence,
        )
        await self._storage.append_receipt(receipt)

        # If all retries exhausted, append dead-letter receipt after
        # the primary receipt to maintain append-only ordering.
        if _needs_dead_letter:
            await self._lifecycle.build_and_persist_dead_letter_receipt(
                self._storage,
                event_id=event.event_id,
                delivery_plan_id=plan.plan_id,
                target_adapter=adapter_id or "",
                previous_receipt_id=receipt_id,
                attempt_number=attempt_number,
                error=error or "Retry exhausted",
                source=source,
                replay_run_id=replay_run_id,
                target_channel=target.channel,
                plan=plan,
            )

        # Store native ref mapping (outbound direction) ONLY on success.
        # Use adapter-provided native IDs; never fabricate synthetic IDs.
        if (
            status == "sent"
            and adapter_result is not None
            and adapter_result.native_message_id is not None
        ):
            # Extract metadata from adapter result, converting
            # MappingProxyType to a plain dict for storage.
            outbound_meta: dict[str, object] = (
                dict(adapter_result.metadata) if adapter_result.metadata else {}
            )
            native_ref = NativeMessageRef(
                id=f"nref-{uuid.uuid4()}",
                event_id=event.event_id,
                adapter=adapter_id or "",
                native_channel_id=adapter_result.native_channel_id,
                native_message_id=adapter_result.native_message_id,
                native_thread_id=adapter_result.native_thread_id,
                native_relation_id=adapter_result.native_relation_id,
                direction="outbound",
                metadata=outbound_meta,
                created_at=now_persist,
            )
            await self._storage.store_native_ref(native_ref)

        # Re-raise adapter errors so that callers (deliver_to_targets)
        # can inspect the exception type for transient/permanent classification.
        # The receipt and native ref are already persisted at this point.
        # Propagate the same failure_kind already persisted in the receipt so
        # the exception and storage do not disagree.
        if status == "failed":
            raise _AdapterDeliveryError(
                adapter_id or "",
                error or "",
                delivery_exc,
                failure_kind=_classified_failure_kind,
                receipt=receipt,
            ) from None

        return receipt

    # -- Internal helpers ---------------------------------------------------

    def _get_adapter_capabilities(self, target: RouteTarget) -> AdapterCapabilities:
        """Retrieve the :class:`AdapterCapabilities` for a target adapter.

        Delegates to :func:`~medre.core.planning.capabilities.resolve_adapter_capabilities`
        with the configured adapter registry.  When the adapter is missing
        from the registry (yields ``None``), falls back to a default
        :class:`AdapterCapabilities` for backward compatibility - the
        pipeline has its own adapter-missing check at Phase 2.5.
        """
        caps = resolve_adapter_capabilities(self._adapters, target)
        if caps is None:
            return AdapterCapabilities()
        return caps
