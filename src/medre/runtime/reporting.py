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

import json
import re
from datetime import datetime, timezone
from typing import Any

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
    # Shutdown drain-timeout abandonment — persisted by MedreApp.stop() when
    # in-flight deliveries remain after the drain deadline expires.  Distinct
    # from the generic shutdown_rejection recorded at capacity-acquire time.
    if "shutdown_drain_timeout" in err:
        return "shutdown_drain_timeout"
    # Route-policy denial — persisted when a target is suppressed by
    # policy configuration.  Detected from "route policy denied" in
    # error text for more specific detail; falls through to generic
    # policy_suppressed when only the failure_kind matches.
    if "route policy denied" in err:
        return "policy_suppressed"
    if failure_kind == "policy_suppressed":
        return "policy_suppressed"
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
    # Meshtastic queue drain cancelled — shutdown or crash while items
    # were enqueued but not yet sent.  Distinguished from queue-full
    # rejection by the "drain cancelled" / "queue abandoned" phrasing.
    if "queue drain cancelled" in err or "queue abandoned" in err:
        return "meshtastic_queue_drain_cancelled"
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
# Capability-evidence derivation
# ---------------------------------------------------------------------------


def _derive_capability_evidence(
    error: str | None,
    rendering_evidence: str | None,
    failure_kind: str | None,
    status: str,
) -> dict[str, Any]:
    """Derive structured capability-suppression fields from receipt data.

    Extracts ``suppression_reason``, ``capability_field``,
    ``capability_level``, and ``delivery_strategy`` from the receipt's
    ``error`` text and/or ``rendering_evidence`` JSON **without** requiring
    storage schema changes.

    Resolution order:

    1. If ``rendering_evidence`` contains valid JSON with capability fields,
       those values are used directly.
    2. If ``status == "suppressed"`` and ``error`` matches known capability
       suppression patterns, the fields are parsed from the error text.
    3. Otherwise, fields are ``None``.

    Returns a dict with keys ``suppression_reason``, ``capability_field``,
    ``capability_level``, ``delivery_strategy`` (all possibly ``None``).
    """
    result: dict[str, Any] = {
        "suppression_reason": None,
        "capability_field": None,
        "capability_level": None,
        "delivery_strategy": None,
    }

    # 1. Try rendering_evidence JSON first (sent/queued receipts).
    if rendering_evidence is not None:
        try:
            ev = json.loads(rendering_evidence)
            if isinstance(ev, dict):
                result["capability_level"] = ev.get("capability_level")
                result["delivery_strategy"] = ev.get("delivery_strategy")
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

    # 2. For suppressed receipts, derive from error text patterns.
    if status == "suppressed" and error:
        # Pattern: "capability_suppressed: {reason}"
        cap_match = re.match(r"^capability_suppressed:\s*(.+)$", error)
        if cap_match:
            reason_text = cap_match.group(1).strip()
            result["suppression_reason"] = reason_text
            # Extract capability_field from reason: first word before
            # " unsupported" or " fallback".
            field_match = re.match(r"^(\w+)\s+(unsupported|fallback)\b", reason_text)
            if field_match:
                result["capability_field"] = field_match.group(1)
                level = field_match.group(2)
                result["capability_level"] = level
                result["delivery_strategy"] = (
                    "skip" if level == "unsupported" else "fallback_text"
                )
            else:
                # Fallback: set level from failure_kind.
                if failure_kind == "capability_suppressed":
                    result["capability_level"] = "unsupported"
                    result["delivery_strategy"] = "skip"
        elif error.startswith("plan_skip:") or error.startswith("delivery_skipped:"):
            result["suppression_reason"] = error
            result["delivery_strategy"] = "skip"
            if failure_kind == "capability_suppressed":
                result["capability_level"] = "unsupported"
        elif failure_kind == "loop_suppressed":
            result["suppression_reason"] = error
            result["capability_level"] = None
            result["delivery_strategy"] = None
        elif failure_kind == "policy_suppressed":
            result["suppression_reason"] = error
        else:
            # Generic suppressed receipt with unknown failure_kind.
            result["suppression_reason"] = error

    return result


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
    # Use getattr for optional enrichment fields that may be absent on
    # minimal test receipts or focused helper structs.
    _next_retry_at: datetime | None = getattr(receipt, "next_retry_at", None)
    _retry_max_attempts: int | None = getattr(receipt, "retry_max_attempts", None)
    _retry_backoff_base: float | None = getattr(receipt, "retry_backoff_base", None)
    _retry_max_delay: float | None = getattr(receipt, "retry_max_delay", None)
    _retry_jitter: bool | None = getattr(receipt, "retry_jitter", None)
    _parent_receipt_id: str | None = getattr(receipt, "parent_receipt_id", None)
    _replay_run_id: str | None = getattr(receipt, "replay_run_id", None)
    _rendering_evidence: str | None = getattr(receipt, "rendering_evidence", None)
    fk_detail: str | None = _derive_failure_kind_detail(
        receipt.failure_kind,
        receipt.error,
    )
    retryable: bool = _compute_retryable(
        receipt.failure_kind,
        receipt.status,
        _next_retry_at,
    )
    cap_evidence: dict[str, Any] = _derive_capability_evidence(
        receipt.error,
        _rendering_evidence,
        receipt.failure_kind,
        receipt.status,
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
        # Capability-evidence fields (derived from error/rendering_evidence).
        "suppression_reason": cap_evidence["suppression_reason"],
        "capability_field": cap_evidence["capability_field"],
        "capability_level": cap_evidence["capability_level"],
        "delivery_strategy": cap_evidence["delivery_strategy"],
    }
