"""Coverage for durable-ingress boundaries in the core pipeline."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from medre.core.engine.pipeline import PipelineRunner
from medre.core.events import NativeRef
from medre.core.routing import Router
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.core.supervision.accounting import RuntimeAccounting
from tests.helpers.pipeline import make_event, make_pipeline_config_for_pipeline


def _native_ref(native_id: str) -> NativeRef:
    return NativeRef(
        adapter="matrix-main",
        native_channel_id="!room:example.org",
        native_message_id=native_id,
    )


def _runner(
    storage: SQLiteStorage, *, accounting: RuntimeAccounting | None = None
) -> PipelineRunner:
    config = make_pipeline_config_for_pipeline(storage, Router([]))
    config.runtime_accounting = accounting
    return PipelineRunner(config)


async def test_admit_ingress_tracks_created_duplicate_and_history(
    temp_storage: SQLiteStorage,
) -> None:
    accounting = RuntimeAccounting()
    runner = _runner(temp_storage, accounting=accounting)

    first = make_event(event_id="evt-first", source_native_ref=_native_ref("$same"))
    created = await runner.admit_ingress(first, "live")
    duplicate = await runner.admit_ingress(
        make_event(event_id="evt-redecoded", source_native_ref=_native_ref("$same")),
        "recovered",
    )
    history = await runner.admit_ingress(
        make_event(event_id="evt-history", source_native_ref=_native_ref("$history")),
        "history",
    )

    assert created.created is True
    assert created.duplicate is False
    assert duplicate.created is False
    assert duplicate.duplicate is True
    assert duplicate.event_id == first.event_id
    assert history.work_status == "suppressed_history"
    assert accounting.snapshot()["inbound_accepted"] == 2
    assert accounting.snapshot()["loop_prevented"] == 1
    assert await temp_storage.resolve_native_ref(
        "matrix-main", "!room:example.org", "$same"
    ) == first.event_id


async def test_admit_ingress_without_native_ref_creates_pending_work(
    temp_storage: SQLiteStorage,
) -> None:
    runner = _runner(temp_storage)
    event = make_event(event_id="evt-no-native", source_native_ref=None)

    result = await runner.admit_ingress(event, "live")

    assert result.created is True
    assert result.work_status == "pending"
    assert PipelineRunner._build_inbound_native_ref(event) is None


def test_build_inbound_native_ref_rejects_empty_native_id() -> None:
    event = make_event(
        event_id="evt-empty-native",
        source_native_ref=NativeRef(
            adapter="matrix-main",
            native_channel_id="!room:example.org",
            native_message_id="",
        ),
    )

    assert PipelineRunner._build_inbound_native_ref(event) is None


async def test_process_admitted_event_rejects_missing_event(
    temp_storage: SQLiteStorage,
) -> None:
    runner = _runner(temp_storage)

    with pytest.raises(RuntimeError, match="admitted ingress event is missing"):
        await runner.process_admitted_event("evt-missing")


async def test_process_admitted_event_suppresses_reaction_to_reaction(
    temp_storage: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _runner(temp_storage)
    event = make_event(event_id="evt-reaction")
    await temp_storage.append(event)
    is_reaction = AsyncMock(return_value=True)
    route_event = AsyncMock()
    monkeypatch.setattr(runner, "_is_reaction_to_reaction", is_reaction)
    monkeypatch.setattr(runner, "route_event", route_event)

    assert await runner.process_admitted_event(event.event_id) == []
    is_reaction.assert_awaited_once_with(event)
    route_event.assert_not_awaited()


async def test_process_admitted_event_handles_no_routes(
    temp_storage: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _runner(temp_storage)
    event = make_event(event_id="evt-no-routes")
    await temp_storage.append(event)
    monkeypatch.setattr(
        runner, "_is_reaction_to_reaction", AsyncMock(return_value=False)
    )
    route_event = AsyncMock(return_value=(event, []))
    deliver = AsyncMock()
    monkeypatch.setattr(runner, "route_event", route_event)
    monkeypatch.setattr(runner, "deliver_to_targets", deliver)

    assert await runner.process_admitted_event(event.event_id) == []
    route_event.assert_awaited_once_with(event)
    deliver.assert_not_awaited()


async def test_process_admitted_event_delivers_routed_targets(
    temp_storage: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _runner(temp_storage)
    event = make_event(event_id="evt-routed")
    await temp_storage.append(event)
    delivery = object()
    outcome = object()
    monkeypatch.setattr(
        runner, "_is_reaction_to_reaction", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(
        runner, "route_event", AsyncMock(return_value=(event, [delivery]))
    )
    deliver = AsyncMock(return_value=[outcome])
    monkeypatch.setattr(runner, "deliver_to_targets", deliver)

    assert await runner.process_admitted_event(event.event_id) == [outcome]
    deliver.assert_awaited_once_with(event, [delivery])
