"""Structural guards for optional adapter SDK contract pin authority."""

from __future__ import annotations

import pytest

from tests.helpers.sdk_contract import declared_extra_pins


@pytest.mark.parametrize(
    ("extra", "expected_distributions"),
    [
        ("matrix", {"mindroom-nio"}),
        ("matrix-e2e", {"mindroom-nio"}),
        ("lxmf", {"lxmf", "rns"}),
        ("meshtastic", {"mtjk", "pypubsub"}),
        ("meshcore", {"meshcore"}),
    ],
)
def test_adapter_sdk_contract_extras_are_exact_pins(
    extra: str,
    expected_distributions: set[str],
) -> None:
    """Project metadata, not duplicated test literals, owns SDK versions."""
    pins = declared_extra_pins(extra)
    assert set(pins) == expected_distributions
    assert all(pin.version for pin in pins.values())
