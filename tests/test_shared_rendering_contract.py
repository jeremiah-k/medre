# medre-test-tier: compatibility
"""Cross-provider presentation invariants, not a common native wire format."""

from datetime import UTC, datetime

import msgspec
import pytest
from medre.core.events import CanonicalEvent, EventMetadata, EventRelation, NativeRef
from medre.core.events.canonical import RelationTargetFact
from medre.core.rendering.attribution import RelayAttribution, resolve_relay_attribution
from medre.core.rendering.relations import (
    destination_native_message_id,
    describe_reaction,
)
from medre.core.rendering.renderer import RenderingContext
from medre.core.rendering.text_helpers import truncate_text_bytes


def _event():
    return CanonicalEvent(
        event_id="mixed",
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(UTC),
        source_adapter="source",
        source_transport_id="sender-native",
        source_channel_id="source-channel",
        parent_event_id=None,
        lineage=(),
        metadata=EventMetadata(),
        payload={"body": "authored"},
        relations=(
            EventRelation("reply", "parent", None, None, "parent text"),
            EventRelation("reaction", "unknown", None, "👍🏽", None),
        ),
    )


def _provider(transport, template):
    if transport == "matrix":
        pytest.importorskip("medre.adapters.matrix")
        from medre.adapters.matrix.renderer import MatrixRenderer
        from medre.config.adapters.matrix import MatrixConfig

        config = MatrixConfig(
            "target",
            "https://example.org",
            "@bridge:example.org",
            access_token="test-token",
            relay_prefix=template,
        )
        return (
            MatrixRenderer(configs={"target": config}),
            config,
            "!room:example.org",
            lambda r: r.payload["_matrix_operation"]["content"]["body"],
        )
    if transport == "meshtastic":
        pytest.importorskip("medre.adapters.meshtastic")
        from medre.adapters.meshtastic.renderer import MeshtasticRenderer
        from medre.config.adapters.meshtastic import MeshtasticConfig

        config = MeshtasticConfig("target", relay_prefix=template)
        return (
            MeshtasticRenderer(configs={"target": config}),
            config,
            "0",
            lambda r: r.payload["text"],
        )
    if transport == "meshcore":
        pytest.importorskip("medre.adapters.meshcore")
        from medre.adapters.meshcore.renderer import MeshCoreRenderer
        from medre.config.adapters.meshcore import MeshCoreConfig

        config = MeshCoreConfig("target", relay_prefix=template)
        return (
            MeshCoreRenderer(configs={"target": config}),
            config,
            "0",
            lambda r: r.payload["text"],
        )
    if transport == "lxmf":
        pytest.importorskip("medre.adapters.lxmf")
        from medre.adapters.lxmf.renderer import LxmfRenderer
        from medre.config.adapters.lxmf import LxmfConfig

        config = LxmfConfig("target", relay_prefix=template)
        return (
            LxmfRenderer(configs={"target": config}),
            config,
            None,
            lambda r: r.payload["content"],
        )
    if transport == "xmpp":
        pytest.importorskip("medre.adapters.xmpp")
        from medre.adapters.xmpp.renderer import XMPPRenderer
        from medre.adapters.xmpp.config import XMPPConfig

        channel = "friend@example.org"
        config = XMPPConfig("target", peers=(channel,), relay_prefix=template)
        return (
            XMPPRenderer({"target": config}),
            config,
            channel,
            lambda r: r.payload["body"],
        )
    if transport == "chatmail":
        pytest.importorskip("medre.adapters.chatmail")
        from medre.adapters.chatmail.renderer import ChatmailRenderer
        from medre.adapters.chatmail.config import ChatmailConfig

        config = ChatmailConfig("target", chats=(10,), relay_prefix=template)
        return (
            ChatmailRenderer({"target": config}),
            config,
            "10",
            lambda r: r.payload["text"],
        )
    pytest.importorskip("medre.adapters.simplexchat")
    from medre.adapters.simplexchat.renderer import SimplexChatRenderer
    from medre.adapters.simplexchat.config import SimplexChatConfig

    config = SimplexChatConfig("target", chats=("group:1",), relay_prefix=template)
    return (
        SimplexChatRenderer({"target": config}),
        config,
        "group:1",
        lambda r: r.payload["text"],
    )


_TRANSPORTS = (
    "matrix",
    "meshtastic",
    "meshcore",
    "lxmf",
    "xmpp",
    "chatmail",
    "simplexchat",
)


@pytest.mark.parametrize("transport", _TRANSPORTS)
async def test_shared_fallback_and_snapshot(transport):
    renderer, _, channel, body = _provider(transport, "{sender}|{origin_label}: ")
    result = await renderer.render(
        _event(),
        RenderingContext(
            "fallback_text",
            "target",
            channel,
            transport,
            source_origin_label="",
            source_attribution=RelayAttribution(
                source_sender_label="Sender", source_origin_label="ignored"
            ),
        ),
    )
    assert result.metadata["relay_prefix_rendered"] == "Sender|: "
    assert (
        body(result)
        == "Sender|: [replying to: parent text] reacted 👍🏽 to an earlier message authored"
    )


@pytest.mark.parametrize("transport", _TRANSPORTS)
@pytest.mark.parametrize(
    "template", ["{not_a_variable}: ", "{sender:>10}: ", "{sender:{sender}}"]
)
async def test_invalid_prefix_rejected_at_validation_and_render(transport, template):
    renderer, config, channel, _ = _provider(transport, template)
    with pytest.raises(ValueError, match="(?i)relay.prefix"):
        config.validate()
    with pytest.raises(ValueError, match="(?i)relay.prefix"):
        await renderer.render(
            _event(), RenderingContext("fallback_text", "target", channel, transport)
        )


@pytest.mark.parametrize("transport", _TRANSPORTS)
async def test_reaction_and_message_preserve_the_same_sender_label(transport):
    renderer, _, channel, body = _provider(transport, "{sender}: ")
    snapshot = RelayAttribution(source_sender_label="Alice Smith")
    ctx = RenderingContext(
        "fallback_text", "target", channel, transport, source_attribution=snapshot
    )
    message = msgspec.structs.replace(_event(), relations=())
    reaction = msgspec.structs.replace(
        _event(),
        event_kind="message.reacted",
        relations=(EventRelation("reaction", None, None, "👍", None),),
    )
    plain = await renderer.render(message, ctx)
    reacted = await renderer.render(reaction, ctx)
    assert plain.metadata["relay_prefix_rendered"] == "Alice Smith: "
    assert reacted.metadata["relay_prefix_rendered"] == "Alice Smith: "
    assert "Alice Smith: reacted 👍 to an earlier message" in body(reacted)


def test_negative_fact_cannot_be_overridden_by_owned_reference():
    rel = EventRelation(
        "reply",
        None,
        NativeRef("target", "0", "12"),
        None,
        None,
        target_fact=RelationTargetFact(status="ambiguous"),
    )
    assert destination_native_message_id(rel, "target", "0") is None
    assert (
        destination_native_message_id(
            EventRelation("reply", None, NativeRef("target", "1", "12"), None, None),
            "target",
            "0",
        )
        is None
    )
    positive = EventRelation(
        "reply",
        None,
        None,
        None,
        None,
        target_fact=RelationTargetFact(
            status="bound",
            adapter="target",
            native_channel_id="0",
            native_message_id="13",
        ),
    )
    assert destination_native_message_id(positive, "target", "0") == "13"


def test_snapshot_cannot_supply_envelope_ids():
    attr = resolve_relay_attribution(
        _event(),
        source_attribution=RelayAttribution(
            source_adapter_id="wrong",
            source_transport="wrong",
            source_room_or_channel="wrong",
            source_native_message_id="wrong",
            source_native_channel_id="wrong",
        ),
    )
    assert (
        attr.source_adapter_id,
        attr.source_transport,
        attr.source_room_or_channel,
    ) == ("source", "sender-native", "source-channel")
    assert (
        attr.source_native_message_id is None and attr.source_native_channel_id is None
    )


def test_preview_and_utf8_boundaries():
    rel = EventRelation("reaction", None, None, " 👍🏽 ", fallback_text="\t" + "é" * 41)
    assert describe_reaction(rel) == 'reacted  👍🏽  to "' + "é" * 40 + '..."'
    assert truncate_text_bytes("A🔥é", 4) == ("A", True, 7, 1)
    assert truncate_text_bytes("A🔥é", 5) == ("A🔥", True, 7, 5)


async def test_matrix_direct_composition_is_bounded():
    renderer, _, channel, body = _provider("matrix", "")
    result = await renderer.render(
        _event(),
        RenderingContext(
            "direct",
            "target",
            channel,
            "matrix",
            max_text_bytes=26,
        ),
    )
    assert result.truncated
    assert len(body(result).encode("utf-8")) <= 26
    assert (
        "authored"
        not in result.payload["_matrix_operation"]["content"]["formatted_body"]
    )
