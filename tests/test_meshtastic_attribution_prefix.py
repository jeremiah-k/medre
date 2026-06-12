"""Tests for MeshtasticRenderer: source origin_label from attribution
registry, Meshtastic-origin clean rendering, and cross-radio attribution.

Split from test_meshtastic_renderer_extra.py to stay under the 1500-line cap.
"""

from __future__ import annotations

from datetime import datetime, timezone

from medre.adapters.meshtastic.renderer import MeshtasticRenderer
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.events import (
    CanonicalEvent,
    EventMetadata,
    NativeMetadata,
)
from medre.core.rendering.renderer import RenderingContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_renderer(
    target_adapter: str = "mesh-1",
    *,
    radio_relay_prefix: str = "",
    meshnet_name: str = "",
    max_text_bytes: int = 227,
) -> MeshtasticRenderer:
    """Create a MeshtasticRenderer with a single-adapter config mapping."""
    config = MeshtasticConfig(
        adapter_id=target_adapter,
        radio_relay_prefix=radio_relay_prefix,
        meshnet_name=meshnet_name,
        max_text_bytes=max_text_bytes,
    )
    return MeshtasticRenderer(configs={target_adapter: config})


def _make_matrix_event(
    event_id: str = "mx-evt-1",
    payload: dict | None = None,
    relations: tuple | None = None,
    source_adapter: str = "matrix-1",
    display_name: str = "Display Name",
) -> CanonicalEvent:
    """Create a CanonicalEvent simulating Matrix origin."""
    native_data: dict[str, object] = {
        "longname": display_name,
        "shortname": display_name.split()[0] if display_name else "",
        "from_id": "@user:example.com",
    }
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.reacted",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="@user:example.com",
        source_channel_id="!room:example.com",
        parent_event_id=None,
        lineage=(),
        relations=relations or (),
        payload=payload or {"body": "👍"},
        metadata=EventMetadata(native=NativeMetadata(data=native_data)),
    )


# ===================================================================
# Meshtastic-origin events: clean rendering
# ===================================================================


class TestMeshtasticOriginNoNonsense:
    """Meshtastic-origin events rendered back to Meshtastic produce clean output."""

    async def test_meshtastic_loop_prefix_clean(self) -> None:
        """Meshtastic-origin event with prefix produces no 'None' or garbled output."""
        renderer = _make_renderer("mesh-1", radio_relay_prefix="{shortname5}[M]: ")
        event = CanonicalEvent(
            event_id="mesh-evt-1",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="mesh-1",
            source_transport_id="!node1",
            source_channel_id="0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "hello from mesh"},
            metadata=EventMetadata(
                native=NativeMetadata(
                    data={
                        "longname": "MeshNode1",
                        "shortname": "Mesh1",
                        "from_id": "1234567890",
                    }
                )
            ),
        )
        result = await renderer.render(
            event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
        )
        text = result.payload["text"]
        assert text.startswith("Mesh1[M]: ")
        assert "None" not in text
        assert "hello from mesh" in text

    async def test_routed_meshtastic_no_prefix_nonsense(self) -> None:
        """Routed Meshtastic event without prefix renders cleanly."""
        renderer = _make_renderer("mesh-1")
        event = CanonicalEvent(
            event_id="mesh-evt-2",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="mesh-other",
            source_transport_id="!node2",
            source_channel_id="0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "routed msg"},
            metadata=EventMetadata(
                native=NativeMetadata(
                    data={
                        "longname": "OtherNode",
                        "shortname": "Othr",
                        "from_id": "9876543210",
                    }
                )
            ),
        )
        result = await renderer.render(
            event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
        )
        text = result.payload["text"]
        assert "None" not in text
        assert "routed msg" in text


# ===================================================================
# Source origin_label from source_attribution registry
# ===================================================================


def _make_renderer_with_attribution(
    target_adapter: str = "mesh-1",
    *,
    radio_relay_prefix: str = "",
    meshnet_name: str = "",
    source_attribution: dict | None = None,
) -> MeshtasticRenderer:
    """Create a MeshtasticRenderer with source_attribution."""
    config = MeshtasticConfig(
        adapter_id=target_adapter,
        radio_relay_prefix=radio_relay_prefix,
        meshnet_name=meshnet_name,
    )
    return MeshtasticRenderer(
        configs={target_adapter: config},
        source_attribution=source_attribution,
    )


class _StubSourceAttribution:
    """Minimal duck-typed SourceAttributionConfig for tests."""

    def __init__(
        self,
        adapter_id: str = "",
        origin_label: str = "",
        meshnet_name: str = "",
    ) -> None:
        self.adapter_id = adapter_id
        self.origin_label = origin_label
        self.meshnet_name = meshnet_name


class TestSourceOriginLabel:
    """Meshtastic target prefix uses source origin_label from registry."""

    async def test_source_origin_label_in_prefix(self) -> None:
        """Source origin_label from registry appears in prefix."""
        renderer = _make_renderer_with_attribution(
            "mesh-1",
            radio_relay_prefix="[{origin_label}]: ",
            source_attribution={
                "matrix-1": _StubSourceAttribution(
                    adapter_id="matrix-1",
                    origin_label="Matrix Server",
                ),
            },
        )
        event = CanonicalEvent(
            event_id="mx-1",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="matrix-1",
            source_transport_id="@alice:example.com",
            source_channel_id="!room:example.com",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "hello"},
            metadata=EventMetadata(
                native=NativeMetadata(
                    data={
                        "longname": "Alice",
                        "shortname": "A",
                        "from_id": "@alice:example.com",
                    }
                )
            ),
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="mesh-1", delivery_strategy="direct"),
        )
        assert "[Matrix Server]: " in result.payload["text"]
        assert "hello" in result.payload["text"]

    async def test_matrix_to_meshtastic_uses_source_origin_label(self) -> None:
        """Matrix→Meshtastic: Matrix source origin_label appears in Meshtastic prefix."""
        renderer = _make_renderer_with_attribution(
            "mesh-1",
            radio_relay_prefix="[{origin_label}/{shortname5}]: ",
            source_attribution={
                "matrix-1": _StubSourceAttribution(
                    adapter_id="matrix-1",
                    origin_label="Home Matrix",
                ),
            },
        )
        event = _make_matrix_event(display_name="TestUser")
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="mesh-1", delivery_strategy="direct"),
        )
        text = result.payload["text"]
        assert "Home Matrix" in text
        # shortname5 derived from display name
        assert "TestU" in text

    async def test_mesh_to_mesh_uses_source_origin_label(self) -> None:
        """MeshtasticA→MeshtasticB: source A's origin_label, not B's."""
        renderer = MeshtasticRenderer(
            configs={
                "radio-alpha": MeshtasticConfig(
                    adapter_id="radio-alpha",
                    radio_relay_prefix="",
                    meshnet_name="AlphaNet",
                ),
                "radio-bravo": MeshtasticConfig(
                    adapter_id="radio-bravo",
                    radio_relay_prefix="[{origin_label}]: ",
                    meshnet_name="BravoNet",
                ),
            },
            source_attribution={
                "radio-alpha": _StubSourceAttribution(
                    adapter_id="radio-alpha",
                    origin_label="East Radio",
                    meshnet_name="AlphaNet",
                ),
                "radio-bravo": _StubSourceAttribution(
                    adapter_id="radio-bravo",
                    origin_label="West Radio",
                    meshnet_name="BravoNet",
                ),
            },
        )
        event = CanonicalEvent(
            event_id="evt-a2b",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="radio-alpha",
            source_transport_id="!nodeA",
            source_channel_id="0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "hello from A"},
            metadata=EventMetadata(
                native=NativeMetadata(
                    data={"longname": "NodeA", "shortname": "NA", "from_id": "!nodeA"}
                )
            ),
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="radio-bravo", delivery_strategy="direct"),
        )
        text = result.payload["text"]
        # Should use radio-alpha's origin_label ("East Radio"), NOT bravo's
        assert "[East Radio]: " in text
        assert "West Radio" not in text
