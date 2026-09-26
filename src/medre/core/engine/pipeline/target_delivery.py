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
from collections.abc import Awaitable, Mapping
from dataclasses import replace
from datetime import datetime, timezone
from typing import (
    Any,
    Callable,
    Literal,
    cast,
    get_args,
)

from medre.core.contracts.adapter import (
    AdapterCapabilities,
    AdapterContract,
    AdapterPermanentError,
    AdapterSendError,
)
from medre.core.contracts.delivery import AdapterHandoffResult
from medre.core.engine.pipeline.delivery_evidence import DeliveryExecutionEvidence
from medre.core.engine.pipeline.delivery_lifecycle import DeliveryLifecycleService
from medre.core.engine.pipeline.receipt_factory import build_delivery_receipt
from medre.core.events.canonical import (
    CanonicalEvent,
    DeliveryReceipt,
    NativeMessageRef,
)
from medre.core.events.delivery import (
    DeliveryAttemptProvenance,
    DeliverySource,
    normalize_delivery_provenance,
)
from medre.core.observability.correlation import correlation_scope
from medre.core.observability.metrics import Diagnostician
from medre.core.planning.capabilities import resolve_adapter_capabilities
from medre.core.planning.delivery_plan import (
    DeliveryFailureKind,
    DeliveryPlan,
)
from medre.core.planning.relation_binding import mutation_binding_suppression_error
from medre.core.rendering.renderer import CapabilityLevel as _CapLevel
from medre.core.rendering.renderer import (
    DeliveryStrategyMethod,
    RenderingPipeline,
    RenderingResult,
)
from medre.core.routing.models import Route, RouteTarget
from medre.core.storage.backend import StorageBackend

# ---------------------------------------------------------------------------
# Derived constants
# ---------------------------------------------------------------------------

_VALID_CAPABILITY_LEVELS: frozenset[str] = frozenset(get_args(_CapLevel))
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


def _rendering_result_identity_mismatch(
    result: RenderingResult,
    *,
    event_id: str,
    target_adapter: str,
    target_channel: str | None,
) -> str | None:
    """Return why a renderer result contradicts the requested target, if any.

    Renderer output is adapter-facing data, not delivery authority. Validate
    its declared identity before attaching outbox provenance so direct/
    outbox-less calls receive the same fail-closed protection as durable
    attempts. Empty and absent channels share the persistence identity.
    """
    if not isinstance(result, RenderingResult):
        return f"renderer returned {type(result).__name__}, expected RenderingResult"
    expected_channel = None if target_channel in (None, "") else target_channel
    actual_channel = (
        None if result.target_channel in (None, "") else result.target_channel
    )
    if result.event_id != event_id:
        return f"event_id mismatch: renderer={result.event_id!r} expected={event_id!r}"
    if result.target_adapter != target_adapter:
        return (
            "target_adapter mismatch: "
            f"renderer={result.target_adapter!r} expected={target_adapter!r}"
        )
    if actual_channel != expected_channel:
        return (
            "target_channel mismatch: "
            f"renderer={actual_channel!r} expected={expected_channel!r}"
        )
    return None


# ---------------------------------------------------------------------------
# Metadata serialization helper
# ---------------------------------------------------------------------------


def _normalize_mapping(value: Any) -> Any:
    """Copy immutable contract metadata into JSON-native persistence values.

    Adapter hand-off metadata is deep-frozen at the contract boundary. Storage
    models intentionally use plain ``dict``/``list`` values, so this helper
    recursively converts generic mappings and tuple-like frozen arrays without
    mutating the adapter result.
    """
    if isinstance(value, dict):
        return {k: _normalize_mapping(v) for k, v in value.items()}
    # Generic Mapping subclasses (including FrozenDict) recurse into a plain dict.
    if isinstance(value, Mapping):
        return {k: _normalize_mapping(v) for k, v in value.items()}
    # Lists / tuples: recurse into elements in case they contain nested maps.
    # Tuples are normalised to lists because JSON has no tuple type and
    # msgspec.json.encode serialises both as JSON arrays.
    if isinstance(value, (list, tuple)):
        return [_normalize_mapping(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Delivery errors
# ---------------------------------------------------------------------------


class _AdapterDeliveryError(Exception):
    """Raised after adapter-facing failure evidence has been persisted.

    The validated :class:`DeliveryExecutionEvidence` object is the sole
    receipt/failure payload crossing into orchestration. ``original`` is kept
    only for exception classification fallback and diagnostics.
    """

    def __init__(
        self,
        adapter_id: str,
        error: str,
        original: Exception | None = None,
        *,
        evidence: DeliveryExecutionEvidence,
    ) -> None:
        self.adapter_id = adapter_id
        self.error = error
        self.original = original
        self.evidence = evidence
        super().__init__(error)

    @property
    def receipt(self) -> DeliveryReceipt | None:
        """Return primary attempt evidence for diagnostics/tests."""
        return self.evidence.primary_receipt

    @property
    def lifecycle_receipt(self) -> DeliveryReceipt | None:
        """Return lifecycle-authority evidence, when one was produced."""
        return self.evidence.authority_receipt

    @property
    def failure_kind(self) -> DeliveryFailureKind | None:
        """Return the typed failure classification carried by evidence."""
        return self.evidence.failure_kind


class _RendererDeliveryError(Exception):
    """Raised after renderer/planner failure evidence has been persisted."""

    def __init__(
        self,
        adapter_id: str,
        error: str,
        *,
        evidence: DeliveryExecutionEvidence,
    ) -> None:
        self.adapter_id = adapter_id
        self.error = error
        self.evidence = evidence
        super().__init__(error)

    @property
    def receipt(self) -> DeliveryReceipt | None:
        """Return primary evidence for diagnostics/tests."""
        return self.evidence.primary_receipt

    @property
    def failure_kind(self) -> DeliveryFailureKind | None:
        """Return the typed failure classification carried by evidence."""
        return self.evidence.failure_kind


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
    native_ref_persisted_fn:
        Optional callback invoked after a successful outbound native-reference
        write.  The pipeline uses it to repair derived conversation membership
        for children that were waiting on that native identity.  Callback
        failure is observational only after transport acceptance and never
        reclassifies the delivery as failed.
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
        native_ref_persisted_fn: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._adapters = adapters
        self._rendering_pipeline = rendering_pipeline
        self._storage = storage
        self._diagnostician = diagnostician
        self._lifecycle = lifecycle
        self._log = logger
        self._native_ref_persisted_fn = native_ref_persisted_fn

    # -- Public API ---------------------------------------------------------

    async def deliver_execution(
        self,
        event: CanonicalEvent,
        route: Route,
        plan: DeliveryPlan,
        *,
        render_event: CanonicalEvent | None = None,
        previous_receipt: DeliveryReceipt | None = None,
        source: str = "live",
        replay_run_id: str | None = None,
        outbox_id: str | None = None,
        reserved_attempt_number: int | None = None,
    ) -> DeliveryExecutionEvidence:
        """Execute one target and return its validated immutable evidence.

        Expected adapter/renderer failures still raise their typed internal
        exceptions, each carrying the same ``DeliveryExecutionEvidence``
        object. Successful, queued, and lifecycle-only outcomes return the
        evidence directly. This is the orchestration-facing API.
        """
        source, replay_run_id = normalize_delivery_provenance(source, replay_run_id)
        receipt_id = f"rcpt-{uuid.uuid4()}"
        target = plan.target
        with correlation_scope(
            trace_id=event.trace_id,
            event_id=event.event_id,
            conversation_id=event.conversation_id,
            route_id=route.id,
            delivery_plan_id=plan.plan_id,
            target_adapter=target.adapter,
            outbox_id=outbox_id,
            receipt_id=receipt_id,
            source=source,
            replay_run_id=replay_run_id,
        ):
            receipt = await self._deliver_to_target_scoped(
                event,
                route,
                plan,
                render_event=render_event,
                previous_receipt=previous_receipt,
                source=source,
                replay_run_id=replay_run_id,
                outbox_id=outbox_id,
                reserved_attempt_number=reserved_attempt_number,
                _receipt_id=receipt_id,
            )
        if receipt.receipt_kind == "lifecycle":
            return DeliveryExecutionEvidence(authority_receipt=receipt)
        return DeliveryExecutionEvidence(attempt_receipt=receipt)

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
        outbox_id: str | None = None,
        reserved_attempt_number: int | None = None,
    ) -> DeliveryReceipt:
        """Execute one target and return its primary receipt.

        This compatibility surface is retained for retry/replay callers that
        operate on receipt lineage directly. Delivery coordination uses
        :meth:`deliver_execution` so receipt/failure plumbing remains typed.
        """
        evidence = await self.deliver_execution(
            event,
            route,
            plan,
            render_event=render_event,
            previous_receipt=previous_receipt,
            source=source,
            replay_run_id=replay_run_id,
            outbox_id=outbox_id,
            reserved_attempt_number=reserved_attempt_number,
        )
        receipt = evidence.primary_receipt
        if receipt is None:  # Defensive: every normal execution persists evidence.
            raise RuntimeError("target delivery produced no receipt evidence")
        return receipt

    async def _deliver_to_target_scoped(
        self,
        event: CanonicalEvent,
        route: Route,
        plan: DeliveryPlan,
        *,
        render_event: CanonicalEvent | None = None,
        previous_receipt: DeliveryReceipt | None = None,
        source: DeliverySource = "live",
        replay_run_id: str | None = None,
        outbox_id: str | None = None,
        reserved_attempt_number: int | None = None,
        _receipt_id: str | None = None,
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

        Retry is opt-in through the delivery plan. Without a retry policy, a
        failed dispatch is terminal evidence: the failed attempt is paired with
        linked ``dead_lettered`` lifecycle evidence at the same attempt number.
        For retryable failures under a retry policy, a scheduler or manual replay
        re-invokes this method with the ``previous_receipt`` parameter for a later
        dispatch generation.

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
        outbox_id:
            Internal correlation key from the durable outbox item tracking
            this delivery attempt.  Stamped onto the
            :class:`~medre.core.rendering.renderer.RenderingResult` so
            deferred adapters can carry it with local work for exact
            asynchronous feedback correlation. ``None`` when no outbox item was
            created.
        reserved_attempt_number:
            The durable outbox attempt identity for this dispatch. Retry
            workers supply the attempt reserved at dispatch-begin; the
            coordinator supplies the attempt of a directly-created live or
            replay outbox generation. When provided it overrides the
            receipt-lineage attempt number for everything stamped onto this
            dispatch (the rendered result and every receipt it produces) so
            adapter callbacks and receipts carry the exact identity the
            outbox will admit. Receipt lineage (``parent_receipt_id``) is
            still derived from *previous_receipt*. ``None`` is only valid
            when no durable outbox attempt identity exists.

        Returns
        -------
        DeliveryReceipt
            The receipt recording the delivery outcome.
        """
        target = plan.target
        adapter_id = target.adapter
        receipt_id = _receipt_id or f"rcpt-{uuid.uuid4()}"

        # Compute attempt number and parent receipt for lineage.  A reserved
        # attempt identity is authoritative over the lineage computation:
        # the outbox reservation is what callback validators compare
        # against, so the dispatch must carry exactly that number.
        attempt_number, parent_receipt_id = self._lifecycle.compute_attempt_context(
            previous_receipt
        )
        if reserved_attempt_number is not None:
            attempt_number = reserved_attempt_number

        adapter = self._adapters.get(adapter_id) if adapter_id else None

        if adapter is None:
            self._log.warning(
                "Target adapter %r not found; event_id=%s",
                adapter_id,
                event.event_id,
            )
            _missing_error = (
                f"Adapter {adapter_id!r} is not registered - "
                f"check if the adapter was configured and built successfully"
            )
            evidence = await self._persist_failure_evidence(
                event=event,
                route=route,
                plan=plan,
                adapter_id=adapter_id or "",
                receipt_id=receipt_id,
                error=_missing_error,
                failure_kind=DeliveryFailureKind.ADAPTER_MISSING,
                attempt_number=attempt_number,
                parent_receipt_id=parent_receipt_id,
                source=source,
                replay_run_id=replay_run_id,
                outbox_id=outbox_id,
            )
            raise _AdapterDeliveryError(
                adapter_id or "",
                _missing_error,
                evidence=evidence,
            ) from None

        # Check delivery plan deadline.
        now = datetime.now(tz=timezone.utc)
        if plan.deadline is not None and now > plan.deadline:
            evidence = await self._persist_failure_evidence(
                event=event,
                route=route,
                plan=plan,
                adapter_id=adapter_id or "",
                receipt_id=receipt_id,
                error="Delivery deadline exceeded",
                failure_kind=DeliveryFailureKind.DEADLINE_EXCEEDED,
                attempt_number=attempt_number,
                parent_receipt_id=parent_receipt_id,
                source=source,
                replay_run_id=replay_run_id,
                outbox_id=outbox_id,
                created_at=now,
            )
            raise _AdapterDeliveryError(
                adapter_id or "",
                "Delivery deadline exceeded",
                evidence=evidence,
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
        # delivery plan.  The plan carries the capability decision made
        # during Phase 2.5 planning; using it here avoids re-resolving
        # and preserves planning authority.  Defaults to "native" when
        # the plan has no explicit capability_level (e.g. plans
        # constructed outside the normal planning path).
        _plan_cap_level = plan.capability_level
        if _plan_cap_level is None:
            _plan_cap_level = "native"
        if _plan_cap_level not in _VALID_CAPABILITY_LEVELS:
            _invalid_cap_error = (
                f"Unexpected capability_level "
                f"{_plan_cap_level!r} from delivery plan "
                f"(expected one of {sorted(_VALID_CAPABILITY_LEVELS)}) "
                f"for event_kind={event.event_kind!r}"
            )
            self._diagnostician.record_planner_failure(
                event.event_id, _invalid_cap_error
            )
            evidence = await self._persist_failure_evidence(
                event=event,
                route=route,
                plan=plan,
                adapter_id=adapter_id or "",
                receipt_id=receipt_id,
                error=_invalid_cap_error,
                failure_kind=DeliveryFailureKind.PLANNER_FAILURE,
                attempt_number=attempt_number,
                parent_receipt_id=parent_receipt_id,
                source=source,
                replay_run_id=replay_run_id,
                outbox_id=outbox_id,
            )
            raise _RendererDeliveryError(
                adapter_id or "",
                _invalid_cap_error,
                evidence=evidence,
            ) from None
        _capability_level = cast(_CapLevel, _plan_cap_level)

        # Honor the delivery plan's strategy: validate and narrow the
        # method string to a typed DeliveryStrategyMethod before passing
        # it to the rendering pipeline.
        _strategy_method = plan.primary_strategy.method

        if _strategy_method == "skip":
            # Defense-in-depth only: the canonical skip path is in
            # _deliver_single_target() Phase 2.75 which runs BEFORE outbox
            # creation, capacity acquisition, and rendering.  This block
            # handles edge cases where deliver_to_target() is called
            # directly by an external caller.  A plan-level skip is
            # NOT a renderer failure - it is a suppressed delivery.
            _skip_error = (
                f"delivery_skipped: plan strategy is 'skip' "
                f"(event_kind={event.event_kind})"
            )
            _skip_receipt = build_delivery_receipt(
                receipt_id=receipt_id,
                event_id=event.event_id,
                delivery_plan_id=plan.plan_id,
                target_adapter=adapter_id or "",
                target_channel=target.channel,
                route_id=route.id,
                status="suppressed",
                receipt_kind="lifecycle",
                error=_skip_error,
                failure_kind=DeliveryFailureKind.CAPABILITY_SUPPRESSED.value,
                attempt_number=attempt_number,
                parent_receipt_id=parent_receipt_id,
                source=source,
                replay_run_id=replay_run_id,
                outbox_id=outbox_id,
                **self._lifecycle.extract_retry_fields(plan),
            )
            await self._storage.append_receipt(_skip_receipt)
            return _skip_receipt

        # Phase 2.75 (dynamic): relation-target binding gate for mutation
        # events.  A ``message.edited`` / ``message.deleted`` event whose
        # destination fact for its edit/delete relation is NOT
        # ``bound_owned`` must not reach the renderer or the adapter: no
        # native mutation is authorized in this destination, and no
        # fallback ordinary message may be substituted.  The fact was
        # computed by the runner-owned per-target enrichment from stored
        # evidence immediately before this call, so replay and retry
        # attempts re-bind at execution time and fail closed identically.
        # The error string carries the stable reason code from the fact
        # (``relation_target_not_bindable:<status>:<reason>``), distinct
        # from the static ``capability_suppressed:`` reasons above.
        _mutation_suppression_error = mutation_binding_suppression_error(_render_event)
        if _mutation_suppression_error is not None:
            self._log.info(
                "relation_target_not_bindable: suppressing mutation delivery: "
                "event_id=%s target_adapter=%s route_id=%s reason=%s",
                event.event_id,
                adapter_id,
                route.id,
                _mutation_suppression_error,
            )
            _gate_receipt = build_delivery_receipt(
                receipt_id=receipt_id,
                event_id=event.event_id,
                delivery_plan_id=plan.plan_id,
                target_adapter=adapter_id or "",
                target_channel=target.channel,
                route_id=route.id,
                status="suppressed",
                receipt_kind="lifecycle",
                error=_mutation_suppression_error,
                failure_kind=DeliveryFailureKind.CAPABILITY_SUPPRESSED.value,
                attempt_number=attempt_number,
                parent_receipt_id=parent_receipt_id,
                source=source,
                replay_run_id=replay_run_id,
                outbox_id=outbox_id,
                **self._lifecycle.extract_retry_fields(plan),
            )
            await self._storage.append_receipt(_gate_receipt)
            return _gate_receipt

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
            evidence = await self._persist_failure_evidence(
                event=event,
                route=route,
                plan=plan,
                adapter_id=adapter_id or "",
                receipt_id=receipt_id,
                error=_invalid_error,
                failure_kind=DeliveryFailureKind.PLANNER_FAILURE,
                attempt_number=attempt_number,
                parent_receipt_id=parent_receipt_id,
                source=source,
                replay_run_id=replay_run_id,
                outbox_id=outbox_id,
            )
            raise _RendererDeliveryError(
                adapter_id or "",
                _invalid_error,
                evidence=evidence,
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
                source_origin_label=route.source.origin_label,
                target_destination=target.destination,
            )
        except Exception as exc:
            rendering_error = f"Rendering failed: {type(exc).__name__}: {exc}"
            self._diagnostician.record_renderer_failure(
                event.event_id, adapter_id or "", rendering_error
            )
            evidence = await self._persist_failure_evidence(
                event=event,
                route=route,
                plan=plan,
                adapter_id=adapter_id or "",
                receipt_id=receipt_id,
                error=rendering_error,
                failure_kind=DeliveryFailureKind.RENDERER_FAILURE,
                attempt_number=attempt_number,
                parent_receipt_id=parent_receipt_id,
                source=source,
                replay_run_id=replay_run_id,
                outbox_id=outbox_id,
            )
            raise _RendererDeliveryError(
                adapter_id or "",
                rendering_error,
                evidence=evidence,
            ) from None

        # Freeze the exact attempt identity and dispatch mechanism before
        # adapter hand-off. Deferred adapters carry this immutable envelope
        # through local work and asynchronous feedback; scalar fields remain compatibility
        # mirrors only. Direct/outbox-less calls have no durable attempt to bind.
        # Renderer output is untrusted: a result whose identity contradicts the
        # requested delivery raises envelope validation, which must fail
        # through the same evidence path as a rendering failure.
        try:
            render_identity_mismatch = _rendering_result_identity_mismatch(
                rendering_result,
                event_id=event.event_id,
                target_adapter=adapter_id or "",
                target_channel=target.channel,
            )
            if render_identity_mismatch is not None:
                raise ValueError(render_identity_mismatch)
            attempt_provenance = (
                DeliveryAttemptProvenance(
                    event_id=event.event_id,
                    delivery_plan_id=plan.plan_id,
                    target_adapter=adapter_id or "",
                    target_channel=target.channel,
                    outbox_id=outbox_id,
                    attempt_number=attempt_number,
                    source=source,
                    replay_run_id=replay_run_id,
                )
                if outbox_id is not None
                else None
            )
            rendering_result = replace(
                rendering_result,
                delivery_plan_id=plan.plan_id,
                outbox_id=outbox_id,
                attempt_number=attempt_number,
                attempt_provenance=attempt_provenance,
            )
        except (TypeError, ValueError) as exc:
            provenance_error = f"Invalid rendering attempt provenance: {exc}"
            self._diagnostician.record_renderer_failure(
                event.event_id, adapter_id or "", provenance_error
            )
            evidence = await self._persist_failure_evidence(
                event=event,
                route=route,
                plan=plan,
                adapter_id=adapter_id or "",
                receipt_id=receipt_id,
                error=provenance_error,
                failure_kind=DeliveryFailureKind.RENDERER_FAILURE,
                attempt_number=attempt_number,
                parent_receipt_id=parent_receipt_id,
                source=source,
                replay_run_id=replay_run_id,
                outbox_id=outbox_id,
            )
            raise _RendererDeliveryError(
                adapter_id or "",
                provenance_error,
                evidence=evidence,
            ) from None

        # Guard: adapter must expose a callable deliver() method.
        deliver_fn: Callable[..., Any] | None = getattr(adapter, "deliver", None)
        if deliver_fn is None or not callable(deliver_fn):
            no_deliver_error = "Adapter has no deliver() method"
            self._log.warning(
                "Adapter %r has no deliver() method; event_id=%s",
                adapter_id,
                event.event_id,
            )
            evidence = await self._persist_failure_evidence(
                event=event,
                route=route,
                plan=plan,
                adapter_id=adapter_id or "",
                receipt_id=receipt_id,
                error=no_deliver_error,
                failure_kind=DeliveryFailureKind.ADAPTER_PERMANENT,
                attempt_number=attempt_number,
                parent_receipt_id=parent_receipt_id,
                source=source,
                replay_run_id=replay_run_id,
                outbox_id=outbox_id,
            )
            raise _AdapterDeliveryError(
                adapter_id or "",
                no_deliver_error,
                evidence=evidence,
            ) from None

        # Deliver the rendered result via adapter.
        delivery_exc: Exception | None = None
        adapter_result: AdapterHandoffResult | None = None
        try:
            raw_result = await deliver_fn(rendering_result)
            if not isinstance(raw_result, AdapterHandoffResult):
                raise AdapterPermanentError(
                    f"adapter {adapter_id!r} violated the delivery contract: "
                    "deliver() must return AdapterHandoffResult on success"
                )
            adapter_result = raw_result
            if (
                adapter_result.disposition == "deferred"
                and rendering_result.attempt_provenance is None
            ):
                raise AdapterPermanentError(
                    f"adapter {adapter_id!r} violated the delivery contract: "
                    "deferred hand-off requires durable attempt provenance"
                )
            status: Literal["sent", "failed", "queued"] = (
                "queued" if adapter_result.disposition == "deferred" else "sent"
            )
            error: str | None = None
            self._log.info(
                "Delivered: event_id=%s -> adapter=%s plan=%s attempt=%d " "handoff=%s",
                event.event_id,
                adapter_id,
                plan.plan_id,
                attempt_number,
                adapter_result.disposition,
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

        # Normalize the execution result into immutable evidence. Failures
        # persist an attempt receipt and, when terminal, a linked lifecycle
        # receipt at the *same* attempt number. Successful immediate/deferred hand-off
        # produces attempt evidence only.
        _classified_failure_kind: DeliveryFailureKind | None = None
        if status == "failed" and delivery_exc is not None:
            _classified_failure_kind = self._lifecycle.classify_failure(
                delivery_exc,
                adapter_registered=True,
            )

        now_persist = datetime.now(tz=timezone.utc)
        _retry_after_seconds = (
            delivery_exc.retry_after_seconds
            if isinstance(delivery_exc, AdapterSendError)
            else None
        )
        _next_retry_at: datetime | None = self._lifecycle.compute_next_retry_at(
            status,
            _classified_failure_kind,
            plan,
            attempt_number,
            now_persist,
            retry_after_seconds=_retry_after_seconds,
        )

        if status == "failed":
            failure_kind = (
                _classified_failure_kind or DeliveryFailureKind.ADAPTER_TRANSIENT
            )
            evidence = await self._persist_failure_evidence(
                event=event,
                route=route,
                plan=plan,
                adapter_id=adapter_id or "",
                receipt_id=receipt_id,
                error=error or "Adapter delivery failed",
                failure_kind=failure_kind,
                attempt_number=attempt_number,
                parent_receipt_id=parent_receipt_id,
                source=source,
                replay_run_id=replay_run_id,
                outbox_id=outbox_id,
                next_retry_at=_next_retry_at,
                created_at=now_persist,
            )
            raise _AdapterDeliveryError(
                adapter_id or "",
                error or "Adapter delivery failed",
                delivery_exc,
                evidence=evidence,
            ) from None

        # Successful immediate/deferred hand-off remains dispatch-attempt evidence.
        assert adapter_result is not None
        _adapter_message_id = (
            adapter_result.native_message_id if status == "sent" else None
        )
        _confirmation_level = adapter_result.confirmation_level

        _rendering_evidence: str | None = None
        _raw_evidence = getattr(rendering_result, "rendering_evidence", None)
        if _raw_evidence is not None:
            _rendering_evidence = _serialize_rendering_evidence_for_receipt(
                _raw_evidence
            )
            if _rendering_evidence is None:
                self._log.warning(
                    "rendering_evidence is unsupported type %s; "
                    "persisting receipt without evidence",
                    type(_raw_evidence).__name__,
                )

        receipt = build_delivery_receipt(
            receipt_id=receipt_id,
            event_id=event.event_id,
            delivery_plan_id=plan.plan_id,
            target_adapter=adapter_id or "",
            target_channel=target.channel,
            route_id=route.id,
            status=status,
            adapter_message_id=_adapter_message_id,
            created_at=now_persist,
            attempt_number=attempt_number,
            parent_receipt_id=parent_receipt_id,
            source=source,
            replay_run_id=replay_run_id,
            **self._lifecycle.extract_retry_fields(plan),
            rendering_evidence=_rendering_evidence,
            outbox_id=outbox_id,
            confirmation_level=_confirmation_level,
        )
        await self._storage.append_receipt(receipt)

        # Store native ref mapping (outbound direction) ONLY on success.
        # Use adapter-provided native IDs; never fabricate synthetic IDs.
        if status == "sent" and adapter_result.native_message_id is not None:
            outbound_meta: dict[str, object] = (
                _normalize_mapping(adapter_result.metadata)
                if adapter_result.metadata
                else {}
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
            if self._native_ref_persisted_fn is not None:
                try:
                    await self._native_ref_persisted_fn(event.event_id)
                except Exception:
                    # Native delivery and its durable reference are already
                    # committed. Derived conversation repair is recoverable on
                    # startup and must not convert an accepted send into a
                    # transport failure that could be retried and duplicated.
                    self._log.exception(
                        "Conversation projection repair failed after outbound "
                        "native-ref persistence: event_id=%s",
                        event.event_id,
                    )

        return receipt

    async def _persist_failure_evidence(
        self,
        *,
        event: CanonicalEvent,
        route: Route,
        plan: DeliveryPlan,
        adapter_id: str,
        receipt_id: str,
        error: str,
        failure_kind: DeliveryFailureKind,
        attempt_number: int,
        parent_receipt_id: str | None,
        source: DeliverySource,
        replay_run_id: str | None,
        outbox_id: str | None,
        next_retry_at: datetime | None = None,
        created_at: datetime | None = None,
    ) -> DeliveryExecutionEvidence:
        """Persist attempt evidence and, when terminal, linked lifecycle evidence."""
        attempt_receipt = build_delivery_receipt(
            receipt_id=receipt_id,
            event_id=event.event_id,
            delivery_plan_id=plan.plan_id,
            target_adapter=adapter_id,
            target_channel=plan.target.channel,
            route_id=route.id,
            status="failed",
            error=error,
            failure_kind=failure_kind.value,
            next_retry_at=next_retry_at,
            created_at=created_at,
            attempt_number=attempt_number,
            parent_receipt_id=parent_receipt_id,
            source=source,
            replay_run_id=replay_run_id,
            outbox_id=outbox_id,
            **self._lifecycle.extract_retry_fields(plan),
        )
        await self._storage.append_receipt(attempt_receipt)

        authority_receipt: DeliveryReceipt | None = None
        if self._lifecycle.is_terminal_failure(
            failure_kind,
            next_retry_at=next_retry_at,
        ):
            authority_receipt = self._lifecycle.build_terminal_lifecycle_receipt(
                attempt_receipt,
                status="dead_lettered",
                error=error,
                failure_kind=failure_kind.value,
            )
            # Outbox-backed lifecycle authority is committed atomically with
            # the terminal outbox transition by DeliveryLifecycleService.
            # Outbox-less delivery has no mutable authority pointer, so append
            # the lifecycle evidence here.
            if outbox_id is None:
                await self._storage.append_receipt(authority_receipt)

        return DeliveryExecutionEvidence(
            attempt_receipt=attempt_receipt,
            authority_receipt=authority_receipt,
            failure_kind=failure_kind,
            error=error,
        )

    # -- Internal helpers ---------------------------------------------------

    def _get_adapter_capabilities(self, target: RouteTarget) -> AdapterCapabilities:
        """Retrieve the :class:`AdapterCapabilities` for a target adapter.

        Delegates to :func:`~medre.core.planning.capabilities.resolve_adapter_capabilities`
        with the configured adapter registry.  When the adapter is missing
        from the registry (yields ``None``), falls back to a default
        :class:`AdapterCapabilities` as a conservative internal default
        used only after adapter-missing checks - the pipeline has its own
        adapter-missing check at Phase 2.5.
        """
        caps = resolve_adapter_capabilities(self._adapters, target)
        if caps is None:
            return AdapterCapabilities()
        return caps
