"""Tests for the capability decision model and resolver.

Covers:
- Event-kind to capability field mapping (native/fallback/unsupported).
- Boolean capability fields (true → native/direct, false → unsupported/skip).
- Three-level string fields (native/fallback/unsupported → direct/fallback_text/skip).
- Relation to capability field mapping (reply → replies, reaction → reactions, etc.).
- Multiple-relation precedence (unsupported > fallback > native).
- Tie-breaking by first candidate in evaluation order.
- Thread relation deferral (no capability_field, native/direct).
- Passthrough for unknown/unmapped event kinds.
- Reason string stability (matching existing user-visible strings).
- ``supported`` field consistency.
- Wrapper parity: ``capability_unsupported()`` returns None when decision.supported.
- FallbackResolver parity: strategy matches resolver's delivery_strategy.
"""

from __future__ import annotations

import dataclasses

import pytest

from medre.core.contracts.adapter import AdapterCapabilities
from medre.core.events.canonical import EventRelation, NativeRef
from medre.core.planning.capabilities import capability_unsupported
from medre.core.planning.capability_decision import (
    CapabilityDecision,
    CapabilityDecisionResolver,
    resolver,
)
from medre.core.planning.fallback_resolution import FallbackResolver
from tests.helpers.pipeline import make_event

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEFAULT_CAPS = AdapterCapabilities()
"""Frozen default instance reused by ``_caps_with``."""


def _caps_with(field: str, value: object) -> AdapterCapabilities:
    """Construct an ``AdapterCapabilities`` with one field overridden.

    Explicit ``isinstance`` narrowing converts the parametrize-typed
    ``object`` into ``bool | str`` before constructing.  The narrowed
    dict is passed through ``dataclasses.replace(..., **changes: Any)``
    which Pyright accepts without a cast or ignore.
    """
    assert isinstance(
        value, (bool, str)
    ), f"Expected bool|str for {field!r}, got {type(value).__name__}"
    return dataclasses.replace(_DEFAULT_CAPS, **{field: value})


_REPLY_RELATION = EventRelation(
    relation_type="reply",
    target_event_id="evt-parent",
    target_native_ref=NativeRef(
        adapter="test_adapter",
        native_channel_id="ch-0",
        native_message_id="native-001",
    ),
    key=None,
    fallback_text="original",
)

_REACTION_RELATION = EventRelation(
    relation_type="reaction",
    target_event_id="evt-parent",
    target_native_ref=None,
    key="\U0001f44d",
    fallback_text=None,
)

_EDIT_RELATION = EventRelation(
    relation_type="edit",
    target_event_id="evt-parent",
    target_native_ref=None,
    key=None,
    fallback_text=None,
)

_DELETE_RELATION = EventRelation(
    relation_type="delete",
    target_event_id="evt-parent",
    target_native_ref=None,
    key=None,
    fallback_text=None,
)

_THREAD_RELATION = EventRelation(
    relation_type="thread",
    target_event_id="evt-parent",
    target_native_ref=NativeRef(
        adapter="test_adapter",
        native_channel_id="ch-0",
        native_message_id="native-thread-001",
    ),
    key=None,
    fallback_text=None,
)


# ===================================================================
# TestEventKindMapping
# ===================================================================


class TestEventKindMapping:
    """Verify event-kind → capability field mapping via the resolver."""

    @pytest.mark.parametrize(
        "event_kind,cap_field,cap_value,expected_level,expected_strategy",
        [
            # -- Reactions (3-level string) ---
            ("message.reacted", "reactions", "native", "native", "direct"),
            ("message.reacted", "reactions", "fallback", "fallback", "fallback_text"),
            ("message.reacted", "reactions", "unsupported", "unsupported", "skip"),
            # -- Edits (3-level string) ---
            ("message.edited", "edits", "native", "native", "direct"),
            ("message.edited", "edits", "fallback", "fallback", "fallback_text"),
            ("message.edited", "edits", "unsupported", "unsupported", "skip"),
            # -- Deletes (3-level string) ---
            ("message.deleted", "deletes", "native", "native", "direct"),
            ("message.deleted", "deletes", "fallback", "fallback", "fallback_text"),
            ("message.deleted", "deletes", "unsupported", "unsupported", "skip"),
            # -- File / attachments (boolean) ---
            ("message.file", "attachments", True, "native", "direct"),
            ("message.file", "attachments", False, "unsupported", "skip"),
            # -- Text (boolean) ---
            ("message.created", "text", True, "native", "direct"),
            ("message.created", "text", False, "unsupported", "skip"),
            ("message.text", "text", True, "native", "direct"),
            ("message.text", "text", False, "unsupported", "skip"),
            # -- Presence (boolean) ---
            ("presence.changed", "presence", True, "native", "direct"),
            ("presence.changed", "presence", False, "unsupported", "skip"),
            # -- Telemetry (boolean metadata_fields) ---
            ("telemetry.received", "metadata_fields", True, "native", "direct"),
            ("telemetry.received", "metadata_fields", False, "unsupported", "skip"),
            ("telemetry.position", "metadata_fields", True, "native", "direct"),
            ("telemetry.position", "metadata_fields", False, "unsupported", "skip"),
        ],
        ids=lambda v: str(v),
    )
    def test_event_kind_resolution(
        self,
        event_kind: str,
        cap_field: str,
        cap_value: object,
        expected_level: str,
        expected_strategy: str,
    ) -> None:
        caps = _caps_with(cap_field, cap_value)
        event = make_event(event_kind=event_kind)
        decision = resolver.decide(event, caps)

        assert decision.capability_level == expected_level
        assert decision.delivery_strategy == expected_strategy
        assert decision.supported == (expected_level != "unsupported")
        assert decision.capability_field == cap_field
        assert decision.event_kind == event_kind


# ===================================================================
# TestPassthroughKinds
# ===================================================================


class TestPassthroughKinds:
    """Verify that unmapped event kinds produce native/direct passthrough."""

    @pytest.mark.parametrize(
        "event_kind",
        ["plugin.custom", "identity.updated", "system.audit", "delivery.queued"],
    )
    def test_passthrough_native_direct(self, event_kind: str) -> None:
        event = make_event(event_kind=event_kind)
        caps = AdapterCapabilities()
        decision = resolver.decide(event, caps)

        assert decision.capability_level == "native"
        assert decision.delivery_strategy == "direct"
        assert decision.supported is True
        assert decision.capability_field is None
        assert decision.reason is None


# ===================================================================
# TestRelationMapping
# ===================================================================


class TestRelationMapping:
    """Verify relation → capability field mapping.

    Uses event kinds with no capability mapping (plugin.custom) to isolate
    relation behavior, or event kinds where the event-kind field is True/native
    so the relation candidate determines the outcome when it is more severe.
    """

    def test_reply_native(self) -> None:
        """Reply relation with replies=native → native (no degradation)."""
        caps = AdapterCapabilities(replies="native")
        # Use an event kind with no capability mapping to isolate relation.
        event = make_event(
            event_kind="plugin.custom",
            relations=(_REPLY_RELATION,),
        )
        decision = resolver.decide(event, caps)

        assert decision.capability_level == "native"
        assert decision.delivery_strategy == "direct"
        assert decision.capability_field == "replies"

    def test_reply_fallback(self) -> None:
        """Reply relation with replies=fallback → fallback_text."""
        caps = AdapterCapabilities(replies="fallback")
        event = make_event(
            event_kind="plugin.custom",
            relations=(_REPLY_RELATION,),
        )
        decision = resolver.decide(event, caps)

        assert decision.capability_level == "fallback"
        assert decision.delivery_strategy == "fallback_text"
        assert decision.capability_field == "replies"

    def test_reply_unsupported(self) -> None:
        caps = AdapterCapabilities(replies="unsupported")
        event = make_event(
            event_kind="plugin.custom",
            relations=(_REPLY_RELATION,),
        )
        decision = resolver.decide(event, caps)

        assert decision.capability_level == "unsupported"
        assert decision.delivery_strategy == "skip"
        assert decision.supported is False
        assert decision.reason is not None
        assert "replies" in decision.reason

    def test_reaction_relation_unsupported(self) -> None:
        caps = AdapterCapabilities(reactions="unsupported")
        event = make_event(
            event_kind="plugin.custom",
            relations=(_REACTION_RELATION,),
        )
        decision = resolver.decide(event, caps)

        assert decision.capability_level == "unsupported"
        assert decision.delivery_strategy == "skip"
        assert decision.capability_field == "reactions"

    def test_edit_relation_unsupported(self) -> None:
        caps = AdapterCapabilities(edits="unsupported")
        event = make_event(
            event_kind="plugin.custom",
            relations=(_EDIT_RELATION,),
        )
        decision = resolver.decide(event, caps)

        assert decision.capability_level == "unsupported"
        assert decision.capability_field == "edits"

    def test_delete_relation_unsupported(self) -> None:
        caps = AdapterCapabilities(deletes="unsupported")
        event = make_event(
            event_kind="plugin.custom",
            relations=(_DELETE_RELATION,),
        )
        decision = resolver.decide(event, caps)

        assert decision.capability_level == "unsupported"
        assert decision.capability_field == "deletes"

    def test_no_relations_no_relation_check(self) -> None:
        """Event with no relations does not check relation capabilities."""
        caps = AdapterCapabilities(replies="unsupported", reactions="unsupported")
        event = make_event(event_kind="message.text")
        decision = resolver.decide(event, caps)

        # No relations, no relation candidates → text is True by default → native.
        assert decision.capability_level == "native"
        assert decision.delivery_strategy == "direct"


# ===================================================================
# TestThreadDeferral
# ===================================================================


class TestThreadDeferral:
    """Verify thread relation is deferred (no capability_field)."""

    def test_thread_only_relation_is_passthrough(self) -> None:
        """Thread relation does not produce a capability candidate."""
        caps = AdapterCapabilities()
        event = make_event(event_kind="message.text", relations=(_THREAD_RELATION,))
        decision = resolver.decide(event, caps)

        # Thread is deferred → no relation candidate.
        # event_kind=message.text maps to "text" which defaults to True → native.
        assert decision.capability_level == "native"
        assert decision.delivery_strategy == "direct"
        assert decision.supported is True

    def test_thread_alongside_reply_reply_wins(self) -> None:
        """Thread + reply: reply produces a candidate, thread is skipped."""
        caps = AdapterCapabilities(replies="unsupported")
        event = make_event(
            event_kind="plugin.custom",
            relations=(_THREAD_RELATION, _REPLY_RELATION),
        )
        decision = resolver.decide(event, caps)

        assert decision.capability_level == "unsupported"
        assert decision.capability_field == "replies"


# ===================================================================
# TestMultipleRelationPrecedence
# ===================================================================


class TestMultipleRelationPrecedence:
    """Verify multiple-relation precedence: unsupported > fallback > native."""

    def test_unsupported_beats_fallback(self) -> None:
        """reply(fallback) + reaction(unsupported) → unsupported."""
        caps = AdapterCapabilities(replies="fallback", reactions="unsupported")
        event = make_event(
            event_kind="plugin.custom",
            relations=(_REPLY_RELATION, _REACTION_RELATION),
        )
        decision = resolver.decide(event, caps)

        assert decision.capability_level == "unsupported"
        assert decision.capability_field == "reactions"

    def test_unsupported_beats_native(self) -> None:
        """reply(native) + reaction(unsupported) → unsupported."""
        caps = AdapterCapabilities(replies="native", reactions="unsupported")
        event = make_event(
            event_kind="plugin.custom",
            relations=(_REPLY_RELATION, _REACTION_RELATION),
        )
        decision = resolver.decide(event, caps)

        assert decision.capability_level == "unsupported"
        assert decision.capability_field == "reactions"

    def test_fallback_beats_native(self) -> None:
        """reply(native) + reaction(fallback) → fallback."""
        caps = AdapterCapabilities(replies="native", reactions="fallback")
        event = make_event(
            event_kind="plugin.custom",
            relations=(_REPLY_RELATION, _REACTION_RELATION),
        )
        decision = resolver.decide(event, caps)

        assert decision.capability_level == "fallback"
        assert decision.delivery_strategy == "fallback_text"
        assert decision.capability_field == "reactions"

    def test_tie_break_first_in_order(self) -> None:
        """Two fallback relations: first relation in event order wins."""
        caps = AdapterCapabilities(replies="fallback", reactions="fallback")
        event = make_event(
            event_kind="plugin.custom",
            relations=(_REPLY_RELATION, _REACTION_RELATION),
        )
        decision = resolver.decide(event, caps)

        assert decision.capability_level == "fallback"
        assert decision.capability_field == "replies"

    def test_tie_break_first_in_order_reversed(self) -> None:
        """Two fallback relations in reverse order: first wins."""
        caps = AdapterCapabilities(replies="fallback", reactions="fallback")
        event = make_event(
            event_kind="plugin.custom",
            relations=(_REACTION_RELATION, _REPLY_RELATION),
        )
        decision = resolver.decide(event, caps)

        assert decision.capability_level == "fallback"
        assert decision.capability_field == "reactions"

    def test_event_kind_unsupported_beats_relation_native(self) -> None:
        """Event-kind unsupported + relation native → unsupported from event-kind."""
        caps = AdapterCapabilities(text=False, replies="native")
        event = make_event(
            event_kind="message.text",
            relations=(_REPLY_RELATION,),
        )
        decision = resolver.decide(event, caps)

        # text=False → event-kind unsupported (severity 2)
        # replies=native → relation native (severity 0)
        # unsupported wins.
        assert decision.capability_level == "unsupported"
        assert decision.capability_field == "text"

    def test_event_kind_native_relation_unsupported_wins(self) -> None:
        """Event-kind native + relation unsupported → relation unsupported wins."""
        caps = AdapterCapabilities(text=True, replies="unsupported")
        event = make_event(
            event_kind="message.text",
            relations=(_REPLY_RELATION,),
        )
        decision = resolver.decide(event, caps)

        assert decision.capability_level == "unsupported"
        assert decision.capability_field == "replies"

    def test_event_kind_fallback_relation_unsupported_wins(self) -> None:
        """Event-kind fallback (message.reacted, reactions=fallback) +
        relation unsupported (replies=unsupported) → unsupported."""
        caps = AdapterCapabilities(reactions="fallback", replies="unsupported")
        event = make_event(
            event_kind="message.reacted",
            relations=(_REPLY_RELATION,),
        )
        decision = resolver.decide(event, caps)

        assert decision.capability_level == "unsupported"
        assert decision.capability_field == "replies"


# ===================================================================
# TestReasonStability
# ===================================================================


class TestReasonStability:
    """Verify reason strings match existing user-visible format."""

    @pytest.mark.parametrize(
        "event_kind,cap_field,cap_value,expected_reason_fragment",
        [
            (
                "message.reacted",
                "reactions",
                "unsupported",
                "reactions unsupported by adapter",
            ),
            ("message.edited", "edits", "unsupported", "edits unsupported by adapter"),
            (
                "message.deleted",
                "deletes",
                "unsupported",
                "deletes unsupported by adapter",
            ),
            (
                "message.file",
                "attachments",
                False,
                "attachments unsupported by adapter",
            ),
            ("message.created", "text", False, "text unsupported by adapter"),
            ("message.text", "text", False, "text unsupported by adapter"),
            ("presence.changed", "presence", False, "presence unsupported by adapter"),
            (
                "telemetry.received",
                "metadata_fields",
                False,
                "metadata_fields unsupported by adapter",
            ),
            (
                "telemetry.position",
                "metadata_fields",
                False,
                "metadata_fields unsupported by adapter",
            ),
        ],
    )
    def test_event_kind_unsupported_reason(
        self,
        event_kind: str,
        cap_field: str,
        cap_value: object,
        expected_reason_fragment: str,
    ) -> None:
        caps = _caps_with(cap_field, cap_value)
        event = make_event(event_kind=event_kind)
        decision = resolver.decide(event, caps)

        assert decision.reason is not None
        assert expected_reason_fragment in decision.reason
        assert event_kind in decision.reason

    def test_reply_relation_unsupported_reason(self) -> None:
        caps = AdapterCapabilities(replies="unsupported")
        event = make_event(
            event_kind="message.text",
            relations=(_REPLY_RELATION,),
        )
        decision = resolver.decide(event, caps)

        assert decision.reason is not None
        assert "replies unsupported" in decision.reason
        assert "reply relation" in decision.reason

    def test_native_has_no_reason(self) -> None:
        caps = AdapterCapabilities(reactions="native")
        event = make_event(event_kind="message.reacted")
        decision = resolver.decide(event, caps)

        assert decision.reason is None

    def test_fallback_has_reason(self) -> None:
        caps = AdapterCapabilities(reactions="fallback")
        event = make_event(event_kind="message.reacted")
        decision = resolver.decide(event, caps)

        assert decision.reason is not None
        assert "fallback" in decision.reason


# ===================================================================
# TestSupportedFieldConsistency
# ===================================================================


class TestSupportedFieldConsistency:
    """Verify the ``supported`` field is consistent with level and strategy."""

    @pytest.mark.parametrize(
        "level",
        ["native", "fallback"],
    )
    def test_supported_when_native_or_fallback(self, level: str) -> None:
        """native and fallback both produce supported=True."""
        caps = AdapterCapabilities(reactions=level)
        event = make_event(event_kind="message.reacted")
        decision = resolver.decide(event, caps)

        assert decision.supported is True
        assert decision.capability_level == level

    def test_unsupported_when_unsupported(self) -> None:
        caps = AdapterCapabilities(reactions="unsupported")
        event = make_event(event_kind="message.reacted")
        decision = resolver.decide(event, caps)

        assert decision.supported is False
        assert decision.capability_level == "unsupported"
        assert decision.delivery_strategy == "skip"


# ===================================================================
# TestDecisionImmutability
# ===================================================================


class TestDecisionImmutability:
    """Verify CapabilityDecision is frozen."""

    def test_frozen(self) -> None:
        caps = AdapterCapabilities()
        event = make_event()
        decision = resolver.decide(event, caps)

        with pytest.raises(AttributeError):
            decision.capability_level = "unsupported"  # type: ignore[misc]


# ===================================================================
# TestTargetAdapterPassthrough
# ===================================================================


class TestTargetAdapterPassthrough:
    """Verify target_adapter is stored in the decision."""

    def test_target_adapter_stored(self) -> None:
        caps = AdapterCapabilities()
        event = make_event()
        decision = resolver.decide(event, caps, target_adapter="my-adapter")

        assert decision.target_adapter == "my-adapter"

    def test_target_adapter_defaults_none(self) -> None:
        caps = AdapterCapabilities()
        event = make_event()
        decision = resolver.decide(event, caps)

        assert decision.target_adapter is None


# ===================================================================
# TestWrapperParity
# ===================================================================


class TestWrapperParity:
    """Verify capability_unsupported() wrapper matches resolver.decide()."""

    @pytest.mark.parametrize(
        "event_kind,cap_field,cap_value",
        [
            ("message.reacted", "reactions", "unsupported"),
            ("message.reacted", "reactions", "native"),
            ("message.reacted", "reactions", "fallback"),
            ("message.edited", "edits", "unsupported"),
            ("message.deleted", "deletes", "unsupported"),
            ("message.file", "attachments", False),
            ("message.file", "attachments", True),
            ("message.created", "text", False),
            ("message.created", "text", True),
            ("presence.changed", "presence", False),
            ("telemetry.received", "metadata_fields", False),
        ],
    )
    def test_capability_unsupported_matches_decision(
        self,
        event_kind: str,
        cap_field: str,
        cap_value: object,
    ) -> None:
        caps = _caps_with(cap_field, cap_value)
        event = make_event(event_kind=event_kind)

        decision = resolver.decide(event, caps)
        reason = capability_unsupported(event, caps)

        if decision.supported:
            assert reason is None, (
                f"Decision is supported but capability_unsupported "
                f"returned: {reason}"
            )
        else:
            assert reason is not None, (
                "Decision is unsupported but capability_unsupported " "returned None"
            )
            # Reason should match the decision's reason.
            assert reason == decision.reason

    def test_reply_relation_wrapper_parity(self) -> None:
        caps = AdapterCapabilities(replies="unsupported")
        event = make_event(
            event_kind="message.text",
            relations=(_REPLY_RELATION,),
        )

        decision = resolver.decide(event, caps)
        reason = capability_unsupported(event, caps)

        assert decision.supported is False
        assert reason is not None
        assert reason == decision.reason

    @pytest.mark.parametrize(
        "relation,cap_field,cap_value",
        [
            (_REACTION_RELATION, "reactions", "unsupported"),
            (_REACTION_RELATION, "reactions", "fallback"),
            (_REACTION_RELATION, "reactions", "native"),
            (_EDIT_RELATION, "edits", "unsupported"),
            (_EDIT_RELATION, "edits", "fallback"),
            (_EDIT_RELATION, "edits", "native"),
            (_DELETE_RELATION, "deletes", "unsupported"),
            (_DELETE_RELATION, "deletes", "fallback"),
            (_DELETE_RELATION, "deletes", "native"),
        ],
        ids=lambda v: str(v),
    )
    def test_relation_wrapper_parity(
        self,
        relation: EventRelation,
        cap_field: str,
        cap_value: str,
    ) -> None:
        """Wrapper parity for reaction/edit/delete relation expansion."""
        caps = _caps_with(cap_field, cap_value)
        event = make_event(
            event_kind="plugin.custom",
            relations=(relation,),
        )

        decision = resolver.decide(event, caps)
        reason = capability_unsupported(event, caps)

        if decision.supported:
            assert reason is None
        else:
            assert reason is not None
            assert reason == decision.reason


# ===================================================================
# TestFallbackResolverParity
# ===================================================================


class TestFallbackResolverParity:
    """Verify FallbackResolver strategy matches resolver's delivery_strategy."""

    @pytest.mark.parametrize(
        "event_kind,cap_field,cap_value",
        [
            ("message.reacted", "reactions", "native"),
            ("message.reacted", "reactions", "fallback"),
            ("message.reacted", "reactions", "unsupported"),
            ("message.edited", "edits", "native"),
            ("message.edited", "edits", "fallback"),
            ("message.edited", "edits", "unsupported"),
            ("message.deleted", "deletes", "native"),
            ("message.deleted", "deletes", "fallback"),
            ("message.deleted", "deletes", "unsupported"),
            ("message.file", "attachments", True),
            ("message.file", "attachments", False),
            ("message.text", "text", True),
            ("message.text", "text", False),
            ("presence.changed", "presence", True),
            ("presence.changed", "presence", False),
            ("telemetry.received", "metadata_fields", True),
            ("telemetry.received", "metadata_fields", False),
        ],
    )
    def test_strategy_matches_resolver(
        self,
        event_kind: str,
        cap_field: str,
        cap_value: object,
    ) -> None:
        caps = _caps_with(cap_field, cap_value)
        event = make_event(event_kind=event_kind)

        decision = resolver.decide(event, caps)
        fb_resolver = FallbackResolver()
        strategy = fb_resolver._resolve_strategy(event, caps)

        assert strategy.method == decision.delivery_strategy, (
            f"FallbackResolver strategy {strategy.method!r} != "
            f"resolver delivery_strategy {decision.delivery_strategy!r}"
        )

    def test_reply_relation_strategy_parity(self) -> None:
        """FallbackResolver reply strategy matches resolver."""
        for level in ("native", "fallback", "unsupported"):
            caps = AdapterCapabilities(replies=level)
            event = make_event(
                event_kind="message.text",
                relations=(_REPLY_RELATION,),
            )

            decision = resolver.decide(event, caps)
            fb_resolver = FallbackResolver()
            strategy = fb_resolver._resolve_strategy(event, caps)

            assert strategy.method == decision.delivery_strategy, (
                f"replies={level}: FallbackResolver {strategy.method!r} != "
                f"resolver {decision.delivery_strategy!r}"
            )

    @pytest.mark.parametrize(
        "relation,cap_field",
        [
            (_REACTION_RELATION, "reactions"),
            (_EDIT_RELATION, "edits"),
            (_DELETE_RELATION, "deletes"),
        ],
        ids=lambda v: str(v),
    )
    def test_relation_strategy_parity(
        self,
        relation: EventRelation,
        cap_field: str,
    ) -> None:
        """FallbackResolver strategy matches resolver for reaction/edit/delete."""
        for level in ("native", "fallback", "unsupported"):
            caps = _caps_with(cap_field, level)
            event = make_event(
                event_kind="plugin.custom",
                relations=(relation,),
            )

            decision = resolver.decide(event, caps)
            fb_resolver = FallbackResolver()
            strategy = fb_resolver._resolve_strategy(event, caps)

            assert strategy.method == decision.delivery_strategy, (
                f"{cap_field}={level}: FallbackResolver {strategy.method!r} != "
                f"resolver {decision.delivery_strategy!r}"
            )


# ===================================================================
# TestModuleSingleton
# ===================================================================


class TestModuleSingleton:
    """Verify the module-level resolver singleton."""

    def test_singleton_is_resolver_instance(self) -> None:
        assert isinstance(resolver, CapabilityDecisionResolver)

    def test_singleton_produces_decisions(self) -> None:
        caps = AdapterCapabilities(reactions="unsupported")
        event = make_event(event_kind="message.reacted")
        decision = resolver.decide(event, caps)

        assert isinstance(decision, CapabilityDecision)
        assert decision.delivery_strategy == "skip"


# ===================================================================
# TestFailClosedSemantics
# ===================================================================


class TestFailClosedSemantics:
    """Verify fail-closed behaviour for mapped capability fields.

    Mapped fields missing from AdapterCapabilities or set to None/invalid
    values must not silently default to native.
    """

    def test_mapped_field_none_is_unsupported(self) -> None:
        """A mapped string field set to None resolves to unsupported."""
        import dataclasses

        caps = dataclasses.replace(_DEFAULT_CAPS, reactions=None)
        event = make_event(event_kind="message.reacted")
        decision = resolver.decide(event, caps)

        assert decision.capability_level == "unsupported"
        assert decision.supported is False

    def test_mapped_field_invalid_string_raises(self) -> None:
        """A mapped string field with an invalid string raises ValueError."""
        import dataclasses

        caps = dataclasses.replace(_DEFAULT_CAPS, reactions="maybe")
        event = make_event(event_kind="message.reacted")
        with pytest.raises(ValueError, match="reactions"):
            resolver.decide(event, caps)

    def test_mapped_field_invalid_type_raises(self) -> None:
        """A mapped string field with a non-string non-bool type raises ValueError."""
        import dataclasses

        caps = dataclasses.replace(_DEFAULT_CAPS, reactions=42)
        event = make_event(event_kind="message.reacted")
        with pytest.raises(ValueError, match="reactions"):
            resolver.decide(event, caps)

    def test_mapped_boolean_field_non_bool_raises(self) -> None:
        """A mapped boolean field with a non-bool value raises ValueError."""
        import dataclasses

        caps = dataclasses.replace(_DEFAULT_CAPS, text="yes")
        event = make_event(event_kind="message.text")
        with pytest.raises(ValueError, match="text"):
            resolver.decide(event, caps)

    def test_mapped_boolean_field_none_is_unsupported(self) -> None:
        """A mapped boolean field set to None resolves to unsupported."""
        import dataclasses

        caps = dataclasses.replace(_DEFAULT_CAPS, text=None)
        event = make_event(event_kind="message.text")
        decision = resolver.decide(event, caps)

        assert decision.capability_level == "unsupported"
        assert decision.supported is False

    def test_unknown_event_kind_passthrough(self) -> None:
        """Unmapped event kinds always produce native/direct passthrough."""
        event = make_event(event_kind="plugin.custom")
        caps = AdapterCapabilities()
        decision = resolver.decide(event, caps)

        assert decision.capability_level == "native"
        assert decision.delivery_strategy == "direct"
        assert decision.supported is True
        assert decision.capability_field is None
        assert decision.reason is None

    def test_unknown_relation_not_treated_as_thread(self) -> None:
        """Thread relation type produces no candidate and is not treated
        as a mapped relation.  Only the four mapped relation types
        (reply, reaction, edit, delete) produce candidates."""
        caps = AdapterCapabilities()
        event = make_event(
            event_kind="plugin.custom",
            relations=(_THREAD_RELATION,),
        )
        decision = resolver.decide(event, caps)

        # Thread relation produces no candidate, event kind is unmapped,
        # so the result is passthrough (native/direct) with no capability_field.
        assert decision.capability_level == "native"
        assert decision.delivery_strategy == "direct"
        assert decision.capability_field is None
        # This is NOT because thread was treated as a mapped relation;
        # it is because thread produces no candidate at all.


# ===================================================================
# TestLiteralTypeAliases
# ===================================================================


class TestLiteralTypeAliases:
    """Verify CapabilityLevel and CapabilityDeliveryStrategy type aliases."""

    def test_capability_level_alias_exists(self) -> None:
        from medre.core.planning.capability_decision import CapabilityLevel

        assert CapabilityLevel is not None

    def test_delivery_strategy_alias_exists(self) -> None:
        from medre.core.planning.capability_decision import (
            CapabilityDeliveryStrategy,
        )

        assert CapabilityDeliveryStrategy is not None
