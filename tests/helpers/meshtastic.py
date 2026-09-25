"""Meshtastic test helpers.

Provides factory functions for building MeshtasticConfig, RenderingResult,
and raw text packet dicts used across meshtastic test modules.
"""

from __future__ import annotations

from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.events import DeliveryAttemptProvenance
from medre.core.rendering.renderer import RenderingResult


def make_meshtastic_config(**overrides) -> MeshtasticConfig:
    """Build a MeshtasticConfig with sensible defaults."""
    defaults = dict(adapter_id="mesh-1")
    defaults.update(overrides)
    return MeshtasticConfig(**defaults)


def make_meshtastic_rendering_result(
    event_id: str = "evt-1",
    target_adapter: str = "mesh-1",
    target_channel: str = "0",
    payload: dict | None = None,
    *,
    outbox_id: str | None = "outbox-test",
    attempt_number: int = 1,
) -> RenderingResult:
    """Build a durable-attempt RenderingResult for Meshtastic delivery.

    Meshtastic is a deferred adapter, so production delivery always carries
    immutable attempt provenance before local queue admission.  Pass
    ``outbox_id=None`` only when a test intentionally exercises rejection of
    outbox-less deferred work.
    """
    provenance = (
        DeliveryAttemptProvenance(
            event_id=event_id,
            delivery_plan_id="plan-test",
            target_adapter=target_adapter,
            target_channel=target_channel,
            outbox_id=outbox_id,
            attempt_number=attempt_number,
            source="live",
        )
        if outbox_id is not None
        else None
    )
    return RenderingResult(
        event_id=event_id,
        target_adapter=target_adapter,
        target_channel=target_channel,
        payload=(
            payload
            if payload is not None
            else {"text": "hello mesh", "channel_index": 0}
        ),
        delivery_plan_id=(provenance.delivery_plan_id if provenance else None),
        outbox_id=outbox_id,
        attempt_number=(attempt_number if provenance else None),
        attempt_provenance=provenance,
    )


def make_meshtastic_text_packet(
    text: str = "hello",
    sender: str = "!node1",
    channel: int = 0,
    packet_id: int = 42,
) -> dict:
    """Build a raw Meshtastic text packet dict for inbound simulation."""
    return {
        "fromId": sender,
        "toId": "",
        "channel": channel,
        "id": packet_id,
        "decoded": {
            "portnum": "text_message",
            "text": text,
        },
    }
