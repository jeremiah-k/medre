"""Centralised report-schema helpers for persisted delivery evidence.

Provides shared functions that construct canonical report dictionaries from
native refs, delivery receipts, and post-handoff delivery observations.
All consumers (trace, evidence, smoke, orchestration) should use these
helpers instead of building dicts manually to prevent schema drift.

Derived helpers:

* :func:`_derive_failure_kind_detail` — conservative, adapter-aware
  enrichment of ``failure_kind`` into a more specific ``failure_kind_detail``
  without changing the :class:`DeliveryFailureKind` enum.
* :func:`_compute_retryable` — determines whether a receipt represents a
  retryable delivery state from ``failure_kind``, ``status``, and
  ``next_retry_at``.
"""

from __future__ import annotations

from datetime import datetime, timezone

from medre.core.events.canonical import (
    DeliveryObservation,
    DeliveryReceipt,
    NativeMessageRef,
)
from medre.core.evidence.capability import (
    CapabilityEvidence,
    derive_capability_evidence,
)
from medre.core.evidence.failure_taxonomy import compute_retryable as _compute_retryable
from medre.core.evidence.failure_taxonomy import (
    derive_failure_kind_detail as _derive_failure_kind_detail,
)
from medre.core.observability.sanitization import sanitize_error, sanitize_for_log


def native_ref_to_report_dict(
    nref: NativeMessageRef,
    resolved_to_event_id: str | None = None,
) -> dict[str, object]:
    """Build a canonical report dict from a :class:`NativeMessageRef`.

    Canonical keys: ``adapter``, ``native_channel_id``,
    ``native_message_id``, ``direction``, ``resolves_to``.

    Short report aliases: ``channel`` (same as
    ``native_channel_id``), ``native_id`` (same as ``native_message_id``).

    Parameters
    ----------
    nref:
        The native message reference to convert.
    resolved_to_event_id:
        If provided, used as the ``resolves_to`` value.
        Falls back to ``nref.event_id`` when ``None``.
    """
    direction_value: str | None = nref.direction or None
    return {
        "adapter": nref.adapter,
        "native_channel_id": nref.native_channel_id or "",
        "native_message_id": nref.native_message_id,
        "direction": direction_value,
        "resolves_to": resolved_to_event_id or nref.event_id,
        # Short report aliases
        "channel": nref.native_channel_id or "",
        "native_id": nref.native_message_id,
    }


def delivery_observation_to_report_dict(
    observation: DeliveryObservation,
) -> dict[str, object]:
    """Build the canonical report dict for one post-handoff observation.

    ``observed_at`` and ``sequence`` are ordering fields owned by the timeline
    or storage surface and are intentionally omitted from the nested data
    payload.  All transport-native evidence remains explicit and separate from
    MEDRE lifecycle receipt state.
    """
    return {
        "observation_id": observation.observation_id,
        "event_id": observation.event_id,
        "delivery_plan_id": observation.delivery_plan_id,
        "target_adapter": observation.target_adapter,
        "target_channel": observation.target_channel,
        "native_channel_id": observation.native_channel_id,
        "outbox_id": observation.outbox_id,
        "attempt_number": observation.attempt_number,
        "adapter_message_id": observation.adapter_message_id,
        "state": observation.state,
        "confirmation_level": observation.confirmation_level,
        "error": (
            sanitize_error(observation.error)
            if observation.error
            else observation.error
        ),
        "metadata": sanitize_for_log(dict(observation.metadata)),
    }


# ---------------------------------------------------------------------------
# Datetime / derivation helpers
# ---------------------------------------------------------------------------


def _to_iso_or_none(dt: datetime | None) -> str | None:
    """Convert a datetime to ISO 8601 or return ``None``."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


# ---------------------------------------------------------------------------
# Capability-evidence derivation
# ---------------------------------------------------------------------------


def _derive_capability_evidence(
    error: str | None,
    failure_kind: str | None,
    status: str,
    *,
    capability_level: str | None = None,
    capability_field: str | None = None,
    capability_reason: str | None = None,
    delivery_strategy: str | None = None,
) -> CapabilityEvidence:
    """Return sanitized report-facing capability evidence for one receipt.

    The core helper owns structured-field derivation; runtime reporting adds
    only the log/evidence sanitization boundary for the operator-facing
    suppression reason.
    """
    result = derive_capability_evidence(
        error,
        failure_kind,
        status,
        capability_level=capability_level,
        capability_field=capability_field,
        capability_reason=capability_reason,
        delivery_strategy=delivery_strategy,
    )
    if result["suppression_reason"] is not None:
        result["suppression_reason"] = sanitize_error(result["suppression_reason"])
    return result


# ---------------------------------------------------------------------------
# Delivery receipt report dict
# ---------------------------------------------------------------------------


def delivery_receipt_to_report_dict(
    receipt: DeliveryReceipt,
) -> dict[str, object]:
    """Build a canonical report dict from a :class:`DeliveryReceipt`.

    Canonical keys: ``sequence``, ``receipt_id``, ``event_id``,
    ``delivery_plan_id``, ``target_adapter``, ``target_channel``,
    ``native_channel_id``, ``native_message_id``, ``status``,
    ``failure_kind``, ``error``, ``attempt_number``, ``route_id``, ``source``.

    Enrichment keys (additive):

    * Retry policy: ``retry_max_attempts``, ``retry_backoff_base``,
      ``retry_max_delay``, ``retry_jitter``, ``next_retry_at``,
      ``parent_receipt_id``.
    * Derived: ``failure_kind_detail``, ``adapter_message_id``,
      ``retryable``.

    ``native_channel_id`` is populated from ``receipt.target_channel``.
    ``native_message_id`` is populated from ``receipt.adapter_message_id``.
    ``error`` is sanitised via :func:`sanitize_error` when present.
    ``failure_kind_detail`` is derived from the raw error text (before
    sanitisation) so that pattern matching works against the original
    message.
    """
    error_value: str | None = (
        sanitize_error(receipt.error) if receipt.error else receipt.error
    )
    # Use getattr for optional enrichment fields that may be absent on
    # minimal test receipts or focused helper structs.
    _next_retry_at: datetime | None = getattr(receipt, "next_retry_at", None)
    _retry_max_attempts: int | None = getattr(receipt, "retry_max_attempts", None)
    _retry_backoff_base: float | None = getattr(receipt, "retry_backoff_base", None)
    _retry_max_delay: float | None = getattr(receipt, "retry_max_delay", None)
    _retry_jitter: bool | None = getattr(receipt, "retry_jitter", None)
    _parent_receipt_id: str | None = getattr(receipt, "parent_receipt_id", None)
    _replay_run_id: str | None = getattr(receipt, "replay_run_id", None)
    fk_detail: str | None = _derive_failure_kind_detail(
        receipt.failure_kind,
        receipt.error,
    )
    retryable: bool = _compute_retryable(
        receipt.failure_kind,
        receipt.status,
        _next_retry_at,
    )
    cap_evidence: CapabilityEvidence = _derive_capability_evidence(
        receipt.error,
        receipt.failure_kind,
        receipt.status,
        # getattr for the same reason as the enrichment fields above: minimal
        # receipt-like doubles may omit them.
        capability_level=getattr(receipt, "capability_level", None),
        capability_field=getattr(receipt, "capability_field", None),
        capability_reason=getattr(receipt, "capability_reason", None),
        delivery_strategy=getattr(receipt, "delivery_strategy", None),
    )
    return {
        # Canonical persisted identity/order fields.
        "sequence": receipt.sequence,
        "receipt_id": receipt.receipt_id,
        "event_id": receipt.event_id,
        "delivery_plan_id": receipt.delivery_plan_id,
        "target_adapter": receipt.target_adapter,
        "target_channel": receipt.target_channel,
        "native_channel_id": receipt.target_channel,
        "native_message_id": receipt.adapter_message_id,
        "status": receipt.status,
        "failure_kind": receipt.failure_kind,
        "error": error_value,
        "attempt_number": receipt.attempt_number,
        "route_id": receipt.route_id,
        "source": receipt.source,
        # Replay context.
        "replay_run_id": _replay_run_id,
        # Retry policy fields (from DeliveryReceipt struct).
        # Tolerant report construction for optional retry fields.
        "retry_max_attempts": _retry_max_attempts,
        "retry_backoff_base": _retry_backoff_base,
        "retry_max_delay": _retry_max_delay,
        "retry_jitter": _retry_jitter,
        "next_retry_at": _to_iso_or_none(_next_retry_at),
        "parent_receipt_id": _parent_receipt_id,
        # Derived enrichment fields.
        "failure_kind_detail": fk_detail,
        "adapter_message_id": receipt.adapter_message_id,
        "retryable": retryable,
        # Capability evidence: structured receipt fields.
        "suppression_reason": cap_evidence["suppression_reason"],
        "capability_field": cap_evidence["capability_field"],
        "capability_level": cap_evidence["capability_level"],
        "delivery_strategy": cap_evidence["delivery_strategy"],
    }
