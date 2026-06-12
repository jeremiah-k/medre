"""Tests for MatrixRenderer relay attribution, prefix formatting, and
missing-target fallback behaviour.

Split from test_matrix_renderer.py to keep each file under the 1 500-line
hard cap while preserving full test coverage.
"""

from __future__ import annotations

from datetime import datetime, timezone

from medre.adapters.matrix.renderer import MatrixRenderer
from medre.core.events import (
    CanonicalEvent,
    EventMetadata,
    EventRelation,
    NativeMetadata,
    NativeRef,
)
from medre.core.rendering.renderer import RenderingContext
from tests.helpers.matrix_events import (
    make_matrix_event,
    make_meshtastic_event,
)
from tests.helpers.matrix_stubs import StubMatrixConfig as _StubMatrixConfig
from tests.helpers.matrix_stubs import StubMeshtasticConfig as _StubMeshtasticConfig
from tests.helpers.matrix_stubs import StubSourceAttribution as _StubSourceAttribution

# Module-level aliases for concise call-sites in this test file.
_make_event = make_matrix_event
_make_meshtastic_event = make_meshtastic_event

# ---------------------------------------------------------------------------
# Helpers specific to relay attribution / prefix tests
# ---------------------------------------------------------------------------


def _make_meshcore_event(
    source_adapter: str = "meshcore-1",
    payload: dict | None = None,
    native_data: dict | None = None,
) -> CanonicalEvent:
    """Build a CanonicalEvent simulating a MeshCore source."""
    metadata = EventMetadata()
    if native_data:
        metadata = EventMetadata(native=NativeMetadata(data=native_data))
    return CanonicalEvent(
        event_id="evt-mc-1",
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="mc-node-1",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload=payload or {"body": "hello meshcore"},
        metadata=metadata,
    )


def _make_lxmf_event(
    source_adapter: str = "lxmf-1",
    payload: dict | None = None,
    native_data: dict | None = None,
) -> CanonicalEvent:
    """Build a CanonicalEvent simulating an LXMF source."""
    metadata = EventMetadata()
    if native_data:
        metadata = EventMetadata(native=NativeMetadata(data=native_data))
    return CanonicalEvent(
        event_id="evt-lxmf-1",
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="lxmf-node-1",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload=payload or {"body": "hello lxmf"},
        metadata=metadata,
    )


class _StubMeshCoreConfig:
    """Minimal duck-typed config for MeshCore source.

    Matrix outbound prefix is target-local via ``MatrixConfig.relay_prefix``
    only; MeshCore/LXMF origins do not contribute a Matrix outbound prefix.
    """

    def __init__(
        self,
        adapter_id: str = "meshcore-1",
        meshnet_name: str = "",
        mmrelay_compatibility: bool = False,
    ) -> None:
        self.adapter_id = adapter_id
        self.meshnet_name = meshnet_name
        self.mmrelay_compatibility = mmrelay_compatibility


class _StubLXMFConfig:
    """Minimal duck-typed config for LXMF source.

    Matrix outbound prefix is target-local via ``MatrixConfig.relay_prefix``
    only; MeshCore/LXMF origins do not contribute a Matrix outbound prefix.
    """

    def __init__(
        self,
        adapter_id: str = "lxmf-1",
        meshnet_name: str = "",
        mmrelay_compatibility: bool = False,
    ) -> None:
        self.adapter_id = adapter_id
        self.meshnet_name = meshnet_name
        self.mmrelay_compatibility = mmrelay_compatibility


# ---------------------------------------------------------------------------
# Native-relations-closure: missing-target fallback/suppression tests
# ---------------------------------------------------------------------------


class TestMatrixMissingTargetFallback:
    """When a relation has no resolvable Matrix-native target, the renderer
    must produce an explicit fallback or suppression — never a malformed
    m.relates_to with missing/empty event_id.

    This class covers the gap between "has a valid Matrix native ref" and
    "has a foreign native ref" — specifically the case where
    target_native_ref is None (no native ref at all).
    """

    async def test_reply_no_native_ref_no_relates_to(self) -> None:
        """Reply with target_native_ref=None produces no m.relates_to.

        Without a Matrix-native target, the renderer cannot emit
        m.in_reply_to.  The body is just the relay text — no fallback
        quoting, no malformed m.relates_to with empty event_id.
        """
        renderer = MatrixRenderer()
        relation = EventRelation(
            relation_type="reply",
            target_event_id="orig-001",
            target_native_ref=None,
            key=None,
            fallback_text="original message text",
        )
        event = _make_event(
            payload={"body": "my reply"},
            relations=(relation,),
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        # No m.relates_to — cannot target Matrix-native reply without ref
        assert "m.relates_to" not in result.payload
        # Body must be clean relay text, no quoting
        assert result.payload["body"] == "my reply"
        assert "> <" not in result.payload["body"]

    async def test_reaction_no_native_ref_emote_fallback(self) -> None:
        """Reaction with target_native_ref=None produces m.emote fallback.

        Without a Matrix-native target, a true m.reaction (m.annotation)
        cannot be emitted.  The renderer falls back to an m.emote with
        MMRelay-compatible metadata — never a broken m.annotation.
        """
        renderer = MatrixRenderer()
        relation = EventRelation(
            relation_type="reaction",
            target_event_id="orig-001",
            target_native_ref=None,
            key="👍",
            fallback_text=None,
        )
        event = _make_event(
            payload={"body": "👍"},
            relations=(relation,),
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        # Must NOT produce a true Matrix reaction
        assert "_matrix_event_type" not in result.payload
        assert "m.relates_to" not in result.payload
        # Must produce emote fallback
        assert result.payload["msgtype"] == "m.emote"
        assert "👍" in result.payload["body"]

    async def test_reply_with_resolved_native_ref_correct_structure(self) -> None:
        """Reply with resolved Matrix native ref produces correct m.in_reply_to.

        The m.relates_to structure must be exactly:
        {"m.in_reply_to": {"event_id": "<matrix_event_id>"}}
        using the native_message_id from the target_native_ref, not
        the canonical target_event_id.
        """
        renderer = MatrixRenderer()
        relation = EventRelation(
            relation_type="reply",
            target_event_id="canonical-orig-001",
            target_native_ref=NativeRef(
                adapter="matrix-1",
                native_channel_id="!room:server",
                native_message_id="$matrix-evt-abc123",
            ),
            key=None,
            fallback_text="original text",
        )
        event = _make_event(
            payload={"body": "my reply"},
            relations=(relation,),
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        relates = result.payload["m.relates_to"]
        # Must use native_message_id, NOT canonical target_event_id
        assert relates == {"m.in_reply_to": {"event_id": "$matrix-evt-abc123"}}
        assert "canonical-orig-001" not in str(relates)

    async def test_reaction_with_resolved_native_ref_correct_structure(self) -> None:
        """Reaction with resolved Matrix native ref produces correct m.annotation.

        The m.relates_to structure must be exactly:
        {"rel_type": "m.annotation", "event_id": "<matrix_event_id>", "key": "<emoji>"}
        using the native_message_id from the target_native_ref.
        """
        renderer = MatrixRenderer()
        relation = EventRelation(
            relation_type="reaction",
            target_event_id="canonical-orig-002",
            target_native_ref=NativeRef(
                adapter="matrix-1",
                native_channel_id="!room:server",
                native_message_id="$matrix-evt-def456",
            ),
            key="❤️",
            fallback_text=None,
        )
        event = _make_event(
            payload={"body": "❤️"},
            relations=(relation,),
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        # Must be a true Matrix reaction
        assert result.payload["_matrix_event_type"] == "m.reaction"
        assert result.payload["m.relates_to"] == {
            "rel_type": "m.annotation",
            "event_id": "$matrix-evt-def456",
            "key": "❤️",
        }
        # No body/msgtype on true reactions
        assert "msgtype" not in result.payload
        assert "body" not in result.payload
        # Must NOT use canonical target_event_id
        assert "canonical-orig-002" not in str(result.payload)

    async def test_reaction_no_target_no_broken_annotation(self) -> None:
        """Reaction without any target produces emote fallback, not m.annotation.

        Ensures the renderer never emits a malformed m.annotation with
        empty/missing event_id when no Matrix-native target is available.
        """
        renderer = MatrixRenderer()
        relation = EventRelation(
            relation_type="reaction",
            target_event_id=None,
            target_native_ref=None,
            key="🔥",
            fallback_text="some original text",
        )
        event = _make_event(
            payload={"body": "🔥"},
            relations=(relation,),
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        payload = result.payload
        # Absolutely no m.annotation or _matrix_event_type
        assert payload.get("_matrix_event_type") != "m.reaction"
        if "m.relates_to" in payload:
            rel = payload["m.relates_to"]
            # If present, must NOT be m.annotation
            assert rel.get("rel_type") != "m.annotation"
        # Must be emote fallback
        assert payload["msgtype"] == "m.emote"

    async def test_reply_wrong_adapter_no_relates_to(self) -> None:
        """Reply targeting a different adapter produces no m.in_reply_to.

        A Meshtastic native ref must not produce a Matrix relation.
        The renderer suppresses the relation cleanly.
        """
        renderer = MatrixRenderer()
        relation = EventRelation(
            relation_type="reply",
            target_event_id="orig-001",
            target_native_ref=NativeRef(
                adapter="meshtastic-1",
                native_channel_id="0",
                native_message_id="12345",
            ),
            key=None,
            fallback_text="original",
        )
        event = _make_event(
            payload={"body": "my reply"},
            relations=(relation,),
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        assert "m.relates_to" not in result.payload
        # Body is clean relay text
        assert result.payload["body"] == "my reply"


# ---------------------------------------------------------------------------
# Core attribution integration tests
# ---------------------------------------------------------------------------


class TestMatrixCoreAttributionIntegration:
    """MatrixRenderer uses core attribution helpers for all relay prefix
    formatting.  Covers missing-variable safety, MeshCore/LXMF prefix
    support, unknown placeholder policy, and reaction prefix integration.
    """

    # -- Missing variables: no 'None' in output --

    async def test_missing_longname_no_none(self) -> None:
        """Missing sender renders as empty, never the literal 'None'."""
        renderer = MatrixRenderer(
            configs={
                "matrix-1": _StubMatrixConfig(
                    adapter_id="matrix-1",
                    relay_prefix="[{sender}]: ",
                ),
            },
        )
        event = _make_meshtastic_event(
            source_adapter="radio-alpha",
            native_data={"shortname": "A"},  # no longname → sender empty
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        body: str = result.payload["body"]
        assert "None" not in body
        # Prefix with empty longname → "[]: hello mesh"
        assert body == "[]: hello mesh"

    async def test_missing_shortname_no_none(self) -> None:
        """Missing sender_short renders as empty, never 'None'."""
        renderer = MatrixRenderer(
            configs={
                "matrix-1": _StubMatrixConfig(
                    adapter_id="matrix-1",
                    relay_prefix="[{sender_short}]: ",
                ),
            },
        )
        event = _make_meshtastic_event(
            source_adapter="radio-alpha",
            native_data={"longname": "Alice"},  # no shortname → sender_short empty
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        body = result.payload["body"]
        assert "None" not in body
        assert body == "[]: hello mesh"

    async def test_missing_all_vars_no_none(self) -> None:
        """All prefix variables missing: no 'None' anywhere in body."""
        renderer = MatrixRenderer(
            configs={
                "matrix-1": _StubMatrixConfig(
                    adapter_id="matrix-1",
                    relay_prefix="<{sender}/{sender_short}/{sender_id}> ",
                ),
            },
        )
        event = _make_meshtastic_event(
            source_adapter="radio-alpha",
            native_data={},  # nothing
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        body = result.payload["body"]
        assert "None" not in body
        assert body == "<//> hello mesh"

    # -- MeshCore prefix: uses pubkey/from_id/source_sender_id --

    async def test_meshcore_prefix_uses_pubkey(self) -> None:
        """MeshCore event renders without prefix (no relay_prefix configured for MeshCore).

        The prefix is target-local via ``MatrixConfig.relay_prefix`` only.
        MeshCoreConfig does not contribute a Matrix outbound prefix.
        The renderer must not crash on non-Meshtastic sources.
        """
        renderer = MatrixRenderer(
            source_configs={
                "meshcore-1": _StubMeshCoreConfig(
                    adapter_id="meshcore-1",
                ),
            },
        )
        event = _make_meshcore_event(
            native_data={"pubkey_prefix": "a1b2c3"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        body: str = result.payload["body"]
        # No prefix applied — body is unchanged
        assert body == "hello meshcore"
        assert "None" not in body

    async def test_meshcore_prefix_source_sender_id_alias(self) -> None:
        """MeshCore event with namespaced keys renders without prefix.

        MeshCoreConfig does not contribute a Matrix outbound prefix.
        The renderer handles MeshCore native keys gracefully without crash.
        """
        renderer = MatrixRenderer(
            source_configs={
                "meshcore-1": _StubMeshCoreConfig(
                    adapter_id="meshcore-1",
                ),
            },
        )
        event = _make_meshcore_event(
            native_data={"pubkey_prefix": "deadbeef"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        body = result.payload["body"]
        # No prefix applied — body is unchanged
        assert body == "hello meshcore"

    # -- LXMF prefix: falls back to safe sender id --

    async def test_lxmf_prefix_uses_source_hash(self) -> None:
        """LXMF event renders without prefix (no relay_prefix configured for LXMF).

        Matrix outbound prefix is target-local via ``MatrixConfig.relay_prefix``
        only.  LxmfConfig does not contribute a Matrix outbound prefix.
        The renderer must not crash on non-Meshtastic sources.
        """
        renderer = MatrixRenderer(
            source_configs={
                "lxmf-1": _StubLXMFConfig(
                    adapter_id="lxmf-1",
                ),
            },
        )
        event = _make_lxmf_event(
            native_data={"source_hash": "feedface"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        body: str = result.payload["body"]
        # No prefix applied — body is unchanged
        assert body == "hello lxmf"
        assert "None" not in body

    async def test_lxmf_prefix_no_sender_safe_empty(self) -> None:
        """LXMF event without source_hash renders body unchanged, not None."""
        renderer = MatrixRenderer(
            source_configs={
                "lxmf-1": _StubLXMFConfig(
                    adapter_id="lxmf-1",
                ),
            },
        )
        event = _make_lxmf_event(native_data={})
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        body = result.payload["body"]
        assert "None" not in body
        # No prefix — body is unchanged
        assert body == "hello lxmf"

    # -- Unknown placeholder policy: left unchanged --

    async def test_unknown_placeholder_left_unchanged(self) -> None:
        """Unknown template variable {bogus} is left as {bogus} in output."""
        renderer = MatrixRenderer(
            configs={
                "matrix-1": _StubMatrixConfig(
                    adapter_id="matrix-1",
                    relay_prefix="[{bogus}] ",
                ),
            },
        )
        event = _make_meshtastic_event(
            source_adapter="radio-alpha",
            native_data={"longname": "Alice"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        body = result.payload["body"]
        # Unknown variable left unchanged in prefix
        assert "{bogus}" in body
        assert body == "[{bogus}] hello mesh"
        # Metadata records the unknown variable
        assert "relay_prefix_template" in result.metadata
        assert "relay_prefix_unknown_variables" in result.metadata
        assert "bogus" in result.metadata["relay_prefix_unknown_variables"]
        assert result.metadata["relay_prefix_formatting_error"] is not None

    async def test_unknown_placeholder_mixed_with_known(self) -> None:
        """Mixed known + unknown variables: known resolved, unknown left."""
        renderer = MatrixRenderer(
            configs={
                "matrix-1": _StubMatrixConfig(
                    adapter_id="matrix-1",
                    relay_prefix="[{sender}/{weird}] ",
                ),
            },
        )
        event = _make_meshtastic_event(
            source_adapter="radio-alpha",
            native_data={"longname": "Alice"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        body = result.payload["body"]
        assert "[Alice/{weird}]" in body

    # -- Reaction prefix still uses core formatter --

    async def test_reaction_prefix_missing_vars_no_none(self) -> None:
        """Reaction prefix with missing variables never renders 'None'."""
        renderer = MatrixRenderer(
            source_configs={
                "radio-alpha": _StubMeshtasticConfig(
                    adapter_id="radio-alpha",
                    mmrelay_compatibility=True,
                ),
            },
            configs={
                "matrix-1": _StubMatrixConfig(
                    adapter_id="matrix-1",
                    relay_prefix="[{sender}] ",
                ),
            },
        )
        relation = EventRelation(
            relation_type="reaction",
            target_event_id="orig-001",
            target_native_ref=None,
            key="👍",
            fallback_text=None,
        )
        event = _make_meshtastic_event(
            source_adapter="radio-alpha",
            native_data={"shortname": "A"},  # no longname → sender empty
            payload={"body": "👍"},
            relations=(relation,),
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        body = result.payload["body"]
        assert "None" not in body
        # Empty sender → "[]" prefix in reaction emote
        assert "[]" in body

    # -- Meshtastic prefix unchanged (regression guard) --

    async def test_meshtastic_prefix_unchanged(self) -> None:
        """Meshtastic → Matrix prefix output is unchanged after wiring."""
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
                    origin_label="AlphaNet",
                ),
            },
            configs={
                "matrix-1": _StubMatrixConfig(
                    adapter_id="matrix-1",
                    relay_prefix="[{sender}/AlphaNet]: ",
                ),
            },
        )
        event = _make_meshtastic_event(
            source_adapter="radio-alpha",
            native_data={"longname": "Alice", "shortname": "A", "packet_id": "42"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        assert result.payload["body"] == "[Alice/AlphaNet]: hello mesh"

    # -- Metadata recorded in rendered result --

    async def test_prefix_formatter_metadata_recorded(self) -> None:
        """PrefixFormatterResult metadata is recorded in RenderingResult.metadata."""
        renderer = MatrixRenderer(
            configs={
                "matrix-1": _StubMatrixConfig(
                    adapter_id="matrix-1",
                    relay_prefix="[{sender}] ",
                ),
            },
        )
        event = _make_meshtastic_event(
            source_adapter="radio-alpha",
            native_data={"longname": "Alice"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        assert "relay_prefix_template" in result.metadata
        assert result.metadata["relay_prefix_template"] == "[{sender}] "
        assert "sender" in result.metadata["relay_prefix_variables_used"]
        assert result.metadata["relay_prefix_formatting_error"] is None
        assert result.metadata["relay_prefix_unknown_variables"] == ()

    async def test_no_prefix_metadata_when_no_template(self) -> None:
        """No prefix_formatter metadata when no relay prefix template is configured."""
        renderer = MatrixRenderer()
        event = _make_event(payload={"body": "hello"})
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        assert "relay_prefix_template" not in result.metadata

    # -- Reaction prefix metadata correctness --

    async def test_reaction_emote_fallback_prefix_metadata_correct(self) -> None:
        """Reaction emote fallback records prefix metadata from _format_reaction_prefix.

        The reaction path must produce correct relay_prefix_template and
        related keys — not stale body-level prefix metadata.
        """
        renderer = MatrixRenderer(
            source_configs={
                "radio-alpha": _StubMeshtasticConfig(
                    adapter_id="radio-alpha",
                    mmrelay_compatibility=False,
                ),
            },
            configs={
                "matrix-1": _StubMatrixConfig(
                    adapter_id="matrix-1",
                    relay_prefix="[{sender}] ",
                ),
            },
        )
        relation = EventRelation(
            relation_type="reaction",
            target_event_id="orig-001",
            target_native_ref=None,
            key="👍",
            fallback_text=None,
        )
        event = _make_meshtastic_event(
            source_adapter="radio-alpha",
            native_data={"longname": "Alice"},
            payload={"body": "👍"},
            relations=(relation,),
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        # Emote fallback was rendered (no native target)
        assert result.payload["msgtype"] == "m.emote"
        # Reaction prefix metadata must be present and correct
        assert "relay_prefix_template" in result.metadata
        assert result.metadata["relay_prefix_template"] == "[{sender}] "
        assert "relay_prefix_rendered" in result.metadata
        assert "sender" in result.metadata["relay_prefix_variables_used"]

    async def test_reaction_true_annotation_no_stale_prefix_metadata(self) -> None:
        """True m.reaction annotation carries no stale body prefix metadata.

        When a true m.reaction is produced (body removed), no prefix
        metadata from the body path should leak into the result.
        """
        renderer = MatrixRenderer(
            source_configs={
                "radio-alpha": _StubMeshtasticConfig(
                    adapter_id="radio-alpha",
                    mmrelay_compatibility=False,
                ),
            },
            configs={
                "matrix-1": _StubMatrixConfig(
                    adapter_id="matrix-1",
                    relay_prefix="[{sender}] ",
                ),
            },
        )
        relation = EventRelation(
            relation_type="reaction",
            target_event_id="canonical-orig",
            target_native_ref=NativeRef(
                adapter="matrix-1",
                native_channel_id="!room:server",
                native_message_id="$evt-reaction-target",
            ),
            key="❤️",
            fallback_text=None,
        )
        event = _make_meshtastic_event(
            source_adapter="radio-alpha",
            native_data={"longname": "Alice"},
            payload={"body": "❤️"},
            relations=(relation,),
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        # True m.reaction — body removed
        assert result.payload["_matrix_event_type"] == "m.reaction"
        assert "body" not in result.payload
        # No stale body prefix metadata; true reactions return empty dict
        assert "relay_prefix_template" not in result.metadata
        assert "relay_prefix_rendered" not in result.metadata

    async def test_plain_text_prefix_metadata_unchanged(self) -> None:
        """Plain text event with prefix: metadata unchanged by reaction fix."""
        renderer = MatrixRenderer(
            configs={
                "matrix-1": _StubMatrixConfig(
                    adapter_id="matrix-1",
                    relay_prefix="[{sender}] ",
                ),
            },
        )
        event = _make_meshtastic_event(
            source_adapter="radio-alpha",
            native_data={"longname": "Bob"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        assert "relay_prefix_template" in result.metadata
        assert result.metadata["relay_prefix_template"] == "[{sender}] "
        assert result.metadata["relay_prefix_rendered"] == "[Bob] "
        assert result.payload["body"] == "[Bob] hello mesh"


# ---------------------------------------------------------------------------
# Target-local prefix (MatrixConfig.relay_prefix) tests
# ---------------------------------------------------------------------------


class TestMatrixTargetLocalPrefix:
    """MatrixConfig.relay_prefix provides target-local prefix template."""

    async def test_target_local_prefix_renders_origin_label(self) -> None:
        """MatrixConfig.relay_prefix produces prefixed output."""
        renderer = MatrixRenderer(
            source_attribution={
                "radio-alpha": _StubSourceAttribution(
                    adapter_id="radio-alpha",
                    origin_label="East Mesh",
                ),
            },
            configs={
                "matrix-1": _StubMatrixConfig(
                    adapter_id="matrix-1",
                    relay_prefix="[{origin_label}]: ",
                ),
            },
        )
        event = _make_meshtastic_event(
            source_adapter="radio-alpha",
            native_data={"longname": "Alice"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        assert result.payload["body"] == "[East Mesh]: hello mesh"

    async def test_origin_label_from_source_attribution_in_prefix(self) -> None:
        """Source origin_label from source_attribution registry appears in prefix."""
        renderer = MatrixRenderer(
            source_attribution={
                "radio-alpha": _StubSourceAttribution(
                    adapter_id="radio-alpha",
                    origin_label="West Net",
                    meshnet_name="WestMesh",
                ),
            },
            configs={
                "matrix-1": _StubMatrixConfig(
                    adapter_id="matrix-1",
                    relay_prefix="[{origin_label}/{sender_id}]: ",
                ),
            },
        )
        event = _make_meshtastic_event(
            source_adapter="radio-alpha",
            native_data={"longname": "Bob", "from_id": "!42"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        body: str = result.payload["body"]
        assert "West Net" in body
        assert "!42" in body

    async def test_no_prefix_without_target_local_config(self) -> None:
        """Without target-local relay_prefix, no prefix is prepended."""
        renderer = MatrixRenderer()
        event = _make_meshtastic_event(
            source_adapter="radio-alpha",
            native_data={"longname": "Alice"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        assert result.payload["body"] == "hello mesh"
        assert "relay_prefix_template" not in result.metadata
