"""Read durable core evidence without treating handoff as native receipt."""

from __future__ import annotations

import asyncio

import msgspec

_EVENT_WINDOW = 1000


async def snapshots(storage, prefix: str) -> list[dict]:
    """Read privileged campaign evidence without writing to the live database.

    Campaigns own their temporary fixture database and explicitly inspect its
    complete native/canonical evidence. This is *not* a production content-read
    API: use a strict read-only SQLite connection, never the replay borrow that
    observes expiry through ``BEGIN IMMEDIATE`` and updates owner state.
    """
    from medre.core.storage.sqlite.serde import _row_to_event, _row_to_relation

    # Bound enumeration and prefix selection in the same read. A separate
    # count followed by an unbounded match query races concurrent admission.
    # Explicit non-string body values also suppress a secondary text alias.
    candidates = await storage._read_all(
        "SELECT e.event_id, EXISTS (SELECT 1 FROM event_body_content b "
        "WHERE b.event_id=e.event_id AND instr(b.body, ?) > 0 "
        "AND (b.field='body' OR (b.field='text' "
        "AND json_type(e.payload, '$.body') IS NULL AND NOT EXISTS ("
        "SELECT 1 FROM event_body_content primary_body "
        "WHERE primary_body.event_id=e.event_id AND primary_body.field='body')))) "
        "AS prefix_matches FROM canonical_events e ORDER BY e.event_id LIMIT ?",
        (prefix, _EVENT_WINDOW),
    )
    if len(candidates) == _EVENT_WINDOW:
        raise AssertionError("Campaign evidence exceeds the bounded event window")
    matches = [row for row in candidates if row["prefix_matches"]]
    results = []
    for match in matches:
        event_id = match["event_id"]
        row = await storage._read_one(
            "SELECT * FROM canonical_events WHERE event_id=?", (event_id,)
        )
        if row is None:
            raise AssertionError("Campaign event disappeared during inspection")
        relations = await storage._read_all(
            "SELECT r.*, x.excerpt AS resolved_fallback_text "
            "FROM event_relations r LEFT JOIN event_relation_excerpts x "
            "ON x.relation_id=r.id WHERE r.event_id=? ORDER BY r.id ASC",
            (event_id,),
        )
        bodies = await storage._read_all(
            "SELECT field, body FROM event_body_content WHERE event_id=?",
            (event_id,),
        )
        event = _row_to_event(row, [_row_to_relation(rel) for rel in relations])
        payload = {**event.payload, **{body["field"]: body["body"] for body in bodies}}
        event = msgspec.structs.replace(event, payload=payload)
        result = {"event": msgspec.to_builtins(event)}
        for name, method in (
            ("receipts", storage.list_receipts_for_event),
            ("outbox", storage.list_outbox_items_for_event),
            ("refs", storage.list_native_refs_for_event),
            ("observations", storage.list_delivery_observations_for_event),
        ):
            result[name] = msgspec.to_builtins(await method(event_id))
        results.append(result)
    return results


def verify(item: dict, *, source: str, channel: str, targets: dict[str, str]) -> None:
    event = item["event"]
    assert event["source_adapter"] == source, "Unexpected canonical source"
    assert event["source_channel_id"] == channel, "Unexpected canonical channel"
    receipts = [row for row in item["receipts"] if row["status"] == "sent"]
    outbox = item["outbox"]
    assert all(
        row["status"] in {"queued", "sent"}
        and row["target_adapter"] in targets
        and row["source"] == "live"
        and row["replay_run_id"] is None
        for row in item["receipts"]
    ), "Unexpected receipt lifecycle or provenance"
    assert len(receipts) == len(targets) and {
        r["target_adapter"] for r in receipts
    } == set(targets), "Unexpected receipt cardinality or destinations"
    assert len(outbox) == len(targets) and {r["target_adapter"] for r in outbox} == set(
        targets
    ), "Unexpected outbox cardinality or destinations"
    for receipt in receipts:
        assert receipt["status"] == "sent", "Delivery did not complete"
        assert (
            receipt["source"] == "live" and receipt["replay_run_id"] is None
        ), "Campaign requires live delivery"
        assert (
            receipt["target_channel"] == targets[receipt["target_adapter"]]
        ), "Incorrect destination conversation"
        rows = [row for row in outbox if row["outbox_id"] == receipt["outbox_id"]]
        assert (
            len(rows) == 1 and rows[0]["status"] == "sent"
        ), "Receipt lacks terminal outbox evidence"
        assert (
            rows[0]["attempt_number"] == receipt["attempt_number"] == 1
        ), "Campaign delivery retried or lost provenance"
        row = rows[0]
        for field in (
            "event_id",
            "delivery_plan_id",
            "target_adapter",
            "target_channel",
            "route_id",
        ):
            assert row[field] == receipt[field], "Receipt and outbox provenance differ"
        assert row["event_id"] == event["event_id"], "Outbox belongs to another event"
        assert (
            row["route_id"] == f"{source}_to_{row['target_adapter']}"
        ), "Delivery used an undeclared route"
        assert (
            row["dispatch_source"] == "live" and row["replay_run_id"] is None
        ), "Outbox is not live work"
        if receipt["adapter_message_id"]:
            assert any(
                ref["direction"] == "outbound"
                and ref["adapter"] == row["target_adapter"]
                and ref["native_message_id"] == receipt["adapter_message_id"]
                for ref in item["refs"]
            ), "Native handoff correlation was not persisted"


async def wait_evidence(
    storage,
    marker: str,
    *,
    source: str,
    channel: str,
    targets: dict[str, str],
    timeout: float,
) -> dict:
    async with asyncio.timeout(timeout):
        while True:
            items = await snapshots(storage, marker)
            if len(items) > 1:
                raise AssertionError(
                    "Campaign nonce produced multiple canonical events"
                )
            if items:
                item = items[0]
                if (
                    sum(row["status"] == "sent" for row in item["receipts"])
                    >= len(targets)
                    and len(item["outbox"]) >= len(targets)
                    and all(row["status"] == "sent" for row in item["outbox"])
                ):
                    verify(item, source=source, channel=channel, targets=targets)
                    return item
            await asyncio.sleep(0.1)
