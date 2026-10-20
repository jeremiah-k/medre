"""Canonical plugin decisions qualified through actual routing and fake delivery."""

from dataclasses import replace

import pytest

from medre.adapters.fakes.presentation import FakePresentationAdapter
from medre.core.engine.pipeline import PipelineRunner
from medre.core.engine.plugin_executor import PluginBinding, PluginExecutor
from medre.core.ingress import DurableIngressDeferredError
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.plugins import (
    Deny,
    DerivedEvent,
    PluginInstanceConfig,
    PluginStage,
    Supersede,
)
from medre.plugins.example import TextStatsPlugin, plugin_spec
from tests.helpers.pipeline import make_event, make_pipeline_config_for_pipeline
from tests.helpers.plugin_host import binding


def _pipeline(storage, host, kinds=()):
    adapter = FakePresentationAdapter("fake_presentation")
    router = Router(
        [
            Route(
                id="bridge",
                source=RouteSource("fake_transport", kinds, "ch-0"),
                targets=[RouteTarget(adapter="fake_presentation")],
            )
        ]
    )
    config = make_pipeline_config_for_pipeline(
        storage, router, {"fake_presentation": adapter}
    )
    config.plugin_executor = host
    return PipelineRunner(config), adapter


@pytest.mark.parametrize("explicit_custom", [False, True])
async def test_reference_plugin_is_stored_without_broadcasting_statistics_by_default(
    temp_storage, explicit_custom
) -> None:
    spec = plugin_spec()
    instance = PluginInstanceConfig(spec.plugin_id, "main", grants=spec.capabilities)
    host = PluginExecutor(
        temp_storage, (PluginBinding(spec, instance, TextStatsPlugin()),)
    )
    runner, adapter = _pipeline(
        temp_storage,
        host,
        ("message.created", "plugin.custom") if explicit_custom else (),
    )
    await host.start()
    await runner.start()
    try:
        await runner.admit_ingress(
            make_event(event_id="source", payload={"text": "hello world"}), "live"
        )
        await runner.process_admitted_event("source")
        assert len(adapter.delivered_payloads) == (2 if explicit_custom else 1)
        assert adapter.delivered_payloads[0].event_id == "source"
        rows = await temp_storage._read_all(
            "SELECT event_id FROM canonical_events WHERE event_kind='plugin.custom'"
        )
        assert len(rows) == 1
        custom = await temp_storage.get(rows[0]["event_id"])
        assert custom.payload["words"] == 2
        assert custom.parent_event_id == "source"
        assert custom.source_native_ref is None
        await runner.process_admitted_event("source")
        assert len(adapter.delivered_payloads) == (2 if explicit_custom else 1)
        phases = runner.phase_snapshot()["counts"]
        assert phases["enrich"] >= 1 and phases["route"] >= 1
    finally:
        await runner.stop()
        assert await host.stop()


async def test_failed_policy_retains_ingress_work_and_prevents_delivery(
    temp_storage,
) -> None:
    async def fail(context, event):
        raise RuntimeError("policy unavailable")

    host = PluginExecutor(
        temp_storage, (binding("gate", PluginStage.EVENT_POLICY, fail),)
    )
    runner, adapter = _pipeline(temp_storage, host)
    await host.start()
    await runner.start()
    try:
        await runner.admit_ingress(make_event(event_id="source"), "live")
        with pytest.raises(
            DurableIngressDeferredError, match="plugin_execution_deferred"
        ):
            await runner.process_admitted_event("source")
        assert adapter.delivered_payloads == []
        phases = runner.phase_snapshot()["counts"]
        assert phases["event_policy"] == 1 and phases["route"] == 0
        row = await temp_storage._read_one(
            "SELECT status FROM durable_ingress_work WHERE event_id='source'"
        )
        assert row["status"] == "pending"
        assert (
            await temp_storage._read_one("SELECT COUNT(*) AS n FROM delivery_receipts")
        )["n"] == 0
    finally:
        await runner.stop()
        await host.stop()


async def test_policy_denial_is_durable_and_distinct_from_zero_route_matches(
    temp_storage,
) -> None:
    async def deny(context, event):
        return Deny("configured gate")

    host = PluginExecutor(
        temp_storage, (binding("gate", PluginStage.EVENT_POLICY, deny),)
    )
    runner, adapter = _pipeline(temp_storage, host)
    await host.start()
    await runner.start()
    try:
        await runner.admit_ingress(make_event(event_id="source"), "live")
        assert await runner.process_admitted_event("source") == []
        assert adapter.delivered_payloads == []
        record = await temp_storage._read_one(
            "SELECT record_json FROM plugin_execution_records"
        )
        assert '"kind":"deny"' in record["record_json"]
        assert '"failure":null' in record["record_json"]
    finally:
        await runner.stop()
        await host.stop()


@pytest.mark.parametrize("durable", [False, True])
async def test_superseding_transform_delivers_only_the_qualified_replacement(
    temp_storage, durable
) -> None:
    async def replace_text(context, event):
        return Supersede(DerivedEvent("message.created", {"text": "translated"}))

    item = binding("translate", PluginStage.TRANSFORM, replace_text)
    host = PluginExecutor(temp_storage, (item,))
    runner, adapter = _pipeline(temp_storage, host)
    await host.start()
    await runner.start()
    try:
        source = make_event(event_id="source", payload={"text": "original"})
        if durable:
            await runner.admit_ingress(source, "live")
            await runner.process_admitted_event("source")
        else:
            await runner.handle_ingress(source)
        assert len(adapter.delivered_payloads) == 1
        assert adapter.delivered_payloads[0].event_id != "source"
        assert adapter.delivered_payloads[0].payload["text"] == "translated"
        assert (await temp_storage.get("source")).payload == {"text": "original"}
    finally:
        await runner.stop()
        await host.stop()


def test_custom_only_and_catch_all_routes_have_distinct_match_domains() -> None:
    from medre.core.routing.router import find_route_conflicts

    catch_all = Route(
        id="bridge",
        source=RouteSource("fake_transport", (), "ch-0"),
        targets=[RouteTarget(adapter="fake_presentation")],
        ownership="exclusive",
    )
    custom = replace(
        catch_all,
        id="observations",
        source=replace(catch_all.source, event_kinds=("plugin.custom",)),
    )
    assert find_route_conflicts([catch_all, custom]) == []
    assert [
        route.id
        for route in Router([catch_all, custom]).match(
            make_event(event_kind="plugin.custom")
        )
    ] == ["observations"]


@pytest.mark.parametrize("mode", ["re_route", "best_effort", "dry_run"])
async def test_routing_replay_cannot_bypass_plugin_policy(temp_storage, mode) -> None:
    from medre.core.engine.replay.engine import ReplayEngine
    from medre.core.engine.replay.types import ReplayMode, ReplayRequest

    async def deny(context, event):
        return Deny("configured gate")

    item = binding("gate", PluginStage.EVENT_POLICY, deny)
    host = PluginExecutor(temp_storage, (item,))
    runner, adapter = _pipeline(temp_storage, host)
    await host.start()
    await runner.start()
    try:
        await runner.admit_ingress(make_event(event_id="source"), "live")
        await runner.process_admitted_event("source")
        replay = ReplayEngine(temp_storage, pipeline=runner)
        with pytest.raises(ValueError, match="Plugin-qualified routing replay"):
            _ = [
                result
                async for result in replay.replay(ReplayRequest(mode=ReplayMode(mode)))
            ]
        assert adapter.delivered_payloads == []
        assert len(item.implementation.calls) == 1
        results = [
            result
            async for result in replay.replay(ReplayRequest(mode=ReplayMode.STRICT))
        ]
        assert len(results) == 1
    finally:
        await runner.stop()
        await host.stop()


@pytest.mark.parametrize(
    "cause",
    [
        "plan_conflict",
        "exhausted",
        "required_contract",
        "corrupt_evidence",
        "corrupt_state",
    ],
)
async def test_permanent_plugin_failures_reach_failed_without_reinvocation(
    temp_storage, cause
) -> None:
    from medre.core.engine.plugin_executor import PluginExecutionLimits
    from medre.core.ingress import DurableIngressWorker
    from medre.plugins import Pass

    async def action(context, event):
        if cause == "exhausted":
            raise RuntimeError("unavailable")
        if cause == "required_contract":
            return Deny("wrong stage")
        return Pass()

    item = binding("example", action=action)
    host = PluginExecutor(
        temp_storage, (item,), limits=PluginExecutionLimits(max_attempts=1)
    )
    runner, adapter = _pipeline(temp_storage, host)
    worker = DurableIngressWorker(
        storage=temp_storage, pipeline=runner, batch_size=1, max_attempts=2
    )
    await host.start()
    await runner.start()
    try:
        await runner.admit_ingress(make_event(event_id="source"), "live")
        if cause == "plan_conflict":
            await temp_storage.bind_plugin_plan("source", "live", "0" * 64)
        elif cause == "corrupt_evidence":
            await host.process("source")
            await temp_storage._write(
                "UPDATE plugin_execution_records SET record_json='{}'"
            )
        elif cause == "corrupt_state":
            await temp_storage._write(
                "INSERT INTO plugin_state(plugin_id, key, value, updated_at) VALUES (?, ?, ?, ?)",
                ("example", '["main","live"]', "{}", "2026-01-01T00:00:00+00:00"),
            )
        assert await worker.run_once() == 0
        row = await temp_storage._read_one(
            "SELECT status, attempts, last_error FROM durable_ingress_work WHERE event_id='source'"
        )
        assert row["status"] == "pending" and row["attempts"] == 1
        assert await worker.run_once() == 0
        row = await temp_storage._read_one(
            "SELECT status, attempts, last_error FROM durable_ingress_work WHERE event_id='source'"
        )
        assert row["status"] == "failed" and row["attempts"] == 2
        assert row["last_error"]
        assert await worker.run_once() == 0
        assert len(item.implementation.calls) == (
            1 if cause in {"exhausted", "required_contract", "corrupt_evidence"} else 0
        )
        assert worker.terminal_failures == 1 and worker.deferrals == 0
        assert adapter.delivered_payloads == []
        assert await temp_storage.get("source") is not None
    finally:
        await runner.stop()
        await host.stop()


async def test_stale_uncommitted_state_uses_operational_retry(
    temp_storage, monkeypatch
) -> None:
    from medre.core.ingress import DurableIngressWorker
    from medre.core.storage.backend import PluginStateConflictError

    item = binding("counter")
    host = PluginExecutor(temp_storage, (item,))
    runner, adapter = _pipeline(temp_storage, host)
    original = temp_storage.commit_plugin_execution
    attempts = []

    async def commit(record, **kwargs):
        attempts.append(record)
        if len(attempts) == 1:
            raise PluginStateConflictError("competing writer")
        return await original(record, **kwargs)

    monkeypatch.setattr(temp_storage, "commit_plugin_execution", commit)
    worker = DurableIngressWorker(
        storage=temp_storage, pipeline=runner, batch_size=1, max_attempts=1
    )
    await host.start()
    await runner.start()
    try:
        await runner.admit_ingress(make_event(event_id="source"), "live")
        assert await worker.run_once() == 0
        assert await worker.run_once() == 1
        assert worker.deferrals == 1 and worker.terminal_failures == 0
        assert len(item.implementation.calls) == 2
        assert len(adapter.delivered_payloads) == 1
    finally:
        await runner.stop()
        await host.stop()
