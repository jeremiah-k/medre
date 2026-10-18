"""Fail-closed capability-field validation inside the decision resolver.

Covers the drift refusal for a mapped field missing from the declared
capabilities and the safety net for a declared field the resolver maps
to neither a boolean nor a three-level string capability.
"""

from __future__ import annotations

import pytest

from medre.core.contracts.adapter import AdapterCapabilities
from medre.core.planning.capability_decision import resolver
from tests.helpers.pipeline import make_event


def test_missing_mapped_capability_field_raises_drift_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mapping to a field absent from AdapterCapabilities fails closed."""
    monkeypatch.setattr(
        "medre.core.planning.capability_decision._EVENT_KIND_FIELDS",
        {"message.created": "no_such_field"},
    )
    with pytest.raises(AttributeError, match="capability-field drift"):
        resolver.decide(make_event(), AdapterCapabilities())


def test_unmapped_capability_field_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A declared field outside the boolean and string sets is refused."""
    monkeypatch.setattr(
        "medre.core.planning.capability_decision._EVENT_KIND_FIELDS",
        {"message.created": "title"},
    )
    with pytest.raises(
        ValueError, match="not a recognised boolean or string capability field"
    ):
        resolver.decide(make_event(), AdapterCapabilities())
