# medre-test-tier: compatibility
"""Native transports carry the generic source and relation context."""

# Construct a literal stanza; no XML is parsed.
from xml.etree.ElementTree import Element  # nosec B405

import pytest
from msgspec.structs import replace

from medre.adapters.lxmf.fields import LxmfFieldsHelper
from medre.adapters.xmpp import bridge_context as xmpp_context
from medre.core.events import CanonicalEvent, EventRelation, NativeRef
from medre.core.rendering.bridge_context import build_bridge_context


@pytest.fixture(params=["reply", "reaction"])
def context_source_event(
    request: pytest.FixtureRequest, sample_event_with_relations: CanonicalEvent
) -> CanonicalEvent:
    reference = NativeRef("radio-east", "3", "0")
    return replace(
        sample_event_with_relations,
        event_kind=(
            "message.reacted" if request.param == "reaction" else "message.created"
        ),
        source_adapter="radio-east",
        source_channel_id="3",
        source_native_ref=reference,
        relations=(
            EventRelation(
                relation_type=request.param,
                target_event_id=None,
                target_native_ref=reference,
                key="🔥" if request.param == "reaction" else None,
                fallback_text=None,
            ),
        ),
    )


def test_lxmf_field_envelope_carries_generic_reply_context(
    context_source_event: CanonicalEvent,
) -> None:
    """A scoped reply reference survives LXMF without Matrix content."""
    source = context_source_event
    context = build_bridge_context(source)
    assert context is not None
    wire = LxmfFieldsHelper.embed_envelope(
        {},
        event_id=source.event_id,
        relations=source.relations,
        metadata={},
        source_adapter=source.source_adapter,
        source_transport_id=source.source_transport_id,
        source_channel_id=source.source_channel_id,
        bridge_context=context,
    )
    recovered = LxmfFieldsHelper.extract_envelope(wire)
    assert recovered is not None
    assert recovered["bridge_context"] == context
    assert recovered["relations"][0]["target_native_ref"]["native_message_id"] == "0"
    assert recovered["bridge_context"]["relations"][0]["key"] == source.relations[0].key


def test_xmpp_extension_carries_generic_reply_context(
    context_source_event: CanonicalEvent,
) -> None:
    """XMPP stanza extensions can carry the same bounded source/relation hints."""
    context = build_bridge_context(context_source_event)
    assert context is not None
    encoded = xmpp_context.encode_context(context)
    assert isinstance(encoded, str)
    assert xmpp_context.decode_context(encoded) == context

    stanza = Element("message")
    xmpp_context.append_context(stanza, encoded)
    assert xmpp_context.extract_context(stanza) == context
