"""Frozen recovery context refuses changed shapes and destination identity."""

import msgspec
import pytest

from medre.core.engine.pipeline.retry_plan import (
    OutboxDispatchContext,
    reconstruct_retry_delivery_plan,
)
from medre.core.events.metadata import RoutingMetadata
from medre.core.planning.delivery_plan import (
    DeliveryPlan,
    DeliveryStrategy,
    delivery_target_identity,
)
from medre.core.routing.models import Route, RouteDestination, RouteSource, RouteTarget
from medre.core.storage.backend import DeliveryOutboxItem, OutboxDispatchSnapshot


def _context(kind):
    target = RouteTarget(
        adapter="fake_transport",
        destination=RouteDestination("mesh", "accepted-peer", "Peer", {"x": [1]}),
    )
    item = DeliveryOutboxItem(
        outbox_id="frozen-work",
        event_id="event",
        route_id="route",
        delivery_plan_id="plan",
        target_adapter=target.adapter,
        target_address=target.destination.destination_hash,
    )
    plan = DeliveryPlan(
        plan_id=item.delivery_plan_id,
        event_id=item.event_id,
        route_id=item.route_id,
        target=target,
        target_identity=delivery_target_identity(target),
        primary_strategy=DeliveryStrategy(method="direct"),
    )
    if kind == "staged":
        context = msgspec.to_builtins(
            OutboxDispatchContext(
                route=Route("route", RouteSource(None, (), None), [target]),
                plan=plan,
                routing=RoutingMetadata(matched_routes=("route",)),
            )
        )
    else:
        context = {
            "capability_level": None,
            "delivery_strategy": "direct",
            "capability_field": None,
            "capability_reason": None,
            "deadline": None,
            "destination_kind": "mesh",
            "destination_hash": "accepted-peer",
            "destination_name": "Peer",
            "destination_metadata": {"x": [1]},
        }
    return item, OutboxDispatchSnapshot(item.outbox_id, 1, 1, kind, context)


def _reconstruct(item, snapshot):
    return reconstruct_retry_delivery_plan(
        item=item,
        previous_receipt=None,
        default_max_attempts=3,
        dispatch_snapshot=snapshot,
    )


@pytest.mark.parametrize("kind", ["staged", "direct"])
def test_valid_snapshot_preserves_opaque_destination_metadata(kind):
    item, snapshot = _context(kind)
    result = _reconstruct(item, snapshot)
    assert result.plan.target.destination == RouteDestination(
        "mesh", "accepted-peer", "Peer", {"x": [1]}
    )


@pytest.mark.parametrize("address", ["different-peer", None])
def test_direct_snapshot_cannot_redirect_the_durable_destination(address):
    item, snapshot = _context("direct")
    snapshot.context["destination_hash"] = address
    with pytest.raises(ValueError):
        _reconstruct(item, snapshot)


@pytest.mark.parametrize(
    "key,value",
    [
        ("destination_kind", 3),
        ("destination_kind", None),
        ("destination_metadata", []),
        ("destination_name", 3),
        ("future_strategy_override", "different"),
    ],
)
def test_direct_snapshot_refuses_changed_or_malformed_fields(key, value):
    item, snapshot = _context("direct")
    snapshot.context[key] = value
    with pytest.raises(ValueError):
        _reconstruct(item, snapshot)


@pytest.mark.parametrize(
    "key",
    [
        "destination_kind",
        "destination_hash",
        "destination_name",
        "destination_metadata",
    ],
)
def test_direct_snapshot_requires_the_complete_destination_shape(key):
    item, snapshot = _context("direct")
    del snapshot.context[key]
    with pytest.raises(ValueError):
        _reconstruct(item, snapshot)


@pytest.mark.parametrize(
    "path",
    [
        ("route",),
        ("route", "source"),
        ("plan",),
        ("plan", "primary_strategy"),
    ],
)
def test_staged_snapshot_refuses_unknown_nested_fields(path):
    item, snapshot = _context("staged")
    document = snapshot.context
    for part in path:
        document = document[part]
    document["future_override"] = "different"
    with pytest.raises(ValueError):
        _reconstruct(item, snapshot)


def test_staged_snapshot_cannot_default_an_omitted_frozen_field():
    item, snapshot = _context("staged")
    del snapshot.context["plan"]["retry_policy"]
    with pytest.raises(ValueError):
        _reconstruct(item, snapshot)


def test_staged_snapshot_refuses_a_naive_delivery_deadline():
    item, snapshot = _context("staged")
    snapshot.context["plan"]["deadline"] = "2026-11-01T12:00:00"
    with pytest.raises(ValueError):
        _reconstruct(item, snapshot)
