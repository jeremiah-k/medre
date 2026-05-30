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
* Default AdapterCapabilities() produce correct decisions for every
  mapped event kind.
* All boolean and string capability fields fail-closed when None.
* Thread relation produces direct/direct strategy via FallbackResolver.

Uses the real CapabilityDecisionResolver and AdapterCapabilities
with values drawn from transport-profile JSONs.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import datetime, timezone

import pytest

from medre.core.contracts.adapter import AdapterCapabilities
from medre.core.events.canonical import CanonicalEvent, EventRelation, NativeRef
from medre.core.events.kinds import EventKind
from medre.core.events.metadata import EventMetadata
from medre.core.planning.capability_decision import (
    CapabilityDecisionResolver,
)
from medre.core.planning.capability_decision import resolver as _module_resolver
from medre.core.planning.fallback_resolution import FallbackResolver
from medre.core.routing.models import RouteTarget

_DEFAULT_TARGET = RouteTarget(adapter="test_target", channel="ch-out")

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


# ---------------------------------------------------------------------------
# Default AdapterCapabilities decisions
# ---------------------------------------------------------------------------


class TestDefaultCapabilitiesDecisions:
    """Verify the default AdapterCapabilities() instance produces correct
    decisions for every mapped event kind.

    Default values: text=True, replies/reactions/edits/deletes="native",
    attachments=False, presence=False, metadata_fields=False.

    This documents the runtime behaviour when no explicit caps are
    configured — the adapter gets default-caps and the resolver must
    produce deterministic, correct decisions.
    """

    def test_default_caps_text_native(self) -> None:
        """text=True → native/direct for message.text."""
        caps = AdapterCapabilities()
        event = make_text_event()
        decision = _resolver.decide(event, caps)
        assert decision.capability_level == "native"
        assert decision.delivery_strategy == "direct"
        assert decision.supported is True

    def test_default_caps_reactions_native(self) -> None:
        """reactions='native' → native/direct for message.reacted."""
        caps = AdapterCapabilities()
        event = make_reaction_event()
        decision = _resolver.decide(event, caps)
        assert decision.capability_level == "native"
        assert decision.delivery_strategy == "direct"
        assert decision.supported is True

    def test_default_caps_edits_native(self) -> None:
        """edits='native' → native/direct for message.edited."""
        caps = AdapterCapabilities()
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
            relations=(),
            payload={"body": "edited"},
            metadata=EventMetadata(),
        )
        decision = _resolver.decide(event, caps)
        assert decision.capability_level == "native"
        assert decision.delivery_strategy == "direct"
        assert decision.supported is True

    def test_default_caps_deletes_native(self) -> None:
        """deletes='native' → native/direct for message.deleted."""
        caps = AdapterCapabilities()
        event = CanonicalEvent(
            event_id=str(uuid.uuid4()),
            event_kind=EventKind.MESSAGE_DELETED,
            schema_version=1,
            timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc),
            source_adapter="test",
            source_transport_id="test",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={},
            metadata=EventMetadata(),
        )
        decision = _resolver.decide(event, caps)
        assert decision.capability_level == "native"
        assert decision.delivery_strategy == "direct"
        assert decision.supported is True

    def test_default_caps_attachments_unsupported(self) -> None:
        """attachments=False → unsupported/skip for message.file."""
        caps = AdapterCapabilities()
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
            payload={"filename": "doc.pdf"},
            metadata=EventMetadata(),
        )
        decision = _resolver.decide(event, caps)
        assert decision.capability_level == "unsupported"
        assert decision.delivery_strategy == "skip"
        assert decision.supported is False

    def test_default_caps_presence_unsupported(self) -> None:
        """presence=False → unsupported/skip for presence.changed."""
        caps = AdapterCapabilities()
        event = CanonicalEvent(
            event_id=str(uuid.uuid4()),
            event_kind=EventKind.PRESENCE_CHANGED,
            schema_version=1,
            timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc),
            source_adapter="test",
            source_transport_id="test",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={},
            metadata=EventMetadata(),
        )
        decision = _resolver.decide(event, caps)
        assert decision.capability_level == "unsupported"
        assert decision.delivery_strategy == "skip"
        assert decision.supported is False

    def test_default_caps_metadata_fields_unsupported(self) -> None:
        """metadata_fields=False → unsupported/skip for telemetry.received."""
        caps = AdapterCapabilities()
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
        assert decision.capability_level == "unsupported"
        assert decision.delivery_strategy == "skip"
        assert decision.supported is False

    def test_default_caps_unmapped_kind_passthrough(self) -> None:
        """Unmapped event kind with default caps → native/direct passthrough."""
        caps = AdapterCapabilities()
        event = CanonicalEvent(
            event_id=str(uuid.uuid4()),
            event_kind="plugin.custom",
            schema_version=1,
            timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc),
            source_adapter="test",
            source_transport_id="test",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={},
            metadata=EventMetadata(),
        )
        decision = _resolver.decide(event, caps)
        assert decision.capability_level == "native"
        assert decision.delivery_strategy == "direct"
        assert decision.supported is True
        assert decision.capability_field is None
        assert decision.reason is None


# ---------------------------------------------------------------------------
# All boolean and string None fail-closed conformance
# ---------------------------------------------------------------------------


class TestAllNoneFailClosedConformance:
    """Verify that every capability field used by the resolver produces
    unsupported/skip when set to None.

    This is a cross-cutting conformance check: it parametrises over all
    boolean and string capability fields that are referenced by the
    event-kind and relation mappings, ensuring None → unsupported for
    every single one.
    """

    @pytest.mark.parametrize(
        ("event_kind", "field"),
        [
            ("message.file", "attachments"),
            ("presence.changed", "presence"),
            ("telemetry.received", "metadata_fields"),
            ("telemetry.position", "metadata_fields"),
        ],
        ids=lambda v: str(v),
    )
    def test_boolean_none_fail_closed(self, event_kind: str, field: str) -> None:
        """Boolean field set to None → unsupported/skip."""
        caps = dataclasses.replace(
            AdapterCapabilities(),
            **{field: None},
        )
        event = CanonicalEvent(
            event_id=str(uuid.uuid4()),
            event_kind=event_kind,
            schema_version=1,
            timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc),
            source_adapter="test",
            source_transport_id="test",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={},
            metadata=EventMetadata(),
        )
        decision = _resolver.decide(event, caps)

        assert decision.capability_level == "unsupported"
        assert decision.delivery_strategy == "skip"
        assert decision.supported is False
        assert decision.capability_field == field

    @pytest.mark.parametrize(
        ("event_kind", "field"),
        [
            ("message.reacted", "reactions"),
            ("message.edited", "edits"),
            ("message.deleted", "deletes"),
        ],
        ids=lambda v: str(v),
    )
    def test_string_none_fail_closed(self, event_kind: str, field: str) -> None:
        """String field set to None → unsupported/skip."""
        caps = dataclasses.replace(
            AdapterCapabilities(),
            **{field: None},
        )
        event = CanonicalEvent(
            event_id=str(uuid.uuid4()),
            event_kind=event_kind,
            schema_version=1,
            timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc),
            source_adapter="test",
            source_transport_id="test",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={},
            metadata=EventMetadata(),
        )
        decision = _resolver.decide(event, caps)

        assert decision.capability_level == "unsupported"
        assert decision.delivery_strategy == "skip"
        assert decision.supported is False
        assert decision.capability_field == field


# ---------------------------------------------------------------------------
# Thread relation FallbackResolver conformance
# ---------------------------------------------------------------------------


class TestThreadRelationFallbackResolverConformance:
    """Verify that thread relations produce direct strategy via FallbackResolver.

    Thread capability is deferred: thread relations do not produce a
    capability candidate.  The FallbackResolver must return direct
    for thread-carrying events.
    """

    def test_thread_only_relation_fallback_resolver_direct(self) -> None:
        """Thread-only relation → direct via FallbackResolver."""
        thread_rel = EventRelation(
            relation_type="thread",
            target_event_id="evt-parent",
            target_native_ref=NativeRef(
                adapter="test",
                native_channel_id="ch-0",
                native_message_id="native-thread-001",
            ),
            key=None,
            fallback_text=None,
        )
        caps = AdapterCapabilities()
        fb = FallbackResolver()
        event = CanonicalEvent(
            event_id=str(uuid.uuid4()),
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc),
            source_adapter="test",
            source_transport_id="test",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(thread_rel,),
            payload={"text": "in thread"},
            metadata=EventMetadata(),
        )
        strategy = fb.resolve_fallback(event, _DEFAULT_TARGET, caps).primary_strategy
        assert strategy.method == "direct"

    def test_thread_with_unsupported_reply_reply_wins(self) -> None:
        """Thread + reply where reply is unsupported → skip (reply wins)."""
        thread_rel = EventRelation(
            relation_type="thread",
            target_event_id="evt-parent",
            target_native_ref=NativeRef(
                adapter="test",
                native_channel_id="ch-0",
                native_message_id="native-thread-001",
            ),
            key=None,
            fallback_text=None,
        )
        reply_rel = EventRelation(
            relation_type="reply",
            target_event_id="evt-parent",
            target_native_ref=NativeRef(
                adapter="test",
                native_channel_id="ch-0",
                native_message_id="native-001",
            ),
            key=None,
            fallback_text=None,
        )
        caps = AdapterCapabilities(replies="unsupported")
        fb = FallbackResolver()
        event = CanonicalEvent(
            event_id=str(uuid.uuid4()),
            event_kind="plugin.custom",
            schema_version=1,
            timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc),
            source_adapter="test",
            source_transport_id="test",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(thread_rel, reply_rel),
            payload={"text": "thread reply"},
            metadata=EventMetadata(),
        )
        strategy = fb.resolve_fallback(event, _DEFAULT_TARGET, caps).primary_strategy
        assert strategy.method == "skip"

    def test_thread_unmapped_event_kind_resolver_passthrough(self) -> None:
        """Thread-only relation on unmapped event kind → passthrough via resolver."""
        thread_rel = EventRelation(
            relation_type="thread",
            target_event_id="evt-parent",
            target_native_ref=NativeRef(
                adapter="test",
                native_channel_id="ch-0",
                native_message_id="native-thread-001",
            ),
            key=None,
            fallback_text=None,
        )
        caps = AdapterCapabilities()
        event = CanonicalEvent(
            event_id=str(uuid.uuid4()),
            event_kind="plugin.custom",
            schema_version=1,
            timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc),
            source_adapter="test",
            source_transport_id="test",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(thread_rel,),
            payload={"text": "custom"},
            metadata=EventMetadata(),
        )
        decision = _resolver.decide(event, caps)

        assert decision.capability_level == "native"
        assert decision.delivery_strategy == "direct"
        assert decision.supported is True
        assert decision.capability_field is None
        assert decision.reason is None


# ---------------------------------------------------------------------------
# Module singleton parity
# ---------------------------------------------------------------------------


class TestModuleResolverSingletonParity:
    """Verify the imported module-level resolver singleton produces the same
    decisions as a freshly constructed instance."""

    def test_singleton_matches_fresh_instance_text(self) -> None:
        caps = AdapterCapabilities(text=True)
        event = make_text_event()
        singleton_decision = _module_resolver.decide(event, caps)
        fresh_decision = _resolver.decide(event, caps)
        assert singleton_decision == fresh_decision

    def test_singleton_matches_fresh_instance_unsupported(self) -> None:
        caps = AdapterCapabilities(reactions="unsupported")
        event = make_reaction_event()
        singleton_decision = _module_resolver.decide(event, caps)
        fresh_decision = _resolver.decide(event, caps)
        assert singleton_decision == fresh_decision
