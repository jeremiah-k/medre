"""Check retained campaign evidence without connecting to any native service."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

from medre.config.identifiers import adapter_id_problem
from scripts.adapter_campaign.evidence import verify
from scripts.adapter_campaign.baseline import (
    SUITE,
    text as baseline_text,
    turns,
    verify_bridge_context,
)
from scripts.adapter_campaign.plan import TRANSPORTS, Plan, load_plan
from scripts.adapter_campaign.run import native_expectation, native_receipt


def summarize(report: dict, *, trusted_plan: Plan | None = None) -> dict:
    """Audit recorded coverage and provenance; never print native message content."""
    if not __debug__:
        raise ValueError("Analysis requires enabled assertions")
    if any(
        not isinstance(pair, list)
        for field in ("expected_pairs", "completed_pairs")
        for pair in report[field]
    ):
        raise ValueError("Invalid campaign direction structure")
    expected = [tuple(pair) for pair in report["expected_pairs"]]
    completed = [tuple(pair) for pair in report["completed_pairs"]]
    if not expected or any(
        len(pair) != 2
        or pair[0] == pair[1]
        or any(not isinstance(name, str) or adapter_id_problem(name) for name in pair)
        for pair in expected + completed
    ):
        raise ValueError("Invalid campaign direction identifiers")
    findings = []
    if report["status"] != "passed":
        findings.append("Campaign did not record a passing final status.")
    if len(set(expected)) != len(expected):
        findings.append("Expected directions contain duplicates.")
    endpoints = {name for pair in expected for name in pair}
    if set(expected) != {
        (source, target)
        for source in endpoints
        for target in endpoints
        if source != target
    }:
        findings.append("Expected directions do not cover every selected peer pair.")
    if set(completed) != set(expected) or len(completed) != len(expected):
        findings.append("Completed directions differ from expected coverage.")
    if not isinstance(report["cleanup_errors"], list) or report["cleanup_errors"]:
        findings.append(
            "Native cleanup evidence is missing or owners did not stop cleanly."
        )
    sources = {source for source, _ in expected}
    cases = report["cases"]
    suite = report.get("suite", "smoke")
    expected_turns = set(turns(sources, suite))
    actual_turns = [(case["source"], case.get("fixture", "smoke")) for case in cases]
    if len(actual_turns) != len(expected_turns) or set(actual_turns) != expected_turns:
        findings.append("Source cases differ from expected coverage.")
    event_ids = [case["core"]["event"]["event_id"] for case in cases]
    if any(not isinstance(value, str) or not value for value in event_ids) or len(
        set(event_ids)
    ) != len(event_ids):
        findings.append("Source cases reuse or omit canonical event identities.")
    topology = report["native_expectations"]
    context_expectations = report.get("bridge_context_expectations", {})
    if not isinstance(context_expectations, dict) or any(
        target not in endpoints or type(required) is not bool
        for target, required in context_expectations.items()
    ):
        raise ValueError("Invalid native bridge context expectations")
    if trusted_plan is not None:
        trusted_endpoints = trusted_plan.endpoints
        trusted_expectations = {
            name: endpoint["transport"] == "matrix"
            or endpoint.get("expect_bridge_context", False)
            for name, endpoint in trusted_endpoints.items()
        }
        if (
            set(trusted_endpoints) != endpoints
            or any(
                topology.get(name, {}).get("transport") != endpoint["transport"]
                for name, endpoint in trusted_endpoints.items()
            )
            or any(
                context_expectations.get(name, False) != required
                for name, required in trusted_expectations.items()
            )
            or any(
                report.get("expected_source_attribution", {}).get(name)
                != endpoint.get("expected_source_attribution")
                or topology.get(name) != native_expectation(endpoint)
                for name, endpoint in trusted_endpoints.items()
            )
        ):
            raise ValueError(
                "Retained bridge context expectations differ from the trusted plan"
            )
        context_expectations = trusted_expectations
    if set(topology) != endpoints or any(
        not isinstance(identity, dict)
        or any(
            not isinstance(identity.get(field), str) or not identity[field]
            for field in (
                "transport",
                "source_channel",
                "receipt_sender",
                "receipt_channel",
            )
        )
        for identity in topology.values()
    ):
        raise ValueError("Invalid independent campaign identity expectations")
    transports = [identity["transport"] for identity in topology.values()]
    if (
        len(set(transports)) != len(transports)
        or not set(transports) <= TRANSPORTS
        or report["coverage"] not in {"full", "partial"}
        or (report["coverage"] == "full") != (set(transports) == TRANSPORTS)
    ):
        findings.append("Declared transport coverage differs from the campaign plan.")
    markers = [case["marker"] for case in cases]
    valid_markers = all(
        isinstance(marker, str) and marker.strip() for marker in markers
    )
    if not valid_markers or any(
        left in right
        for i, left in enumerate(markers)
        for j, right in enumerate(markers)
        if i != j
    ):
        findings.append("Source nonces are missing, duplicated, or overlapping.")
    native_count, durable_count = 0, 0
    for case in cases:
        source = case["source"]
        if suite == SUITE:
            try:
                assert case["sent_text"] == baseline_text(
                    case["fixture"], case["marker"], topology[source]["transport"]
                )
            except (AssertionError, KeyError):
                findings.append(
                    "Authored text differs from the fixed baseline fixture."
                )
        targets = {target for origin, target in expected if origin == source}
        receipts = case["native_receipts"]
        if set(receipts) != targets:
            findings.append("Independent native receipts differ from expected targets.")
        for target, receipt in receipts.items():
            expectation = topology.get(target, {})
            if (
                receipt["kind"] != "received"
                or not valid_markers
                or not isinstance(receipt.get("text"), str)
                or receipt["text"].count(case["marker"]) != 1
            ):
                findings.append(
                    "Native receipt must contain its source nonce exactly once as text."
                )
            elif receipt.get("sender") != expectation.get(
                "receipt_sender"
            ) or receipt.get("channel") != expectation.get("receipt_channel"):
                findings.append(
                    "Native receipt sender or conversation differs "
                    "from the campaign plan."
                )
            else:
                native_count += 1
                if suite == SUITE:
                    try:
                        contract = report["presentation_expectations"][target]
                        if expectation["transport"] in {"meshtastic", "meshcore"}:
                            assert type(contract["max_text_bytes"]) is int
                            assert contract["max_text_bytes"] >= 0
                        native_receipt(
                            SimpleNamespace(name=target, records=[receipt]),
                            {
                                "transport": expectation["transport"],
                                "expected_sender": expectation["receipt_sender"],
                                "channel": expectation["source_channel"],
                                "peer": {"channel": expectation["receipt_channel"]},
                                "expected_prefixes": contract["prefixes"],
                            },
                            case["marker"],
                            source=source,
                            message=baseline_text(
                                case["fixture"],
                                case["marker"],
                                topology[source]["transport"],
                            ),
                            max_text_bytes=contract["max_text_bytes"],
                        )
                        assert source in contract["prefixes"]
                        if expectation[
                            "transport"
                        ] == "matrix" or context_expectations.get(target, False):
                            verify_bridge_context(
                                receipt,
                                case["core"]["event"],
                                source,
                                topology[source]["transport"],
                                report["expected_source_attribution"][source],
                            )
                    except (AssertionError, KeyError, TypeError):
                        findings.append(
                            "Native baseline presentation differs from its contract."
                        )
            if context_expectations.get(target, False) and (suite != SUITE):
                try:
                    verify_bridge_context(
                        receipt,
                        case["core"]["event"],
                        source,
                        topology[source]["transport"],
                        report["expected_source_attribution"][source],
                    )
                except (AssertionError, KeyError, TypeError):
                    findings.append(
                        "Required native bridge context differs from its contract."
                    )
        item = case["core"]
        destinations = {
            target: topology[target]["source_channel"] for target in targets
        }
        try:
            assert valid_markers
            body = item["event"]["payload"].get(
                "body", item["event"]["payload"].get("text", "")
            )
            assert isinstance(body, str) and body.count(case["marker"]) == 1
            if suite == SUITE:
                assert body == baseline_text(
                    case["fixture"], case["marker"], topology[source]["transport"]
                )
            verify(
                item,
                source=source,
                channel=topology[source]["source_channel"],
                targets=destinations,
            )
        except (AssertionError, KeyError, TypeError):
            findings.append("Durable source or delivery provenance is inconsistent.")
        else:
            durable_count += 1
    return {
        "assessment": "failed" if findings else "passed",
        "expected_directions": len(expected),
        "completed_directions": len(completed),
        "independent_receipts": native_count,
        "durable_source_cases": durable_count,
        "findings": list(dict.fromkeys(findings)),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="Private native campaign output")
    parser.add_argument(
        "--plan",
        type=Path,
        help="Original trusted campaign plan; required to qualify XMPP evidence",
    )
    args = parser.parse_args(argv)
    if not __debug__:
        print("Analysis requires enabled assertions; do not run with -O.")
        return 1
    try:
        report = json.loads((args.directory / "results.json").read_text())
        topology = report["native_expectations"]
        if args.plan is None and any(
            identity.get("transport") == "xmpp" for identity in topology.values()
        ):
            raise ValueError("XMPP evidence requires a trusted campaign plan")
        trusted = (
            load_plan(args.plan, only={v["transport"] for v in topology.values()})
            if args.plan is not None
            else None
        )
        result = summarize(report, trusted_plan=trusted)
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError):
        # Parser exceptions can include complete source lines from private data.
        print("Campaign report is unreadable or has an unsupported structure.")
        return 1
    print(json.dumps(result, indent=2))
    return int(result["assessment"] != "passed")


if __name__ == "__main__":
    raise SystemExit(main())
