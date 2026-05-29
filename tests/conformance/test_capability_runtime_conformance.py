"""Capability runtime conformance tests.

Asserts that CapabilityDecisionResolver produces correct capability
decisions for transport-profile capability configurations:
native -> direct, fallback -> fallback_text, unsupported -> skip.

Also asserts:
* text=false -> skip text events
* attachments=false -> skip file events
* metadata_fields=false -> skip telemetry events
* Relation capability fields map correctly: replies, reactions,
  edits, deletes.

Uses the real CapabilityDecisionResolver and AdapterCapabilities
with values drawn from transport-profile JSONs.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from medre.core.contracts.adapter import AdapterCapabilities
from medre.core.events.canonical import CanonicalEvent, EventRelation
from medre.core.events.kinds import EventKind
from medre.core.events.metadata import EventMetadata
from medre.core.planning.capability_decision import CapabilityDecisionResolver

from .conftest import make_reaction_event, make_reply_event, make_text_event

# Re-use singleton resolver
_resolver = CapabilityDecisionResolver()


def _make_caps(**overrides) -> AdapterCapabilities:
    """Build AdapterCapabilities with defaults matching a capable adapter."""
    defaults = dict(
        text=True,
        title=False,
        replies="native",
        reactions="native",
        edits="unsupported",
        deletes="unsupported",
        attachments=False,
        metadata_fields=True,
        delivery_receipts=False,
        store_and_forward=False,
        direct_messages=False,
        channels=True,
        ack_tracking=False,
        async_delivery=True,
        identity_encryption=False,
        presence=False,
        topic_rooms=False,
        mesh_routing=False,
        priority_delivery=False,
    )
    defaults.update(overrides)
    return AdapterCapabilities(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Native -> direct
# ---------------------------------------------------------------------------


class TestCapabilityNativeDirect:
    """Capability level 'native' yields delivery_strategy='direct'."""

    def test_text_native_is_direct(self):
        """text=True -> native -> direct."""
        caps = _make_caps(text=True)
        event = make_text_event()
        decision = _resolver.decide(event, caps)
        assert decision.capability_level == "native"
        assert decision.delivery_strategy == "direct"
        assert decision.supported is True

    def test_replies_native_is_direct(self):
        """replies='native' -> direct for reply events."""
        caps = _make_caps(replies="native")
        event = make_reply_event()
        decision = _resolver.decide(event, caps)
        assert decision.delivery_strategy == "direct"
        assert decision.supported is True

    def test_reactions_native_is_direct(self):
        """reactions='native' -> direct for reaction events."""
        caps = _make_caps(reactions="native")
        event = make_reaction_event()
        decision = _resolver.decide(event, caps)
        assert decision.delivery_strategy == "direct"
        assert decision.supported is True


# ---------------------------------------------------------------------------
# Fallback -> fallback_text
# ---------------------------------------------------------------------------


class TestCapabilityFallbackText:
    """Capability level 'fallback' yields delivery_strategy='fallback_text'."""

    def test_replies_fallback(self):
        """replies='fallback' -> fallback_text for reply events."""
        caps = _make_caps(replies="fallback")
        event = make_reply_event()
        decision = _resolver.decide(event, caps)
        assert decision.capability_level == "fallback"
        assert decision.delivery_strategy == "fallback_text"
        assert decision.supported is True

    def test_reactions_fallback(self):
        """reactions='fallback' -> fallback_text for reaction events."""
        caps = _make_caps(reactions="fallback")
        event = make_reaction_event()
        decision = _resolver.decide(event, caps)
        assert decision.delivery_strategy == "fallback_text"
        assert decision.supported is True


# ---------------------------------------------------------------------------
# Unsupported -> skip
# ---------------------------------------------------------------------------


class TestCapabilityUnsupportedSkip:
    """Capability level 'unsupported' yields delivery_strategy='skip'."""

    def test_text_false_skips_text_events(self):
        """text=False -> unsupported -> skip for MESSAGE_CREATED."""
        caps = _make_caps(text=False)
        event = make_text_event()
        decision = _resolver.decide(event, caps)
        assert decision.capability_level == "unsupported"
        assert decision.delivery_strategy == "skip"
        assert decision.supported is False

    def test_attachments_false_skips_file_events(self):
        """attachments=False -> skip for MESSAGE_FILE."""
        caps = _make_caps(attachments=False)
        event = CanonicalEvent(
            event_id=str(uuid.uuid4()),
            event_kind=EventKind.MESSAGE_FILE,
            schema_version=1,
            timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc),
            source_adapter="test",
            source_transport_id="test",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "file.pdf"},
            metadata=EventMetadata(),
        )
        decision = _resolver.decide(event, caps)
        assert decision.delivery_strategy == "skip"
        assert decision.supported is False

    def test_metadata_fields_false_skips_telemetry(self):
        """metadata_fields=False -> skip for TELEMETRY_RECEIVED."""
        caps = _make_caps(metadata_fields=False)
        event = CanonicalEvent(
            event_id=str(uuid.uuid4()),
            event_kind=EventKind.TELEMETRY_RECEIVED,
            schema_version=1,
            timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc),
            source_adapter="test",
            source_transport_id="test",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"metrics": {}},
            metadata=EventMetadata(),
        )
        decision = _resolver.decide(event, caps)
        assert decision.delivery_strategy == "skip"
        assert decision.supported is False

    def test_reactions_unsupported_skips_reaction(self):
        """reactions='unsupported' -> skip for reaction events."""
        caps = _make_caps(reactions="unsupported")
        event = make_reaction_event()
        decision = _resolver.decide(event, caps)
        assert decision.delivery_strategy == "skip"
        assert decision.supported is False


# ---------------------------------------------------------------------------
# Transport-profile relation conformance
# ---------------------------------------------------------------------------


class TestCapabilityTransportProfileRelationConformance:
    """Assert relation capability mapping against transport profiles."""

    def test_matrix_profile_replies_native(self, matrix_capabilities):
        """Matrix transport profile: replies=native -> direct for reply."""
        event = make_reply_event(target_adapter="matrix_conf")
        decision = _resolver.decide(event, matrix_capabilities)
        assert decision.delivery_strategy == "direct"

    def test_matrix_profile_reactions_native(self, matrix_capabilities):
        """Matrix transport profile: reactions=native -> direct."""
        event = make_reaction_event(target_adapter="matrix_conf")
        decision = _resolver.decide(event, matrix_capabilities)
        assert decision.delivery_strategy == "direct"

    def test_matrix_profile_edits_unsupported(self, matrix_capabilities):
        """Matrix transport profile: edits=unsupported -> skip for edits."""
        rel = EventRelation(
            relation_type="edit",
            target_event_id=None,
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = CanonicalEvent(
            event_id=str(uuid.uuid4()),
            event_kind=EventKind.MESSAGE_EDITED,
            schema_version=1,
            timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc),
            source_adapter="test",
            source_transport_id="test",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"body": "edited"},
            metadata=EventMetadata(),
        )
        decision = _resolver.decide(event, matrix_capabilities)
        assert decision.delivery_strategy == "skip"

    def test_meshtastic_profile_replies_native(self, meshtastic_capabilities):
        """Meshtastic transport profile: replies=native -> direct."""
        event = make_reply_event(
            target_adapter="mesh_conf",
            target_channel="0",
            target_message_id="42",
        )
        decision = _resolver.decide(event, meshtastic_capabilities)
        assert decision.delivery_strategy == "direct"

    def test_meshtastic_profile_reactions_native(self, meshtastic_capabilities):
        """Meshtastic transport profile: reactions=native -> direct."""
        event = make_reaction_event(
            target_adapter="mesh_conf",
            target_channel="0",
            target_message_id="42",
        )
        decision = _resolver.decide(event, meshtastic_capabilities)
        assert decision.delivery_strategy == "direct"
