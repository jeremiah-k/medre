"""Shared helper factories for rendering evidence tests.

Provides reusable factory functions for creating canonical events,
rendering contexts, and adapter renderers used across the rendering
evidence test suite.  Import from this module instead of duplicating
factories in each test file.
"""

from __future__ import annotations

from datetime import datetime, timezone

from medre.adapters.lxmf.renderer import LxmfRenderer
from medre.adapters.matrix.renderer import MatrixRenderer
from medre.adapters.meshcore.renderer import MeshCoreRenderer
from medre.adapters.meshtastic.renderer import MeshtasticRenderer
from medre.config.adapters.meshcore import MeshCoreConfig
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.events import (
    CanonicalEvent,
    EventMetadata,
    EventRelation,
)
from medre.core.rendering.renderer import (
    RenderingContext,
)


def make_event(
    event_id: str = "evt-evidence-001",
    payload: dict | None = None,
    relations: tuple[EventRelation, ...] | None = None,
    source_adapter: str = "source-adapter",
) -> CanonicalEvent:
    """Create a minimal canonical event for rendering tests."""
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="transport-1",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=relations or (),
        payload=payload or {"text": "evidence test message"},
        metadata=EventMetadata(),
    )


def make_context(
    target_adapter: str = "target-adapter",
    target_platform: str | None = None,
    delivery_strategy: str = "direct",
    target_channel: str | None = None,
    max_text_bytes: int | None = None,
    max_text_chars: int | None = None,
) -> RenderingContext:
    """Create a RenderingContext with sensible defaults."""
    return RenderingContext(
        delivery_strategy=delivery_strategy,  # type: ignore[arg-type]
        target_adapter=target_adapter,
        target_channel=target_channel,
        target_platform=target_platform,
        max_text_bytes=max_text_bytes,
        max_text_chars=max_text_chars,
    )


def make_meshtastic_renderer(
    adapter_id: str = "mesh-target",
    max_text_bytes: int = 227,
) -> MeshtasticRenderer:
    config = MeshtasticConfig(
        adapter_id=adapter_id,
        max_text_bytes=max_text_bytes,
    )
    return MeshtasticRenderer(configs={adapter_id: config})


def make_meshcore_renderer(
    adapter_id: str = "mc-target",
    max_text_bytes: int = 512,
) -> MeshCoreRenderer:
    config = MeshCoreConfig(
        adapter_id=adapter_id,
        max_text_bytes=max_text_bytes,
    )
    return MeshCoreRenderer(configs={adapter_id: config})


def make_lxmf_renderer() -> LxmfRenderer:
    return LxmfRenderer(metadata_embedding=True)


def make_matrix_renderer() -> MatrixRenderer:
    return MatrixRenderer(source_configs=None)
