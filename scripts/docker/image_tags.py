"""Validate release/dev image tags before giving a publish job any authority."""

from __future__ import annotations

import argparse
import json
import os
import re

from scripts.ci.validate_release_version import normalize_release_tag


def tags(
    event: str, release_tag: str, prerelease: bool, dev_tag: str, revision: str
) -> list[str]:
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Image provenance requires a full commit SHA.")
    if event == "release":
        version = normalize_release_tag(release_tag)
        if len(release_tag) > 128:
            raise ValueError("Invalid release image tag.")
        if not prerelease and re.search(r"(?:a|b|rc)[0-9]|\.dev[0-9]", version):
            raise ValueError("Prerelease versions must be marked as prereleases.")
        return [release_tag] + ([] if prerelease else ["latest"])
    if event == "workflow_dispatch":
        if (
            not re.fullmatch(r"dev(?:[._-][A-Za-z0-9_.-]+)?", dev_tag)
            or len(dev_tag) > 80
            or re.search(r"-[0-9a-fA-F]{40}$", dev_tag)
        ):
            raise ValueError(
                "Manual image tags must be 'dev' or begin with 'dev-', 'dev.' or 'dev_'; maximum 80 characters, with no full-SHA suffix."
            )
        return [dev_tag, f"{dev_tag}-{revision}"]
    return []


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True)
    revision = parser.parse_args().revision
    event = os.environ["GITHUB_EVENT_NAME"]
    event_sha = os.environ["GITHUB_SHA"]
    core_ref = os.environ.get("CORE_REF", "").strip()
    if event == "release" or not core_ref:
        if revision != event_sha:
            raise ValueError("Checkout revision differs from the triggering event SHA.")
    elif re.fullmatch(r"[0-9a-fA-F]{40}", core_ref) and revision != core_ref.lower():
        raise ValueError("Checkout revision differs from the requested core SHA.")
    prerelease = os.environ.get("PRERELEASE", "")
    if event == "release" and prerelease not in {"true", "false"}:
        raise ValueError("Missing release prerelease classification.")
    selected = tags(
        event,
        os.environ.get("RELEASE_TAG", ""),
        prerelease == "true",
        os.environ.get("DEV_TAG", "dev"),
        revision,
    )
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(f"revision={revision}\ntags={json.dumps(selected)}\n")


if __name__ == "__main__":
    main()
