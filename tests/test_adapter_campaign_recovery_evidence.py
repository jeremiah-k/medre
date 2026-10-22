"""Recovery oracles use configured identities and committed lifecycle authority."""

from __future__ import annotations

import json
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from scripts.adapter_campaign import recovery
from scripts.adapter_campaign.recovery_evidence import (
    native_deliveries,
    verify_identity,
    verify_native_history,
    verify_preserved,
    verify_terminal,
)
from scripts.analyze_recovery_campaign import analyze, main


@pytest.mark.parametrize("late", ["delivery", "canary", None])
def test_final_native_history_rejects_late_delivery_and_canary_duplicates(late):
    marker = "nonce-retry_exhaustion "
    canary = {"kind": "received", "text": "nonce-retry_exhaustion-canary café ✓"}
    cases = [
        {
            "scenario": "retry_exhaustion",
            "marker": marker,
            "native_receipts": [],
            "restored_transport_canary": [canary],
        }
    ]
    records = [canary]
    if late == "delivery":
        records.append({"kind": "received", "text": marker + "café ✓"})
    elif late == "canary":
        records.append(dict(canary))
    if late:
        with pytest.raises(AssertionError, match="changed after observation"):
            verify_native_history(cases, records)
    else:
        verify_native_history(cases, records)


async def test_storage_evidence_uses_the_same_representation_as_json_reports(
    monkeypatch,
):
    item = {
        "event": {
            "relations": (),
            "lineage": ("parent",),
            "payload": {"body": "café ✓"},
        },
        "outbox": [{"attempt_number": 2}],
    }
    original = deepcopy(item)
    monkeypatch.setattr(recovery, "snapshots", AsyncMock(return_value=[item]))

    result = await recovery.read_event(None, "nonce")

    assert result == json.loads(json.dumps(item))
    assert result["event"]["lineage"] == ["parent"]
    assert item == original


@pytest.mark.parametrize("items", [[], [{}, {}]])
async def test_storage_evidence_rejects_duplicate_ingress_and_preserves_absence(
    monkeypatch, items
):
    monkeypatch.setattr(recovery, "snapshots", AsyncMock(return_value=items))
    if items:
        with pytest.raises(AssertionError, match="duplicated canonical ingress"):
            await recovery.read_event(None, "nonce")
    else:
        assert await recovery.read_event(None, "nonce") is None


def evidence():
    fields = {
        "event_id": "event",
        "delivery_plan_id": "plan",
        "outbox_id": "outbox",
        "route_id": "recovery_route",
        "target_adapter": "xmpp",
        "target_channel": "room@rooms.example.test",
        "attempt_number": 2,
        "status": "sent",
        "receipt_id": "receipt",
        "replay_run_id": None,
    }
    return {
        "event": {
            "event_id": "event",
            "source_adapter": "matrix",
            "source_channel_id": "!room:example.test",
            "source_native_ref": {
                "adapter": "matrix",
                "native_channel_id": "!room:example.test",
                "native_message_id": "$native",
            },
            "payload": {"body": "nonce café ✓"},
        },
        "receipts": [{**fields, "source": "retry", "adapter_message_id": "stanza"}],
        "outbox": [
            {
                **fields,
                "dispatch_source": "retry",
                "active_attempt": None,
                "worker_id": None,
            }
        ],
        "refs": [
            {
                "direction": "outbound",
                "event_id": "event",
                "adapter": "xmpp",
                "native_channel_id": "room@rooms.example.test",
                "native_message_id": "stanza",
            }
        ],
    }


def identity(item):
    return verify_identity(
        item,
        source="matrix",
        channel="!room:example.test",
        native_id="$native",
        target="xmpp",
        target_channel="room@rooms.example.test",
    )


def test_terminal_recovery_requires_committed_receipt_and_native_correlation():
    item = evidence()
    identity(item)
    assert verify_terminal(item, status="sent")["receipt_id"] == "receipt"


@pytest.mark.parametrize(
    "change",
    [
        "source_channel",
        "source_ref",
        "native_id",
        "target_channel",
        "target_adapter",
        "fork",
        "receipt_pointer",
        "receipt_identity",
        "retry_budget",
        "active_claim",
        "replay",
        "missing_native_ref",
    ],
)
def test_consistently_wrong_rows_cannot_satisfy_the_independent_contract(change):
    item = evidence()
    row = item["outbox"][0]
    receipt = item["receipts"][0]
    if change == "source_channel":
        item["event"]["source_channel_id"] = "!wrong:example.test"
        item["event"]["source_native_ref"]["native_channel_id"] = "!wrong:example.test"
    elif change == "source_ref":
        item["event"]["source_native_ref"]["adapter"] = "other"
    elif change == "native_id":
        item["event"]["source_native_ref"]["native_message_id"] = "$wrong"
    elif change in ("target_channel", "target_adapter"):
        row[change] = receipt[change] = "wrong"
    elif change == "fork":
        item["outbox"].append(deepcopy(row))
    elif change == "receipt_pointer":
        row["receipt_id"] = "uncommitted"
    elif change == "receipt_identity":
        receipt["delivery_plan_id"] = "another-plan"
    elif change == "retry_budget":
        row["attempt_number"] = receipt["attempt_number"] = 4
    elif change == "active_claim":
        row["active_attempt"] = 3
    elif change == "replay":
        row["replay_run_id"] = receipt["replay_run_id"] = "unexpected-replay"
    elif change == "missing_native_ref":
        item["refs"] = []
    with pytest.raises(AssertionError):
        identity(item)
        verify_terminal(item, status="sent")


def test_late_uncommitted_receipt_does_not_override_current_outbox_authority():
    item = evidence()
    late = {
        **item["receipts"][0],
        "receipt_id": "uncommitted",
        "status": "failed",
        "attempt_number": 3,
    }
    item["receipts"].append(late)
    assert verify_terminal(item, status="sent")["receipt_id"] == "receipt"


def test_recovery_preserves_canonical_event_work_identity_and_receipt_history():
    before = evidence()
    after = deepcopy(before)
    after["receipts"].append({**after["receipts"][0], "receipt_id": "later"})
    verify_preserved(before, after)
    after["receipts"][0]["status"] = "failed"
    with pytest.raises(AssertionError, match="history"):
        verify_preserved(before, after)


def endpoint():
    return {
        "transport": "xmpp",
        "channel": "room@rooms.example.test",
        "expected_sender": "room@rooms.example.test/bot",
        "peer": {},
        "expected_prefixes": {"matrix": "[Matrix] Peer: "},
    }


def received():
    return {
        "kind": "received",
        "text": "[Matrix] Peer: nonce café ✓",
        "sender": "room@rooms.example.test/bot",
        "channel": "room@rooms.example.test",
    }


@pytest.mark.parametrize(
    "change", ["sender", "channel", "text", "duplicate", "empty_nonce"]
)
def test_native_recovery_checks_do_not_hide_wrong_receipts_by_filtering(change):
    record = received()
    rows = [record]
    marker = "nonce "
    if change in ("sender", "channel", "text"):
        record[change] += "wrong"
    elif change == "duplicate":
        rows.append(dict(record))
    else:
        marker = ""
    with pytest.raises(AssertionError):
        native_deliveries(
            rows,
            endpoint(),
            marker=marker,
            source="matrix",
            text="nonce café ✓",
            minimum=1,
            maximum=1,
        )


def test_ambiguous_handoff_reports_bounded_duplicates_without_inventing_a_guarantee():
    rows = [received(), received()]
    assert (
        len(
            native_deliveries(
                rows,
                endpoint(),
                marker="nonce ",
                source="matrix",
                text="nonce café ✓",
                minimum=1,
                maximum=2,
            )
        )
        == 2
    )


async def test_offline_recovery_analyzer_rejects_a_passing_label_without_coverage(
    tmp_path,
):
    (tmp_path / "results.json").write_text(
        json.dumps(
            {
                "version": 1,
                "status": "passed",
                "cleanup_errors": [],
                "run_prefix": "MR-recovery-0123456789ab-",
                "expected_scenarios": [],
                "cases": [],
            }
        )
    )
    with pytest.raises(AssertionError, match="coverage"):
        await analyze(tmp_path)


def test_analyzer_failure_does_not_print_private_input(tmp_path, capsys):
    (tmp_path / "results.json").write_text('{"private-token":invalid')
    assert main([str(tmp_path)]) == 1
    output = capsys.readouterr().out
    assert json.loads(output)["status"] == "failed"
    assert "private-token" not in output


@pytest.mark.parametrize("timeout", [True, 0, -1, float("nan"), float("inf"), 601])
async def test_invalid_recovery_deadline_does_not_create_output(tmp_path, timeout):
    from scripts.adapter_campaign.recovery import run

    output = tmp_path / "recovery"
    with pytest.raises(ValueError, match="timeout"):
        await run(None, output, timeout=timeout)
    assert not output.exists()
