"""Exact delivery inspection cannot widen to another adapter/channel."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from medre.cli.recover_commands import _build_event_recovery_runbook
from medre.core.delivery_authority import DeliveryAuthorityResolver, DeliveryIdentity
from medre.core.events import DeliveryReceipt
from medre.core.observability.exact_recovery import (
    ExactDeliverySelector,
    exact_delivery_preview,
)
from medre.core.storage.backend import DeliveryOutboxItem


def outbox(*, channel="!room-a", plan="p", attempt=0):
    return DeliveryOutboxItem(
        outbox_id=f"{plan}-{channel}",
        event_id="e",
        route_id="r",
        delivery_plan_id=plan,
        target_adapter="matrix",
        target_channel=channel,
        status="in_progress",
        attempt_number=attempt,
        active_attempt=3,
    )


def select(channel="!room-a", plan="p"):
    return ExactDeliverySelector(DeliveryIdentity("e", plan, "matrix", channel))


def test_exact_preview_excludes_sibling_and_binds_active_generation():
    authority = DeliveryAuthorityResolver([], [outbox(), outbox(channel="!room-b")])
    report = exact_delivery_preview(authority, select())
    assert report["authority"]["target_channel"] == "!room-a"
    assert report["authority"]["attempt_number"] == 3
    assert report["excluded_sibling_count"] == 1
    assert report["execution_authorized"] is False
    assert len(report["observed_fingerprint"]) == 64


def test_missing_or_changed_plan_never_widens():
    authority = DeliveryAuthorityResolver([], [outbox(), outbox(channel="!room-b")])
    for selector in (select("!room-c"), select(plan="rebound")):
        with pytest.raises(ValueError, match="absent or ambiguous"):
            exact_delivery_preview(authority, selector)


def test_empty_and_absent_channels_are_one_explicit_identity():
    authority = DeliveryAuthorityResolver([], [outbox(channel="")])
    report = exact_delivery_preview(authority, select(None))
    assert report["authority"]["target_channel"] is None
    with pytest.raises(ValueError):
        ExactDeliverySelector(DeliveryIdentity("", "p", "matrix", None))


def test_preview_fingerprint_changes_when_attempt_generation_changes():
    first = exact_delivery_preview(DeliveryAuthorityResolver([], [outbox()]), select())
    changed = outbox()
    changed = replace(changed, active_attempt=4)
    second = exact_delivery_preview(DeliveryAuthorityResolver([], [changed]), select())
    assert first["observed_fingerprint"] != second["observed_fingerprint"]


async def test_exact_cli_excludes_siblings_and_has_no_commands(
    temp_storage, sample_event
):
    await temp_storage.append(sample_event)
    for channel in ("!room-a", "!room-b"):
        await temp_storage.create_outbox_item(
            DeliveryOutboxItem(
                outbox_id=f"box-{channel}",
                event_id=sample_event.event_id,
                delivery_plan_id="plan",
                target_adapter="matrix",
                target_channel=channel,
                route_id="route",
                status="pending",
            )
        )
    selector = ExactDeliverySelector(
        DeliveryIdentity(sample_event.event_id, "plan", "matrix", "!room-a")
    )
    report = await _build_event_recovery_runbook(
        temp_storage,
        sample_event.event_id,
        storage_path="/private/db.sqlite",
        exact_selector=selector,
    )
    assert report["scope"] == "exact_delivery_preview"
    assert report["exact_preview"]["excluded_sibling_count"] == 1
    assert len(report["delivery_actions"]) == 1
    assert report["delivery_actions"][0]["target_channel"] == "!room-a"
    assert report["recommended_commands"] == []
    assert report["delivery_actions"][0]["commands"] == []
    assert "timeline" not in report
    validate_exact_schema(report)


def validate_exact_schema(report):
    """Validate the in-memory runbook, not a JSON-round-tripped copy."""
    directory = Path(__file__).parents[1] / "docs/schemas"
    action_schema = json.loads((directory / "recovery-action.schema.json").read_text())
    registry = Registry().with_resource(
        action_schema["$id"], Resource.from_contents(action_schema)
    )
    schema = json.loads((directory / "exact-recovery-preview.schema.json").read_text())
    Draft202012Validator(schema, registry=registry).validate(report)


async def test_exact_cli_no_channel_matches_empty_persisted_channel(
    temp_storage, sample_event
):
    """No-channel runbooks use the same identity normalization as the resolver."""
    await temp_storage.append(sample_event)
    await temp_storage.create_outbox_item(
        DeliveryOutboxItem(
            outbox_id="box-no-channel",
            event_id=sample_event.event_id,
            delivery_plan_id="plan",
            target_adapter="matrix",
            target_channel="",
            route_id="route",
            status="pending",
        )
    )
    report = await _build_event_recovery_runbook(
        temp_storage,
        sample_event.event_id,
        storage_path="/private/db.sqlite",
        exact_selector=ExactDeliverySelector(
            DeliveryIdentity(sample_event.event_id, "plan", "matrix", None)
        ),
    )
    assert len(report["delivery_actions"]) == 1
    assert report["delivery_actions"][0]["target_channel"] is None
    assert report["exact_preview"]["authority"]["target_channel"] is None
    validate_exact_schema(report)


async def test_receipt_only_exact_preview_has_stable_generation_and_schema(
    temp_storage, sample_event
):
    """Build real event evidence with a receipt but no durable outbox row."""
    await temp_storage.append(sample_event)
    receipt = DeliveryReceipt(
        receipt_id="receipt-3",
        event_id=sample_event.event_id,
        delivery_plan_id="plan",
        target_adapter="matrix",
        target_channel="!room",
        status="sent",
        attempt_number=3,
        receipt_kind="attempt",
    )
    await temp_storage.append_receipt(receipt)
    selector = ExactDeliverySelector(
        DeliveryIdentity(sample_event.event_id, "plan", "matrix", "!room")
    )
    authority = DeliveryAuthorityResolver([receipt], [])
    preview = exact_delivery_preview(authority, selector)
    assert preview == exact_delivery_preview(authority, selector)
    assert preview["authority"]["outbox_id"] is None
    assert preview["authority"]["attempt_number"] == 3
    assert preview["authority"]["receipt_id"] == "receipt-3"
    assert preview["authority"]["receipt_status"] == "sent"
    reports = [
        await _build_event_recovery_runbook(
            temp_storage,
            sample_event.event_id,
            storage_path="/private/db.sqlite",
            exact_selector=selector,
        )
        for _ in range(2)
    ]
    assert reports[0]["exact_preview"]["authority"] == preview["authority"]
    assert (
        reports[0]["exact_preview"]["observed_fingerprint"]
        == reports[1]["exact_preview"]["observed_fingerprint"]
    )
    for report in reports:
        validate_exact_schema(report)


def test_exact_cli_argument_group_requires_complete_explicit_channel():
    from medre.cli.main import _build_parser

    parser = _build_parser(include_adapter_cli=False)
    base = ["recover", "--storage-path", "db.sqlite", "--event", "event"]
    args = parser.parse_args(
        [
            *base,
            "--exact-plan-id",
            "p",
            "--exact-adapter",
            "matrix",
            "--exact-channel",
            "!room-a",
        ]
    )
    assert args.exact_channel == "!room-a"
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                *base,
                "--exact-plan-id",
                "p",
                "--exact-adapter",
                "matrix",
                "--exact-channel",
                "!room-a",
                "--exact-no-channel",
            ]
        )


async def test_empty_exact_event_is_a_config_error_before_storage(capsys):
    from medre.cli.exit_codes import EXIT_CONFIG
    from medre.cli.recover_commands import _recover

    with pytest.raises(SystemExit) as exc:
        await _recover(
            "",
            None,
            None,
            None,
            True,
            storage_path="must-not-open.sqlite",
            exact_plan_id="p",
            exact_adapter="matrix",
            exact_channel="!room-a",
        )
    assert exc.value.code == EXIT_CONFIG
    assert "exact preview requires" in capsys.readouterr().err


@pytest.mark.parametrize(
    "exact_options",
    [
        {"exact_no_channel": True},
        {"exact_plan_id": "p"},
        {"exact_adapter": "matrix"},
        {"exact_channel": "!room-a"},
        {"exact_plan_id": "p", "exact_adapter": "matrix", "exact_no_channel": False},
        {"exact_plan_id": "p", "exact_adapter": "matrix", "exact_channel": ""},
        {
            "exact_plan_id": "p",
            "exact_adapter": "matrix",
            "exact_channel": "!room-a",
            "exact_no_channel": True,
        },
    ],
)
async def test_incomplete_exact_selector_refuses_before_opening_storage(
    exact_options, monkeypatch, capsys
):
    from medre.cli import recover_commands
    from medre.cli.exit_codes import EXIT_CONFIG

    def refuse_open(*args, **kwargs):
        pytest.fail("Incomplete selectors must not open storage")

    monkeypatch.setattr(recover_commands, "_open_readonly_storage", refuse_open)
    with pytest.raises(SystemExit) as exc:
        await recover_commands._recover(
            "e",
            None,
            None,
            None,
            True,
            storage_path="not-opened.sqlite",
            **exact_options,
        )
    assert exc.value.code == EXIT_CONFIG
    assert "exact preview requires" in capsys.readouterr().err


async def test_exact_target_absent_exits_not_found_without_mutating_storage(
    monkeypatch, capsys
):
    """Absent exact destinations have the same code as absent events."""
    from medre.cli import recover_commands
    from medre.cli.exit_codes import EXIT_NOT_FOUND
    from medre.core.observability.exact_recovery import ExactPreviewError

    class ReadOnlyStorage:
        async def close(self):
            pass

    async def open_storage(_path):
        return ReadOnlyStorage()

    async def missing(*_args, **_kwargs):
        raise ExactPreviewError("Exact recovery target is absent or ambiguous")

    monkeypatch.setattr(recover_commands, "_open_readonly_storage", open_storage)
    monkeypatch.setattr(recover_commands, "_build_event_recovery_runbook", missing)
    with pytest.raises(SystemExit) as result:
        await recover_commands._recover(
            "event",
            None,
            None,
            None,
            True,
            storage_path="ignored.sqlite",
            exact_plan_id="plan",
            exact_adapter="matrix",
            exact_channel="!room",
        )
    assert result.value.code == EXIT_NOT_FOUND
    assert "absent or ambiguous" in capsys.readouterr().err


@pytest.mark.parametrize("exact", [False, True])
async def test_recover_does_not_mask_unrelated_storage_value_errors(monkeypatch, exact):
    """Storage/data errors must never become selector-not-found errors."""
    from medre.cli import recover_commands

    class ReadOnlyStorage:
        async def close(self):
            pass

    async def open_storage(_path):
        return ReadOnlyStorage()

    async def bad_data(*_args, **_kwargs):
        raise ValueError("Malformed durable receipt")

    monkeypatch.setattr(recover_commands, "_open_readonly_storage", open_storage)
    monkeypatch.setattr(recover_commands, "_build_event_recovery_runbook", bad_data)
    with pytest.raises(ValueError, match="Malformed durable receipt"):
        await recover_commands._recover(
            "event",
            None,
            None,
            None,
            True,
            storage_path="ignored.sqlite",
            **(
                {
                    "exact_plan_id": "plan",
                    "exact_adapter": "matrix",
                    "exact_channel": "!room",
                }
                if exact
                else {}
            ),
        )
