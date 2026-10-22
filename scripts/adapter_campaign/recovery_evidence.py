"""Independent native observations and durable authority for recovery campaigns."""

from __future__ import annotations

from .run import native_expectation


def verify_native_history(cases: list[dict], records: list[dict]) -> None:
    """Reject late receipts after a case's bounded observation has completed."""
    for case in cases:
        hits = [
            row
            for row in records
            if row["kind"] == "received" and case["marker"] in row["text"]
        ]
        assert (
            hits == case["native_receipts"]
        ), "Native receipts changed after observation"
        if case["scenario"] == "retry_exhaustion":
            marker = case["marker"].removesuffix(" ") + "-canary "
            canaries = [
                row
                for row in records
                if row["kind"] == "received" and marker in row["text"]
            ]
            assert (
                canaries == case["restored_transport_canary"]
            ), "Native canary receipts changed after observation"


def native_deliveries(
    records: list[dict],
    endpoint: dict,
    *,
    marker: str,
    source: str,
    text: str,
    minimum: int,
    maximum: int,
) -> list[dict]:
    """Return received records containing marker after validating native delivery.

    text must begin with the nonempty marker. Check the inclusive minimum and
    maximum counts, configured sender and conversation, and exact source prefix
    plus text. Contract violations raise AssertionError.
    """
    assert (
        isinstance(marker, str) and marker and text.startswith(marker)
    ), "Invalid recovery nonce"
    expected = native_expectation(endpoint)
    hits = [r for r in records if r["kind"] == "received" and marker in r["text"]]
    if not minimum <= len(hits) <= maximum:
        raise AssertionError("Native delivery count violates the recovery contract")
    prefix = endpoint["expected_prefixes"][source]
    for hit in hits:
        assert hit["sender"] == expected["receipt_sender"], "Native sender differs"
        assert (
            hit["channel"] == expected["receipt_channel"]
        ), "Native conversation differs"
        assert hit["text"] == prefix + text, "Native presentation differs"
    return hits


def verify_identity(
    item: dict,
    *,
    source: str,
    channel: str,
    native_id: str,
    target: str,
    target_channel: str,
) -> dict:
    """Return the sole outbox row after checking canonical and native identity.

    channel and native_id identify the source conversation and acknowledged
    message; target_channel identifies the destination. Require recovery_route
    work without replay provenance. Mismatches raise AssertionError.
    """
    event = item["event"]
    assert event["source_adapter"] == source, "Unexpected canonical source"
    assert event["source_channel_id"] == channel, "Unexpected canonical conversation"
    assert (
        event["source_native_ref"]["native_message_id"] == native_id
    ), "Source correlation differs"
    assert (
        event["source_native_ref"]["adapter"] == source
    ), "Native source adapter differs"
    assert (
        event["source_native_ref"]["native_channel_id"] == channel
    ), "Native source conversation differs"
    assert len(item["outbox"]) == 1, "Recovery must retain one delivery generation"
    row = item["outbox"][0]
    assert row["event_id"] == event["event_id"], "Outbox event differs"
    assert row["target_adapter"] == target, "Outbox destination differs"
    assert row["target_channel"] == target_channel, "Outbox conversation differs"
    assert row["route_id"] == "recovery_route", "Unexpected recovery route"
    assert row["replay_run_id"] is None, "Recovery must not become explicit replay"
    return row


def verify_terminal(item: dict, *, status: str, max_attempts: int = 3) -> dict:
    """Return the receipt committed by the sole outbox row in the requested status.

    Check matching delivery provenance, released claims, and attempt numbers
    from 1 through max_attempts inclusive. For sent receipts with a native
    message ID, require its outbound correlation. Mismatches raise AssertionError;
    uncommitted receipts do not replace the outbox's receipt authority.
    """
    assert len(item["outbox"]) == 1, "Recovery must not fork outbox work"
    row = item["outbox"][0]
    assert row["status"] == status, "Outbox has not reached the expected terminal state"
    matches = [r for r in item["receipts"] if r["receipt_id"] == row["receipt_id"]]
    assert len(matches) == 1, "Terminal authority lacks its committed receipt"
    receipt = matches[0]
    assert receipt["status"] == status, "Committed receipt and outbox differ"
    for field in (
        "event_id",
        "delivery_plan_id",
        "target_adapter",
        "target_channel",
        "route_id",
        "attempt_number",
        "outbox_id",
    ):
        assert row[field] == receipt[field], "Committed delivery provenance differs"
    assert 1 <= row["attempt_number"] <= max_attempts, "Retry budget exceeded"
    assert receipt["replay_run_id"] is None, "Retry was misclassified as replay"
    assert (
        receipt["source"]
        == row["dispatch_source"]
        == ("live" if row["attempt_number"] == 1 else "retry")
    ), "Dispatch provenance differs"
    assert (
        row["active_attempt"] is None and row["worker_id"] is None
    ), "Terminal work retains an active claim"
    assert all(
        1 <= r["attempt_number"] <= max_attempts for r in item["receipts"]
    ), "Receipt invents an attempt outside the budget"
    if status == "sent" and receipt["adapter_message_id"]:
        refs = [
            r
            for r in item["refs"]
            if r["direction"] == "outbound"
            and r["adapter"] == row["target_adapter"]
            and r["native_channel_id"] == row["target_channel"]
            and r["native_message_id"] == receipt["adapter_message_id"]
        ]
        assert (
            len(refs) == 1 and refs[0]["event_id"] == row["event_id"]
        ), "Sent receipt lacks native correlation"
    return receipt


def verify_preserved(before: dict, after: dict) -> None:
    """Require an unchanged event and outbox ID with append-only receipt history.

    Raise AssertionError if the snapshots violate these recovery invariants.
    """
    assert before["event"] == after["event"], "Recovery rewrote the canonical event"
    assert len(before["outbox"]) == len(after["outbox"]) == 1
    assert (
        before["outbox"][0]["outbox_id"] == after["outbox"][0]["outbox_id"]
    ), "Recovery replaced durable work"
    assert (
        after["receipts"][: len(before["receipts"])] == before["receipts"]
    ), "Recovery rewrote receipt history"
