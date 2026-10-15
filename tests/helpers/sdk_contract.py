"""Compatibility SDK checks; installed providers own their dependency pins."""

import json
from importlib.metadata import entry_points
from pathlib import Path
from typing import Iterable

from medre.testing.sdk_contract import assert_installed_sdk_pins, declared_sdk_pins


def _provider(transport: str, sdk_extra: str | None) -> tuple[str, tuple[str, ...]]:
    providers = [
        ep for ep in entry_points(group="medre.adapters") if ep.name == transport
    ]
    if len(providers) != 1 or providers[0].dist is None:
        raise LookupError(
            f"expected exactly one installed adapter provider for {transport!r}"
        )
    if sdk_extra is None:
        manifest = json.loads(
            (
                Path(__file__).resolve().parents[2]
                / ".github/adapter-compatibility.json"
            ).read_text(encoding="utf-8")
        )
        sdk_extra = next(
            (entry["extras"] for entry in manifest if entry["transport"] == transport),
            "",
        )
    return providers[0].dist.name, (sdk_extra,) if sdk_extra else ()


def declared_extra_pins(transport: str, *, sdk_extra: str | None = None):
    """Read any installed provider; curated qualification supplies default extras."""
    distribution, extras = _provider(transport, sdk_extra)
    return declared_sdk_pins(distribution, extras=extras)


def assert_installed_extra_matches_declared_pins(
    transport: str, distributions: Iterable[str], *, sdk_extra: str | None = None
) -> None:
    distribution, extras = _provider(transport, sdk_extra)
    assert_installed_sdk_pins(distribution, distributions, extras=extras)
