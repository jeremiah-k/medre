"""Recovery advice follows dispatch ownership and keeps previews scoped."""

import json
import shlex
from dataclasses import asdict
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from medre.cli.recover_commands import _build_event_recovery_runbook
from medre.core.delivery_authority import DeliveryAuthorityResolver, delivery_identity
from medre.core.events import DeliveryReceipt
from medre.core.observability.classification import recommended_commands
from medre.core.observability.recovery import recovery_action
from medre.core.storage.backend import DeliveryOutboxItem


def outbox(status="pending", **values):
    return DeliveryOutboxItem(
        **{
            "outbox_id": "box",
            "event_id": "event",
            "route_id": "route",
            "delivery_plan_id": "plan",
            "target_adapter": "destination",
            "target_channel": "channel",
            "status": status,
            **values,
        }
    )


def action(row, receipts=()):
    snapshot = DeliveryAuthorityResolver(receipts, [row]).resolve(
        delivery_identity(row)
    )
    return recovery_action(
        snapshot, storage_path="/private/state with spaces/db.sqlite"
    )


@pytest.mark.parametrize(
    "status,deadline,expected",
    [
        ("pending", None, "pending"),
        ("in_progress", None, "in_progress"),
        ("queued", None, "awaiting_feedback"),
        ("retry_wait", "2026-10-29T12:00:00Z", "retry_scheduled"),
        ("retry_wait", None, "needs_operator_action"),
        ("completed", None, "completed"),
        ("cancelled", None, "cancelled"),
        ("abandoned", None, "inspect_native"),
        ("future-state", None, "needs_operator_action"),
    ],
)
def test_guidance_preserves_unsettled_ownership_without_receipt(
    status, deadline, expected
):
    result = action(
        outbox(
            status,
            next_attempt_at=deadline,
            active_attempt=2 if status == "in_progress" else None,
        )
    )
    assert result.disposition == expected
    assert result.receipt_id is None
    assert all("replay" not in shlex.split(command) for command in result.commands)
    assert result.attempt_number == (2 if status == "in_progress" else 1)
    schema = json.loads(
        (
            Path(__file__).parents[1] / "docs/schemas/recovery-action.schema.json"
        ).read_text()
    )
    # JSON round-trip reflects tuple-to-array conversion on the CLI wire.
    Draft202012Validator(schema).validate(json.loads(json.dumps(asdict(result))))


def test_current_failed_destination_gets_scoped_preview_only():
    row = outbox("dead_lettered", receipt_id="failure")
    receipt = DeliveryReceipt(
        receipt_id="failure",
        receipt_kind="attempt",
        event_id="event",
        delivery_plan_id="plan",
        outbox_id="box",
        target_adapter="destination",
        target_channel="channel",
        route_id="route",
        status="failed",
        failure_kind="retry_exhausted",
    )
    result = action(row, [receipt])
    assert result.disposition == "preview_replay"
    commands = [shlex.split(command) for command in result.commands]
    preview = next(command for command in commands if "replay" in command)
    assert preview[preview.index("--mode") + 1] == "dry_run"
    assert preview[preview.index("--target-adapters") + 1] == "destination"
    assert preview[preview.index("--route-ids") + 1] == "route"
    assert not any("best_effort" in command for command in commands)


def test_stale_failed_receipt_cannot_override_active_outbox_generation():
    receipt = DeliveryReceipt(
        receipt_id="prior",
        receipt_kind="attempt",
        event_id="event",
        delivery_plan_id="plan",
        outbox_id="box",
        target_adapter="destination",
        target_channel="channel",
        status="failed",
        failure_kind="adapter_transient",
    )
    result = action(
        outbox("in_progress", active_attempt=2, receipt_id="prior"), [receipt]
    )
    assert result.disposition == "in_progress"
    assert result.receipt_id is None
    assert all("replay" not in shlex.split(command) for command in result.commands)


@pytest.mark.parametrize(
    "category", ["retryable", "permanent", "operational", "unknown"]
)
def test_recommended_commands_quote_opaque_event_and_storage_values(category):
    event = "event; touch /tmp/should-never-run ' snow ☃"
    path = "/private/state $(not-a-command)/state.sqlite"
    for command in recommended_commands(category, event, storage_path=path):
        argv = shlex.split(command)
        if "inspect" in argv:
            assert event in argv
            assert argv[argv.index("--storage-path") + 1] == path
        if "replay" in argv:
            assert argv[argv.index("--event") + 1] == event


async def test_runbook_includes_admitted_work_before_first_receipt(
    temp_storage, sample_event
):
    await temp_storage.append(sample_event)
    await temp_storage.create_outbox_item(
        DeliveryOutboxItem(
            outbox_id="fresh",
            event_id=sample_event.event_id,
            delivery_plan_id="fresh-plan",
            route_id="route",
            target_adapter="destination",
            target_channel="channel",
        )
    )
    result = await _build_event_recovery_runbook(
        temp_storage, sample_event.event_id, storage_path="fixture.sqlite"
    )
    assert result is not None
    assert result["failed_targets"] == []
    assert len(result["delivery_actions"]) == 1
    assert result["delivery_actions"][0]["disposition"] == "pending"
    assert all(
        "replay" not in shlex.split(command)
        for command in result["recommended_commands"]
    )
    assert await temp_storage.list_receipts_for_event(sample_event.event_id) == []


@pytest.mark.parametrize(
    "category", ["retryable", "permanent", "operational", "unknown"]
)
def test_opaque_leading_dash_event_ids_remain_values_in_emitted_commands(category):
    from medre.cli.main import _build_parser

    parser = _build_parser(include_adapter_cli=False)
    for command in recommended_commands(
        category, "--help", storage_path="/private/state.sqlite"
    ):
        argv = shlex.split(command)[1:]
        if argv[0] in {"replay", "inspect"}:
            parsed = parser.parse_args(argv)
            assert (
                getattr(parsed, "event_id", getattr(parsed, "event", None)) == "--help"
            )


async def test_recover_and_evidence_share_current_guidance(temp_storage, sample_event):
    from medre.runtime.evidence._storage_sections import (
        _collect_storage_data_from_backend,
    )

    await temp_storage.append(sample_event)
    await temp_storage.create_outbox_item(
        DeliveryOutboxItem(
            outbox_id="shared",
            event_id=sample_event.event_id,
            delivery_plan_id="shared-plan",
            route_id="route",
            target_adapter="destination",
            target_channel="channel",
        )
    )
    runbook = await _build_event_recovery_runbook(
        temp_storage, sample_event.event_id, storage_path="fixture.sqlite"
    )
    section = await _collect_storage_data_from_backend(
        temp_storage, "fixture.sqlite", sample_event.event_id, None
    )
    assert section["status"] == "passed", section.get("error")
    summary = section["data"]["incident_summary"]
    assert summary["delivery_actions"] == runbook["delivery_actions"]
    assert summary["recommended_commands"] == runbook["recommended_commands"]
    assert all(
        "replay" not in shlex.split(command)
        for command in summary["recommended_commands"]
    )


def test_option_like_filter_and_path_values_do_not_become_cli_flags():
    from medre.cli.main import _build_parser

    parser = _build_parser(include_adapter_cli=False)
    commands = recommended_commands(
        "retryable",
        "event",
        storage_path="-state.sqlite",
        target_adapter="-adapter",
        route_id="-route",
        include_send=False,
    )
    for command in commands:
        args = parser.parse_args(shlex.split(command)[1:])
        if "replay" in shlex.split(command):
            assert args.target_adapters == ["-adapter"]
            assert args.route_ids == ["-route"]
        else:
            assert args.storage_path == "-state.sqlite"


def test_outboxless_scheduled_retry_is_not_a_manual_replay_suggestion():
    from datetime import datetime, timezone

    receipt = DeliveryReceipt(
        receipt_id="legacy",
        receipt_kind="attempt",
        event_id="event",
        delivery_plan_id="plan",
        target_adapter="destination",
        target_channel="channel",
        status="failed",
        failure_kind="adapter_transient",
        next_retry_at=datetime(2026, 10, 29, tzinfo=timezone.utc),
    )
    snapshot = DeliveryAuthorityResolver([receipt], []).resolve(
        delivery_identity(receipt)
    )
    result = recovery_action(snapshot, storage_path="fixture.sqlite")
    assert result.disposition == "retry_scheduled"
    assert all("replay" not in shlex.split(command) for command in result.commands)


@pytest.mark.parametrize(
    "failure_kind,expected",
    [("adapter_permanent", "--evidence"), ("capacity_rejection", "diagnostics")],
)
@pytest.mark.parametrize("with_outbox", [False, True])
def test_settled_failures_retain_readonly_diagnostic_advice(
    failure_kind, expected, with_outbox
):
    receipt = DeliveryReceipt(
        receipt_id="failure",
        receipt_kind="attempt",
        event_id="event",
        delivery_plan_id="plan",
        outbox_id="box" if with_outbox else None,
        target_adapter="destination",
        target_channel="channel",
        route_id="route",
        status="failed",
        failure_kind=failure_kind,
    )
    row = outbox("dead_lettered", receipt_id="failure")
    resolver = DeliveryAuthorityResolver([receipt], [row] if with_outbox else [])
    result = recovery_action(
        resolver.resolve(delivery_identity(receipt)), storage_path="fixture.sqlite"
    )
    assert result.disposition == "needs_operator_action"
    assert any(expected in shlex.split(command) for command in result.commands)
    assert all("replay" not in shlex.split(command) for command in result.commands)


@pytest.mark.parametrize("status", ["abandoned", "future-state"])
def test_uncertain_or_unrecognized_outbox_never_inherits_replay_remedy(status):
    receipt = DeliveryReceipt(
        receipt_id="failure",
        receipt_kind="attempt",
        event_id="event",
        delivery_plan_id="plan",
        outbox_id="box",
        target_adapter="destination",
        target_channel="channel",
        status="failed",
        failure_kind="adapter_transient",
    )
    result = action(outbox(status, receipt_id="failure"), [receipt])
    assert result.disposition in {"inspect_native", "needs_operator_action"}
    assert all("replay" not in shlex.split(command) for command in result.commands)


async def test_specialized_commands_preserve_option_like_values(
    temp_storage, sample_event
):
    from medre.cli.main import _build_parser
    from medre.runtime.evidence._storage_sections import (
        _collect_storage_data_from_backend,
    )

    await temp_storage.append(sample_event)
    parser = _build_parser(include_adapter_cli=False)
    runbook = await _build_event_recovery_runbook(
        temp_storage, sample_event.event_id, storage_path="-state.sqlite"
    )
    section = await _collect_storage_data_from_backend(
        temp_storage, "-state.sqlite", sample_event.event_id, "--run"
    )
    summary = section["data"]["incident_summary"]
    commands = [
        *runbook["commands"]["specialized"],
        *summary["commands"]["specialized"],
        summary["commands"]["primary"][-1],
    ]
    for command in commands:
        args = parser.parse_args(shlex.split(command)[1:])
        assert args.storage_path == "-state.sqlite"


async def test_pending_guidance_omits_unrelated_replay_configuration_note(
    temp_storage, sample_event, capsys
):
    from medre.cli.recover_commands import _print_event_runbook

    await temp_storage.append(sample_event)
    await temp_storage.create_outbox_item(
        DeliveryOutboxItem(
            outbox_id="pending",
            event_id=sample_event.event_id,
            delivery_plan_id="plan",
            route_id="route",
            target_adapter="destination",
        )
    )
    result = await _build_event_recovery_runbook(
        temp_storage, sample_event.event_id, storage_path="fixture.sqlite"
    )
    _print_event_runbook(result)
    output = capsys.readouterr().out
    assert "Current delivery guidance:" in output
    assert "destination route=route: pending" in output
    assert "medre replay has no" not in output
