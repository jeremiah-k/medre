"""Read-only radio attempt inspection never recovers or grants native submission."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from medre.cli.inspect_commands import _inspect_radio_attempt
from medre.cli.main import main
from medre.core.events import DeliveryAttemptProvenance
from medre.core.rendering.radio_text import plan_radio_text
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage_outbox import (
    create_outbox_item_with_parent,
    make_outbox_item,
)


async def test_inspection_preserves_inflight_checkpoint_and_validates_report(
    tmp_path, capsys
):
    """An operator sees the exact plan and current parent without any database writes."""
    from pathlib import Path

    import jsonschema
    from referencing import Registry, Resource

    path = tmp_path / "inspect.db"
    storage = SQLiteStorage(db_path=str(path))
    await storage.initialize()
    item = replace(
        make_outbox_item(status="in_progress"),
        worker_id="pipeline:owner",
        lease_until=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    )
    item = await create_outbox_item_with_parent(storage, item)
    claim = DeliveryAttemptProvenance(
        item.event_id,
        item.delivery_plan_id,
        item.target_adapter,
        item.target_channel,
        item.outbox_id,
        1,
        "live",
    )
    checkpoint = await storage.bind_radio_attempt(
        claim, "pipeline:owner", plan_radio_text("hello " * 15, max_text_bytes=40)
    )
    await storage.begin_radio_part(checkpoint, "pipeline:owner", 1)
    await storage.close()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    await _inspect_radio_attempt(item.outbox_id, 1, storage_path=str(path))
    report = json.loads(capsys.readouterr().out)
    assert report["outbox"]["event_id"] == item.event_id
    assert report["outbox"]["current_status"] == "in_progress"
    assert report["progress"]["parts"][0]["state"] == "submitting"
    assert report["progress"]["parts"][1]["state"] == "unsent"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    directory = Path(__file__).resolve().parents[1] / "docs/schemas"
    registry = Registry()
    for name in (
        "radio-text-plan",
        "radio-attempt-progress",
        "radio-attempt-metadata",
        "radio-attempt-inspection",
    ):
        schema = json.loads((directory / f"{name}.schema.json").read_text())
        registry = registry.with_resource(schema["$id"], Resource.from_contents(schema))
    jsonschema.Draft202012Validator(schema, registry=registry).validate(report)


@pytest.mark.parametrize("attempt", ["0", "-1", "bad"])
def test_cli_rejects_invalid_generations_before_opening_storage(attempt, tmp_path):
    """Invalid arguments never create or open an operator's storage path."""
    path = tmp_path / "absent.db"
    with pytest.raises(SystemExit) as caught:
        main(
            ["inspect", "radio-attempt", "outbox", attempt, "--storage-path", str(path)]
        )
    assert caught.value.code == 2
    assert not path.exists()


async def test_absent_radio_attempt_exits_not_found_without_creating_state(
    tmp_path, capsys
):
    """A valid empty database reports absence rather than inventing progress."""
    path = tmp_path / "empty.db"
    storage = SQLiteStorage(db_path=str(path))
    await storage.initialize()
    await storage.close()
    with pytest.raises(SystemExit) as caught:
        await _inspect_radio_attempt("missing", 1, storage_path=str(path))
    assert caught.value.code == 5
    assert "not found" in capsys.readouterr().err
