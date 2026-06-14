"""End-to-end tests for LXMF display-name enrichment and dispatch wiring.

Covers the full ingress-to-rendering pipeline:
- Codec maps ``source_name`` from packet to ``lxmf.display_name`` in
  native metadata.
- Dispatch ``project_source_fields`` for an LXMF native dict sets
  ``source_sender_id`` AND ``source_sender_label`` /
  ``source_sender_short_label`` when a display name is present.
- Renderer prefix ``{sender}`` shows the display name when present and
  renders empty when absent (opaque hash never becomes ``{sender}``).
- Renderer prefix ``{sender_id}`` shows the source_hash.
- Session ``_normalise_inbound_message`` captures ``source_name``
  defensively without breaking existing normalisation or announce
  diagnostics.

These tests exercise the architecture rule:
``adapter-native state -> adapter-local enrichment/projection ->
generic RelayAttribution fields -> renderer templates``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from medre.adapters._attribution_dispatch import project_source_fields
from medre.adapters.lxmf.codec import LxmfCodec
from medre.adapters.lxmf.renderer import LxmfRenderer
from medre.adapters.lxmf.session import LxmfSession
from medre.config.adapters.lxmf import LxmfConfig
from medre.core.events import CanonicalEvent, EventMetadata, NativeMetadata
from medre.core.rendering.renderer import RenderingContext

# ---------------------------------------------------------------------------
# Helpers (inlined — no shared test helpers per task constraints)
# ---------------------------------------------------------------------------


def _config(adapter_id: str = "lxmf-test") -> LxmfConfig:
    return LxmfConfig(adapter_id=adapter_id)


def _make_lxmf_packet(
    *,
    content: str = "hello from lxmf",
    source_hash: str = "ab" * 16,
    source_name: str = "",
    msg_id: str = "ff" * 32,
    title: str = "",
) -> dict[str, Any]:
    """Build a normalised LXMF packet dict (as produced by the session)."""
    return {
        "content": content,
        "source_hash": source_hash,
        "destination_hash": "00" * 16,
        "message_id": msg_id,
        "timestamp": 1700000000.0,
        "title": title,
        "fields": {},
        "signature_validated": True,
        "has_fields": False,
        "source_name": source_name,
    }


def _make_event_with_native(
    native_data: dict[str, Any] | None = None,
    *,
    source_adapter: str = "lxmf-1",
    source_transport_id: str = "ab" * 16,
    payload: dict[str, Any] | None = None,
) -> CanonicalEvent:
    """Create a CanonicalEvent with LXMF native metadata."""
    return CanonicalEvent(
        event_id="evt-enrich-1",
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id=source_transport_id,
        source_channel_id=None,
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload=payload or {"body": "hello world"},
        metadata=EventMetadata(native=NativeMetadata(data=native_data or {})),
    )


# ---------------------------------------------------------------------------
# Codec: source_name → lxmf.display_name enrichment
# ---------------------------------------------------------------------------


def test_codec_injects_display_name_when_source_name_present() -> None:
    """Codec maps non-empty source_name to lxmf.display_name in native metadata."""
    codec = LxmfCodec("lxmf-test", _config())
    packet = _make_lxmf_packet(source_name="Alice Walker")
    event = codec.decode(packet)
    assert event.metadata.native.data["lxmf.display_name"] == "Alice Walker"


def test_codec_omits_display_name_when_source_name_absent() -> None:
    """No source_name key → lxmf.display_name absent from native metadata."""
    codec = LxmfCodec("lxmf-test", _config())
    packet = _make_lxmf_packet(source_name="")
    event = codec.decode(packet)
    assert "lxmf.display_name" not in event.metadata.native.data


def test_codec_preserves_source_hash_alongside_display_name() -> None:
    """Both source_hash and display_name are present in native metadata."""
    codec = LxmfCodec("lxmf-test", _config())
    packet = _make_lxmf_packet(source_hash="cd" * 16, source_name="Bob")
    event = codec.decode(packet)
    assert event.metadata.native.data["source_hash"] == "cd" * 16
    assert event.metadata.native.data["lxmf.display_name"] == "Bob"


def test_codec_decodes_bytes_source_name() -> None:
    """Bytes source_name is decoded as UTF-8."""
    codec = LxmfCodec("lxmf-test", _config())
    packet = _make_lxmf_packet(source_name="Café".encode("utf-8"))
    event = codec.decode(packet)
    assert event.metadata.native.data["lxmf.display_name"] == "Café"


def test_codec_ignores_non_text_source_name() -> None:
    """Non-text source_name (int) is ignored — no lxmf.display_name injected."""
    codec = LxmfCodec("lxmf-test", _config())
    packet = _make_lxmf_packet(source_name="")
    packet["source_name"] = 12345  # type: ignore[assignment]
    event = codec.decode(packet)
    assert "lxmf.display_name" not in event.metadata.native.data


def test_codec_metadata_envelope_preserved_with_display_name() -> None:
    """MEDRE envelope extraction is unaffected by display_name enrichment."""
    codec = LxmfCodec("lxmf-test", _config())
    packet = _make_lxmf_packet(source_name="Alice")
    event = codec.decode(packet)
    # Standard fields are still present.
    assert "source_hash" in event.metadata.native.data
    assert "destination_hash" in event.metadata.native.data
    assert "message_id" in event.metadata.native.data
    assert "timestamp" in event.metadata.native.data
    assert "title" in event.metadata.native.data
    assert "delivery_method" in event.metadata.native.data
    assert "has_fields" in event.metadata.native.data


# ---------------------------------------------------------------------------
# Dispatch: project_source_fields for LXMF native dicts
# ---------------------------------------------------------------------------


def test_dispatch_lxmf_display_name_sets_all_three_fields() -> None:
    """Dispatch wires sender_id, sender_label, and sender_short_label."""
    native = {
        "source_hash": "ab" * 16,
        "lxmf.display_name": "Alice Walker",
    }
    fields = project_source_fields(native, source_adapter="lxmf-1")
    assert fields["source_platform"] == "lxmf"
    assert fields["source_sender_id"] == "ab" * 16
    assert fields["source_sender_label"] == "Alice Walker"
    assert fields["source_sender_short_label"] == "AliceWalker"


def test_dispatch_lxmf_no_display_name_labels_none() -> None:
    """Without display_name, labels are None; sender_id is the hash."""
    native = {"source_hash": "cd" * 16}
    fields = project_source_fields(native, source_adapter="lxmf-1")
    assert fields["source_sender_id"] == "cd" * 16
    assert fields["source_sender_label"] is None
    assert fields["source_sender_short_label"] is None


def test_dispatch_lxmf_with_short_name() -> None:
    """Dispatch maps lxmf.short_name to sender_short_label."""
    native = {
        "source_hash": "ef" * 16,
        "lxmf.display_name": "Mesh Node",
        "lxmf.short_name": "MN",
    }
    fields = project_source_fields(native, source_adapter="lxmf-1")
    assert fields["source_sender_label"] == "Mesh Node"
    assert fields["source_sender_short_label"] == "MN"


def test_dispatch_lxmf_bytes_source_hash_normalised() -> None:
    """Bytes source_hash normalised to hex through dispatch."""
    native = {
        "source_hash": b"\xab\xcd\xef\x01",
    }
    fields = project_source_fields(native, source_adapter="lxmf-1")
    assert fields["source_sender_id"] == "abcdef01"
    assert fields["source_sender_label"] is None


def test_dispatch_lxmf_platform_detected_from_keys() -> None:
    """Platform detection identifies LXMF from source_hash key."""
    native = {"source_hash": "ab" * 16}
    fields = project_source_fields(native, source_adapter="unknown-adapter")
    assert fields["source_platform"] == "lxmf"


# ---------------------------------------------------------------------------
# Renderer prefix: {sender} and {sender_id}
# ---------------------------------------------------------------------------


async def test_renderer_sender_shows_display_name() -> None:
    """{sender} shows the display name when present."""
    renderer = LxmfRenderer(relay_prefix="[{sender}] ")
    event = _make_event_with_native(
        native_data={
            "source_hash": "ab" * 16,
            "lxmf.display_name": "Alice Walker",
        },
        payload={"body": "hello"},
    )
    result = await renderer.render(
        event,
        RenderingContext(target_adapter="lxmf_node", delivery_strategy="direct"),
    )
    assert result.payload["content"] == "[Alice Walker] hello"
    assert result.metadata["relay_prefix_rendered"] == "[Alice Walker] "


async def test_renderer_sender_empty_without_display_name() -> None:
    """{sender} renders empty when no display name; hash never leaks."""
    renderer = LxmfRenderer(relay_prefix="[{sender}] ")
    event = _make_event_with_native(
        native_data={"source_hash": "ab" * 16},
        payload={"body": "hello"},
    )
    result = await renderer.render(
        event,
        RenderingContext(target_adapter="lxmf_node", delivery_strategy="direct"),
    )
    assert result.payload["content"] == "[] hello"
    # The hash must not appear in the prefix.
    assert "ab" * 16 not in result.metadata["relay_prefix_rendered"]


async def test_renderer_sender_id_shows_source_hash() -> None:
    """{sender_id} shows the source_hash."""
    renderer = LxmfRenderer(relay_prefix="({sender_id}) ")
    event = _make_event_with_native(
        native_data={"source_hash": "deadbeef" * 4},
        payload={"body": "ping"},
    )
    result = await renderer.render(
        event,
        RenderingContext(target_adapter="lxmf_node", delivery_strategy="direct"),
    )
    assert result.payload["content"] == f"({('deadbeef' * 4)}) ping"


async def test_renderer_sender_short_shows_compact_name() -> None:
    """{sender_short} shows compact display name when no explicit short_name."""
    renderer = LxmfRenderer(relay_prefix="<{sender_short}> ")
    event = _make_event_with_native(
        native_data={
            "source_hash": "ab" * 16,
            "lxmf.display_name": "Alice Walker",
        },
        payload={"body": "msg"},
    )
    result = await renderer.render(
        event,
        RenderingContext(target_adapter="lxmf_node", delivery_strategy="direct"),
    )
    assert result.payload["content"] == "<AliceWalker> msg"


async def test_renderer_sender_and_sender_id_together() -> None:
    """Both {sender} and {sender_id} resolve correctly in one template."""
    renderer = LxmfRenderer(relay_prefix="{sender}({sender_id}): ")
    event = _make_event_with_native(
        native_data={
            "source_hash": "cafebabe",
            "lxmf.display_name": "Bob",
        },
        payload={"body": "hi"},
    )
    result = await renderer.render(
        event,
        RenderingContext(target_adapter="lxmf_node", delivery_strategy="direct"),
    )
    assert result.payload["content"] == "Bob(cafebabe): hi"


async def test_renderer_no_none_in_prefix_without_display_name() -> None:
    """No literal 'None' appears when display name is absent."""
    renderer = LxmfRenderer(relay_prefix="[{sender}] ")
    event = _make_event_with_native(
        native_data={"source_hash": "ab" * 16},
        payload={"body": "mystery"},
    )
    result = await renderer.render(
        event,
        RenderingContext(target_adapter="lxmf_node", delivery_strategy="direct"),
    )
    assert "None" not in result.payload["content"]


# ---------------------------------------------------------------------------
# Session: _normalise_inbound_message defensive source_name capture
# ---------------------------------------------------------------------------


def test_session_normalise_includes_source_name_key() -> None:
    """_normalise_inbound_message includes source_name in output dict."""
    msg = SimpleNamespace(
        source_hash=b"\xab" * 16,
        destination_hash=b"\x00" * 16,
        hash=b"\xff" * 32,
        timestamp=1700000000.0,
        content="hello",
        title="",
        fields={},
        signature_validated=True,
        method=None,
        source_name="Alice",
    )
    result = LxmfSession._normalise_inbound_message(msg)
    assert result["source_name"] == "Alice"
    # Existing fields are preserved.
    assert result["source_hash"] == "ab" * 16
    assert result["content"] == "hello"


def test_session_normalise_source_name_defaults_empty() -> None:
    """Missing source_name attribute normalises to empty string."""
    msg = SimpleNamespace(
        source_hash=b"\xab" * 16,
        destination_hash=b"\x00" * 16,
        hash=b"\xff" * 32,
        timestamp=1700000000.0,
        content="hello",
        title="",
        fields={},
        signature_validated=True,
        method=None,
    )
    result = LxmfSession._normalise_inbound_message(msg)
    assert result["source_name"] == ""


def test_session_normalise_source_name_bytes_decoded() -> None:
    """Bytes source_name is decoded as UTF-8."""
    msg = SimpleNamespace(
        source_hash=b"\xab" * 16,
        destination_hash=b"\x00" * 16,
        hash=b"\xff" * 32,
        timestamp=1700000000.0,
        content="hello",
        title="",
        fields={},
        signature_validated=True,
        method=None,
        source_name="Café".encode("utf-8"),
    )
    result = LxmfSession._normalise_inbound_message(msg)
    assert result["source_name"] == "Café"


def test_session_normalise_preserves_all_existing_fields() -> None:
    """Adding source_name does not remove or alter existing normalised fields."""
    msg = SimpleNamespace(
        source_hash=b"\xab" * 16,
        destination_hash=b"\xcd" * 16,
        hash=b"\xff" * 32,
        timestamp=1700000000.0,
        content="test content",
        title="Test Title",
        fields={1: "value"},
        signature_validated=True,
        method=None,
        source_name="Alice",
    )
    result = LxmfSession._normalise_inbound_message(msg)
    assert result["source_hash"] == "ab" * 16
    assert result["destination_hash"] == "cd" * 16
    assert result["message_id"] == "ff" * 32
    assert result["timestamp"] == 1700000000.0
    assert result["content"] == "test content"
    assert result["title"] == "Test Title"
    assert result["fields"] == {1: "value"}
    assert result["signature_validated"] is True
    assert result["has_fields"] is True
    assert result["delivery_method"] is None
    assert result["source_name"] == "Alice"


# ---------------------------------------------------------------------------
# Announce loop diagnostics regression
# ---------------------------------------------------------------------------


def test_session_diagnostics_structure_unchanged() -> None:
    """Session diagnostics dataclass still exposes announce fields after
    the source_name addition to _normalise_inbound_message."""
    # The diagnostics dataclass is frozen and its field set must remain
    # stable — adding source_name to normalisation does not alter it.
    from medre.adapters.lxmf.session import LxmfSessionDiagnostics

    expected_fields = {
        "connected",
        "router_running",
        "reconnecting",
        "reconnect_attempts",
        "last_message_time",
        "transient_delivery_failures",
        "permanent_delivery_failures",
        "last_error",
        "known_path_count",
        "propagation_enabled",
        "pending_delivery_count",
        "mode",
        "announces_sent",
        "announce_failures",
        "last_announce_error",
    }
    actual_fields = set(LxmfSessionDiagnostics.__dataclass_fields__.keys())
    assert actual_fields == expected_fields
