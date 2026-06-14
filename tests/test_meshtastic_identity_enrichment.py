"""Meshtastic identity-enrichment integration tests.

Validates the complete ingress enrichment pipeline that fills in
``longname``/``shortname`` from the session's local node database
(the in-memory ``client.nodes`` cache populated by the SDK as
``NODEINFO_APP`` packets arrive) when a text-message packet does not
carry sender display names.

Pipeline under test (mirrors ``MeshtasticAdapter._on_packet`` lines
680-681)::

    session.get_node_info(from_id)   # network-free dict lookup
    -> adapter._enrich_with_node_info(packet)
    -> codec.decode(packet, node_info=...)
    -> project_meshtastic_attribution(native_data, ...)

Resolution order asserted throughout:
    event/packet names -> local node metadata names -> sender_id fallback.

Meshtastic-native names stay native (bare ``longname``/``shortname``
keys recognised by ``_MESHTASTIC_KEYS`` in the dispatch); they are not
added as core ``RelayAttribution`` fields.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from medre.adapters.meshtastic.adapter import MeshtasticAdapter
from medre.adapters.meshtastic.attribution import project_meshtastic_attribution
from medre.adapters.meshtastic.renderer import MeshtasticRenderer
from medre.adapters.meshtastic.session import MeshtasticSession
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.events import CanonicalEvent, EventMetadata, NativeMetadata
from medre.core.rendering.renderer import RenderingContext

# ---------------------------------------------------------------------------
# Inline helpers
# ---------------------------------------------------------------------------


def _make_config(adapter_id: str = "mesh-1") -> MeshtasticConfig:
    return MeshtasticConfig(adapter_id=adapter_id)


def _make_text_packet(
    text: str = "hello mesh",
    sender: str = "!aabbccdd",
    channel: int = 0,
    packet_id: int = 42,
    to_id: str = "",
) -> dict[str, Any]:
    """Build a minimal Meshtastic text-message packet dict.

    Text-message packets do not carry ``longname``/``shortname``; those
    arrive via separate ``NODEINFO_APP`` packets and live in the SDK
    node database.
    """
    return {
        "fromId": sender,
        "toId": to_id,
        "channel": channel,
        "id": packet_id,
        "decoded": {
            "portnum": "text_message",
            "text": text,
        },
    }


def _make_node_client(nodes: dict[str, dict[str, Any]]) -> SimpleNamespace:
    """Build a fake SDK client exposing a ``nodes`` dict.

    The real Meshtastic SDK populates ``interface.nodes`` as
    ``{node_id: {"user": {"longName": ..., "shortName": ...}, ...}}``.
    """
    return SimpleNamespace(nodes=nodes)


def _make_session(
    client: Any = None,
    config: MeshtasticConfig | None = None,
) -> MeshtasticSession:
    """Build a MeshtasticSession with an injected fake client."""
    session = MeshtasticSession(
        config or _make_config(),
        adapter_id="mesh-1",
        platform="meshtastic",
    )
    if client is not None:
        session._client = client
    return session


def _enrich_and_decode(
    adapter: MeshtasticAdapter,
    packet: dict[str, Any],
) -> CanonicalEvent:
    """Mirror ``_on_packet`` lines 680-681 exactly.

    Enrich native metadata from the session node database, then decode
    via the codec, then return the canonical event.
    """
    node_info = adapter._enrich_with_node_info(packet)
    return adapter._codec.decode(packet, node_info=node_info)


def _project_from_event(event: CanonicalEvent) -> dict[str, str | None]:
    """Project an event's native metadata through the Meshtastic helper."""
    native_data: dict[str, Any] = {}
    if event.metadata and event.metadata.native:
        native_data = dict(event.metadata.native.data)
    return project_meshtastic_attribution(
        native_data,
        source_transport_id=event.source_transport_id,
    )


def _make_event_with_native(
    native: dict[str, Any],
    *,
    source_transport_id: str = "!aabbccdd",
    source_adapter: str = "mesh-1",
) -> CanonicalEvent:
    """Build a minimal CanonicalEvent carrying the given native metadata."""
    return CanonicalEvent(
        event_id="evt-1",
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        source_adapter=source_adapter,
        source_transport_id=source_transport_id,
        source_channel_id="0",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"body": "hello"},
        metadata=EventMetadata(native=NativeMetadata(data=native)),
        source_native_ref=None,
    )


# ===================================================================
# Group 1: Complete enrichment pipeline (node DB -> projection)
# ===================================================================


def test_pipeline_node_db_names_produce_readable_labels() -> None:
    """Packet lacks names but local node DB has them -> projection yields
    readable source_sender_label and source_sender_short_label."""
    client = _make_node_client(
        {"!aabbccdd": {"user": {"longName": "Alpha Node", "shortName": "AN"}}}
    )
    session = _make_session(client=client)
    adapter = MeshtasticAdapter(_make_config())
    adapter._session = session

    packet = _make_text_packet(sender="!aabbccdd")
    event = _enrich_and_decode(adapter, packet)
    fields = _project_from_event(event)

    assert fields["source_sender_label"] == "Alpha Node"
    assert fields["source_sender_short_label"] == "AN"
    assert fields["source_sender_id"] == "!aabbccdd"


def test_pipeline_unknown_node_falls_back_to_sender_id() -> None:
    """Node not in the local DB -> projection falls back to sender_id."""
    client = _make_node_client({"!other": {"user": {"longName": "Other"}}})
    session = _make_session(client=client)
    adapter = MeshtasticAdapter(_make_config())
    adapter._session = session

    packet = _make_text_packet(sender="!aabbccdd")
    event = _enrich_and_decode(adapter, packet)
    fields = _project_from_event(event)

    assert fields["source_sender_label"] == "!aabbccdd"
    assert fields["source_sender_short_label"] == "!aabbccdd"
    assert fields["source_sender_id"] == "!aabbccdd"


def test_pipeline_no_session_falls_back_to_sender_id() -> None:
    """No session available -> no enrichment -> sender_id fallback."""
    adapter = MeshtasticAdapter(_make_config())
    adapter._session = None

    packet = _make_text_packet(sender="!aabbccdd")
    event = _enrich_and_decode(adapter, packet)
    fields = _project_from_event(event)

    assert fields["source_sender_label"] == "!aabbccdd"
    assert fields["source_sender_id"] == "!aabbccdd"


def test_pipeline_partial_longname_only() -> None:
    """Node DB has only longname -> label from longname, short_label from
    compact longname (each field falls through independently)."""
    client = _make_node_client(
        {"!aabbccdd": {"user": {"longName": "Alpha Node", "shortName": ""}}}
    )
    session = _make_session(client=client)
    adapter = MeshtasticAdapter(_make_config())
    adapter._session = session

    packet = _make_text_packet(sender="!aabbccdd")
    event = _enrich_and_decode(adapter, packet)
    fields = _project_from_event(event)

    assert fields["source_sender_label"] == "Alpha Node"
    # shortname absent -> compact(longname)
    assert fields["source_sender_short_label"] == "AlphaNode"


def test_pipeline_partial_shortname_only() -> None:
    """Node DB has only shortname -> label falls back to shortname,
    short_label uses shortname."""
    client = _make_node_client(
        {"!aabbccdd": {"user": {"longName": "", "shortName": "AN"}}}
    )
    session = _make_session(client=client)
    adapter = MeshtasticAdapter(_make_config())
    adapter._session = session

    packet = _make_text_packet(sender="!aabbccdd")
    event = _enrich_and_decode(adapter, packet)
    fields = _project_from_event(event)

    assert fields["source_sender_label"] == "AN"
    assert fields["source_sender_short_label"] == "AN"


def test_pipeline_get_node_info_exception_returns_none_no_propagation() -> None:
    """When session.get_node_info raises, enrichment returns None and
    projection falls back to sender_id without propagating the error."""
    adapter = MeshtasticAdapter(_make_config())
    mock_session = MagicMock()
    mock_session.get_node_info.side_effect = RuntimeError("SDK internal error")
    adapter._session = mock_session

    packet = _make_text_packet(sender="!aabbccdd")
    # Must not raise.
    node_info = adapter._enrich_with_node_info(packet)
    assert node_info is None

    event = adapter._codec.decode(packet, node_info=node_info)
    fields = _project_from_event(event)
    assert fields["source_sender_label"] == "!aabbccdd"


# ===================================================================
# Group 2: Projection resolution order
# (event/packet names -> node metadata -> sender_id)
# ===================================================================


def test_projection_event_names_win_over_sender_id() -> None:
    """When native_data carries longname (event name), it wins over the
    sender_id fallback.  This is the 'event names always win' contract."""
    fields = project_meshtastic_attribution(
        {"longname": "Event Name", "shortname": "EN", "from_id": "!node"},
    )
    assert fields["source_sender_label"] == "Event Name"
    assert fields["source_sender_short_label"] == "EN"


def test_projection_node_metadata_used_when_packet_lacks_names() -> None:
    """Native data populated solely from the node DB (no packet names)
    produces readable labels via projection."""
    fields = project_meshtastic_attribution(
        {"longname": "From DB", "shortname": "DB", "from_id": "!node"},
        source_transport_id="!node",
    )
    assert fields["source_sender_label"] == "From DB"
    assert fields["source_sender_short_label"] == "DB"


def test_projection_sender_id_fallback_when_all_absent() -> None:
    """No names anywhere -> sender_id fallback (current behaviour preserved)."""
    fields = project_meshtastic_attribution(
        {"from_id": "!aabbccdd"},
        source_transport_id="!aabbccdd",
    )
    assert fields["source_sender_label"] == "!aabbccdd"
    assert fields["source_sender_short_label"] == "!aabbccdd"


# ===================================================================
# Group 3: Edge cases (non-string, None, partial)
# ===================================================================


def test_projection_non_string_longname_coerced() -> None:
    """Non-string longname (int) is coerced via str() and used as the label."""
    fields = project_meshtastic_attribution(
        {"longname": 42, "shortname": None, "from_id": "!node"},
    )
    assert fields["source_sender_label"] == "42"


def test_projection_non_string_shortname_coerced() -> None:
    """Non-string shortname (int) is coerced via str() and used as short label."""
    fields = project_meshtastic_attribution(
        {"longname": None, "shortname": 7, "from_id": "!node"},
    )
    assert fields["source_sender_short_label"] == "7"


def test_projection_none_values_treated_as_absent() -> None:
    """Explicit None values fall through independently to sender_id."""
    fields = project_meshtastic_attribution(
        {"longname": None, "shortname": None, "from_id": "!node"},
    )
    assert fields["source_sender_label"] == "!node"
    assert fields["source_sender_short_label"] == "!node"


def test_pipeline_node_db_int_values_safely_coerced() -> None:
    """The session node lookup coerces SDK values via str(); an integer
    longName/shortName does not raise and produces string labels."""
    client = _make_node_client(
        {"!aabbccdd": {"user": {"longName": 12345, "shortName": 67}}}
    )
    session = _make_session(client=client)
    adapter = MeshtasticAdapter(_make_config())
    adapter._session = session

    result = session.get_node_info("!aabbccdd")
    assert result == {"longname": "12345", "shortname": "67"}

    packet = _make_text_packet(sender="!aabbccdd")
    event = _enrich_and_decode(adapter, packet)
    fields = _project_from_event(event)
    assert fields["source_sender_label"] == "12345"
    assert fields["source_sender_short_label"] == "67"


def test_pipeline_node_db_none_name_values_ignored() -> None:
    """When the node DB has None longName/shortName, get_node_info returns
    None and projection falls back to sender_id without raising."""
    client = _make_node_client(
        {"!aabbccdd": {"user": {"longName": None, "shortName": None}}}
    )
    session = _make_session(client=client)
    adapter = MeshtasticAdapter(_make_config())
    adapter._session = session

    assert session.get_node_info("!aabbccdd") is None

    packet = _make_text_packet(sender="!aabbccdd")
    event = _enrich_and_decode(adapter, packet)
    fields = _project_from_event(event)
    assert fields["source_sender_label"] == "!aabbccdd"


# ===================================================================
# Group 4: Compact mode (strips spaces from enriched names)
# ===================================================================


def test_compact_strips_spaces_from_enriched_longname() -> None:
    """compact=True strips spaces from labels sourced from the node DB."""
    fields = project_meshtastic_attribution(
        {"longname": "Alpha Node", "shortname": "A N", "from_id": "!node"},
        compact=True,
    )
    assert fields["source_sender_label"] == "AlphaNode"
    assert fields["source_sender_short_label"] == "AN"


def test_compact_preserves_space_free_enriched_names() -> None:
    """compact=True is idempotent on already-compact node-DB names."""
    fields = project_meshtastic_attribution(
        {"longname": "Alpha", "shortname": "AL", "from_id": "!node"},
        compact=True,
    )
    assert fields["source_sender_label"] == "Alpha"
    assert fields["source_sender_short_label"] == "AL"


# ===================================================================
# Group 5: Byte-safe prefix truncation regression guard
# ===================================================================


def _make_renderer(
    target_adapter: str = "mesh-1",
    *,
    radio_relay_prefix: str = "{sender_short}: ",
    max_text_bytes: int = 227,
) -> MeshtasticRenderer:
    config = MeshtasticConfig(
        adapter_id=target_adapter,
        radio_relay_prefix=radio_relay_prefix,
        max_text_bytes=max_text_bytes,
    )
    return MeshtasticRenderer(configs={target_adapter: config})


def _make_render_ctx(target_adapter: str = "mesh-1") -> RenderingContext:
    return RenderingContext(
        target_adapter=target_adapter,
        delivery_strategy="direct",
    )


async def test_truncation_with_enriched_prefix_stays_within_budget() -> None:
    """A long enriched longname used in the prefix does not break the
    UTF-8 byte-safe truncation budget.  The rendered text stays at or
    below max_text_bytes."""
    renderer = _make_renderer(max_text_bytes=40)
    event = _make_event_with_native(
        {
            "from_id": "!aabbccdd",
            "longname": "A" * 100,
            "shortname": "B" * 50,
        }
    )
    result = await renderer.render(event, _make_render_ctx())
    rendered_text = str(result.payload["text"])
    assert len(rendered_text.encode("utf-8")) <= 40
    assert result.truncated is True


async def test_truncation_multibyte_no_codepoint_split() -> None:
    """Multi-byte UTF-8 codepoints in enriched names are never split by
    the byte-budget truncation."""
    renderer = _make_renderer(max_text_bytes=10)
    # longname with 4-byte emoji ensures truncation lands mid-codepoint
    # if the truncation were byte-naive.
    event = _make_event_with_native(
        {
            "from_id": "!node",
            "longname": "\U0001f600" * 20,
            "shortname": "\U0001f600",
        }
    )
    result = await renderer.render(event, _make_render_ctx())
    rendered_text = str(result.payload["text"])
    # Decoding the truncated text must never raise (no split codepoints).
    rendered_text.encode("utf-8").decode("utf-8")
    assert len(rendered_text.encode("utf-8")) <= 10


async def test_truncation_within_budget_not_flagged() -> None:
    """Short enriched prefix + text within budget is not flagged truncated."""
    renderer = _make_renderer(max_text_bytes=227)
    event = _make_event_with_native(
        {
            "from_id": "!aabbccdd",
            "longname": "Alpha",
            "shortname": "AL",
        }
    )
    result = await renderer.render(event, _make_render_ctx())
    assert result.truncated is False
    assert str(result.payload["text"]).startswith("AL: ")


# ===================================================================
# Group 6: Native key naming (Meshtastic-native, not core fields)
# ===================================================================


def test_enriched_names_are_meshtastic_native_keys() -> None:
    """Enriched names are embedded as bare Meshtastic-native keys
    (longname/shortname), NOT as core RelayAttribution field names."""
    client = _make_node_client(
        {"!aabbccdd": {"user": {"longName": "Alpha", "shortName": "AL"}}}
    )
    session = _make_session(client=client)
    adapter = MeshtasticAdapter(_make_config())
    adapter._session = session

    packet = _make_text_packet(sender="!aabbccdd")
    event = _enrich_and_decode(adapter, packet)
    assert event.metadata.native is not None
    native = event.metadata.native.data

    # Meshtastic-native bare keys present.
    assert native["longname"] == "Alpha"
    assert native["shortname"] == "AL"
    # Core RelayAttribution field names are NOT present in native metadata.
    assert "source_sender_label" not in native
    assert "source_sender_short_label" not in native


def test_projection_returns_only_three_generic_fields() -> None:
    """Projection output contains only the three generic attribution
    fields, regardless of how many native keys are present."""
    fields = project_meshtastic_attribution(
        {
            "from_id": "!node",
            "longname": "Alpha",
            "shortname": "AL",
            "channel": 0,
            "packet_id": 42,
        }
    )
    assert set(fields.keys()) == {
        "source_sender_id",
        "source_sender_label",
        "source_sender_short_label",
    }


def test_dispatch_recognises_enriched_native_as_meshtastic() -> None:
    """The attribution dispatch recognises bare longname/shortname keys
    as Meshtastic-characteristic (platform detection)."""
    from medre.adapters._attribution_dispatch import detect_source_platform

    native = {"longname": "Alpha", "shortname": "AL", "from_id": "!node"}
    assert detect_source_platform("radio-a", native) == "meshtastic"
