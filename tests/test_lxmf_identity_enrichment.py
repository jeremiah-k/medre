"""End-to-end integration tests for the LXMF display-name enrichment pipeline.

Verifies the complete chain across multiple components::

    adapter ingress (announce-cache resolution)
      -> codec decode (produces ``lxmf.display_name`` in native metadata)
      -> attribution projection (produces ``source_sender_label``)
      -> renderer prefix formatting (``{sender}``, ``{sender_id}``,
         ``{sender_short}``)

Unit tests for the individual components live in
``test_lxmf_session_display_name.py`` and
``test_lxmf_adapter_display_name.py``.  The tests here exercise the
*integration* between those components — verifying that data produced
by one stage is correctly consumed by the next, catching wiring bugs
that isolated unit tests miss.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

from medre.adapters._attribution_dispatch import project_source_fields
from medre.adapters.lxmf.adapter import LxmfAdapter
from medre.adapters.lxmf.renderer import LxmfRenderer
from medre.config.adapters.lxmf import LxmfConfig
from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.events.metadata import NativeMetadata
from medre.core.rendering.renderer import RenderingContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides: Any) -> LxmfConfig:
    """Build a fake-mode LxmfConfig with optional overrides."""
    defaults: dict = dict(adapter_id="lxmf-1", connection_type="fake")
    defaults.update(overrides)
    return LxmfConfig(**defaults)


def _make_text_packet(
    content: str = "hello",
    source_hash: str = "abcdef0123456789",
    msg_id: str = "cd" * 32,
    source_name: str | None = None,
) -> dict:
    """Build a minimal text-classifiable LXMF packet dict.

    ``source_name`` is omitted by default so enrichment is exercised.
    """
    packet: dict = {
        "source_hash": source_hash,
        "destination_hash": "00" * 16,
        "message_id": msg_id,
        "timestamp": 1700000000.0,
        "title": "",
        "content": content,
        "fields": {},
        "signature_validated": True,
        "has_fields": False,
    }
    if source_name is not None:
        packet["source_name"] = source_name
    return packet


def _make_event_with_native(
    native_data: dict | None = None,
    payload: dict | None = None,
    source_adapter: str = "lxmf-1",
) -> CanonicalEvent:
    """Build a CanonicalEvent carrying LXMF native metadata.

    Used for renderer tests that simulate codec output without running
    the full adapter ingress path.
    """
    metadata = EventMetadata(native=NativeMetadata(data=native_data or {}))
    return CanonicalEvent(
        event_id="evt-int-1",
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="ab" * 16,
        source_channel_id=None,
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload=payload or {"body": "message_body"},
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# Path A: adapter ingress -> codec -> native metadata
# ---------------------------------------------------------------------------


async def test_announce_resolved_display_name_in_native_metadata(
    make_adapter_context, inbound_collector
) -> None:
    """The announce-resolved display name flows through enrich then codec.

    When the packet carries no ``source_name``, enrichment fills it
    from the session's announce cache, and the codec projects the value
    into ``lxmf.display_name`` native metadata on the published event.
    """
    adapter = LxmfAdapter(_make_config())
    ctx = make_adapter_context("lxmf-1")
    await adapter.start(ctx)
    adapter._session = MagicMock()
    adapter._session.resolve_display_name.return_value = "Alice"

    packet = _make_text_packet(source_hash="abcdef0123456789", content="hi")
    await adapter.simulate_inbound(packet)

    assert len(inbound_collector.events) == 1
    event = inbound_collector.events[0]
    assert event.metadata.native.data["lxmf.display_name"] == "Alice"


async def test_announce_resolved_display_name_in_attribution(
    make_adapter_context, inbound_collector
) -> None:
    """Codec-produced native metadata drives correct attribution fields.

    Takes the event produced by adapter ingress and runs the shared
    attribution dispatch (``project_source_fields``) over its native
    data.  The display name projects to ``source_sender_label`` and the
    source hash projects to ``source_sender_id``.
    """
    adapter = LxmfAdapter(_make_config())
    ctx = make_adapter_context("lxmf-1")
    await adapter.start(ctx)
    adapter._session = MagicMock()
    adapter._session.resolve_display_name.return_value = "Alice"

    source_hash = "abcdef0123456789"
    packet = _make_text_packet(source_hash=source_hash, content="hi")
    await adapter.simulate_inbound(packet)

    assert len(inbound_collector.events) == 1
    event = inbound_collector.events[0]
    native_data = dict(event.metadata.native.data)

    projected = project_source_fields(native_data, source_adapter="lxmf-1")

    assert projected["source_platform"] == "lxmf"
    assert projected["source_sender_label"] == "Alice"
    assert projected["source_sender_id"] == source_hash
    # _compact("Alice") == "Alice" (no spaces to strip).
    assert projected["source_sender_short_label"] == "Alice"


# ---------------------------------------------------------------------------
# Path B: renderer prefix from native metadata
# ---------------------------------------------------------------------------


async def test_announce_resolved_display_name_in_renderer_prefix() -> None:
    """An event with ``lxmf.display_name`` renders ``{sender}`` as that name.

    The renderer resolves attribution via the dispatch, which delegates
    to ``project_lxmf_attribution``.  ``{sender}`` maps to
    ``source_sender_label``, which comes from ``lxmf.display_name``.
    """
    renderer = LxmfRenderer(relay_prefix="[{sender}] ")
    event = _make_event_with_native(
        native_data={
            "source_hash": "ab" * 16,
            "lxmf.display_name": "Alice",
        },
        payload={"body": "message_body"},
    )

    result = await renderer.render(
        event,
        RenderingContext(target_adapter="lxmf_node", delivery_strategy="direct"),
    )

    assert result.payload["content"] == "[Alice] message_body"


# ---------------------------------------------------------------------------
# Path A: message-carried name precedence
# ---------------------------------------------------------------------------


async def test_message_carried_display_name_precedence(
    make_adapter_context, inbound_collector
) -> None:
    """A message-carried ``source_name`` takes precedence over the cache.

    The packet carries ``source_name="Bob"``; even though the mock
    session would resolve "Alice", enrichment must not overwrite the
    message-carried value.  The codec then projects "Bob" into native
    metadata.
    """
    adapter = LxmfAdapter(_make_config())
    ctx = make_adapter_context("lxmf-1")
    await adapter.start(ctx)
    adapter._session = MagicMock()
    adapter._session.resolve_display_name.return_value = "Alice"

    packet = _make_text_packet(
        source_hash="abcdef0123456789",
        content="hi",
        source_name="Bob",
    )
    await adapter.simulate_inbound(packet)

    assert len(inbound_collector.events) == 1
    event = inbound_collector.events[0]
    assert event.metadata.native.data["lxmf.display_name"] == "Bob"
    # Enrichment must not have called the session for a non-empty source_name.
    adapter._session.resolve_display_name.assert_not_called()


# ---------------------------------------------------------------------------
# Path A + Path B: no display name — source hash only
# ---------------------------------------------------------------------------


async def test_no_display_name_source_hash_only(
    make_adapter_context, inbound_collector
) -> None:
    """When no display name resolves, labels stay absent and ``{sender}`` is empty.

    Path A: the mock session returns ``None``, so no
    ``lxmf.display_name`` key is emitted by the codec.
    Path B: rendering that event shows ``{sender}`` empty and
    ``{sender_id}`` populated with the source hash — the opaque hash
    never leaks into ``{sender}``.
    """
    # --- Path A: adapter ingress produces no display-name key ---
    adapter = LxmfAdapter(_make_config())
    ctx = make_adapter_context("lxmf-1")
    await adapter.start(ctx)
    adapter._session = MagicMock()
    adapter._session.resolve_display_name.return_value = None

    source_hash = "abcdef0123456789"
    packet = _make_text_packet(source_hash=source_hash, content="message_body")
    await adapter.simulate_inbound(packet)

    assert len(inbound_collector.events) == 1
    event = inbound_collector.events[0]
    assert "lxmf.display_name" not in event.metadata.native.data

    # --- Path B: render the same event through the renderer ---
    renderer = LxmfRenderer(relay_prefix="[{sender}]({sender_id}) ")
    result = await renderer.render(
        event,
        RenderingContext(target_adapter="lxmf_node", delivery_strategy="direct"),
    )

    # {sender} renders empty (no display name); {sender_id} has the hash.
    assert result.payload["content"] == f"[]({source_hash}) message_body"


# ---------------------------------------------------------------------------
# Path B: short label derivation
# ---------------------------------------------------------------------------


async def test_display_name_short_label_derivation() -> None:
    """``{sender_short}`` falls back to a compact form of the display name.

    When ``lxmf.short_name`` is absent, the attribution projection
    derives ``source_sender_short_label`` by stripping spaces from the
    display name (the ``_compact`` helper in ``attribution.py``).
    """
    renderer = LxmfRenderer(relay_prefix="<{sender_short}> ")
    event = _make_event_with_native(
        native_data={
            "source_hash": "ab" * 16,
            "lxmf.display_name": "Alice Walker",
        },
        payload={"body": "message_body"},
    )

    result = await renderer.render(
        event,
        RenderingContext(target_adapter="lxmf_node", delivery_strategy="direct"),
    )

    # _compact("Alice Walker") == "AliceWalker".
    assert result.payload["content"] == "<AliceWalker> message_body"


# ---------------------------------------------------------------------------
# Path A: enrichment failure resilience
# ---------------------------------------------------------------------------


async def test_enrichment_does_not_fail_ingestion_on_error(
    make_adapter_context, inbound_collector
) -> None:
    """A raising session never blocks ingestion; the event still publishes.

    ``resolve_display_name`` raises, but ``_resolve_display_name``
    swallows the exception and returns ``None``.  Enrichment injects
    nothing and the packet flows through decode and publish unchanged.
    """
    adapter = LxmfAdapter(_make_config())
    ctx = make_adapter_context("lxmf-1")
    await adapter.start(ctx)
    adapter._session = MagicMock()
    adapter._session.resolve_display_name.side_effect = RuntimeError("boom")

    packet = _make_text_packet(source_hash="abcdef0123456789", content="hi")
    await adapter.simulate_inbound(packet)

    assert len(inbound_collector.events) == 1
    event = inbound_collector.events[0]
    assert "lxmf.display_name" not in event.metadata.native.data


# ---------------------------------------------------------------------------
# Path A: consistent resolution across multiple messages
# ---------------------------------------------------------------------------


async def test_multiple_messages_same_peer_consistent_resolution(
    make_adapter_context, inbound_collector
) -> None:
    """Two messages from the same peer both resolve to the same display name.

    Uses distinct ``message_id`` values so the dedup filter does not
    suppress the second packet.
    """
    adapter = LxmfAdapter(_make_config())
    ctx = make_adapter_context("lxmf-1")
    await adapter.start(ctx)
    adapter._session = MagicMock()
    adapter._session.resolve_display_name.return_value = "Alice"

    source_hash = "abcdef0123456789"
    await adapter.simulate_inbound(
        _make_text_packet(
            source_hash=source_hash,
            msg_id="aa" * 32,
            content="first",
        )
    )
    await adapter.simulate_inbound(
        _make_text_packet(
            source_hash=source_hash,
            msg_id="bb" * 32,
            content="second",
        )
    )

    assert len(inbound_collector.events) == 2
    for event in inbound_collector.events:
        assert event.metadata.native.data["lxmf.display_name"] == "Alice"
    # Both lookups targeted the same source hash.
    assert adapter._session.resolve_display_name.call_count == 2
