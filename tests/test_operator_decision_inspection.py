"""Operator decision inspection is read-only, scoped, and explicit about provenance."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import jsonschema
import msgspec
import pytest

from medre.cli.decision_inspect import _inspect_decisions
from medre.cli.main import _build_parser, main
from medre.core.delivery_authority import DeliveryIdentity
from medre.core.storage.sqlite._operator_decision import CancelUnstartedDecision
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage_outbox import make_outbox_item


def test_inspect_decisions_cli_requires_event_and_storage():
    parser = _build_parser(include_adapter_cli=False)
    with pytest.raises(SystemExit):
        parser.parse_args(["inspect", "decisions", "--event", "event-1"])
    parsed = parser.parse_args(
        ["inspect", "decisions", "--event", "event-1", "--storage-path", "state.db"]
    )
    assert parsed.inspect_command == "decisions"


async def test_inspection_is_bounded_and_reports_decision_provenance(
    tmp_path, sample_event, capsys
):
    path = str(tmp_path / "state.sqlite")
    store = SQLiteStorage(path)
    await store.initialize()
    try:
        await store.append(sample_event)
        first = make_outbox_item(
            event_id=sample_event.event_id,
            delivery_plan_id="plan-a",
            target_adapter="matrix",
            target_channel="!a:example.net",
        )
        await store.create_outbox_item(first)
        current = await store.get_outbox_item(first.outbox_id)
        assert current is not None
        observed_updated_at = current.updated_at
        assert await store.cancel_unstarted_delivery(
            CancelUnstartedDecision(
                decision_id="choice-1",
                actor_id="operator-label",
                identity=DeliveryIdentity(
                    sample_event.event_id, "plan-a", "matrix", "!a:example.net"
                ),
                outbox_id=current.outbox_id,
                expected_generation=current.attempt_number,
                expected_status=current.status,
                expected_updated_at=current.updated_at,
            )
        )
    finally:
        await store.close()

    await _inspect_decisions(sample_event.event_id, storage_path=path)
    output = json.loads(capsys.readouterr().out)
    schema_path = (
        Path(__file__).parents[1]
        / "docs/schemas/operator-decision-inspection.schema.json"
    )
    schema = json.loads(schema_path.read_text())
    jsonschema.validate(output, schema)
    assert output["version"] == 1
    assert output["actor_labels_authenticated"] is False
    assert len(output["decisions"]) == 1
    assert output["decisions"][0]["decision_id"] == "choice-1"
    assert output["decisions"][0]["actor_label"] == "operator-label"
    assert output["decisions"][0]["observed_updated_at"] == observed_updated_at
    assert output["decisions"][0]["recorded_at"]
    assert (
        "observed_updated_at" in schema["properties"]["decisions"]["items"]["required"]
    )
    assert "payload" not in str(output)
    await _inspect_decisions(sample_event.event_id, storage_path=path)
    assert len(json.loads(capsys.readouterr().out)["decisions"]) == 1


async def test_inspection_distinguishes_missing_event(tmp_path, capsys):
    path = str(tmp_path / "state.sqlite")
    store = SQLiteStorage(path)
    await store.initialize()
    await store.close()
    with pytest.raises(SystemExit) as error:
        await _inspect_decisions("absent", storage_path=path)
    assert error.value.code == 5
    assert not capsys.readouterr().out


async def test_inspection_does_not_decode_corrupt_event_content(
    tmp_path, sample_event, capsys
):
    """Audit evidence is available even when canonical event JSON is unreadable."""
    path = str(tmp_path / "damaged.sqlite")
    store = SQLiteStorage(path)
    await store.initialize()
    try:
        await store.append(sample_event)
        item = make_outbox_item(
            event_id=sample_event.event_id,
            delivery_plan_id="plan-a",
            target_adapter="matrix",
            target_channel="!a:example.net",
        )
        await store.create_outbox_item(item)
        current = await store.get_outbox_item(item.outbox_id)
        assert current is not None
        await store.cancel_unstarted_delivery(
            CancelUnstartedDecision(
                decision_id="damaged-body-decision",
                actor_id="local-operator",
                identity=DeliveryIdentity(
                    sample_event.event_id, "plan-a", "matrix", "!a:example.net"
                ),
                outbox_id=current.outbox_id,
                expected_generation=current.attempt_number,
                expected_status=current.status,
                expected_updated_at=current.updated_at,
            )
        )
        # Simulated damaged on-disk payload: inspection needs only event ID.
        await store._write(
            "UPDATE canonical_events SET payload=? WHERE event_id=?",
            ("{invalid-json", sample_event.event_id),
        )
    finally:
        await store.close()

    reopened = await SQLiteStorage.open_readonly(path)
    try:
        assert await reopened.event_exists(sample_event.event_id)
        with pytest.raises(msgspec.DecodeError):
            await reopened.get(sample_event.event_id)
    finally:
        await reopened.close()
    await _inspect_decisions(sample_event.event_id, storage_path=path)
    report = json.loads(capsys.readouterr().out)
    assert report["decisions"][0]["decision_id"] == "damaged-body-decision"
    assert report["decisions"][0]["observed_updated_at"]


@pytest.mark.parametrize("event_id", [" ", "x" * 513, "\t\n"])
async def test_inspection_accepts_canonical_ids_without_decisions(
    tmp_path, sample_event, event_id, capsys
):
    """Read-only inspection uses the canonical ID contract, not write limits."""
    path = str(tmp_path / "state.sqlite")
    store = SQLiteStorage(path)
    await store.initialize()
    try:
        await store.append(msgspec.structs.replace(sample_event, event_id=event_id))
    finally:
        await store.close()
    await _inspect_decisions(event_id, storage_path=path)
    output = json.loads(capsys.readouterr().out)
    assert output["event_id"] == event_id
    assert output["decisions"] == []


@pytest.mark.parametrize("event_id", ["", None, 42])
async def test_decision_lookup_rejects_invalid_canonical_ids(temp_storage, event_id):
    """Malformed read identities are refused before any audit lookup."""
    with pytest.raises(ValueError, match="non-empty event identifier"):
        await temp_storage.list_operator_decisions(event_id)


def test_decision_inspection_runs_through_cli_dispatch(tmp_path, sample_event, capsys):
    """The public command reaches read-only inspection with the requested event."""
    path = str(tmp_path / "state.sqlite")

    async def seed():
        store = SQLiteStorage(path)
        await store.initialize()
        try:
            await store.append(sample_event)
        finally:
            await store.close()

    asyncio.run(seed())
    main(
        [
            "inspect",
            "decisions",
            "--event",
            sample_event.event_id,
            "--storage-path",
            path,
        ]
    )
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "version": 1,
        "event_id": sample_event.event_id,
        "actor_labels_authenticated": False,
        "decisions": [],
    }
