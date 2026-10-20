"""Explicit plugin assembly remains separate from declarative discovery."""

from dataclasses import replace

import msgspec
import pytest

from medre.core.engine.plugin_executor import (
    PluginExecutionLimits,
    PluginExecutor,
)
from medre.plugins import PluginInstanceConfig, PluginStage
from medre.plugins.registry import PluginTypeRegistry
from medre.registration import SymbolRef
from medre.runtime.plugins import build_plugin_executor
from tests.helpers.plugin_host import binding


class Options(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    label: str = "default"


@pytest.mark.parametrize("enabled", [False, True])
def test_disabled_and_ungranted_instances_never_load_implementation(
    temp_storage, monkeypatch, enabled
) -> None:
    item = binding("sample")
    spec = item.spec
    instance = PluginInstanceConfig("sample", "main", enabled=enabled)
    monkeypatch.setattr(
        SymbolRef,
        "load",
        lambda _: pytest.fail("unexpected implementation/config load"),
    )
    if enabled:
        with pytest.raises(PermissionError, match="grants"):
            build_plugin_executor(
                temp_storage, [instance], registry=PluginTypeRegistry([spec])
            )
    else:
        build_plugin_executor(
            temp_storage, [instance], registry=PluginTypeRegistry([spec])
        )


async def test_model_defaults_are_applied_before_configuration_digest(
    temp_storage, monkeypatch
) -> None:
    item = binding("sample")
    config_ref = SymbolRef(__name__, "Options")
    spec = replace(item.spec, config=config_ref)
    implementation = type(item.implementation)
    original = SymbolRef.load
    monkeypatch.setattr(
        SymbolRef,
        "load",
        lambda ref: implementation if ref == spec.plugin else original(ref),
    )
    host = build_plugin_executor(
        temp_storage, [item.instance], registry=PluginTypeRegistry([spec])
    )
    await host.start()
    try:
        assert host._instances[0].context.config == {"label": "default"}
        explicit = build_plugin_executor(
            temp_storage,
            [replace(item.instance, config={"label": "default"})],
            registry=PluginTypeRegistry([spec]),
        )
        assert explicit.plan_digest == host.plan_digest
        with pytest.raises(msgspec.ValidationError):
            build_plugin_executor(
                temp_storage,
                [replace(item.instance, config={"unknown": 1})],
                registry=PluginTypeRegistry([spec]),
            )
    finally:
        await host.stop()


def test_instances_cannot_share_mutable_implementation_or_optional_policy(
    temp_storage,
) -> None:
    item = binding("sample")
    other = replace(item, instance=replace(item.instance, instance_id="other"))
    with pytest.raises(ValueError, match="separate implementations"):
        PluginExecutor(temp_storage, (item, other))
    gate = binding("gate", PluginStage.EVENT_POLICY)
    with pytest.raises(ValueError, match="only enrichment"):
        replace(gate, required=False)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": 0},
        {"max_attempts": True},
        {"max_events": -1},
    ],
)
def test_invalid_execution_limits_are_rejected(kwargs) -> None:
    with pytest.raises(ValueError):
        PluginExecutionLimits(**kwargs)


async def test_all_instance_configs_are_validated_before_implementation_loading(
    temp_storage, monkeypatch
) -> None:
    item = binding("sample")
    spec = replace(item.spec, config=SymbolRef(__name__, "Options"))
    original = SymbolRef.load

    def load(ref):
        if ref == spec.plugin:
            pytest.fail("implementation loaded before complete plan validation")
        return original(ref)

    monkeypatch.setattr(SymbolRef, "load", load)
    with pytest.raises(msgspec.ValidationError):
        build_plugin_executor(
            temp_storage,
            [
                item.instance,
                replace(item.instance, instance_id="invalid", config={"unknown": True}),
            ],
            registry=PluginTypeRegistry([spec]),
        )
