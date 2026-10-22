"""Native reply oracles independent of outbox addressing and renderer output."""

from __future__ import annotations

from .recovery_evidence import verify_terminal
from .run import _VisibleText


def message_body(record: dict, transport: str) -> str:
    text = record["text"]
    details = record.get("native_details", {})
    if transport == "xmpp" and details.get("reply_to"):
        start, end = details.get("fallback_start"), details.get("fallback_end")
        if start is not None or end is not None:
            assert (
                start == "0"
                and isinstance(end, str)
                and end.isascii()
                and end.isdecimal()
            ), "Malformed native quote range"
            assert len(end) <= 10 and 0 < int(end) < len(
                text
            ), "Invalid native quote extent"
            text = text[int(end) :]
    return text


def native_hits(records: list[dict], marker: str, transport: str) -> list[dict]:
    assert marker and isinstance(marker, str), "Missing fidelity nonce"
    return [
        row
        for row in records
        if row["kind"] == "received" and marker in message_body(row, transport)
    ]


def native_id(record: dict, transport: str) -> str:
    value = (
        record.get("native_details", {}).get("room_stanza_id")
        if transport == "xmpp"
        else record.get("native_id")
    )
    assert (
        isinstance(value, str) and value.strip()
    ), "Native witness lacks a scoped message ID"
    return value


def verify_case(
    case: dict,
    item: dict,
    incoming: list[dict],
    outgoing: list[dict],
    topology: dict,
    parents: dict,
) -> dict:
    source, target = case["source"], case["target"]
    src, dst = topology[source], topology[target]
    source_hits = native_hits(incoming, case["marker"], src["transport"])
    target_hits = native_hits(outgoing, case["marker"], dst["transport"])
    assert (
        len(source_hits) == len(target_hits) == 1
    ), "Fidelity message duplicated or missing"
    sent, delivered = source_hits[0], target_hits[0]
    acknowledged_id = (
        sent["native_details"].get("origin_id")
        if src["transport"] == "xmpp"
        else sent["native_id"]
    )
    assert (
        acknowledged_id and acknowledged_id == case["source_ack"]
    ), "Source acknowledgement differs from native witness"
    assert (
        sent["sender"] == src["peer_identity"] and sent["channel"] == src["channel"]
    ), "Native source identity differs"
    assert (
        delivered["sender"] == dst["bot_identity"]
        and delivered["channel"] == dst["channel"]
    ), "Native destination identity differs"
    assert message_body(sent, src["transport"]) == case["text"], "Source body differs"
    body = message_body(delivered, dst["transport"])
    if dst["transport"] == "matrix":
        formatted = delivered.get("formatted_body")
        assert (
            isinstance(formatted, str) and formatted
        ), "Missing Matrix HTML presentation"
        parser = _VisibleText()
        parser.feed(formatted)
        parser.close()
        assert (
            "".join(parser.parts) == delivered["text"]
        ), "Matrix HTML and plain text differ"
    expected = dst["prefixes"][source] + case["text"]
    if case["mode"] == "missing":
        assert (
            body.startswith(dst["prefixes"][source])
            and body.count(case["text"]) == 1
            and "[reply" in body.replace(case["text"], "").lower()
        ), "Missing target lost readable reply context"
    else:
        assert body == expected, "Native presentation differs"
    event = item["event"]
    assert (
        event["event_kind"] in {"message.created", "message.text"}
        and event["schema_version"] == 1
    ), "Unexpected canonical message kind or schema"
    assert event["payload"]["body"] == case["text"], "Quote leaked into canonical body"
    assert (
        event["source_adapter"] == source
        and event["source_channel_id"] == src["channel"]
    ), "Canonical source differs"
    assert (
        event["source_transport_id"] == src["peer_identity"]
    ), "Canonical sender differs"
    ref = event["source_native_ref"]
    assert (
        ref["adapter"] == source
        and ref["native_channel_id"] == src["channel"]
        and ref["native_message_id"] == native_id(sent, src["transport"])
    ), "Canonical source correlation differs"
    assert len(item["outbox"]) == 1, "Unexpected fanout or forked delivery"
    row = item["outbox"][0]
    assert (
        row["event_id"] == event["event_id"]
        and row["target_adapter"] == target
        and row["target_channel"] == dst["channel"]
    ), "Outbox destination differs"
    assert (
        row["route_id"] == f"{source}_to_{target}" and row["replay_run_id"] is None
    ), "Unexpected route or replay"
    receipt = verify_terminal(item, status="sent", max_attempts=1)
    assert receipt["adapter_message_id"] == native_id(
        delivered, dst["transport"]
    ), "Native handoff correlation differs"
    parent = parents.get(case["parent"])
    if parent is not None:
        expected_source_target = (
            parent["source_id"] if source == parent["source"] else parent["target_id"]
        )
        assert (
            case["native_target"] == expected_source_target
        ), "Source replied to a different native parent"
    expected_reply = case["native_target"] if case["parent"] else None
    assert (
        sent["native_details"]["reply_to"] == expected_reply
    ), "Source did not send the declared native relation"
    if parent is not None:
        assert (
            len(event["relations"]) == 1
            and event["relations"][0]["relation_type"] == "reply"
        ), "Canonical reply relation missing"
        assert (
            event["relations"][0]["target_event_id"]
            == parent["core"]["event"]["event_id"]
        ), "Canonical parent correlation differs"
        expected_target = (
            parent["source_id"] if target == parent["source"] else parent["target_id"]
        )
        assert (
            delivered["native_details"]["reply_to"] == expected_target
        ), "Native reply points to the wrong parent"
    elif case["mode"] == "missing":
        assert (
            len(event["relations"]) == 1
            and event["relations"][0]["target_event_id"] is None
        )
        assert (
            delivered["native_details"]["reply_to"] is None
        ), "Unresolved target was sent in the wrong namespace"
    else:
        assert (
            not event["relations"] and delivered["native_details"]["reply_to"] is None
        )
    return {
        "source_id": native_id(sent, src["transport"]),
        "target_id": native_id(delivered, dst["transport"]),
    }
