"""Meshtastic test helpers.

Provides factory functions for building MeshtasticConfig, RenderingResult,
and raw text packet dicts used across meshtastic test modules.
"""

from __future__ import annotations

from medre.config.adapters.meshtastic import MeshtasticConfig
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
) -> RenderingResult:
    """Build a RenderingResult suitable for Meshtastic adapter delivery."""
    return RenderingResult(
        event_id=event_id,
        target_adapter=target_adapter,
        target_channel=target_channel,
        payload=(
            payload
            if payload is not None
            else {"text": "hello mesh", "channel_index": 0}
        ),
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
