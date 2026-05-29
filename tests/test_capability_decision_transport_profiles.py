"""Transport-profile capability decision conformance tests.

Loads ``docs/spec/transport-profiles/*-capabilities.json``, instantiates
:class:`AdapterCapabilities` directly from each JSON, and asserts that
the :class:`CapabilityDecisionResolver` produces correct decisions for
every declared capability — relation capabilities map to
direct/fallback_text/skip; boolean capabilities map to direct/skip.

No optional SDK imports.  Pure capability model validation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from medre.core.contracts.adapter import AdapterCapabilities
from medre.core.planning.capability_decision import resolver
from tests.helpers.pipeline import make_event

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TRANSPORTS = ("matrix", "meshtastic", "meshcore", "lxmf")

PROFILES_DIR = (
    Path(__file__).resolve().parent.parent / "docs" / "spec" / "transport-profiles"
)

# Event-kind → (capability_field, is_boolean_field)
_EVENT_KIND_CHECKS: list[tuple[str, str, bool]] = [
    ("message.reacted", "reactions", False),
    ("message.edited", "edits", False),
    ("message.deleted", "deletes", False),
    ("message.file", "attachments", True),
    ("message.created", "text", True),
    ("message.text", "text", True),
    ("presence.changed", "presence", True),
    ("telemetry.received", "metadata_fields", True),
    ("telemetry.position", "metadata_fields", True),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_caps(transport: str) -> AdapterCapabilities:
    """Load AdapterCapabilities from the transport's JSON profile."""
    path = PROFILES_DIR / f"{transport}-capabilities.json"
    assert path.exists(), f"Missing capabilities file: {path}"
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    assert data["transport"] == transport
    return AdapterCapabilities(**data["capabilities"])  # type: ignore[arg-type]


def _expected_strategy_for_value(
    value: object,
    *,
    is_boolean: bool,
) -> str:
    """Derive the expected delivery_strategy from a capability value."""
    if is_boolean:
        return "direct" if value else "skip"
    # String field: native → direct, fallback → fallback_text, unsupported → skip.
    if value == "native":
        return "direct"
    if value == "fallback":
        return "fallback_text"
    return "skip"


def _expected_supported_for_value(
    value: object,
    *,
    is_boolean: bool,
) -> bool:
    """Derive whether the capability is supported (deliverable)."""
    if is_boolean:
        return bool(value)
    return value != "unsupported"


# ===================================================================
# Parametrized transport × event-kind decision tests
# ===================================================================


@pytest.mark.parametrize("transport", TRANSPORTS)
class TestTransportProfileDecisions:
    """Verify each transport's JSON capabilities produce correct decisions."""

    def test_text_capability(self, transport: str) -> None:
        """text=false skips message.text; text=true delivers directly."""
        caps = _load_caps(transport)
        event = make_event(event_kind="message.text")
        decision = resolver.decide(event, caps)

        expected = _expected_strategy_for_value(caps.text, is_boolean=True)
        assert decision.delivery_strategy == expected
        assert decision.supported == _expected_supported_for_value(
            caps.text, is_boolean=True
        )

    def test_attachments_capability(self, transport: str) -> None:
        """attachments=false skips message.file; true delivers directly."""
        caps = _load_caps(transport)
        event = make_event(event_kind="message.file")
        decision = resolver.decide(event, caps)

        expected = _expected_strategy_for_value(caps.attachments, is_boolean=True)
        assert decision.delivery_strategy == expected

    def test_reactions_capability(self, transport: str) -> None:
        """reactions=unsupported skips; native → direct; fallback → fallback_text."""
        caps = _load_caps(transport)
        event = make_event(event_kind="message.reacted")
        decision = resolver.decide(event, caps)

        expected = _expected_strategy_for_value(caps.reactions, is_boolean=False)
        assert decision.delivery_strategy == expected

    def test_edits_capability(self, transport: str) -> None:
        """edits=unsupported skips message.edited."""
        caps = _load_caps(transport)
        event = make_event(event_kind="message.edited")
        decision = resolver.decide(event, caps)

        expected = _expected_strategy_for_value(caps.edits, is_boolean=False)
        assert decision.delivery_strategy == expected

    def test_deletes_capability(self, transport: str) -> None:
        """deletes=unsupported skips message.deleted."""
        caps = _load_caps(transport)
        event = make_event(event_kind="message.deleted")
        decision = resolver.decide(event, caps)

        expected = _expected_strategy_for_value(caps.deletes, is_boolean=False)
        assert decision.delivery_strategy == expected

    def test_presence_capability(self, transport: str) -> None:
        """presence=false skips presence.changed; true delivers directly."""
        caps = _load_caps(transport)
        event = make_event(event_kind="presence.changed")
        decision = resolver.decide(event, caps)

        expected = _expected_strategy_for_value(caps.presence, is_boolean=True)
        assert decision.delivery_strategy == expected

    def test_metadata_fields_capability(self, transport: str) -> None:
        """metadata_fields=false skips telemetry; true delivers directly."""
        caps = _load_caps(transport)
        event = make_event(event_kind="telemetry.received")
        decision = resolver.decide(event, caps)

        expected = _expected_strategy_for_value(caps.metadata_fields, is_boolean=True)
        assert decision.delivery_strategy == expected

    def test_passthrough_kinds_always_direct(self, transport: str) -> None:
        """Unmapped event kinds always produce direct delivery."""
        caps = _load_caps(transport)

        for kind in ("plugin.custom", "identity.updated", "system.audit"):
            event = make_event(event_kind=kind)
            decision = resolver.decide(event, caps)

            assert decision.delivery_strategy == "direct"
            assert decision.supported is True

    def test_capability_level_matches_strategy(self, transport: str) -> None:
        """For every event-kind check, capability_level is consistent
        with delivery_strategy."""
        caps = _load_caps(transport)

        for event_kind, _field, _is_bool in _EVENT_KIND_CHECKS:
            event = make_event(event_kind=event_kind)
            decision = resolver.decide(event, caps)

            if decision.delivery_strategy == "direct":
                assert decision.capability_level == "native"
            elif decision.delivery_strategy == "fallback_text":
                assert decision.capability_level == "fallback"
            elif decision.delivery_strategy == "skip":
                assert decision.capability_level == "unsupported"
