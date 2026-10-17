"""Capability planning has an explicit policy for every routeable kind."""

import pytest

from medre.core.contracts.adapter import AdapterCapabilities
from medre.core.events.kinds import KNOWN_KINDS
from medre.core.planning.capability_decision import (
    PASSTHROUGH_EVENT_KINDS,
    _EVENT_KIND_FIELDS,
    resolver,
)
from medre.core.planning.capabilities import capability_unsupported
from tests.helpers.pipeline import make_event


@pytest.mark.parametrize(
    "kind", ["message.poll", "typing.changed", "future.notification"]
)
def test_unknown_kinds_fail_closed_even_with_all_default_capabilities(kind):
    event = make_event(event_kind=kind)
    caps = AdapterCapabilities()
    decision = resolver.decide(event, caps)
    assert not decision.supported
    assert decision.capability_level == "unsupported"
    assert decision.delivery_strategy == "skip"
    assert decision.capability_field == "event_kind"
    assert capability_unsupported(event, caps) == decision.reason


def test_every_known_kind_has_one_explicit_policy():
    assert not PASSTHROUGH_EVENT_KINDS.intersection(_EVENT_KIND_FIELDS)
    assert PASSTHROUGH_EVENT_KINDS.union(_EVENT_KIND_FIELDS) == KNOWN_KINDS


async def test_wildcard_route_suppresses_unknown_kind_before_rendering(temp_storage):
    from medre.adapters.fakes.presentation import FakePresentationAdapter
    from medre.core.engine.pipeline import PipelineRunner
    from medre.core.planning.delivery_plan import DeliveryFailureKind
    from medre.core.routing import Route, Router, RouteSource, RouteTarget
    from tests.helpers.pipeline import make_pipeline_config_for_pipeline

    adapter = FakePresentationAdapter(adapter_id="dest")
    router = Router(
        routes=[
            Route(
                id="wildcard",
                source=RouteSource(adapter="src", event_kinds=(), channel=None),
                targets=[RouteTarget(adapter="dest")],
            )
        ]
    )
    runner = PipelineRunner(
        make_pipeline_config_for_pipeline(
            storage=temp_storage, router=router, adapters={"dest": adapter}
        )
    )
    await runner.start()
    try:
        event = make_event(event_kind="message.poll", source_adapter="src")
        outcomes = await runner.handle_ingress(event)
        assert len(outcomes) == 1
        assert outcomes[0].status == "skipped"
        assert outcomes[0].failure_kind is DeliveryFailureKind.CAPABILITY_SUPPRESSED
        assert not adapter.received_events
        receipts = await temp_storage.list_receipts_for_event(event.event_id)
        assert receipts
        assert receipts[-1].capability_field == "event_kind"
        assert receipts[-1].delivery_strategy == "skip"
        stored = await temp_storage.get(event.event_id)
        assert stored is not None
        assert stored.event_kind == "message.poll"
    finally:
        await runner.stop()


def test_unknown_kind_cannot_be_authorized_by_a_supported_relation():
    """Known reply support never overrides an unknown event-kind suppression."""
    from medre.core.events.canonical import EventRelation

    relation = EventRelation(
        relation_type="reply",
        target_event_id="parent",
        target_native_ref=None,
        key=None,
        fallback_text=None,
    )
    event = make_event(event_kind="future.notification", relations=(relation,))
    decision = resolver.decide(event, AdapterCapabilities(replies="native"))
    assert decision.capability_level == "unsupported"
    assert decision.capability_field == "event_kind"
    assert decision.delivery_strategy == "skip"
