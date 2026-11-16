"""Validate release/dev image tags before giving a publish job any authority."""

from __future__ import annotations

import json
import os
import re
import subprocess


def tags(
    event: str, release_tag: str, prerelease: bool, dev_tag: str, revision: str
) -> list[str]:
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Image provenance requires a full commit SHA.")
    if event == "release":
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", release_tag):
            raise ValueError("Invalid release image tag.")
        return [release_tag] + ([] if prerelease else ["latest"])
    if event == "workflow_dispatch":
        if (
            not re.fullmatch(r"dev(?:[._-][A-Za-z0-9_.-]+)?", dev_tag)
            or len(dev_tag) > 80
        ):
            raise ValueError(
                "Manual image tags must be 'dev' or begin with 'dev-', 'dev.' or 'dev_'; maximum 80 characters."
            )
        return [dev_tag, f"{dev_tag}-{revision}"]
    return []


def main() -> None:
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    selected = tags(
        os.environ["GITHUB_EVENT_NAME"],
        os.environ.get("RELEASE_TAG", ""),
        os.environ.get("PRERELEASE") == "true",
        os.environ.get("DEV_TAG", "dev"),
        revision,
    )
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(f"revision={revision}\ntags={json.dumps(selected)}\n")


if __name__ == "__main__":
    main()
