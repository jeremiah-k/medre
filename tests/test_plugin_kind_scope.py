"""Stage-independent event subscriptions and least-authority emission contracts."""

from dataclasses import replace

import pytest

from medre.core.engine.plugin_executor import PluginExecutor
from medre.plugins import (
    Allow,
    Derive,
    DerivedEvent,
    Pass,
    PluginCapability,
    PluginStage,
)
from medre.plugins.records import PluginExecutionKey, PluginFailure
from medre.plugins.registry import PluginTypeRegistry
from tests.helpers.plugin_host import binding
from tests.helpers.plugin_spec import make_plugin_spec
from tests.helpers.storage import make_storage_event


def test_emission_requires_a_declared_kind_allowlist() -> None:
    spec = replace(
        make_plugin_spec("author"),
        capabilities={PluginCapability.READ_EVENTS, PluginCapability.EMIT_EVENTS},
    )
    with pytest.raises(ValueError, match="emit_events requires declared output_kinds"):
        PluginTypeRegistry((spec,))


def test_read_only_plugin_cannot_claim_output_kinds() -> None:
    spec = replace(make_plugin_spec("viewer"), output_kinds=("plugin.custom",))
    with pytest.raises(ValueError, match="output_kinds requires emit_events"):
        PluginTypeRegistry((spec,))


def test_transform_without_emission_authority_is_invalid() -> None:
    spec = replace(
        make_plugin_spec("noop"),
        stages={PluginStage.TRANSFORM},
        input_kinds=("message.created",),
    )
    with pytest.raises(ValueError, match="transform stage requires emit_events"):
        PluginTypeRegistry((spec,))


@pytest.mark.parametrize("stage", list(PluginStage))
async def test_subscription_skips_unmatched_kinds_and_runs_matching_kinds(
    temp_storage,
    stage,
) -> None:
    await temp_storage.append(make_storage_event("evt-1"))
    seen = []

    async def observe(context, event):
        seen.append(event.event_id)
        return Allow() if stage is PluginStage.EVENT_POLICY else Pass()

    base = binding("observer", stage=stage, action=observe)
    item = replace(base, spec=replace(base.spec, input_kinds=("message.text",)))
    executor = PluginExecutor(temp_storage, (item,))
    await executor.start()
    try:
        assert [event.event_id for event in await executor.process("evt-1")] == [
            "evt-1"
        ]
        assert seen == []
        assert (
            await temp_storage.get_plugin_execution(
                PluginExecutionKey("evt-1", "observer", "main", stage)
            )
            is None
        )
        await temp_storage.append(
            make_storage_event("evt-2", event_kind="message.text")
        )
        assert [event.event_id for event in await executor.process("evt-2")] == [
            "evt-2"
        ]
        assert seen == ["evt-2"]
        record = await temp_storage.get_plugin_execution(
            PluginExecutionKey("evt-2", "observer", "main", stage)
        )
        assert type(record.decision) is (
            Allow if stage is PluginStage.EVENT_POLICY else Pass
        )
    finally:
        await executor.stop()


async def test_enrichment_cannot_emit_an_undeclared_kind(temp_storage) -> None:
    await temp_storage.append(make_storage_event("evt-1"))

    async def bad(context, event):
        return Derive((DerivedEvent("message.text", {"text": "forged"}),))

    base = binding("author", action=bad)
    item = replace(base, spec=replace(base.spec, output_kinds=("plugin.custom",)))
    executor = PluginExecutor(temp_storage, (item,))
    await executor.start()
    try:
        from medre.core.engine.plugin_executor import PluginExecutionFailedError

        with pytest.raises(
            PluginExecutionFailedError, match="no successful disposition"
        ):
            await executor.process("evt-1")
        record = await temp_storage.get_plugin_execution(
            PluginExecutionKey("evt-1", "author", "main", PluginStage.ENRICH)
        )
        assert record.failure is PluginFailure.CONTRACT
        assert record.produced_event_ids == ()
        count = await temp_storage._read_one(
            "SELECT COUNT(*) AS n FROM canonical_events"
        )
        assert count["n"] == 1
    finally:
        await executor.stop()


async def test_policy_scope_is_defined_by_input_kinds(temp_storage) -> None:
    await temp_storage.append(make_storage_event("evt-1"))
    seen = []

    async def allow(context, event):
        seen.append(event.event_id)
        return Allow()

    base = binding("gate", PluginStage.EVENT_POLICY, allow)
    item = replace(base, spec=replace(base.spec, input_kinds=("message.text",)))
    executor = PluginExecutor(temp_storage, (item,))
    await executor.start()
    try:
        assert len(await executor.process("evt-1")) == 1
        assert seen == []
    finally:
        await executor.stop()
