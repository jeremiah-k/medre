"""Plugin contract types: capabilities, decisions, instances, reference plugin."""

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from medre.plugins import (
    HOST_GRANTABLE_CAPABILITIES,
    Allow,
    Deny,
    Derive,
    DerivedEvent,
    Pass,
    PluginCapability,
    PluginInstanceConfig,
    PluginStage,
    Supersede,
    effective_capabilities,
    missing_required_grants,
    unexpected_grants,
)
from medre.plugins.context import (
    EnrichPlugin,
    Plugin,
    PluginContext,
    PolicyPlugin,
    TransformPlugin,
)
from medre.plugins.example import TextStatsPlugin, plugin_spec
from medre.plugins.registry import PluginTypeRegistry
from medre.registration import SymbolRef
from tests.helpers.pipeline import make_event
from tests.helpers.plugin_spec import make_plugin_spec


def test_capability_vocabulary_is_closed() -> None:
    """The capability vocabulary separates observation, derivation, and suppression."""
    values = {capability.value for capability in PluginCapability}
    assert values == {
        "read_events",
        "emit_events",
        "suppress_events",
        "read_routes",
        "modify_routes",
        "read_identity",
        "read_storage",
        "access_telemetry",
    }


def test_host_grantable_capabilities_exclude_route_mutation() -> None:
    """MODIFY_ROUTES stays reserved; every other capability is grantable."""
    assert PluginCapability.MODIFY_ROUTES not in HOST_GRANTABLE_CAPABILITIES
    assert HOST_GRANTABLE_CAPABILITIES == frozenset(
        {
            PluginCapability.READ_EVENTS,
            PluginCapability.EMIT_EVENTS,
            PluginCapability.SUPPRESS_EVENTS,
        }
    )


def test_stage_vocabulary_matches_reserved_extension_stages() -> None:
    """Plugin stages are the reserved enrich/transform/event-policy stages."""
    assert {stage.value for stage in PluginStage} == {
        "enrich",
        "transform",
        "event_policy",
    }


def test_mapping_defaults_are_factories_and_independent() -> None:
    """These dataclasses must also import under Python 3.11, not just 3.12+."""
    from dataclasses import MISSING, fields

    payload_field = next(f for f in fields(DerivedEvent) if f.name == "payload")
    config_field = next(f for f in fields(PluginInstanceConfig) if f.name == "config")
    assert payload_field.default is MISSING
    assert config_field.default is MISSING
    assert payload_field.default_factory is dict
    assert config_field.default_factory is dict
    first, second = DerivedEvent("plugin.custom"), DerivedEvent("plugin.custom")
    assert first.payload == second.payload == {}
    one = PluginInstanceConfig("text_stats", "one")
    two = PluginInstanceConfig("text_stats", "two")
    assert one.config == two.config == {}


def test_valid_proposal_freezes_payload() -> None:
    proposal = DerivedEvent(kind="plugin.custom", payload={"subtype": "s", "n": 1})
    assert proposal.payload["n"] == 1
    with pytest.raises(TypeError):
        proposal.payload["n"] = 2


def test_source_payload_mutation_does_not_leak() -> None:
    source: dict = {"nested": {"count": 1}}
    proposal = DerivedEvent(kind="plugin.custom", payload=source)
    source["nested"]["count"] = 99
    assert proposal.payload["nested"]["count"] == 1


@pytest.mark.parametrize("kind", ["", "not-a-kind", "message.does-not-exist"])
def test_kind_must_be_registered(kind: str) -> None:
    with pytest.raises(ValueError, match="registered event kind"):
        DerivedEvent(kind=kind)


@pytest.mark.parametrize("payload", [None, "text", 3, [("k", 1)]])
def test_payload_must_be_a_mapping(payload: object) -> None:
    with pytest.raises(TypeError, match="must be a mapping"):
        DerivedEvent(kind="plugin.custom", payload=payload)


@pytest.mark.parametrize(
    "payload",
    [{"blob": b"\x01"}, {"items": [{"when": datetime.now(timezone.utc)}]}],
)
def test_payload_must_be_json_safe(payload: dict) -> None:
    with pytest.raises(TypeError, match="JSON-safe"):
        DerivedEvent(kind="plugin.custom", payload=payload)


def test_payload_keys_must_be_strings() -> None:
    with pytest.raises(TypeError, match="keys must be strings"):
        DerivedEvent(kind="plugin.custom", payload={1: "one"})


def test_pass_and_allow_default_notes() -> None:
    assert Pass().note == ""
    assert Allow().note == ""


def test_deny_requires_a_reason() -> None:
    with pytest.raises(ValueError, match="non-empty string"):
        Deny(reason="  ")


def test_derive_requires_at_least_one_event() -> None:
    with pytest.raises(ValueError, match="use Pass()"):
        Derive(events=())


def test_derive_entries_must_be_proposals() -> None:
    with pytest.raises(TypeError, match="DerivedEvent"):
        Derive(events=({"kind": "plugin.custom"},))  # type: ignore[arg-type]


def test_supersede_requires_a_proposal() -> None:
    with pytest.raises(TypeError, match="DerivedEvent"):
        Supersede(replacement="message.text")  # type: ignore[arg-type]


def test_invalid_identifiers_are_rejected() -> None:
    with pytest.raises(ValueError, match="plugin type id"):
        PluginInstanceConfig(plugin_id="Bad Id", instance_id="main")
    with pytest.raises(ValueError, match="plugin instance id"):
        PluginInstanceConfig(plugin_id="text_stats", instance_id="Bad Id")


def test_effective_capabilities_intersect_declaration_and_grants() -> None:
    spec = make_plugin_spec("sensor")
    instance = PluginInstanceConfig(
        plugin_id="sensor",
        instance_id="one",
        grants=frozenset({PluginCapability.READ_EVENTS, PluginCapability.EMIT_EVENTS}),
    )
    assert effective_capabilities(spec, instance) == frozenset(
        {PluginCapability.READ_EVENTS}
    )
    assert missing_required_grants(spec, instance) == frozenset()
    assert unexpected_grants(spec, instance) == frozenset(
        {PluginCapability.EMIT_EVENTS}
    )


def test_missing_grants_are_a_readiness_diagnostic() -> None:
    spec = make_plugin_spec("sensor")
    instance = PluginInstanceConfig(plugin_id="sensor", instance_id="one")
    assert missing_required_grants(spec, instance) == frozenset(
        {PluginCapability.READ_EVENTS}
    )
    assert effective_capabilities(spec, instance) == frozenset()


def test_reference_spec_is_registry_valid() -> None:
    registry = PluginTypeRegistry((plugin_spec(),))
    assert registry.plugin_ids() == ("text_stats",)


@pytest.mark.parametrize("plugin_id", ["Bad", "", "1st", "has space"])
def test_invalid_plugin_id_is_rejected(plugin_id: str) -> None:
    with pytest.raises(ValueError, match="invalid plugin id"):
        PluginTypeRegistry((make_plugin_spec(plugin_id),))


def test_duplicate_plugin_ids_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate plugin id"):
        PluginTypeRegistry((make_plugin_spec("one"), make_plugin_spec("one")))


def test_blank_version_is_rejected() -> None:
    spec = replace(make_plugin_spec("sensor"), version="  ")
    with pytest.raises(ValueError, match="declare a version"):
        PluginTypeRegistry((spec,))


def test_empty_capabilities_are_rejected() -> None:
    spec = replace(make_plugin_spec("sensor"), capabilities=frozenset())
    with pytest.raises(ValueError, match="at least one capability"):
        PluginTypeRegistry((spec,))


def test_reserved_capability_is_rejected() -> None:
    spec = replace(
        make_plugin_spec("sensor"),
        capabilities=frozenset(
            {PluginCapability.READ_EVENTS, PluginCapability.MODIFY_ROUTES}
        ),
    )
    with pytest.raises(ValueError, match="reserved capability 'modify_routes'"):
        PluginTypeRegistry((spec,))


def test_read_events_is_required() -> None:
    spec = replace(
        make_plugin_spec("sensor"),
        capabilities=frozenset({PluginCapability.EMIT_EVENTS}),
    )
    with pytest.raises(ValueError, match="read_events"):
        PluginTypeRegistry((spec,))


def test_empty_stages_are_rejected() -> None:
    spec = replace(make_plugin_spec("sensor"), stages=frozenset())
    with pytest.raises(ValueError, match="at least one stage"):
        PluginTypeRegistry((spec,))


def test_transform_requires_kind_declarations() -> None:
    spec = replace(
        make_plugin_spec("sensor"),
        stages=frozenset({PluginStage.TRANSFORM}),
    )
    with pytest.raises(ValueError, match="transform_inputs and transform_outputs"):
        PluginTypeRegistry((spec,))


def test_kind_declarations_require_transform_stage() -> None:
    spec = replace(
        make_plugin_spec("sensor"),
        transform_inputs=("message.text",),
    )
    with pytest.raises(ValueError, match="without subscribing to the transform"):
        PluginTypeRegistry((spec,))


def test_transform_kinds_must_be_registered() -> None:
    spec = replace(
        make_plugin_spec("sensor"),
        stages=frozenset({PluginStage.TRANSFORM}),
        transform_inputs=("message.text",),
        transform_outputs=("message.invented",),
    )
    with pytest.raises(ValueError, match="unregistered event kind"):
        PluginTypeRegistry((spec,))


@pytest.mark.parametrize(
    "declaration", [("medre", "WARNING"), ("root", "INFO"), ("sdk", "INVALID")]
)
def test_logger_declarations_follow_adapter_rules(declaration) -> None:
    spec = replace(make_plugin_spec("sensor"), logger_levels=(declaration,))
    with pytest.raises(ValueError, match="dependency logger"):
        PluginTypeRegistry((spec,))


def test_invalid_capability_member_is_rejected() -> None:
    spec = replace(make_plugin_spec("sensor"), capabilities=frozenset({"nope"}))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid capability declaration"):
        PluginTypeRegistry((spec,))


def _plugin_context() -> PluginContext:
    import asyncio
    import logging

    return PluginContext(
        plugin_id="text_stats",
        instance_id="main",
        logger=logging.getLogger("medre.plugins.text_stats"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )


def test_satisfies_lifecycle_and_enrich_protocols() -> None:
    plugin = TextStatsPlugin()
    assert isinstance(plugin, Plugin)
    assert isinstance(plugin, EnrichPlugin)
    assert not isinstance(plugin, TransformPlugin)
    assert not isinstance(plugin, PolicyPlugin)


@pytest.mark.parametrize("kind", ["message.created", "message.text"])
async def test_derives_stats_for_text_messages(kind: str) -> None:
    plugin = TextStatsPlugin()
    await plugin.initialize(_plugin_context())
    event = make_event(event_kind=kind, payload={"text": "one two three"})
    outcome = await plugin.enrich(event)
    assert isinstance(outcome, Derive)
    (proposal,) = outcome.events
    assert proposal.kind == "plugin.custom"
    assert proposal.payload["subtype"] == "text_stats"
    assert proposal.payload["words"] == 3
    assert proposal.payload["chars"] == 13


async def test_passes_non_text_events_through_unchanged() -> None:
    plugin = TextStatsPlugin()
    await plugin.initialize(_plugin_context())
    outcome = await plugin.enrich(make_event(event_kind="telemetry.received"))
    assert isinstance(outcome, Pass)


async def test_passes_empty_or_non_string_text() -> None:
    plugin = TextStatsPlugin()
    await plugin.initialize(_plugin_context())
    for payload in ({"text": ""}, {"text": None}, {"other": 1}):
        outcome = await plugin.enrich(
            make_event(event_kind="message.text", payload=payload)
        )
        assert isinstance(outcome, Pass)


async def test_derivation_is_deterministic() -> None:
    plugin = TextStatsPlugin()
    event = make_event(event_kind="message.text", payload={"text": "stable input"})
    first = await plugin.enrich(event)
    second = await plugin.enrich(event)
    assert first == second


def test_dormant_scaffolding_symbols_are_gone() -> None:
    """The superseded list-returning protocol and payload validator are removed."""
    from medre import plugins

    assert not hasattr(plugins, "validate_plugin_payload")
    assert not hasattr(plugins, "PluginBoundaryError")


def test_registration_symbols_are_shared_with_adapters() -> None:
    """Adapters and plugins share one lazy-reference primitive."""
    from medre.adapter_spec import SymbolRef as AdapterSymbolRef

    assert AdapterSymbolRef is SymbolRef


def test_declarations_snapshot_caller_owned_containers() -> None:
    capabilities = {PluginCapability.READ_EVENTS}
    stages = {PluginStage.ENRICH}
    loggers = [["sdk", "INFO"]]
    spec = replace(
        make_plugin_spec("sensor"),
        capabilities=capabilities,
        stages=stages,
        logger_levels=loggers,
    )
    capabilities.clear()
    stages.clear()
    loggers[0][1] = "ERROR"
    assert spec.capabilities == frozenset({PluginCapability.READ_EVENTS})
    assert spec.stages == frozenset({PluginStage.ENRICH})
    assert spec.logger_levels == (("sdk", "INFO"),)
    PluginTypeRegistry((spec,))


def test_instance_snapshots_config_and_grants() -> None:
    values = {"nested": [1, {"flag": True}]}
    grants = {PluginCapability.READ_EVENTS}
    instance = PluginInstanceConfig("sensor", "main", config=values, grants=grants)
    values["nested"][1]["flag"] = False
    grants.clear()
    assert instance.config["nested"][1]["flag"] is True
    assert instance.grants == frozenset({PluginCapability.READ_EVENTS})
    with pytest.raises(TypeError):
        instance.config["nested"][1]["flag"] = False


@pytest.mark.parametrize(
    "function", [effective_capabilities, missing_required_grants, unexpected_grants]
)
def test_grant_helpers_reject_mismatched_type(function) -> None:
    with pytest.raises(ValueError, match="different plugin type"):
        function(make_plugin_spec("sensor"), PluginInstanceConfig("other", "main"))


def test_missing_grants_includes_unsupported_host_services() -> None:
    spec = replace(
        make_plugin_spec("sensor"), capabilities={PluginCapability.MODIFY_ROUTES}
    )
    instance = PluginInstanceConfig("sensor", "main", grants=spec.capabilities)
    assert missing_required_grants(spec, instance) == spec.capabilities


@pytest.mark.parametrize(
    "kwargs", [{"enabled": "false"}, {"grants": {"read_events"}}, {"config": None}]
)
def test_invalid_instance_values_fail_at_construction(kwargs) -> None:
    with pytest.raises(TypeError):
        PluginInstanceConfig("sensor", "main", **kwargs)


def test_derive_snapshots_proposal_list() -> None:
    proposals = [DerivedEvent("plugin.custom", {"subtype": "one"})]
    decision = Derive(proposals)
    proposals.clear()
    assert len(decision.events) == 1
    assert isinstance(decision.events, tuple)


@pytest.mark.parametrize("decision", [Pass, Allow])
def test_decision_notes_are_text(decision) -> None:
    with pytest.raises(TypeError, match="note must be a string"):
        decision(note={"secret": "value"})


@pytest.mark.parametrize(
    "payload", [{"float": float("nan")}, {"float": float("inf")}, {"set": {1}}]
)
def test_non_json_payload_values_are_rejected(payload) -> None:
    with pytest.raises(TypeError, match="JSON-safe"):
        DerivedEvent("plugin.custom", payload)


def test_recursive_payload_is_a_contract_error() -> None:
    cycle = {}
    cycle["self"] = cycle
    with pytest.raises(ValueError, match="cycle"):
        DerivedEvent("plugin.custom", cycle)


def test_shared_noncyclic_containers_are_valid() -> None:
    values = {"one": [1]}
    event = DerivedEvent("plugin.custom", {"a": values, "b": values})
    assert event.payload["a"] == event.payload["b"]


def test_overdeep_payload_is_a_contract_error() -> None:
    payload = {}
    for _ in range(66):
        payload = {"child": payload}
    with pytest.raises(ValueError, match="depth"):
        DerivedEvent("plugin.custom", payload)


@pytest.mark.parametrize(
    "kind",
    [
        "delivery.queued",
        "delivery.sent",
        "delivery.failed",
        "system.audit",
        "system.lifecycle",
    ],
)
def test_plugins_cannot_propose_core_evidence(kind) -> None:
    with pytest.raises(ValueError, match="permitted for plugin output"):
        DerivedEvent(kind)
    spec = replace(
        make_plugin_spec("sensor"),
        stages={PluginStage.TRANSFORM},
        transform_inputs=("message.created",),
        transform_outputs=(kind,),
    )
    with pytest.raises(ValueError, match="core-owned"):
        PluginTypeRegistry((spec,))


@pytest.mark.parametrize(
    "ref",
    [
        None,
        "module:factory",
        SymbolRef("", "factory"),
        SymbolRef("pkg..api", "factory"),
        SymbolRef("pkg", ""),
    ],
)
def test_invalid_implementation_reference_is_rejected_without_import(ref) -> None:
    spec = replace(make_plugin_spec("sensor"), plugin=ref)
    with pytest.raises(ValueError, match="SymbolRef"):
        PluginTypeRegistry((spec,))


def test_invalid_config_reference_is_rejected_without_import() -> None:
    spec = replace(make_plugin_spec("sensor"), config="pkg:Config")
    with pytest.raises(ValueError, match="SymbolRef"):
        PluginTypeRegistry((spec,))


def _plugin_instance(spec):
    return PluginInstanceConfig(spec.plugin_id, "main", grants=spec.capabilities)


def test_policy_requires_explicit_suppression_declaration() -> None:
    spec = replace(make_plugin_spec("sensor"), stages={PluginStage.EVENT_POLICY})
    with pytest.raises(ValueError, match="suppress_events"):
        PluginTypeRegistry((spec,))


def test_observer_cannot_emit() -> None:
    from medre.plugins import PluginCapabilityError, validate_plugin_decision

    spec = make_plugin_spec("sensor")
    with pytest.raises(PluginCapabilityError, match="emit_events"):
        validate_plugin_decision(
            spec,
            _plugin_instance(spec),
            PluginStage.ENRICH,
            "message.created",
            Derive((DerivedEvent("plugin.custom"),)),
        )


@pytest.mark.parametrize("decision", [Allow(), Deny("policy")])
def test_policy_grant_is_required_for_every_verdict(decision) -> None:
    from medre.plugins import PluginCapabilityError, validate_plugin_decision

    spec = replace(
        make_plugin_spec("sensor"),
        stages={PluginStage.EVENT_POLICY},
        capabilities={
            PluginCapability.READ_EVENTS,
            PluginCapability.SUPPRESS_EVENTS,
        },
    )
    PluginTypeRegistry((spec,))
    validate_plugin_decision(
        spec,
        _plugin_instance(spec),
        PluginStage.EVENT_POLICY,
        "message.created",
        decision,
    )
    missing = replace(_plugin_instance(spec), grants={PluginCapability.READ_EVENTS})
    with pytest.raises(PluginCapabilityError, match="missing required grants"):
        validate_plugin_decision(
            spec, missing, PluginStage.EVENT_POLICY, "message.created", decision
        )


def test_replacement_requires_suppression_as_well_as_emission() -> None:
    from medre.plugins import PluginCapabilityError, validate_plugin_decision

    spec = replace(
        make_plugin_spec("sensor"),
        stages={PluginStage.TRANSFORM},
        capabilities={PluginCapability.READ_EVENTS, PluginCapability.EMIT_EVENTS},
        transform_inputs=("message.created",),
        transform_outputs=("message.created",),
    )
    PluginTypeRegistry((spec,))
    proposal = DerivedEvent("message.created", {"text": "replacement"})
    validate_plugin_decision(
        spec,
        _plugin_instance(spec),
        PluginStage.TRANSFORM,
        "message.created",
        Derive((proposal,)),
    )
    with pytest.raises(PluginCapabilityError, match="suppress_events"):
        validate_plugin_decision(
            spec,
            _plugin_instance(spec),
            PluginStage.TRANSFORM,
            "message.created",
            Supersede(proposal),
        )
    spec = replace(
        spec, capabilities=spec.capabilities | {PluginCapability.SUPPRESS_EVENTS}
    )
    validate_plugin_decision(
        spec,
        _plugin_instance(spec),
        PluginStage.TRANSFORM,
        "message.created",
        Supersede(proposal),
    )


def test_declared_kind_flow_is_checked() -> None:
    from medre.plugins import validate_plugin_decision

    spec = replace(
        make_plugin_spec("sensor"),
        stages={PluginStage.TRANSFORM},
        capabilities={PluginCapability.READ_EVENTS, PluginCapability.EMIT_EVENTS},
        transform_inputs=("message.created",),
        transform_outputs=("message.text",),
    )
    instance = _plugin_instance(spec)
    with pytest.raises(ValueError, match="input kind"):
        validate_plugin_decision(
            spec, instance, PluginStage.TRANSFORM, "telemetry.received", Pass()
        )
    with pytest.raises(ValueError, match="output kind"):
        validate_plugin_decision(
            spec,
            instance,
            PluginStage.TRANSFORM,
            "message.created",
            Derive((DerivedEvent("plugin.custom"),)),
        )
    with pytest.raises(TypeError, match="not permitted"):
        validate_plugin_decision(
            spec, instance, PluginStage.TRANSFORM, "message.created", Deny("no")
        )
    with pytest.raises(ValueError, match="subscribe"):
        validate_plugin_decision(
            spec, instance, PluginStage.ENRICH, "message.created", Pass()
        )


def test_disabled_instance_cannot_return_an_authorized_decision() -> None:
    from medre.plugins import PluginCapabilityError, validate_plugin_decision

    spec = make_plugin_spec("sensor")
    with pytest.raises(PluginCapabilityError, match="disabled"):
        validate_plugin_decision(
            spec,
            replace(_plugin_instance(spec), enabled=False),
            PluginStage.ENRICH,
            "message.created",
            Pass(),
        )


@pytest.mark.parametrize(
    "capability",
    [
        PluginCapability.READ_ROUTES,
        PluginCapability.READ_IDENTITY,
        PluginCapability.READ_STORAGE,
        PluginCapability.ACCESS_TELEMETRY,
    ],
)
def test_context_services_without_implementations_are_reserved(capability) -> None:
    spec = replace(
        make_plugin_spec("sensor"),
        capabilities={PluginCapability.READ_EVENTS, capability},
    )
    with pytest.raises(ValueError, match="reserved capability"):
        PluginTypeRegistry((spec,))
