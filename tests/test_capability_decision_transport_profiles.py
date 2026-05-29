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
from medre.core.events.canonical import EventRelation, NativeRef
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
    if value == "unsupported":
        return "skip"
    raise AssertionError(
        f"Unexpected capability string value: {value!r} "
        f"(expected 'native', 'fallback', or 'unsupported')"
    )


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
            else:
                pytest.fail(
                    f"Unexpected delivery_strategy: " f"{decision.delivery_strategy!r}"
                )


# ===================================================================
# Relation-specific transport profile tests
# ===================================================================


_REPLY_RELATION = EventRelation(
    relation_type="reply",
    target_event_id="evt-parent",
    target_native_ref=NativeRef(
        adapter="test_adapter",
        native_channel_id="ch-0",
        native_message_id="native-001",
    ),
    key=None,
    fallback_text="original",
)

_REACTION_RELATION = EventRelation(
    relation_type="reaction",
    target_event_id="evt-parent",
    target_native_ref=None,
    key="\U0001f44d",
    fallback_text=None,
)

_EDIT_RELATION = EventRelation(
    relation_type="edit",
    target_event_id="evt-parent",
    target_native_ref=None,
    key=None,
    fallback_text=None,
)

_DELETE_RELATION = EventRelation(
    relation_type="delete",
    target_event_id="evt-parent",
    target_native_ref=None,
    key=None,
    fallback_text=None,
)


def _expected_relation_strategy(caps: AdapterCapabilities, cap_field: str) -> str:
    """Derive expected strategy from a relation's capability field value."""
    raw = getattr(caps, cap_field)
    if raw == "native":
        return "direct"
    if raw == "fallback":
        return "fallback_text"
    return "skip"


@pytest.mark.parametrize("transport", TRANSPORTS)
class TestRelationTransportProfileDecisions:
    """Verify each transport's relation capabilities produce correct decisions.

    Uses unmapped event kind (plugin.custom) to isolate relation behavior.
    """

    def test_reply_relation_uses_caps_replies(self, transport: str) -> None:
        """Reply relation uses caps.replies for decision."""
        caps = _load_caps(transport)
        event = make_event(
            event_kind="plugin.custom",
            relations=(_REPLY_RELATION,),
        )
        decision = resolver.decide(event, caps)

        expected = _expected_relation_strategy(caps, "replies")
        assert decision.delivery_strategy == expected
        assert decision.capability_field == "replies"

    def test_reaction_relation_uses_caps_reactions(self, transport: str) -> None:
        """Reaction relation uses caps.reactions for decision."""
        caps = _load_caps(transport)
        event = make_event(
            event_kind="plugin.custom",
            relations=(_REACTION_RELATION,),
        )
        decision = resolver.decide(event, caps)

        expected = _expected_relation_strategy(caps, "reactions")
        assert decision.delivery_strategy == expected
        assert decision.capability_field == "reactions"

    def test_edit_relation_uses_caps_edits(self, transport: str) -> None:
        """Edit relation uses caps.edits for decision."""
        caps = _load_caps(transport)
        event = make_event(
            event_kind="plugin.custom",
            relations=(_EDIT_RELATION,),
        )
        decision = resolver.decide(event, caps)

        expected = _expected_relation_strategy(caps, "edits")
        assert decision.delivery_strategy == expected
        assert decision.capability_field == "edits"

    def test_delete_relation_uses_caps_deletes(self, transport: str) -> None:
        """Delete relation uses caps.deletes for decision."""
        caps = _load_caps(transport)
        event = make_event(
            event_kind="plugin.custom",
            relations=(_DELETE_RELATION,),
        )
        decision = resolver.decide(event, caps)

        expected = _expected_relation_strategy(caps, "deletes")
        assert decision.delivery_strategy == expected
        assert decision.capability_field == "deletes"


def test_expected_strategy_helper_fails_on_unknown_string() -> None:
    """Harden _expected_strategy_for_value: unknown strings should raise."""
    # Valid strings should not raise
    assert _expected_strategy_for_value("native", is_boolean=False) == "direct"
    assert _expected_strategy_for_value("fallback", is_boolean=False) == "fallback_text"
    assert _expected_strategy_for_value("unsupported", is_boolean=False) == "skip"

    # Unknown string must raise AssertionError.
    with pytest.raises(AssertionError, match="Unexpected capability string value"):
        _expected_strategy_for_value("maybe", is_boolean=False)
