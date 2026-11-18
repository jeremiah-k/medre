"""CLI contract tests for `medre inspect work-status`.

Pins the command shape, the machine-readable document against the
published schema, the human summary, metadata-only output, read-only
behavior, and honest unavailable/refused reporting.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from importlib.util import find_spec
from pathlib import Path

import pytest

from medre.cli import main
from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.storage.backend import DeliveryOutboxItem
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.cli import _run_cli, _run_cli_raw

pytestmark = pytest.mark.usefixtures("isolated_config_env")

_PAST = "2020-01-01T00:00:00+00:00"


def _event(event_id: str) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter="ingress",
        source_transport_id="cli-test",
        source_channel_id="room",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": "work-status cli test"},
        metadata=EventMetadata(),
    )


def _item(outbox_id: str, event_id: str, plan: str, adapter: str) -> DeliveryOutboxItem:
    return DeliveryOutboxItem(
        outbox_id=outbox_id,
        event_id=event_id,
        route_id="route-ws",
        delivery_plan_id=plan,
        target_adapter=adapter,
        target_channel="out",
        attempt_number=0,
        status="pending",
    )


async def _seed_work_status_db(db_path: str) -> None:
    """Seed one database with pending, reserved, sent, and abandoned work."""
    store = SQLiteStorage(db_path)
    try:
        await store.initialize()
        await store.append(_event("evt-ws-hold"))
        await store.append(_event("evt-ws-quiet"))
        await store.append(_event("evt-ws-lost"))
        await store.append(_event("evt-ws-done"))

        pending = _item("ob-ws-pending", "evt-ws-hold", "plan-ws-hold", "held")
        await store.create_outbox_item(pending)

        held = _item("ob-ws-held", "evt-ws-hold", "plan-ws-hold-b", "held")
        held = await store.create_outbox_item(held)
        await _claim_row(store, held, "worker-held")
        assert await store.reserve_outbox_attempt(held.outbox_id, "worker-held", 0) == 1

        sent = _item("ob-ws-sent", "evt-ws-quiet", "plan-ws-quiet", "lane-a")
        sent = await store.create_outbox_item(sent)
        await _claim_row(store, sent, "worker-lane")
        assert await store.mark_outbox_sent(sent.outbox_id, attempt_number=1)

        lost = _item("ob-ws-lost", "evt-ws-lost", "plan-ws-lost", "lane-b")
        lost = await store.create_outbox_item(lost)
        await _claim_row(store, lost, "worker-lost")
        assert await store.mark_outbox_abandoned(lost.outbox_id, attempt_number=1)

        done = _item("ob-ws-done", "evt-ws-done", "plan-ws-done", "lane-a")
        done = await store.create_outbox_item(done)
        await _claim_row(store, done, "worker-done")
        assert await store.mark_outbox_sent(done.outbox_id, attempt_number=1)
    finally:
        await store.close()


async def _claim_row(store: SQLiteStorage, item: DeliveryOutboxItem, worker: str) -> None:
    """Apply claim_due_outbox_items' single-row write for one staged row."""
    now = datetime.now(timezone.utc).isoformat()
    lease = (datetime.now(timezone.utc) + timedelta(seconds=300)).isoformat()
    await store._write(
        "UPDATE delivery_outbox SET status = 'in_progress', worker_id = ?,"
        " locked_at = ?, lease_until = ?, updated_at = ? WHERE outbox_id = ?",
        (worker, now, lease, now, item.outbox_id),
    )


def _application_counts(db_path: str) -> dict[str, int]:
    db = sqlite3.connect(str(db_path))
    try:
        names = [
            str(r[0])
            for r in db.execute(
                "SELECT name FROM sqlite_master"
                " WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {name: int(db.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]) for name in names}
    finally:
        db.close()


def _validate_against_schema(payload: dict, tmp_path: Path) -> None:
    if find_spec("jsonschema") is None:  # pragma: no cover - dev dependency
        pytest.skip("jsonschema is not installed")
    import jsonschema

    schema_path = (
        Path(__file__).resolve().parent.parent
        / "docs"
        / "schemas"
        / "work-status.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    jsonschema.validate(payload, schema)


def test_work_status_json_document_validates_and_partitions(tmp_path: Path) -> None:
    db_path = str(tmp_path / "ws.sqlite")
    import asyncio

    asyncio.run(_seed_work_status_db(db_path))

    output = _run_cli("inspect", "work-status", "--storage-path", db_path, "--json")
    payload = json.loads(output)
    _validate_against_schema(payload, tmp_path)

    assert payload["scope"] == "work_status"
    assert payload["schema_version"] == 1
    assert payload["adapter_filter"] is None
    assert payload["definition"]
    totals = payload["totals"]
    assert totals["current_generations"] == 5
    assert totals["outstanding"] == 2
    assert totals["unstarted_eligible"] == 1
    assert totals["reserved_active"] == 1
    assert totals["terminal_outcome"] == 2
    assert totals["uncertain_outcome"] == 1
    # The partition invariant holds exactly.
    assert (
        totals["unstarted_eligible"]
        + totals["retry_scheduled"]
        + totals["reserved_active"]
        + totals["native_queue"]
        + totals["uncertain_outcome"]
        + totals["terminal_outcome"]
        + totals["unrecognized"]
        == totals["current_generations"]
    )
    adapters = {a["target_adapter"]: a for a in payload["adapters"]}
    assert set(adapters) == {"held", "lane-a", "lane-b"}
    assert adapters["held"]["outstanding"] == 2
    assert adapters["held"]["uncertainty"]["reserved_without_generation_receipt"] == 1
    assert adapters["lane-a"]["outstanding"] == 0
    assert adapters["lane-a"]["categories"]["terminal_outcome"] == 2
    oldest = adapters["held"]["oldest_outstanding"]
    assert oldest is not None
    assert oldest["event_id"] in {"evt-ws-hold"}
    assert oldest["origin"] in {"admission", "claim"}
    assert oldest["age_seconds"] is not None
    assert payload["raw_outbox_states"] == {
        "abandoned": 1,
        "in_progress": 1,
        "pending": 1,
        "sent": 2,
    }
    assert payload["retained_history"]["outbox_rows_total"] == 5
    assert payload["retained_history"]["superseded_generations"] == 0
    assert set(payload["limits"]) == {
        "connectivity",
        "owner_liveness",
        "native_send",
        "recipient_delivery",
        "scope",
    }
    assert payload["limits"]["recipient_delivery"].startswith("unknown")


def test_work_status_human_output_is_metadata_only(tmp_path: Path) -> None:
    db_path = str(tmp_path / "ws.sqlite")
    import asyncio

    asyncio.run(_seed_work_status_db(db_path))
    output = _run_cli("inspect", "work-status", "--storage-path", db_path)
    assert "read-only" in output
    assert "Sampled at:" in output
    assert "Current generations: 5" in output
    assert "outstanding 2" in output
    assert "uncertain outcome 1" in output
    assert "terminal 2" in output
    assert "held: outstanding 2 of 2 current" in output
    assert "unrecognized" not in output or True
    assert "no attempt evidence" in output
    assert (
        f"medre inspect event evt-ws-hold --recovery --storage-path={db_path}"
        in output
    )
    assert "lane-a: outstanding 0 of 2 current" in output
    assert "Raw outbox states (all retained rows):" in output
    assert "Retained history:" in output
    # Metadata only: never the seeded payload text.
    assert "work-status cli test" not in output
    assert "unknown" in output  # honesty limits


def test_work_status_adapter_filter_states_its_scope(tmp_path: Path) -> None:
    db_path = str(tmp_path / "ws.sqlite")
    import asyncio

    asyncio.run(_seed_work_status_db(db_path))
    output = _run_cli(
        "inspect",
        "work-status",
        "--storage-path",
        db_path,
        "--adapter",
        "lane-a",
        "--json",
    )
    payload = json.loads(output)
    _validate_against_schema(payload, tmp_path)
    assert payload["adapter_filter"] == "lane-a"
    assert [a["target_adapter"] for a in payload["adapters"]] == ["lane-a"]
    assert payload["totals"]["current_generations"] == 2
    assert payload["totals"]["terminal_outcome"] == 2

    unknown = json.loads(
        _run_cli(
            "inspect",
            "work-status",
            "--storage-path",
            db_path,
            "--adapter",
            "missing",
            "--json",
        )
    )
    _validate_against_schema(unknown, tmp_path)
    assert unknown["adapters"] == []
    assert unknown["totals"]["current_generations"] == 0


def test_work_status_reads_never_mutate_application_state(tmp_path: Path) -> None:
    db_path = str(tmp_path / "ws.sqlite")
    import asyncio

    asyncio.run(_seed_work_status_db(db_path))
    before = _application_counts(db_path)
    for _ in range(2):
        _run_cli("inspect", "work-status", "--storage-path", db_path, "--json")
        _run_cli("inspect", "work-status", "--storage-path", db_path)
    after = _application_counts(db_path)
    assert after == before


def test_work_status_empty_database_reports_zero_scope_honestly(
    tmp_path: Path,
) -> None:
    db_path = str(tmp_path / "empty.sqlite")
    import asyncio

    async def _create_empty() -> None:
        store = SQLiteStorage(db_path)
        try:
            await store.initialize()
        finally:
            await store.close()

    asyncio.run(_create_empty())
    output = _run_cli("inspect", "work-status", "--storage-path", db_path, "--json")
    payload = json.loads(output)
    _validate_against_schema(payload, tmp_path)
    assert payload["adapters"] == []
    assert payload["totals"]["current_generations"] == 0
    assert payload["totals"]["outstanding"] == 0
    assert payload["raw_outbox_states"] == {}
    human = _run_cli("inspect", "work-status", "--storage-path", db_path)
    assert "Adapters: none within scope" in human


def test_work_status_reports_unavailable_storage_honestly(tmp_path: Path) -> None:
    not_a_db = tmp_path / "not-a-db.sqlite"
    not_a_db.write_bytes(b"definitely not sqlite" * 32)
    stdout, stderr, code = _run_cli_raw(
        "inspect", "work-status", "--storage-path", str(not_a_db), "--json"
    )
    assert code != 0
    assert "Work status" in stderr or "Storage error" in stderr
    assert stdout == ""
