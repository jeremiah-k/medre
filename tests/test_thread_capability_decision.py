"""Thread-relation capability planning tests."""

from __future__ import annotations

from medre.core.contracts.adapter import AdapterCapabilities
from medre.core.events.canonical import EventRelation, NativeRef
from medre.core.planning.capability_decision import resolver
from tests.helpers.pipeline import make_event

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


def test_thread_default_is_unsupported() -> None:
    """Thread relations are capability-gated and fail closed by default."""
    caps = AdapterCapabilities()
    event = make_event(event_kind="message.text", relations=(_THREAD_RELATION,))
    decision = resolver.decide(event, caps)

    assert decision.capability_level == "unsupported"
    assert decision.delivery_strategy == "skip"
    assert decision.capability_field == "threads"


def test_thread_fallback_is_deliverable() -> None:
    caps = AdapterCapabilities(threads="fallback")
    event = make_event(event_kind="message.text", relations=(_THREAD_RELATION,))
    decision = resolver.decide(event, caps)

    assert decision.capability_level == "fallback"
    assert decision.delivery_strategy == "fallback_text"
    assert decision.supported is True
    assert decision.capability_field == "threads"


def test_thread_alongside_reply_most_severe_wins() -> None:
    caps = AdapterCapabilities(threads="fallback", replies="unsupported")
    event = make_event(
        event_kind="plugin.custom",
        relations=(_THREAD_RELATION, _REPLY_RELATION),
    )
    decision = resolver.decide(event, caps)

    assert decision.capability_level == "unsupported"
    assert decision.capability_field == "replies"


def test_thread_relation_is_gated_independently_of_event_kind() -> None:
    caps = AdapterCapabilities()
    event = make_event(
        event_kind="plugin.custom",
        relations=(_THREAD_RELATION,),
    )
    decision = resolver.decide(event, caps)

    assert decision.capability_level == "unsupported"
    assert decision.delivery_strategy == "skip"
    assert decision.capability_field == "threads"
