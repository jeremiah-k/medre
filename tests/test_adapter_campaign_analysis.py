"""Offline summaries reject incomplete or inconsistent retained evidence."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.analyze_adapter_campaign import main, summarize
from tests.helpers.campaign_evidence import report

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_complete_evidence_summarizes_without_private_content():
    result = summarize(report())
    assert result["assessment"] == "passed"
    assert result["independent_receipts"] == 2
    assert result["durable_source_cases"] == 2
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize(
    "change",
    [
        "status",
        "direction",
        "source",
        "native",
        "nonce",
        "attempt",
        "cleanup",
        "empty_marker",
        "duplicate_marker",
        "overlapping_marker",
        "wrong_sender",
        "missing_sender",
        "wrong_group",
        "wrong_source_channel",
        "wrong_destination",
        "wrong_receipt_channel",
        "claimed_full",
        "wrong_transport",
    ],
)
def test_inconsistent_evidence_cannot_pass(change):
    data = report()
    if change == "status":
        data["status"] = "running"
    elif change == "direction":
        data["completed_pairs"].pop()
    elif change == "source":
        data["cases"].pop()
    elif change == "native":
        data["cases"][0]["native_receipts"].clear()
    elif change == "nonce":
        data["cases"][0]["core"]["event"]["payload"]["body"] = "unrelated"
    elif change == "attempt":
        data["cases"][0]["core"]["receipts"][0]["attempt_number"] = 2
    elif change == "cleanup":
        data["cleanup_errors"] = ["left"]
    elif change == "empty_marker":
        data["cases"][0]["marker"] = ""
    elif change == "duplicate_marker":
        data["cases"][0]["marker"] = data["cases"][1]["marker"]
    elif change == "overlapping_marker":
        data["cases"][0]["marker"] = data["cases"][1]["marker"][:6]
    elif change == "wrong_sender":
        data["cases"][0]["native_receipts"]["right"]["sender"] = "imposter"
    elif change == "missing_sender":
        del data["cases"][0]["native_receipts"]["right"]["sender"]
    elif change == "wrong_group":
        data["cases"][0]["native_receipts"]["right"]["channel"] = "group:wrong"
    elif change == "wrong_source_channel":
        data["cases"][0]["core"]["event"]["source_channel_id"] = "group:wrong"
    elif change == "wrong_destination":
        for row in (
            data["cases"][0]["core"]["outbox"] + data["cases"][0]["core"]["receipts"]
        ):
            row["target_channel"] = "group:wrong"
    elif change == "wrong_receipt_channel":
        data["native_expectations"]["right"]["receipt_channel"] = "group:wrong"
    elif change == "claimed_full":
        data["coverage"] = "full"
    elif change == "wrong_transport":
        data["native_expectations"]["right"]["transport"] = "matrix"
    assert summarize(data)["assessment"] == "failed"


def test_one_direction_cannot_masquerade_as_a_complete_topology():
    data = report()
    data["expected_pairs"].pop()
    data["completed_pairs"].pop()
    data["cases"].pop()
    assert summarize(data)["assessment"] == "failed"


def test_malformed_private_report_does_not_echo_parser_input(tmp_path, capsys):
    (tmp_path / "results.json").write_text('{"password": "private-secret"')
    assert main([str(tmp_path)]) == 1
    assert "private-secret" not in capsys.readouterr().out


def test_unsafe_endpoint_names_are_not_printed(tmp_path, capsys):
    data = report()
    data["expected_pairs"] = [["password=private-secret", "right"]]
    (tmp_path / "results.json").write_text(json.dumps(data))
    assert main([str(tmp_path)]) == 1
    assert "private-secret" not in capsys.readouterr().out


def test_optimized_python_cannot_disable_evidence_checks(tmp_path):
    (tmp_path / "results.json").write_text(json.dumps(report()))
    result = subprocess.run(
        [sys.executable, "-O", "-m", "scripts.analyze_adapter_campaign", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=REPO_ROOT,
        check=False,
    )
    assert result.returncode == 1
    assert "requires enabled assertions" in result.stdout
    assert '"assessment": "passed"' not in result.stdout


@pytest.mark.parametrize(
    "change",
    [
        "null_cleanup",
        "list_receipt_text",
        "list_body",
        "repeated_native_nonce",
        "repeated_core_nonce",
    ],
)
def test_malformed_evidence_cannot_look_like_successful_text(change):
    data = report()
    case = data["cases"][0]
    if change == "null_cleanup":
        data["cleanup_errors"] = None
    elif change == "list_receipt_text":
        case["native_receipts"]["right"]["text"] = [case["marker"]]
    elif change == "list_body":
        case["core"]["event"]["payload"]["body"] = [case["marker"]]
    elif change == "repeated_native_nonce":
        case["native_receipts"]["right"]["text"] = case["marker"] * 2
    else:
        case["core"]["event"]["payload"]["body"] = case["marker"] * 2
    assert summarize(data)["assessment"] == "failed"
