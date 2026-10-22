"""Explicit activity grants never create durable work or bypass static policy."""

import asyncio

import pytest

from medre.config.errors import ConfigValidationError
from medre.config.routes import RouteConfig
from medre.config.route_expansion import expand_route_config
from medre.core.contracts.typing import TypingActivity
from medre.core.policies.route_policy import RoutePolicy
from medre.core.routing.models import Route, RouteSource, RouteTarget
from medre.core.routing.router import Router
from medre.runtime.typing import TypingRelay
from tests.helpers.async_utils import wait_until


class Port:
    """Record native calls independently of canonical storage and rendering."""

    def __init__(self):
        """Allocate only an observed update list."""
        self.updates = []

    async def send_typing(self, update):
        """Witness the native desired state without fabricating delivery receipts."""
        self.updates.append(update)


def route(
    *, enabled=True, forwarding=True, policy=None, source="source", target="target"
):
    """Build a concrete opt-in route with independently authored identities."""
    return Route(
        "activity",
        RouteSource(source, ("message.created",), "room-a"),
        [RouteTarget(target, "room-b")],
        enabled=enabled,
        forward_typing=forwarding,
        policy=policy,
    )


def activity(sender="alice", typing=True, seconds=30):
    """Express native activity without a canonical event or source adapter claim."""
    return TypingActivity("room-a", sender, typing, seconds)


@pytest.mark.parametrize("value", [True, False, "true", 1, None])
def test_forward_typing_requires_an_explicit_boolean_and_concrete_channels(value):
    """YAML opt-in cannot silently coerce strings, integers, or null."""
    config = dict(
        source_adapters=["source"],
        dest_adapters=["target"],
        source_channel="room-a",
        dest_channel="room-b",
        forward_typing=value,
    )
    if type(value) is bool:
        parsed = RouteConfig.from_dict("activity", config)
        assert parsed.forward_typing is value
    else:
        with pytest.raises(ConfigValidationError, match="boolean"):
            RouteConfig.from_dict("activity", config)


def test_forward_typing_default_and_directional_expansion_are_explicit():
    """A message route does not opt into activity; each expanded leg retains the flag."""
    data = dict(
        source_adapters=["source"],
        dest_adapters=["target"],
        source_channel="room-a",
        dest_channel="room-b",
    )
    assert not RouteConfig.from_dict("activity", data).forward_typing
    configured = RouteConfig.from_dict(
        "activity", {**data, "forward_typing": True, "directionality": "bidirectional"}
    )
    legs = expand_route_config(configured)
    assert len(legs) == 2 and all(leg.route.forward_typing for leg in legs)


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"source_channel": "room-a"},
        {"dest_channel": "room-b"},
        {"source_channel": "room-a", "dest_destination": {"kind": "channel"}},
    ],
)
def test_unscoped_typing_configuration_fails_closed(data):
    """Activity cannot introduce wildcard disclosure or structured message destinations."""
    with pytest.raises(ConfigValidationError):
        RouteConfig.from_dict(
            "activity",
            dict(
                source_adapters=["source"],
                dest_adapters=["target"],
                forward_typing=True,
                **data,
            ),
        )


async def test_source_binding_aggregation_and_shutdown_use_only_native_ports():
    """Another actor's stop cannot clear the indicator or spoof a source adapter."""
    source, target = Port(), Port()
    relay = TypingRelay(Router([route()]), {"source": source, "target": target})
    publish = relay.publisher("source")
    assert not await publish(activity())
    relay.start(frozenset({"source", "target"}))
    try:
        assert await publish(activity())
        assert await wait_until(lambda: len(target.updates) == 1)
        assert (
            target.updates[0].typing and target.updates[0].target.channel_id == "room-b"
        )
        assert await publish(activity("bob"))
        assert await publish(activity("alice", False))
        assert not await relay.publisher("spoofed")(activity())
    finally:
        await relay.stop()
    assert not target.updates[-1].typing
    assert not source.updates
    assert not await publish(activity())
    assert not any(
        t.get_name().startswith("medre-typing")
        for t in asyncio.all_tasks()
        if not t.done()
    )


@pytest.mark.parametrize(
    "policy",
    [
        RoutePolicy(sender_allowlist=("someone-else",)),
        RoutePolicy(room_allowlist=("another-room",)),
        RoutePolicy(channel_allowlist=("another-room",)),
        RoutePolicy(allowed_source_adapters=("other",)),
        RoutePolicy(allowed_dest_adapters=("other",)),
    ],
)
async def test_existing_static_allowlists_authorize_activity(policy):
    """The independent activity flag cannot bypass existing source/target policy."""
    source, target = Port(), Port()
    relay = TypingRelay(
        Router([route(policy=policy)]), {"source": source, "target": target}
    )
    relay.start(frozenset({"source", "target"}))
    try:
        assert not await relay.publisher("source")(activity())
    finally:
        await relay.stop()
    assert target.updates == []


@pytest.mark.parametrize("change", ["disable", "withdraw_flag", "remove", "policy"])
async def test_dynamic_grant_withdrawal_clears_without_new_source_activity(change):
    """Permission withdrawal never renews or waits for a fresh actor notification."""
    source, target = Port(), Port()
    grant = route()
    router = Router([grant])
    relay = TypingRelay(router, {"source": source, "target": target})
    relay.start(frozenset({"source", "target"}))
    try:
        await relay.publisher("source")(activity())
        assert await wait_until(lambda: bool(target.updates))
        if change == "disable":
            grant.enabled = False
        elif change == "withdraw_flag":
            grant.forward_typing = False
        elif change == "remove":
            router.remove_route(grant.id)
        else:
            grant.policy = RoutePolicy(sender_allowlist=("other",))
        assert await wait_until(lambda: not target.updates[-1].typing, timeout=3)
    finally:
        await relay.stop()


async def test_silent_activity_expires_without_native_stop():
    """An actual deadline clears the receiver even when the native source goes quiet."""
    source, target = Port(), Port()
    relay = TypingRelay(Router([route()]), {"source": source, "target": target})
    relay.start(frozenset({"source", "target"}))
    try:
        await relay.publisher("source")(activity(seconds=0.5))
        assert await wait_until(lambda: len(target.updates) >= 2)
        assert target.updates[0].typing and not target.updates[-1].typing
    finally:
        await relay.stop()


async def test_cancelled_stop_retains_owned_cleanup_until_joined():
    """Cancelling a caller cannot orphan a blocked native send or its final clear."""
    entered, cancelled = asyncio.Event(), asyncio.Event()

    class Blocked(Port):
        """Block positive requests and acknowledge task cancellation independently."""

        async def send_typing(self, update):
            """Leave clear calls live so cancellation does not mask cleanup."""
            if update.typing:
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            else:
                self.updates.append(update)

    target = Blocked()
    relay = TypingRelay(Router([route()]), {"source": Port(), "target": target})
    relay.start(frozenset({"source", "target"}))
    await relay.publisher("source")(activity())
    await asyncio.wait_for(entered.wait(), 1)
    stopping = asyncio.create_task(relay.stop())
    await asyncio.wait_for(cancelled.wait(), 1)
    stopping.cancel()
    await asyncio.gather(stopping, return_exceptions=True)
    await relay.stop()
    assert target.updates and not target.updates[-1].typing


@pytest.mark.parametrize(
    "schema_name,definition",
    [
        ("routing-config.schema.json", None),
        ("routing-config.schema.json", "RouteConfigRef"),
        ("runtime-config.schema.json", "RouteEntry"),
    ],
)
def test_schema_and_model_agree_on_explicit_typing_scopes(schema_name, definition):
    """All schema copies reject wildcard activity and accept concrete mapping forms."""
    import json
    from pathlib import Path
    from jsonschema import Draft202012Validator

    schema = json.loads(
        (Path(__file__).parents[1] / "docs/schemas" / schema_name).read_text()
    )
    if definition:
        schema = {"$ref": "#/$defs/" + definition, "$defs": schema["$defs"]}
    validator = Draft202012Validator(schema)
    data = dict(
        source_adapters=["source"], dest_adapters=["target"], forward_typing=True
    )
    if definition != "RouteEntry":
        data["route_id"] = "activity"
    assert list(validator.iter_errors(data))
    assert not list(
        validator.iter_errors({**data, "source_channel": "a", "dest_channel": "b"})
    )
    assert not list(
        validator.iter_errors({**data, "source_room": "a", "dest_room": "b"})
    )
    assert not list(
        validator.iter_errors({**data, "context_map": {"a": {"dest_context": "b"}}})
    )
    assert list(
        validator.iter_errors({**data, "source_channel": "a", "dest_channel": None})
    )
    assert list(
        validator.iter_errors(
            {**data, "context_map": {"a": {"dest_destination": {"kind": "channel"}}}}
        )
    )


def test_environment_overrides_preserve_and_explicitly_disable_typing(monkeypatch):
    """Editing a route's enabled state cannot silently discard or enable activity."""
    from medre.config.env import apply_env_overrides
    from medre.config.model import RuntimeConfig
    from medre.config.routes import RouteConfigSet

    grant = RouteConfig.from_dict(
        "activity",
        dict(
            source_adapters=["source"],
            dest_adapters=["target"],
            source_channel="a",
            dest_channel="b",
            forward_typing=True,
        ),
    )
    original = RuntimeConfig(routes=RouteConfigSet((grant,)))
    monkeypatch.setenv("MEDRE_ROUTE__ACTIVITY__ENABLED", "false")
    updated = apply_env_overrides(original).routes.routes[0]
    assert not updated.enabled and updated.forward_typing
    monkeypatch.setenv("MEDRE_ROUTE__ACTIVITY__FORWARD_TYPING", "false")
    updated = apply_env_overrides(original).routes.routes[0]
    assert not updated.enabled and not updated.forward_typing


async def test_stopped_typing_relay_cannot_reuse_closed_native_sessions():
    """A second runtime gets fresh lifecycle ownership and empty transient state."""
    relay = TypingRelay(Router([route()]), {"source": Port(), "target": Port()})
    relay.start(frozenset({"source", "target"}))
    await relay.stop()
    with pytest.raises(RuntimeError, match="fresh runtime"):
        relay.start(frozenset({"source", "target"}))


async def test_native_replacement_list_releases_omitted_actors_without_adapter_cache():
    """Matrix-style complete room state coexists with actor-scoped stop updates."""
    from medre.core.contracts.typing import TypingSnapshot

    target = Port()
    relay = TypingRelay(Router([route()]), {"source": Port(), "target": target})
    relay.start(frozenset({"source", "target"}))
    publish = relay.publisher("source")
    try:
        assert await publish(TypingSnapshot("room-a", ("alice", "bob")))
        assert await wait_until(lambda: bool(target.updates))
        assert await publish(TypingSnapshot("room-a", ("bob",)))
        assert await publish(activity("alice", False))
        assert target.updates[-1].typing
        assert await publish(TypingSnapshot("room-a", ()))
        assert await wait_until(lambda: not target.updates[-1].typing)
    finally:
        await relay.stop()


async def test_obsolete_clear_cannot_promote_an_untracked_positive():
    """Renewal while a clear waits must go through a tracked poll before sending."""
    from medre.core.contracts.typing import TypingTarget, TypingUpdate

    source, target = Port(), Port()
    relay = TypingRelay(Router([route()]), {"source": source, "target": target})
    destination = TypingTarget("target", "room-b")
    relay._pool.observe("source", activity(), (destination,))
    relay._pool.poll()
    relay._pool.observe("source", activity(typing=False), ())
    (clear,) = relay._pool.poll()
    assert clear == TypingUpdate(destination, 0)
    relay._pool.observe("source", activity(), (destination,))
    await relay._send(clear)
    assert target.updates == []
    (positive,) = relay._pool.poll()
    await relay._send(positive)
    assert target.updates[-1].typing
    await relay.stop()
    assert not target.updates[-1].typing
