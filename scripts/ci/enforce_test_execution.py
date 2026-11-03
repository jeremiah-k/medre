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
import xml.etree.ElementTree as ET
from pathlib import Path

_SUITE_TOTALS = ("tests", "skipped", "errors", "failures")


def _int_total(suite: ET.Element, name: str) -> int:
    """Read a testsuite total, treating an absent attribute as zero."""
    raw = suite.get(name)
    if raw is None:
        return 0
    return int(raw)


def check_report(path: Path, expected_tests: int) -> list[str]:
    """Return every reason the junit report does not prove execution."""
    if not path.is_file():
        return [
            f"junit report is missing at {path}: pytest did not run, or "
            "collection crashed before a report could be written"
        ]
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        return [f"junit report at {path} is not parseable: {exc}"]
    suites = list(root.iter("testsuite"))
    if not suites:
        return [f"junit report at {path} contains no testsuite element"]
    totals = dict.fromkeys(_SUITE_TOTALS, 0)
    for suite in suites:
        for name in _SUITE_TOTALS:
            totals[name] += _int_total(suite, name)
    problems: list[str] = []
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
    args = parser.parse_args()
    problems = check_report(args.junit, args.expect_tests)
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
