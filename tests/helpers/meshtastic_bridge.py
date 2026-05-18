"""Shared helpers for Meshtastic fake-bridge tests."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from medre.core.contracts.adapter import AdapterContext
from medre.core.engine.pipeline import PipelineRunner


def make_text_packet(
    text: str = "hello bridge",
    sender: str = "!node1",
    channel: int = 0,
    packet_id: int = 42,
) -> dict:
    """Minimal Meshtastic text packet for bridge tests."""
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


def make_adapter_context(adapter_id: str, runner: PipelineRunner) -> AdapterContext:
    """Create an AdapterContext wired to a PipelineRunner's ingress handler."""
    return AdapterContext(
        adapter_id=adapter_id,
        event_bus=None,
        publish_inbound=runner.ingress_handler,
        logger=logging.getLogger(f"test.bridge.{adapter_id}"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )
