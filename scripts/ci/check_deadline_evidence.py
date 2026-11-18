"""Validate the four SDK-boundary deadline diagnostic records from one CI run."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

_EXPECTED_CALLS = {
    "positive_control": 1,
    "equality_refusal": 0,
    "earlier_plan_deadline_wins": 0,
    "radio_prefix_boundary": 1,
}


def validate_evidence(evidence: Any, *, expected_revision: str | None = None) -> None:
    """Refuse missing cases, wrong call counts or inconsistent provider provenance."""
    if not isinstance(evidence, dict) or set(evidence) != set(_EXPECTED_CALLS):
        raise ValueError(
            "deadline evidence must contain exactly the four expected scenarios"
        )
    provider = None
    for scenario, count in _EXPECTED_CALLS.items():
        row = evidence[scenario]
        if not isinstance(row, dict) or row.get("scenario") != scenario:
            raise ValueError(f"invalid deadline evidence for {scenario}")
        if (
            type(row.get("sdk_invocations")) is not int
            or row["sdk_invocations"] != count
        ):
            raise ValueError(f"unexpected SDK invocation count for {scenario}")
        qualified = row.get("provider_provenance")
        if (
            not isinstance(qualified, dict)
            or qualified.get("transport") != "meshtastic"
            or not isinstance(qualified.get("revision"), str)
            or re.fullmatch(r"[0-9a-f]{40}", qualified["revision"]) is None
            or not isinstance(qualified.get("sdk_versions"), dict)
            or not row.get("mtjk_version")
            or qualified["sdk_versions"].get("mtjk") != row["mtjk_version"]
        ):
            raise ValueError(
                f"missing verified provider or SDK provenance for {scenario}"
            )
        if expected_revision is not None and qualified["revision"] != expected_revision:
            raise ValueError(f"unexpected provider revision for {scenario}")
        if provider is not None and qualified != provider:
            raise ValueError("deadline scenarios used different provider provenance")
        provider = qualified


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--expected-revision")
    args = parser.parse_args()
    try:
        validate_evidence(
            json.loads(args.evidence.read_text(encoding="utf-8")),
            expected_revision=args.expected_revision,
        )
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Deadline qualification evidence rejected: {exc}\n")


if __name__ == "__main__":
    main()
