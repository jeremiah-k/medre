"""Standalone adapter reuse examples — API-boundary tests.

These tests demonstrate that core adapter components (renderers, codecs,
interop constants) are usable **without** RuntimeBuilder, PipelineRunner,
storage, sessions, or any runtime infrastructure.  Each test constructs the
minimum viable inputs and calls the adapter component directly.

The purpose is two-fold:

1. Validate the API boundary — constructor signatures, render/decode
   contracts, and return types remain stable.
2. Serve as living documentation for developers who want to reuse a single
   adapter component outside the full MEDRE runtime.
"""

from __future__ import annotations

import types
from datetime import datetime, timezone

import pytest

from medre.adapters.matrix.renderer import MatrixRenderer
from medre.adapters.meshtastic.codec import MeshtasticCodec
from medre.adapters.meshtastic.renderer import MeshtasticRenderer
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.events.canonical import CanonicalEvent
from medre.core.events.metadata import EventMetadata
from medre.core.rendering.renderer import RenderingResult
from medre.interop.mmrelay import (
    EMOJI_FLAG_VALUE,
    KEY_EMOJI,
    KEY_ID,
    KEY_LONGNAME,
    KEY_MESHNET,
    KEY_PORTNUM,
    KEY_REPLY_ID,
    KEY_SHORTNAME,
    KEY_TEXT,
    PORTNUM_TEXT,
)


def _make_event(
    *,
    event_kind: str = "message.created",
    payload: dict[str, object] | None = None,
    source_adapter: str = "test-source",
) -> CanonicalEvent:
    """Build a minimal CanonicalEvent for testing."""
    return CanonicalEvent(
        event_id="evt-001",
        event_kind=event_kind,
        schema_version=1,
        timestamp=datetime(2025, 5, 19, 12, 0, 0, tzinfo=timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="transport-1",
        source_channel_id="ch-1",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload=payload or {},
        metadata=EventMetadata(),
    )


# ======================================================================
# Test Class 1: MatrixRenderer standalone
# ======================================================================


class TestMatrixRendererStandalone:
    """MatrixRenderer can render events without any runtime or adapter."""

    @pytest.mark.asyncio
    async def test_render_text_message(self) -> None:
        event = _make_event(
            payload={"body": "hello from mesh"},
        )
        renderer = MatrixRenderer()
        result = await renderer.render(event, target_adapter="matrix-1")

        assert isinstance(result, RenderingResult)
        assert result.payload["msgtype"] == "m.text"
        assert result.payload["body"] == "hello from mesh"
        assert "medre" in result.payload


# ======================================================================
# Test Class 2: MeshtasticRenderer standalone
# ======================================================================


class TestMeshtasticRendererStandalone:
    """MeshtasticRenderer can render events without any runtime or adapter."""

    @pytest.mark.asyncio
    async def test_render_text_message(self) -> None:
        event = _make_event(
            payload={"body": "hello from matrix"},
        )
        renderer = MeshtasticRenderer(
            configs={
                "mesh-1": MeshtasticConfig(adapter_id="mesh-1", radio_relay_prefix="")
            }
        )
        result = await renderer.render(
            event, target_adapter="mesh-1", target_channel="3"
        )

        assert isinstance(result, RenderingResult)
        assert result.payload["text"] == "hello from matrix"
        assert result.payload["channel_index"] == 3


# ======================================================================
# Test Class 3: MeshtasticCodec standalone
# ======================================================================


class TestMeshtasticCodecStandalone:
    """MeshtasticCodec can decode packets without any runtime or storage."""

    def test_decode_text_packet(self) -> None:
        packet: dict[str, object] = {
            "from": 11256099,
            "fromId": "!abcd1234",
            "to": 0xFFFFFFFF,
            "toId": "",
            "channel": 0,
            "id": 42,
            "decoded": {
                "portnum": "TEXT_MESSAGE_APP",
                "text": "hello mesh",
            },
        }
        config_mock = types.SimpleNamespace(default_channel=0)
        codec = MeshtasticCodec(adapter_id="mesh-1", config=config_mock)
        result = codec.decode(packet)

        assert isinstance(result, CanonicalEvent)
        assert "hello mesh" in str(result.payload.get("body", ""))
        assert result.source_adapter == "mesh-1"
        assert isinstance(result.event_kind, str) and result.event_kind.startswith(
            "message"
        )


# ======================================================================
# Test Class 4: MMRelay interop constants standalone
# ======================================================================


class TestMMRelayInteropStandalone:
    """mmrelay constants are plain strings with zero medre imports."""

    def test_all_constants_are_non_empty_strings(self) -> None:
        for name in (
            KEY_ID,
            KEY_LONGNAME,
            KEY_SHORTNAME,
            KEY_MESHNET,
            KEY_PORTNUM,
            KEY_TEXT,
            KEY_REPLY_ID,
            KEY_EMOJI,
        ):
            assert isinstance(name, str)
            assert len(name) > 0

    def test_portnum_text_value(self) -> None:
        assert PORTNUM_TEXT == "TEXT_MESSAGE_APP"

    def test_emoji_flag_value(self) -> None:
        assert EMOJI_FLAG_VALUE == 1
