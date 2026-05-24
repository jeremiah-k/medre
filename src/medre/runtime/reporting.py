"""Centralised report-schema helpers for native-ref and delivery-receipt dicts.

Provides two shared functions that construct canonical report dictionaries
from :class:`NativeMessageRef` and :class:`DeliveryReceipt` structs.
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

from medre.core.events.canonical import DeliveryReceipt, NativeMessageRef
from medre.core.observability.sanitization import sanitize_error


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


def _derive_failure_kind_detail(
    failure_kind: str | None,
    error: str | None,
    target_adapter: str,  # noqa: ARG001 – kept for API compatibility
) -> str | None:
    """Derive a conservative *failure_kind_detail* from error context.

    Produces a more specific classification without changing the
    :class:`~medre.core.planning.delivery_plan.DeliveryFailureKind` enum.
    Returns the original ``failure_kind`` when no specialised pattern
    matches, or ``None`` when ``failure_kind`` itself is ``None``.

    Patterns:

    * ``"e2ee_blocked"`` — Matrix E2EE decryption or blocking errors
      (Matrix-specific patterns only; generic "encrypted" alone does
      not match).
    * ``"meshtastic_queue_rejected"`` — Queue-full or enqueue-rejected
      error patterns (detected from error text alone; no requirement
      for "meshtastic" in adapter ID or error text).
    * Otherwise — same as ``failure_kind``.
    """
    if not failure_kind:
        return None
    err = (error or "").lower()
    # E2EE / encrypted blocking (Matrix adapters).
    # Tightened to Matrix-specific patterns only — generic "encrypted"
    # alone is insufficient (e.g. "encrypted packet" is not E2EE).
    if any(
        s in err
        for s in (
            "e2ee",
            "megolm",
            "olm session",
            "unable to decrypt",
            "crypto is not active",
            "matrix room is encrypted",
            "room is encrypted but e2ee",
        )
    ):
        return "e2ee_blocked"
    # Meshtastic queue-full / rejection — detect from error text alone.
    # No requirement for "meshtastic" in error or target_adapter so
    # adapters with non-standard IDs (e.g. radio/mesh/test-full) are
    # still recognised.
    if ("queue" in err and "full" in err) or "enqueue rejected" in err:
        return "meshtastic_queue_rejected"
    # Meshtastic outbound gate suppression — listen_only mode.
    if "outbound suppressed" in err and "listen_only" in err:
        return "meshtastic_outbound_suppressed"
    # Default: preserve the original failure_kind.
    return failure_kind


def _compute_retryable(
    failure_kind: str | None,
    status: str,
    next_retry_at: datetime | None,
) -> bool:
    """Determine whether a receipt represents a retryable delivery state.

    Rules (evaluated in order; first match wins):

    * ``status == "dead_lettered"`` → ``False`` (terminal).
    * ``status == "suppressed"`` → ``False`` (terminal).
    * ``next_retry_at is not None`` → ``True`` (scheduled retry).
    * ``status == "failed"`` and ``failure_kind == "adapter_transient"``
      → ``True``.
    * Everything else → ``False``.
    """
    if status == "dead_lettered":
        return False
    if status == "suppressed":
        return False
    if next_retry_at is not None:
        return True
    if status == "failed" and failure_kind == "adapter_transient":
        return True
    return False


# ---------------------------------------------------------------------------
# Delivery receipt report dict
# ---------------------------------------------------------------------------


def delivery_receipt_to_report_dict(
    receipt: DeliveryReceipt,
) -> dict[str, object]:
    """Build a canonical report dict from a :class:`DeliveryReceipt`.

    Canonical keys: ``receipt_id``, ``event_id``, ``delivery_plan_id``,
    ``target_adapter``, ``target_channel``, ``native_channel_id``,
    ``native_message_id``, ``status``, ``failure_kind``, ``error``,
    ``attempt_number``, ``route_id``, ``source``.

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
    # Use getattr for enrichment fields so duck-typed receipt fakes that
    # lack newly-added attributes (next_retry_at, retry policy, etc.)
    # remain compatible without raising AttributeError.
    _next_retry_at: datetime | None = getattr(receipt, "next_retry_at", None)
    _retry_max_attempts: int | None = getattr(receipt, "retry_max_attempts", None)
    _retry_backoff_base: float | None = getattr(receipt, "retry_backoff_base", None)
    _retry_max_delay: float | None = getattr(receipt, "retry_max_delay", None)
    _retry_jitter: bool | None = getattr(receipt, "retry_jitter", None)
    _parent_receipt_id: str | None = getattr(receipt, "parent_receipt_id", None)
    fk_detail: str | None = _derive_failure_kind_detail(
        receipt.failure_kind,
        receipt.error,
        receipt.target_adapter,
    )
    retryable: bool = _compute_retryable(
        receipt.failure_kind,
        receipt.status,
        _next_retry_at,
    )
    return {
        # Original keys (unchanged).
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
        # Retry policy fields (from DeliveryReceipt struct).
        # Safe getattr defaults for duck-typed compatibility.
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
    }
