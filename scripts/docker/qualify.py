"""Validate image qualification evidence independently of CLI exit status."""

from __future__ import annotations

import re
from typing import Any


def _name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def validate_installation(
    record: dict[str, Any], manifest: list[dict[str, Any]], version: str
) -> None:
    """The official image must contain the selected provider commits, not only names."""
    if (
        record["core"] != version
        or sorted(record["providers"])
        != sorted(entry["transport"] for entry in manifest)
        or len(record["providers"]) != 7
    ):
        raise ValueError(
            "Installed core version or provider inventory differs from selection"
        )
    sources = {_name(name): source for name, source in record["direct_sources"].items()}
    for entry in manifest:
        source = sources.get(_name(entry["distribution"]), {})
        vcs = source.get("vcs_info", {})
        if (
            source.get("url")
            not in {
                f"https://github.com/{entry['repository']}",
                f"https://github.com/{entry['repository']}.git",
            }
            or vcs.get("vcs") != "git"
            or vcs.get("commit_id") != entry["ref"]
        ):
            raise ValueError(
                f"Installed provider provenance mismatch: {entry['transport']}"
            )


def startup_ready(logs: str) -> bool:
    """Wait for the seven-adapter image's complete, healthy startup banner."""
    messages = [
        line.split(" [INFO] medre: ", 1)[1]
        for line in logs.splitlines()
        if " [INFO] medre: " in line
    ]
    observed_banner = False
    for message in messages:
        if message.startswith("MEDRE ") and " — startup (" in message:
            observed_banner = message.endswith(" — startup (7 adapter(s))")
        elif (
            observed_banner and message == "All adapters ready — MEDRE runtime running"
        ):
            return True
    return False


def validate_shutdown(state: dict[str, Any], snapshot: dict[str, Any]) -> None:
    """A zero process exit alone does not prove that the runtime stopped."""
    lifecycle = snapshot["lifecycle"]
    adapters = lifecycle["adapters"]
    if (
        state["ExitCode"] != 0
        or state["OOMKilled"]
        or state["Running"]
        or lifecycle["runtime_state"] != "stopped"
        or len(adapters) != 7
        or any(value != "stopped" for value in adapters.values())
    ):
        raise ValueError("Runtime or adapter shutdown was not clean")
