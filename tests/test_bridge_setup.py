"""Guided authoring shares the runtime's models, identities and routing rules."""

from dataclasses import replace

import pytest

from medre.adapter_spec import SymbolRef
from medre.config._yaml import dump_yaml_config, parse_yaml_config
from medre.config.env import apply_env_overrides
from medre.config.errors import ConfigValidationError
from medre.config.loader import load_config
from medre.config.paths import resolve
from medre.config.routes import RouteDirectionality
from medre.core.routing import Router
from medre.runtime.bridge_setup import BridgeRequest, draft_bridge
from medre.runtime.route_engine import build_runtime_routes
from medre.runtime.route_plan import build_route_plan
from tests.helpers.bridge_setup import CONFIG, install_provider
from tests.helpers.pipeline import make_event


@pytest.fixture(autouse=True)
def provider(monkeypatch, tmp_path):
    install_provider(monkeypatch)
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path / "private-state"))


def request(**kwargs):
    return replace(BridgeRequest("left", "right", (("room α", "topic=0"),)), **kwargs)


def test_bidirectional_draft_round_trips_to_the_runtime_plan(tmp_path):
    draft = draft_bridge(CONFIG, request(), paths=resolve())
    path = tmp_path / "bridge.yaml"
    path.write_text(draft.yaml)
    config, _, paths = load_config(path)
    actual = build_route_plan(apply_env_overrides(config, paths))
    assert actual == draft.plan
    legs = actual.routes[0].legs
    assert {
        (
            leg.source_adapter_id,
            leg.dest_adapter_id,
            leg.source_channel,
            leg.dest_channel,
        )
        for leg in legs
    } == {
        ("left", "right", "room α", "topic=0"),
        ("right", "left", "topic=0", "room α"),
    }
    raw = parse_yaml_config(draft.yaml)
    assert raw["adapters"] == parse_yaml_config(CONFIG)["adapters"]
    assert raw["routes"]["bridge"]["policy"]["allowed_event_types"] == [
        "message.created",
        "message.text",
    ]
    assert not paths.state_dir.exists()


@pytest.mark.parametrize("direction", list(RouteDirectionality))
def test_directionality_and_contexts_control_production_routing(direction, tmp_path):
    """The saved YAML reaches the same matcher used for live canonical events."""
    draft = draft_bridge(CONFIG, request(directionality=direction), paths=resolve())
    path = tmp_path / "bridge.yaml"
    path.write_text(draft.yaml)
    assert draft.plan.total_legs == (
        2 if direction is RouteDirectionality.BIDIRECTIONAL else 1
    )
    config, _, _ = load_config(path)
    router = Router(build_runtime_routes(config.routes))
    for source, channel, target, target_channel, forbidden in (
        ("left", "room α", "right", "topic=0", RouteDirectionality.DEST_TO_SOURCE),
        ("right", "topic=0", "left", "room α", RouteDirectionality.SOURCE_TO_DEST),
    ):
        event = make_event(source_adapter=source, source_channel_id=channel)
        targets = [
            (destination.adapter, destination.channel)
            for route in router.match(event)
            for destination in router.resolve_targets(event, route)
        ]
        assert targets == ([] if direction is forbidden else [(target, target_channel)])
        unrelated = make_event(source_adapter=source, source_channel_id="unmapped")
        assert router.match(unrelated) == []
        edit = make_event(
            source_adapter=source,
            source_channel_id=channel,
            event_kind="message.edited",
        )
        assert router.match(edit) == []


def test_raw_secrets_placeholders_and_environment_overrides_stay_separate(monkeypatch):
    monkeypatch.setenv("MEDRE_ADAPTER__LEFT__CREDENTIAL", "environment-only-secret")
    monkeypatch.setenv("MEDRE_ADAPTER__LEFT__ORIGIN_LABEL", "Environment A")
    draft = draft_bridge(CONFIG, request(), paths=resolve())
    assert "environment-only-secret" not in draft.yaml
    assert "Environment A" not in draft.yaml
    assert "keep-this-private" in draft.yaml
    assert "{state}/left" in draft.yaml
    forward = next(
        leg for leg in draft.plan.routes[0].legs if leg.source_adapter_id == "left"
    )
    assert forward.source_origin_label == "Environment A"


def test_explicit_empty_label_suppresses_adapter_fallback():
    draft = draft_bridge(
        CONFIG,
        request(source_origin_label="", dest_origin_label="Other side"),
        paths=resolve(),
    )
    by_source = {leg.source_adapter_id: leg for leg in draft.plan.routes[0].legs}
    assert by_source["left"].source_origin_label == ""
    assert by_source["left"].source_origin_label_source == "route"
    assert by_source["right"].source_origin_label == "Other side"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"source_adapter": "unknown"}, "Unknown adapter"),
        ({"dest_adapter": "left"}, "two different"),
        ({"contexts": ()}, "explicit context"),
        ({"contexts": (("", "target"),)}, "non-empty"),
        ({"contexts": (("one", ""),)}, "non-empty"),
        ({"contexts": (("one", "x"), ("one", "y"))}, "Duplicate source"),
        ({"contexts": (("one", "x"), ("two", "x"))}, "fan-in"),
        ({"route_id": "bad.route"}, "Invalid route ID"),
    ],
)
def test_invalid_authoring_never_returns_a_successful_draft(change, reason):
    with pytest.raises(ConfigValidationError, match=reason):
        draft_bridge(CONFIG, request(**change), paths=resolve())


def test_forward_only_fan_in_uses_production_compiler_warning():
    draft = draft_bridge(
        CONFIG,
        request(
            contexts=(("one", "x"), ("two", "x")),
            directionality=RouteDirectionality.SOURCE_TO_DEST,
        ),
        paths=resolve(),
    )
    assert draft.plan.total_legs == 2
    assert draft.plan.routes[0].warnings


def test_disabled_endpoint_is_rejected():
    data = parse_yaml_config(CONFIG)
    data["adapters"]["independent_chat"]["left"]["enabled"] = False
    with pytest.raises(ConfigValidationError, match="disabled"):
        draft_bridge(dump_yaml_config(data), request(), paths=resolve())


def test_existing_route_cannot_be_replaced():
    original = draft_bridge(CONFIG, request(), paths=resolve())
    with pytest.raises(ConfigValidationError, match="already exists"):
        draft_bridge(original.yaml, request(), paths=resolve())


def test_renaming_an_existing_bridge_cannot_create_duplicate_delivery():
    original = draft_bridge(CONFIG, request(), paths=resolve())
    with pytest.raises(ConfigValidationError, match="already connects"):
        draft_bridge(original.yaml, request(route_id="another"), paths=resolve())


def test_existing_wildcard_source_cannot_duplicate_new_context_delivery():
    data = parse_yaml_config(CONFIG)
    data["routes"]["existing"] = {
        "source_adapters": ["left"],
        "dest_adapters": ["right"],
        "dest_channel": "topic=0",
    }
    with pytest.raises(ConfigValidationError, match="already connects"):
        draft_bridge(dump_yaml_config(data), request(), paths=resolve())


def test_existing_nontext_policy_can_share_the_same_addresses():
    original = draft_bridge(CONFIG, request(), paths=resolve())
    data = parse_yaml_config(original.yaml)
    data["routes"]["bridge"]["policy"]["allowed_event_types"] = ["message.edited"]
    draft = draft_bridge(
        dump_yaml_config(data), request(route_id="text_bridge"), paths=resolve()
    )
    assert len(draft.plan.routes) == 2
    assert draft.plan.total_legs == 4
    assert parse_yaml_config(draft.yaml)["routes"]["bridge"] == data["routes"]["bridge"]


@pytest.mark.parametrize("enabled", [True, False])
def test_errors_elsewhere_in_document_prevent_a_misleading_valid_draft(enabled):
    data = parse_yaml_config(CONFIG)
    data["routes"]["broken"] = {
        "source_adapters": ["absent"],
        "dest_adapters": ["right"],
        "enabled": enabled,
    }
    with pytest.raises(ConfigValidationError, match="unknown.*adapter"):
        draft_bridge(dump_yaml_config(data), request(), paths=resolve())


def test_environment_cannot_disable_the_new_route(monkeypatch):
    # Partial creation of an env-only route is already rejected by the env gate.
    monkeypatch.setenv("MEDRE_ROUTE__BRIDGE__ENABLED", "false")
    with pytest.raises(ConfigValidationError):
        draft_bridge(CONFIG, request(), paths=resolve())


def prepare_chat_config(config, *, adapter_id, paths, routes):
    """An SDK-free provider hook with route-dependent ingress restrictions."""
    for route in routes:
        if (
            route.enabled
            and route.source.adapter == adapter_id
            and config.allowed_contexts
            and route.source.channel not in config.allowed_contexts
        ):
            raise ValueError(f"Context {route.source.channel!r} is not allowed")
    return config


@pytest.mark.parametrize("direction", list(RouteDirectionality))
def test_provider_preflight_checks_only_active_ingress_contexts(monkeypatch, direction):
    from medre import adapter_registry
    from medre.adapter_registry import AdapterTypeRegistry
    from medre.runtime.errors import RuntimeConfigError

    spec = replace(
        install_provider(monkeypatch),
        runtime_config_preparer=SymbolRef(__name__, "prepare_chat_config"),
    )
    monkeypatch.setattr(
        adapter_registry, "ADAPTER_REGISTRY", AdapterTypeRegistry((spec,))
    )
    data = parse_yaml_config(CONFIG)
    data["adapters"]["independent_chat"]["right"]["allowed_contexts"] = ["other"]
    kwargs = dict(paths=resolve())
    text = dump_yaml_config(data)
    if direction is RouteDirectionality.SOURCE_TO_DEST:
        assert (
            draft_bridge(
                text, request(directionality=direction), **kwargs
            ).plan.total_legs
            == 1
        )
    else:
        with pytest.raises(
            RuntimeConfigError, match="Context 'topic=0' is not allowed"
        ):
            draft_bridge(text, request(directionality=direction), **kwargs)
    assert not resolve().state_dir.exists()
