"""Shared bridge test setup helpers.

Provides factory functions for creating AdapterContext, PipelineConfig,
and minimal packet dicts used by bridge integration tests.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from medre.core.contracts.adapter import AdapterContext
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
from medre.core.events.bus import EventBus
from medre.core.planning import FallbackResolver, RelationResolver
from medre.core.rendering.renderer import RenderingPipeline
from medre.core.rendering.text import TextRenderer
from medre.core.routing import Router
from medre.core.routing.stats import RouteStats
from medre.core.supervision.accounting import RuntimeAccounting
from medre.core.storage.backend import StorageBackend


def make_adapter_context(
    adapter_id: str,
    runner: PipelineRunner,
) -> AdapterContext:
    """Create an AdapterContext wired to a PipelineRunner's ingress handler."""
    return AdapterContext(
        adapter_id=adapter_id,
        event_bus=None,
        publish_inbound=runner.ingress_handler,
        logger=logging.getLogger(f"test.bridge.{adapter_id}"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )


def make_pipeline_config(
    storage: StorageBackend,
    router: Router,
    *,
    adapters: dict[str, Any] | None = None,
    event_bus: EventBus | None = None,
    rendering_pipeline: RenderingPipeline | None = None,
    accounting: RuntimeAccounting | None = None,
    route_stats: RouteStats | None = None,
) -> PipelineConfig:
    """Build a PipelineConfig with standard renderers registered.

    Registers TextRenderer as fallback when *rendering_pipeline* has no
    renderers.
    """
    rp = rendering_pipeline or RenderingPipeline()
    if not rp.status_summary()["renderer_count"]:
        rp.register(TextRenderer(), priority=100)

    return PipelineConfig(
        storage=storage,
        router=router,
        fallback_resolver=FallbackResolver(),
        relation_resolver=RelationResolver(storage=storage),
        adapters=adapters or {},
        event_bus=event_bus or EventBus(),
        rendering_pipeline=rp,
        runtime_accounting=accounting,
        route_stats=route_stats,
    )


def make_text_packet(
    text: str = "hello bridge",
    sender: str = "!node1",
    channel: int = 0,
    packet_id: int = 42,
) -> dict[str, Any]:
    """Minimal Meshtastic text packet dict."""
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


def make_meshcore_packet(
    text: str = "hello meshcore",
    sender: str = "abc123",
    channel: int = 0,
    packet_id: int = 99,
) -> dict[str, Any]:
    """Minimal MeshCore text packet dict."""
    packet: dict[str, Any] = {
        "text": text,
        "pubkey_prefix": sender,
        "sender_timestamp": packet_id,
        "type": "CHAN",
        "txt_type": 0,
    }
    if channel is not None:
        packet["channel_idx"] = channel
    return packet
