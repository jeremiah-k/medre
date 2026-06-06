"""Matrix <-> Meshtastic relation, reaction, capability, failure classification,
adapter lifecycle, and rendering evidence operational tests.

Contains:
- Reply relation rendering (both directions)
- Fallback text rendering
- Cross-platform reactions
- Capability decision characterization
- Failure classification characterization
- Adapter lifecycle (start/stop) state tests
- Rendering evidence characterization

All tests use fakes -- no real Matrix homeserver, no real Meshtastic radio.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest

from medre.adapters.fakes.matrix import FakeMatrixAdapter
from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter
from medre.adapters.matrix.renderer import MatrixRenderer
from medre.adapters.meshtastic.adapter import MeshtasticAdapter
from medre.adapters.meshtastic.renderer import MeshtasticRenderer
from medre.core.contracts.adapter import (
    AdapterCapabilities,
)
from medre.core.events.canonical import (
    CanonicalEvent,
    EventRelation,
    NativeRef,
)
from medre.core.events.kinds import EventKind
from medre.core.events.metadata import EventMetadata, NativeMetadata
from medre.core.planning.capability_decision import (
    CapabilityDecisionResolver,
)
from medre.core.rendering.evidence import RenderingEvidence
from medre.core.rendering.renderer import (
    RenderingResult,
)

# Reuse helpers from the flow module.
from tests.operational.test_matrix_meshtastic_flow import (
    _make_ctx,
    _make_meshtastic_config,
    _matrix_inbound_event,
    _matrix_rendering_context,
    _mesh_rendering_context,
    _meshtastic_inbound_event,
)

# ===========================================================================
# Matrix -> Meshtastic reply rendering
# ===========================================================================


class TestMatrixToMeshtasticReply:
    """Reply relation resolved with native ref."""

    @pytest.mark.asyncio
    async def test_reply_uses_native_ref_reply_id(self) -> None:
        config = _make_meshtastic_config()
        renderer = MeshtasticRenderer(configs={"test_mesh": config})

        target_ref = NativeRef(
            adapter="test_mesh",
            native_channel_id="0",
            native_message_id="9999",
        )
        reply_rel = EventRelation(
            relation_type="reply",
            target_event_id=None,
            target_native_ref=target_ref,
            key=None,
            fallback_text=None,
        )
        event = _matrix_inbound_event(
            body="Reply msg",
            relations=(reply_rel,),
        )
        ctx = _mesh_rendering_context()

        result = await renderer.render(event, ctx)
        assert result.payload["reply_id"] == 9999
        assert "Reply msg" in result.payload["text"]

    @pytest.mark.asyncio
    async def test_reply_without_native_ref_plain_text(self) -> None:
        config = _make_meshtastic_config()
        renderer = MeshtasticRenderer(configs={"test_mesh": config})

        reply_rel = EventRelation(
            relation_type="reply",
            target_event_id="canonical-123",
            target_native_ref=None,
            key=None,
            fallback_text="original text",
        )
        event = _matrix_inbound_event(
            body="Reply msg",
            relations=(reply_rel,),
        )
        ctx = _mesh_rendering_context()
        result = await renderer.render(event, ctx)
        assert "reply_id" not in result.payload
        assert "Reply msg" in result.payload["text"]


# ===========================================================================
# Matrix -> Meshtastic fallback text rendering
# ===========================================================================


class TestMatrixToMeshtasticFallbackText:
    """fallback_text degrades relations but preserves envelope."""

    @pytest.mark.asyncio
    async def test_fallback_text_preserves_channel_and_meshnet(self) -> None:
        config = _make_meshtastic_config()
        renderer = MeshtasticRenderer(configs={"test_mesh": config})

        reply_rel = EventRelation(
            relation_type="reply",
            target_event_id="evt-999",
            target_native_ref=None,
            key=None,
            fallback_text="original msg",
        )
        event = _matrix_inbound_event(
            body="Fallback reply",
            relations=(reply_rel,),
        )
        ctx = _mesh_rendering_context(delivery_strategy="fallback_text")

        result = await renderer.render(event, ctx)
        assert result.payload["channel_index"] == config.default_channel
        assert result.payload["meshnet_name"] == config.meshnet_name
        assert "reply_id" not in result.payload
        assert result.fallback_applied == "strategy_fallback_text"


# ===========================================================================
# Meshtastic -> Matrix reply/relation rendering
# ===========================================================================


class TestMeshtasticToMatrixReply:
    """Meshtastic reply_id maps to Matrix m.relates_to."""

    @pytest.mark.asyncio
    async def test_reply_with_matrix_native_ref(self) -> None:
        renderer = MatrixRenderer()
        target_ref = NativeRef(
            adapter="test_matrix",
            native_channel_id="!test:example.com",
            native_message_id="$orig001",
        )
        reply_rel = EventRelation(
            relation_type="reply",
            target_event_id=None,
            target_native_ref=target_ref,
            key=None,
            fallback_text=None,
        )
        event = _meshtastic_inbound_event(
            body="Mesh reply",
            relations=(reply_rel,),
        )
        ctx = _matrix_rendering_context()

        result = await renderer.render(event, ctx)
        relates = result.payload.get("m.relates_to")
        assert relates is not None
        assert relates["m.in_reply_to"]["event_id"] == "$orig001"

    @pytest.mark.asyncio
    async def test_fallback_text_no_m_relates_to(self) -> None:
        renderer = MatrixRenderer()
        reply_rel = EventRelation(
            relation_type="reply",
            target_event_id=None,
            target_native_ref=None,
            key=None,
            fallback_text="original",
        )
        event = _meshtastic_inbound_event(
            body="Fallback reply",
            relations=(reply_rel,),
        )
        ctx = _matrix_rendering_context(delivery_strategy="fallback_text")

        result = await renderer.render(event, ctx)
        assert "m.relates_to" not in result.payload
        assert result.fallback_applied == "strategy_fallback_text"


# ===========================================================================
# Cross-platform reaction rendering
# ===========================================================================


class TestCrossPlatformReactions:
    """Reactions between Matrix and Meshtastic adapters."""

    @pytest.mark.asyncio
    async def test_matrix_reaction_to_meshtastic_descriptive(self) -> None:
        config = _make_meshtastic_config()
        renderer = MeshtasticRenderer(configs={"test_mesh": config})

        target_ref = NativeRef(
            adapter="test_mesh",
            native_channel_id="0",
            native_message_id="42",
        )
        rel = EventRelation(
            relation_type="reaction",
            target_event_id=None,
            target_native_ref=target_ref,
            key="\U0001f44d",
            fallback_text=None,
        )
        event = CanonicalEvent(
            event_id=str(uuid.uuid4()),
            event_kind=EventKind.MESSAGE_REACTED,
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="test_matrix",
            source_transport_id="@alice:example.com",
            source_channel_id="!test:example.com",
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"body": "\U0001f44d"},
            metadata=EventMetadata(),
        )
        ctx = _mesh_rendering_context()

        result = await renderer.render(event, ctx)
        assert result.payload.get("emoji") != 1
        assert "reacted" in result.payload["text"]
        assert "\U0001f44d" in result.payload["text"]
        assert result.payload["reply_id"] == 42

    @pytest.mark.asyncio
    async def test_meshtastic_reaction_to_matrix_emote_fallback(self) -> None:
        renderer = MatrixRenderer()

        target_ref = NativeRef(
            adapter="test_matrix",
            native_channel_id="!test:example.com",
            native_message_id="$orig001",
        )
        rel = EventRelation(
            relation_type="reaction",
            target_event_id=None,
            target_native_ref=target_ref,
            key="\u2764\ufe0f",
            fallback_text=None,
            metadata={"meshtastic_reply_id": "42", "meshtastic_emoji": "1"},
        )
        event = CanonicalEvent(
            event_id=str(uuid.uuid4()),
            event_kind=EventKind.MESSAGE_REACTED,
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="test_mesh",
            source_transport_id="!abc123",
            source_channel_id="0",
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"body": "\u2764\ufe0f", "key": "\u2764\ufe0f"},
            metadata=EventMetadata(
                native=NativeMetadata(
                    data={
                        "packet_id": 100,
                        "longname": "Sender",
                        "shortname": "Snd",
                    }
                )
            ),
        )
        ctx = _matrix_rendering_context()

        result = await renderer.render(event, ctx)
        relates = result.payload.get("m.relates_to", {})
        assert relates.get("rel_type") == "m.annotation"
        assert relates.get("key") == "\u2764\ufe0f"


# ===========================================================================
# Capability decision (characterization)
# ===========================================================================


class TestCapabilityDecision:
    """CapabilityDecisionResolver for Matrix/Meshtastic."""

    def test_matrix_native_reactions(self) -> None:
        caps = AdapterCapabilities(
            reactions="native",
            replies="native",
            text=True,
        )
        event = CanonicalEvent(
            event_id=str(uuid.uuid4()),
            event_kind=EventKind.MESSAGE_REACTED,
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="test_mesh",
            source_transport_id="!abc123",
            source_channel_id="0",
            parent_event_id=None,
            lineage=(),
            relations=(
                EventRelation(
                    relation_type="reaction",
                    target_event_id="evt-1",
                    target_native_ref=None,
                    key="\U0001f44d",
                    fallback_text=None,
                ),
            ),
            payload={"body": "\U0001f44d"},
            metadata=EventMetadata(),
        )

        resolver = CapabilityDecisionResolver()
        decision = resolver.decide(event, caps, target_adapter="test_matrix")
        assert decision.supported is True
        assert decision.capability_level == "native"

    def test_meshtastic_edits_unsupported(self) -> None:
        caps = AdapterCapabilities(
            edits="unsupported",
            text=True,
        )
        event = CanonicalEvent(
            event_id=str(uuid.uuid4()),
            event_kind=EventKind.MESSAGE_EDITED,
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="test_matrix",
            source_transport_id="@alice:example.com",
            source_channel_id="!test:example.com",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "edited"},
            metadata=EventMetadata(),
        )

        resolver = CapabilityDecisionResolver()
        decision = resolver.decide(event, caps, target_adapter="test_mesh")
        assert decision.supported is False
        assert decision.delivery_strategy == "skip"
        assert decision.capability_field == "edits"

    def test_fallback_reactions(self) -> None:
        caps = AdapterCapabilities(
            reactions="fallback",
            text=True,
        )
        event = CanonicalEvent(
            event_id=str(uuid.uuid4()),
            event_kind=EventKind.MESSAGE_REACTED,
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="test_matrix",
            source_transport_id="@alice:example.com",
            source_channel_id="!test:example.com",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "\U0001f44d"},
            metadata=EventMetadata(),
        )

        resolver = CapabilityDecisionResolver()
        decision = resolver.decide(event, caps, target_adapter="test_mesh")
        assert decision.supported is True
        assert decision.capability_level == "fallback"
        assert decision.delivery_strategy == "fallback_text"

    def test_text_passthrough_for_created(self) -> None:
        caps = AdapterCapabilities(text=True)
        event = _matrix_inbound_event()

        resolver = CapabilityDecisionResolver()
        decision = resolver.decide(event, caps, target_adapter="test_mesh")
        assert decision.supported is True
        assert decision.capability_level == "native"

    def test_capability_suppressed_reaction_event(self) -> None:
        caps = AdapterCapabilities(
            reactions="unsupported",
            text=True,
        )
        event = CanonicalEvent(
            event_id=str(uuid.uuid4()),
            event_kind=EventKind.MESSAGE_REACTED,
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="test_mesh",
            source_transport_id="!abc",
            source_channel_id="0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "\U0001f44d"},
            metadata=EventMetadata(),
        )
        resolver = CapabilityDecisionResolver()
        decision = resolver.decide(event, caps, target_adapter="test_matrix")
        assert decision.supported is False
        assert decision.capability_field == "reactions"


# ===========================================================================
# Failure classification (characterization)
# ===========================================================================


class TestFailureClassification:
    """Transient vs permanent failure classification."""

    def test_transient_error_detection(self) -> None:
        from medre.adapters.matrix.adapter import _is_transient_error

        assert _is_transient_error(asyncio.TimeoutError()) is True
        assert _is_transient_error(ConnectionError("conn")) is True
        assert _is_transient_error(OSError("os")) is True

    def test_permanent_error_not_transient(self) -> None:
        from medre.adapters.matrix.adapter import _is_transient_error
        from medre.adapters.matrix.errors import MatrixSendError

        perm = MatrixSendError("bad", transient=False)
        assert _is_transient_error(perm) is False

    def test_rate_limit_detection(self) -> None:
        from medre.adapters.matrix.adapter import (
            _is_nio_rate_limited_response,
            _is_transient_error,
            _NioRateLimitError,
        )

        exc = _NioRateLimitError("rate limited", retry_after_ms=2000)
        assert exc.retry_after_ms == 2000
        assert _is_transient_error(exc) is True

        class _FakeResp:
            errcode = "M_LIMIT_EXCEEDED"
            status_code = 429

        assert _is_nio_rate_limited_response(_FakeResp()) is True


# ===========================================================================
# G. Adapter lifecycle state tests
# ===========================================================================


class TestAdapterLifecycle:
    """Adapter start/stop state management.

    Tests verify that adapters transition between started/stopped states
    correctly and that the state contract is documented.  These tests do
    not assert post-stop delivery rejection because the current adapter
    contract does not enforce it -- adapters are passive recipients of
    RenderingResult objects and do not guard against post-stop calls.
    """

    @pytest.mark.asyncio
    async def test_matrix_adapter_start_stop_state(self) -> None:
        """Matrix adapter transitions from started to stopped."""
        adapter = FakeMatrixAdapter("test_matrix")
        ctx = _make_ctx("test_matrix")
        try:
            await adapter.start(ctx)
            assert adapter.is_started

            # Delivery works while started.
            result = RenderingResult(
                event_id="evt-1",
                target_adapter="test_matrix",
                target_channel="!test:example.com",
                payload={"msgtype": "m.text", "body": "msg"},
            )
            delivery = await adapter.deliver(result)
            assert delivery is not None

            await adapter.stop()
            assert not adapter.is_started
        finally:
            if adapter.is_started:
                await adapter.stop()

    @pytest.mark.asyncio
    async def test_meshtastic_adapter_start_stop_state(self) -> None:
        """Meshtastic adapter transitions from started to stopped."""
        config = _make_meshtastic_config()
        adapter = FakeMeshtasticAdapter(config)
        ctx = _make_ctx("test_mesh")
        try:
            await adapter.start(ctx)
            assert adapter.is_started

            await adapter.stop()
            assert not adapter.is_started
        finally:
            if adapter.is_started:
                await adapter.stop()

    @pytest.mark.asyncio
    async def test_real_meshtastic_adapter_stops_and_health_changes(self) -> None:
        """Real MeshtasticAdapter (fake connection) health transitions."""
        config = _make_meshtastic_config()
        adapter = MeshtasticAdapter(config)
        ctx = _make_ctx("test_mesh")
        try:
            await adapter.start(ctx)

            info = await adapter.health_check()
            assert info.health == "healthy"

            await adapter.stop()
            info_after = await adapter.health_check()
            assert info_after.health in ("failed", "unknown")
        finally:
            try:
                await adapter.stop()
            except Exception:
                pass


# ===========================================================================
# Rendering evidence (characterization)
# ===========================================================================


class TestRenderingEvidence:
    """RenderingEvidence captures decision inputs and outcomes."""

    def test_evidence_from_context_and_result(self) -> None:
        ctx = _mesh_rendering_context(max_text_bytes=227)
        result = RenderingResult(
            event_id="evt-1",
            target_adapter="test_mesh",
            target_channel="0",
            payload={"text": "Hello", "channel_index": 0, "meshnet_name": ""},
            metadata={"truncated": False},
            truncated=False,
        )
        evidence = RenderingEvidence.from_context_and_result(
            renderer_name="meshtastic",
            ctx=ctx,
            result=result,
        )
        assert evidence.renderer == "meshtastic"
        assert evidence.target_platform == "meshtastic"
        assert evidence.max_text_bytes == 227
        assert evidence.truncated is False
        assert evidence.schema_version == "1"

    def test_evidence_to_dict_json_safe(self) -> None:
        ctx = _matrix_rendering_context()
        result = RenderingResult(
            event_id="evt-1",
            target_adapter="test_matrix",
            target_channel="!test:example.com",
            payload={"msgtype": "m.text", "body": "Hello"},
            metadata={},
        )
        evidence = RenderingEvidence.from_context_and_result(
            renderer_name="matrix",
            ctx=ctx,
            result=result,
        )
        d = evidence.to_dict()
        for k, v in d.items():
            assert isinstance(
                v, (str, int, float, bool, type(None), list)
            ), f"Key {k!r} has non-JSON-safe value: {type(v)}"
