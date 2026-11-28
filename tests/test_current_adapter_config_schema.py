# medre-test-tier: compatibility
"""Current provider configuration can evolve without rewriting release snapshots."""

import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1] / "docs/schemas"


def schema(current=False):
    path = ROOT / "current" if current else ROOT
    return json.loads((path / "adapter-config.schema.json").read_text())


def test_current_schema_contains_only_reviewed_changes():
    released = schema()
    current = schema(True)
    Draft202012Validator.check_schema(current)
    expected = deepcopy(released)
    expected["$id"] = current["$id"]
    arm = next(x for x in expected["anyOf"] if x["title"] == "MeshtasticConfig")
    field = next(x for x in current["anyOf"] if x["title"] == "MeshtasticConfig")[
        "properties"
    ]["startup_packet_drain_seconds"]
    assert field["type"] == "number" and field["minimum"] == 0
    assert field["maximum"] == sys.float_info.max
    from medre.config.adapters.meshtastic import MeshtasticConfig

    assert (
        field["default"]
        == MeshtasticConfig(adapter_id="radio").startup_packet_drain_seconds
        == 15
    )
    arm["properties"]["startup_packet_drain_seconds"] = field
    matrix_arm = next(x for x in expected["anyOf"] if x["title"] == "MatrixConfig")
    matrix_arm["properties"]["trusted_relays"] = {
        "type": "array",
        "items": {
            "type": "string",
            "pattern": r"^@[^:]+:.+",
            "not": {"pattern": r"[\s*]"},
        },
        "uniqueItems": True,
        "default": [],
        "description": (
            "Exact Matrix user IDs permitted to send the reserved MEDRE envelope. "
            "An empty list trusts no peer. Membership compares the wire sender "
            "exactly. A permitted sender does not give embedded claims authority."
        ),
    }
    # Description-only rewordings of copied frozen arms: the adapter and
    # transport vocabulary replaces the retired provider wording. Each delta
    # names its arm with the pinned new literal.
    for title, description in (
        (
            "XMPPConfig",
            "Immutable configuration for the XMPP adapter: explicitly configured"
            " bare-JID rooms and peers, text limits, and optional relay"
            " attribution. Installed-adapter validation also checks native"
            " identities, allowlists and cross-field constraints.",
        ),
        (
            "ChatmailConfig",
            "Immutable configuration for the Chatmail adapter: one private native"
            " account with an explicit allowlist of ordinary chats."
            " Installed-adapter validation also checks native identities,"
            " allowlists and cross-field constraints.",
        ),
        (
            "SimplexChatConfig",
            "Immutable configuration for the SimpleX Chat adapter: one native"
            " profile with explicit group and contact allowlists."
            " Installed-adapter validation also checks native identities,"
            " allowlists and cross-field constraints.",
        ),
    ):
        expected_arm = next(x for x in expected["anyOf"] if x["title"] == title)
        assert expected_arm["description"] != description
        expected_arm["description"] = description
    assert current == expected
    assert current["$id"].endswith("/current/adapter-config.schema.json")


@pytest.mark.parametrize(
    ("relays", "valid"),
    [
        ([], True),
        (["@relay:example.org", "@second:[::1]:8448"], True),
        (["@relay:example.org", "@relay:example.org"], False),
        (["@:example.org"], False),
        (["@relay:"], False),
        (["@relay:example.org\n"], False),
        (["@*:example.org"], False),
    ],
)
def test_trusted_relays_schema_matches_matrix_config(relays, valid):
    from medre.config.adapters.matrix import MatrixConfig

    base = {
        "adapter_id": "relay",
        "homeserver": "https://matrix.example.org",
        "user_id": "@bridge:example.org",
    }
    assert Draft202012Validator(schema()).is_valid(base)
    assert not Draft202012Validator(schema()).is_valid({**base, "trusted_relays": []})
    assert (
        Draft202012Validator(schema(True)).is_valid({**base, "trusted_relays": relays})
        is valid
    )
    config = MatrixConfig(**base, trusted_relays=tuple(relays))
    if valid:
        config.validate()
    else:
        from medre.config.adapters.matrix import MatrixConfigError

        with pytest.raises(MatrixConfigError, match="trusted_relays"):
            config.validate()


@pytest.mark.parametrize("window", [0, 0.5, 15, 60])
def test_current_schema_accepts_drain_configs_without_changing_the_baseline(window):
    config = {"adapter_id": "radio", "connection_type": "fake"}
    assert Draft202012Validator(schema()).is_valid(config)
    assert Draft202012Validator(schema(True)).is_valid(config)
    config["startup_packet_drain_seconds"] = window
    assert not Draft202012Validator(schema()).is_valid(config)
    assert Draft202012Validator(schema(True)).is_valid(config)


@pytest.mark.parametrize("window", [True, False, -1, "15", None, 10**309])
def test_current_schema_rejects_invalid_drain_values(window):
    assert not Draft202012Validator(schema(True)).is_valid(
        {
            "adapter_id": "radio",
            "connection_type": "fake",
            "startup_packet_drain_seconds": window,
        }
    )
