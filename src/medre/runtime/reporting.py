"""Centralised report-schema helpers for native-ref and delivery-receipt dicts.

Provides two shared functions that construct canonical report dictionaries
from :class:`NativeMessageRef` and :class:`DeliveryReceipt` structs.
All consumers (trace, evidence, smoke, orchestration) should use these
helpers instead of building dicts manually to prevent schema drift.
"""

from __future__ import annotations

from medre.core.events.canonical import DeliveryReceipt, NativeMessageRef
from medre.core.observability.sanitization import sanitize_error


def native_ref_to_report_dict(
    nref: NativeMessageRef,
    resolved_to_event_id: str | None = None,
) -> dict[str, object]:
    """Build a canonical report dict from a :class:`NativeMessageRef`.

    Canonical keys: ``adapter``, ``native_channel_id``,
    ``native_message_id``, ``direction``, ``resolves_to``.

    Legacy aliases: ``channel`` (same as ``native_channel_id``),
    ``native_id`` (same as ``native_message_id``).

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
        # Legacy aliases
        "channel": nref.native_channel_id or "",
        "native_id": nref.native_message_id,
    }


def delivery_receipt_to_report_dict(
    receipt: DeliveryReceipt,
) -> dict[str, object]:
    """Build a canonical report dict from a :class:`DeliveryReceipt`.

    Canonical keys: ``receipt_id``, ``event_id``, ``delivery_plan_id``,
    ``target_adapter``, ``target_channel``, ``native_channel_id``,
    ``native_message_id``, ``status``, ``failure_kind``, ``error``,
    ``attempt_number``, ``route_id``, ``source``.

    ``native_channel_id`` is populated from ``receipt.target_channel``.
    ``native_message_id`` is populated from ``receipt.adapter_message_id``.
    ``error`` is sanitised via :func:`sanitize_error` when present.
    """
    error_value: str | None = (
        sanitize_error(receipt.error) if receipt.error else receipt.error
    )
    return {
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
    }
