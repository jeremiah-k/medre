# medre-test-tier: compatibility
"""Tests for Meshtastic->Matrix mapped reactions.

Split from test_matrix_reaction_mmrelay.py for line-cap compliance.
Tests cover:
- Meshtastic->Matrix reaction rendering for mapped Matrix-originated messages
- Unknown replyId fallback (no crash)
- Codec ignores KEY_REACTION_KEY and other flat reaction claims
- Authored emotes remain messages regardless of flat reaction keys
"""

from __future__ import annotations

from typing import Any

import pytest

from medre.adapters.matrix.renderer import MatrixRenderer
from medre.core.events.canonical import CanonicalEvent, EventRelation
from medre.core.events.kinds import EventKind
from medre.core.events.metadata import EventMetadata, NativeMetadata
from medre.core.rendering.renderer import RenderingContext
from tests.helpers.matrix import matrix_payload_content as _payload_content
from tests.helpers.matrix_stubs import StubMatrixConfig as _StubMatrixConfig

# ---------------------------------------------------------------------------
# Helpers (imported from tests.helpers.matrix_stubs)
# ---------------------------------------------------------------------------


def _make_native_event(
    body: str = "hello",
    sender: str = "@alice:example.com",
    event_id: str = "$evt-001",
    content: dict | None = None,
    has_source: bool = True,
) -> Any:
    class _Fake:
        pass

    evt = _Fake()
    evt.body = body
    evt.sender = sender
    evt.event_id = event_id
    if has_source:
        evt.source = {
            "content": content or {"msgtype": "m.text", "body": body},
            "event_id": event_id,
            "sender": sender,
            "type": "m.room.message",
        }
    return evt


def _make_mesh_reaction(
    key: str = "👍",
    body: str = "👍",
    fallback_text: str | None = None,
    rel_metadata: dict | None = None,
    native_data: dict | None = None,
    longname: str = "TestNode",
    shortname: str = "TN",
    packet_id: str = "pkt-42",
    source_adapter: str = "mesh-1",
) -> CanonicalEvent:
    """Build a canonical reaction originating from Meshtastic (no Matrix target ref)."""
    rel = EventRelation(
        relation_type="reaction",
        target_event_id=None,
        target_native_ref=None,
        key=key,
        fallback_text=fallback_text,
        metadata=rel_metadata or {},
    )
    if native_data is not None:
        nd = native_data
    else:
        # The meshtastic adapter persists transport-native identity under
        # the versioned ``native.meshtastic`` namespace; attribution and
        # sender projection read only that namespace.
        nd = {
            "meshtastic": {
                "schema_version": 1,
                "longname": longname,
                "shortname": shortname,
                "packet_id": packet_id,
                "from_id": "!abcdef01",
            },
        }
    return CanonicalEvent(
        event_id="evt-mesh-reaction-001",
        event_kind=EventKind.MESSAGE_REACTED,
        schema_version=1,
        timestamp=__import__("datetime").datetime.now(
            tz=__import__("datetime").timezone.utc
        ),
        source_adapter=source_adapter,
        source_transport_id="!node-1",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=(rel,),
        payload={"body": body, "msgtype": "m.text"},
        metadata=EventMetadata(native=NativeMetadata(data=nd)),
    )


# ===========================================================================
# Meshtastic->Matrix mapped reaction tests
# ===========================================================================


class TestMeshtasticToMatrixMappedReaction:
    """Meshtastic→Matrix reaction to a message bridged out from Matrix.

    Without a Matrix-native target the reaction renders the emote fallback
    with the attribution prefix and quoted original-text preview, preserving
    longname spaces and casing.
    """

    @pytest.mark.asyncio
    async def test_comprehensive_emote_reaction_fields(self) -> None:
        """Single test verifying the full emote-fallback reaction body."""
        renderer = MatrixRenderer(
            configs={
                "matrix-1": _StubMatrixConfig(
                    adapter_id="matrix-1",
                    relay_prefix="[{sender}] ",
                ),
            },
        )
        original_text = "Hello from mesh world this is a longer test message"
        event = _make_mesh_reaction(
            key="👍",
            body="👍",
            longname="Alpha Bravo",
            shortname="AB",
            packet_id="pkt-77",
            fallback_text=original_text,
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )

        payload = _payload_content(result)
        # msgtype must be m.emote, not m.text
        assert payload["msgtype"] == "m.emote"

        # Body carries the reacted wording with the quoted original preview
        body = payload["body"]
        assert 'reacted 👍 to "' in body

        # Body preserves generic Meshtastic longname with spaces and casing
        assert "[Alpha Bravo]" in body

        # Body contains abbreviated original text (40 chars + ...)
        assert "Hello from mesh world this is a longer test message" not in body
        assert "Hello from mesh world this is a longer t..." in body

        # NOT rendered as a regular message with only the emoji
        assert body != "👍"
        assert payload["msgtype"] != "m.text"


class TestMeshtasticToMatrixUnknownReplyIdFallback:
    """Missing mapping fallback: Meshtastic→Matrix reaction with unknown replyId.

    Renders MMRelay-style emote when enough metadata exists; safe fallback
    when not. No crash.
    """

    @pytest.mark.asyncio
    async def test_unknown_reply_id_safe_emote(self) -> None:
        """Reaction with no replyId metadata still renders safely as emote."""
        renderer = MatrixRenderer()
        event = _make_mesh_reaction(
            key="👍",
            body="👍",
            native_data={
                "longname": "Some Node",
                "shortname": "SN",
                "packet_id": "pkt-1",
                "from_id": "!node1",
            },
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )

        assert _payload_content(result)["msgtype"] == "m.emote"
        assert "reacted" in _payload_content(result)["body"]

    @pytest.mark.asyncio
    async def test_minimal_metadata_no_crash(self) -> None:
        """Reaction with completely empty metadata renders without crash."""
        renderer = MatrixRenderer()
        event = _make_mesh_reaction(
            key="🔥",
            body="🔥",
            native_data={},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )

        assert _payload_content(result)["msgtype"] == "m.emote"
        assert "reacted 🔥" in _payload_content(result)["body"]
