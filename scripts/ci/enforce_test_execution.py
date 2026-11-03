#!/usr/bin/env python3
"""Enforce that a required qualification suite executed, from its junit report.

A required CI job that collects a gated qualification suite must fail when
nothing actually ran: zero collected tests, skipped tests, or a broken
collection each mean the qualification did not execute, and a skip must
never silently count as a pass. This script reads the junit XML pytest
writes and fails unless the report proves the expected number of tests
executed with no skip, failure, or error. Console output is never parsed;
the junit report is the only evidence.
"""

from __future__ import annotations

import argparse
import glob
import xml.etree.ElementTree as ET
from pathlib import Path

_SUITE_TOTALS = ("tests", "skipped", "errors", "failures")


def _int_total(suite: ET.Element, name: str) -> int:
    """Read a testsuite total, treating an absent attribute as zero."""
    raw = suite.get(name)
    if raw is None:
        return 0
    value = int(raw)
    if value < 0:
        raise ValueError(f"{name} must be nonnegative, got {raw!r}")
    return value


def check_report(
    path: Path, expected_tests: int, *, required_artifacts: tuple[str, ...] = ()
) -> list[str]:
    """Return every reason the junit report does not prove execution."""
    if not path.is_file():
        return [
            f"junit report is missing at {path}: pytest did not run, or "
            "collection crashed before a report could be written"
        ]
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        return [f"junit report at {path} is not parseable: {exc}"]
    suites = list(root.iter("testsuite"))
    if not suites:
        return [f"junit report at {path} contains no testsuite element"]
    totals = dict.fromkeys(_SUITE_TOTALS, 0)
    problems: list[str] = []
    identities: set[tuple[str, str]] = set()
    for suite in suites:
        cases = suite.findall("testcase")
        actual = {"tests": len(cases)}
        for counter, tag in (
            ("skipped", "skipped"),
            ("errors", "error"),
            ("failures", "failure"),
        ):
            actual[counter] = sum(case.find(tag) is not None for case in cases)
        for name in _SUITE_TOTALS:
            totals[name] += actual[name]
            try:
                declared = _int_total(suite, name)
            except ValueError as exc:
                problems.append(f"junit has an invalid {name} total: {exc}")
                continue
            if declared != actual[name]:
                problems.append(
                    f"junit {name} total {declared} contradicts "
                    f"{actual[name]} testcase record(s)"
                )
        for case in cases:
            identity = (case.get("classname", ""), case.get("name", ""))
            if not identity[1] or identity in identities:
                problems.append(
                    f"junit has a missing or duplicate testcase identity: {identity}"
                )
            identities.add(identity)
    for pattern in required_artifacts:
        if not any(
            Path(match).is_file() for match in glob.glob(pattern, recursive=True)
        ):
            problems.append(f"required qualification artifact is missing: {pattern}")
    if totals["tests"] <= 0:
        problems.append(
            f"junit records {totals['tests']} tests: the suite did not execute"
        )
    elif totals["tests"] != expected_tests:
        problems.append(
            f"junit records {totals['tests']} tests, expected {expected_tests}"
        )
    if totals["skipped"]:
        problems.append(
            f"junit records {totals['skipped']} skipped test(s): a skip must "
            "never count as a pass"
        )
    if totals["errors"]:
        problems.append(
            f"junit records {totals['errors']} error(s), including collection errors"
        )
    if totals["failures"]:
        problems.append(f"junit records {totals['failures']} failure(s)")
    return problems


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--junit", type=Path, required=True, help="junit XML written by pytest"
    )
    parser.add_argument(
        "--expect-tests",
        type=int,
        required=True,
        help="exact number of tests the report must show executed",
    )
    parser.add_argument(
        "--require-artifact",
        action="append",
        default=[],
        help="file glob that must match a preserved qualification artifact (repeatable)",
    )
    args = parser.parse_args()
    problems = check_report(
        args.junit, args.expect_tests, required_artifacts=tuple(args.require_artifact)
    )
    if problems:
        for problem in problems:
            print(f"::error::{problem}")
        raise SystemExit(1)
    print(
        f"Execution enforced: junit records {args.expect_tests} executed "
        "test(s), 0 skipped, 0 errors, 0 failures"
    )


if __name__ == "__main__":
    main()
