"""Tests for source-only origin_label across all transports.

Verifies:
- mmrelay KEY_MESHNET populated from origin_label (not meshnet_name config)
- Meshtastic→Meshtastic uses source origin_label, not target's
- Matrix→Meshtastic uses Matrix origin_label from source_attribution
- ``{meshnet_name}`` renders as ``{meshnet_name}`` (unknown placeholder,
  passed through unchanged)
- ``{origin_label}`` resolves correctly from source_attribution registry
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from medre.adapters.matrix.renderer import MatrixRenderer
from medre.adapters.meshtastic.renderer import MeshtasticRenderer
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.events import (
    CanonicalEvent,
    EventMetadata,
    EventRelation,
    NativeMetadata,
)
from medre.core.rendering.renderer import RenderingContext
from medre.interop.mmrelay import KEY_MESHNET

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _StubSourceAttribution:
    """Minimal duck-typed SourceAttributionConfig for tests."""

    adapter_id: str = ""
    origin_label: str = ""


@dataclass(slots=True)
class _StubMeshtasticConfig:
    """Minimal duck-typed MeshtasticConfig for Matrix source_configs."""

    adapter_id: str = "radio-alpha"
    matrix_relay_prefix: str = ""
    mmrelay_compatibility: bool = False


def _make_meshtastic_event(
    source_adapter: str = "radio-alpha",
    body: str = "hello mesh",
    relations: tuple | None = None,
    native_data: dict | None = None,
) -> CanonicalEvent:
    metadata = EventMetadata()
    if native_data:
        metadata = EventMetadata(native=NativeMetadata(data=native_data))
    return CanonicalEvent(
        event_id="evt-1",
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="node-1",
        source_channel_id="0",
        parent_event_id=None,
        lineage=(),
        relations=relations or (),
        payload={"body": body},
        metadata=metadata,
    )


def _make_matrix_event(
    source_adapter: str = "matrix-1",
    body: str = "hello from matrix",
    display_name: str = "Alice",
) -> CanonicalEvent:
    native_data: dict[str, object] = {
        "longname": display_name,
        "shortname": display_name.split()[0],
        "from_id": "@alice:example.com",
    }
    return CanonicalEvent(
        event_id="mx-1",
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="@alice:example.com",
        source_channel_id="!room:example.com",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"body": body},
        metadata=EventMetadata(native=NativeMetadata(data=native_data)),
    )


# ===================================================================
# mmrelay KEY_MESHNET populated from origin_label
# ===================================================================


async def test_key_meshnet_from_origin_label() -> None:
    """KEY_MESHNET in mmrelay metadata equals source origin_label."""
    renderer = MatrixRenderer(
        source_configs={
            "radio-alpha": _StubMeshtasticConfig(
                adapter_id="radio-alpha",
                mmrelay_compatibility=True,
            ),
        },
        source_attribution={
            "radio-alpha": _StubSourceAttribution(
                adapter_id="radio-alpha",
                origin_label="East Radio",
            ),
        },
    )
    event = _make_meshtastic_event(
        source_adapter="radio-alpha",
        native_data={"longname": "Node1", "shortname": "N1", "packet_id": "42"},
    )
    result = await renderer.render(
        event,
        RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
    )
    assert result.payload[KEY_MESHNET] == "East Radio"


async def test_key_meshnet_empty_when_no_origin_label() -> None:
    """KEY_MESHNET is empty string when source has no origin_label."""
    renderer = MatrixRenderer(
        source_configs={
            "radio-alpha": _StubMeshtasticConfig(
                adapter_id="radio-alpha",
                mmrelay_compatibility=True,
            ),
        },
    )
    event = _make_meshtastic_event(
        source_adapter="radio-alpha",
        native_data={"longname": "Node1", "packet_id": "42"},
    )
    result = await renderer.render(
        event,
        RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
    )
    assert result.payload[KEY_MESHNET] == ""


# ===================================================================
# Meshtastic→Meshtastic uses source origin_label
# ===================================================================


async def test_uses_source_origin_label_not_target() -> None:
    """Source origin_label appears in prefix, not target config's origin_label."""
    renderer = MeshtasticRenderer(
        configs={
            "radio-alpha": MeshtasticConfig(
                adapter_id="radio-alpha",
                radio_relay_prefix="",
                origin_label="East Radio",
            ),
            "radio-bravo": MeshtasticConfig(
                adapter_id="radio-bravo",
                radio_relay_prefix="[{origin_label}]: ",
                origin_label="West Radio",
            ),
        },
        source_attribution={
            "radio-alpha": _StubSourceAttribution(
                adapter_id="radio-alpha",
                origin_label="East Radio",
            ),
            "radio-bravo": _StubSourceAttribution(
                adapter_id="radio-bravo",
                origin_label="West Radio",
            ),
        },
    )
    event = _make_meshtastic_event(
        source_adapter="radio-alpha",
        native_data={"longname": "NodeA", "shortname": "NA", "from_id": "!a"},
    )
    result = await renderer.render(
        event,
        RenderingContext(target_adapter="radio-bravo", delivery_strategy="direct"),
    )
    text = result.payload["text"]
    # Should use source (radio-alpha) origin_label, NOT target's
    assert "[East Radio]: " in text
    assert "West Radio" not in text


# ===================================================================
# Matrix→Meshtastic uses Matrix origin_label
# ===================================================================


async def test_matrix_origin_label_in_meshtastic_prefix() -> None:
    """Matrix source origin_label appears in Meshtastic prefix."""
    renderer = MeshtasticRenderer(
        configs={
            "mesh-1": MeshtasticConfig(
                adapter_id="mesh-1",
                radio_relay_prefix="[{origin_label}] ",
            ),
        },
        source_attribution={
            "matrix-1": _StubSourceAttribution(
                adapter_id="matrix-1",
                origin_label="Home Matrix",
            ),
        },
    )
    event = _make_matrix_event()
    result = await renderer.render(
        event,
        RenderingContext(target_adapter="mesh-1", delivery_strategy="direct"),
    )
    text = result.payload["text"]
    assert "[Home Matrix] " in text
    assert "hello from matrix" in text


# ===================================================================
# {meshnet_name} renders as {meshnet_name} (unknown)
# ===================================================================


async def test_meshnet_name_unchanged_in_meshtastic_prefix() -> None:
    """``{meshnet_name}`` in prefix template passes through as literal."""
    renderer = MeshtasticRenderer(
        configs={
            "mesh-1": MeshtasticConfig(
                adapter_id="mesh-1",
                radio_relay_prefix="[{meshnet_name}] ",
            ),
        },
    )
    event = _make_meshtastic_event(
        native_data={"longname": "Node1", "shortname": "N1", "from_id": "1"},
    )
    result = await renderer.render(
        event,
        RenderingContext(target_adapter="mesh-1", delivery_strategy="direct"),
    )
    text = result.payload["text"]
    # {meshnet_name} is unknown → passes through as literal
    assert "[{meshnet_name}] " in text
    # Verify formatting_error indicates unknown
    assert result.metadata["relay_prefix_formatting_error"] is not None
    assert "meshnet_name" in result.metadata["relay_prefix_formatting_error"]


async def test_meshnet_name_in_reaction_compact_prefix() -> None:
    """``{meshnet_name}`` in reaction compact prefix passes through as literal."""
    renderer = MeshtasticRenderer(
        configs={
            "mesh-1": MeshtasticConfig(
                adapter_id="mesh-1",
                radio_relay_prefix="[{meshnet_name}/{sender_short}] ",
            ),
        },
    )
    relation = EventRelation(
        relation_type="reaction",
        target_event_id="orig-001",
        target_native_ref=None,
        key="👍",
        fallback_text="original msg",
    )
    event = _make_meshtastic_event(
        source_adapter="matrix-1",
        relations=(relation,),
        native_data={"longname": "Alice", "shortname": "A", "from_id": "@a:b"},
    )
    result = await renderer.render(
        event,
        RenderingContext(target_adapter="mesh-1", delivery_strategy="direct"),
    )
    text = result.payload["text"]
    # {meshnet_name} stays as literal in the compact prefix
    assert "{meshnet_name}" in text
    assert "reacted" in text
