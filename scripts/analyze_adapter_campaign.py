"""Check retained campaign evidence without connecting to any native service."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from medre.config.identifiers import adapter_id_problem
from scripts.adapter_campaign.evidence import verify
from scripts.adapter_campaign.plan import TRANSPORTS


def summarize(report: dict) -> dict:
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
    if len(cases) != len(sources) or {case["source"] for case in cases} != sources:
        findings.append("Source cases differ from expected coverage.")
    topology = report["native_expectations"]
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
            verify(
                item,
                source=source,
                channel=topology[source]["source_channel"],
                targets=destinations,
            )
        except AssertionError:
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
    args = parser.parse_args(argv)
    if not __debug__:
        print("Analysis requires enabled assertions; do not run with -O.")
        return 1
    try:
        result = summarize(json.loads((args.directory / "results.json").read_text()))
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError):
        # Parser exceptions can include complete source lines from private data.
        print("Campaign report is unreadable or has an unsupported structure.")
        return 1
    print(json.dumps(result, indent=2))
    return int(result["assessment"] != "passed")


if __name__ == "__main__":
    raise SystemExit(main())
