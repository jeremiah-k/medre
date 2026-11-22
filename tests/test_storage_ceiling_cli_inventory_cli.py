"""The CLI inventory lists every registered ``storage ceiling`` leaf.

The storage group check alone passes when the inventory lists only the
group; the leaves registered under ``medre storage ceiling`` must each be
listed in configuration.md's CLI inventory too.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from medre.cli.main import _build_parser

_DOC = Path(__file__).resolve().parents[1] / "docs/ops/configuration.md"


def _subparser_choices(parser: argparse.ArgumentParser, *path: str) -> set[str]:
    current: argparse.ArgumentParser = parser
    for name in path:
        found = None
        for action in current._actions:
            if (
                isinstance(action, argparse._SubParsersAction)
                and name in action.choices
            ):
                found = action.choices[name]
                break
        if found is None:
            return set()
        current = found
    choices: set[str] = set()
    for action in current._actions:
        if isinstance(action, argparse._SubParsersAction):
            choices.update(action.choices.keys())
    return choices


def _cli_inventory() -> str:
    text = _DOC.read_text(encoding="utf-8")
    section = re.search(
        r"^## CLI Commands(.*?)(?=^## |\Z)", text, re.DOTALL | re.MULTILINE
    )
    if not section:
        return ""
    fence = re.search(r"```text\n(.*?)```", section.group(1), re.DOTALL)
    return fence.group(1) if fence else ""


def test_storage_ceiling_leaves_are_listed_in_cli_inventory() -> None:
    leaves = _subparser_choices(_build_parser(), "storage", "ceiling")
    assert leaves == {"audit", "verify"}
    inventory = _cli_inventory()
    assert inventory, "CLI inventory text block not found in configuration.md"
    for leaf in sorted(leaves):
        assert f"medre storage ceiling {leaf}" in inventory
