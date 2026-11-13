"""An untagged adapter config union must not reject overlapping valid shapes.

The surrounding runtime configuration selects the transport before binding a
provider dataclass.  This standalone schema has no transport discriminator.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

_SCHEMAS = Path(__file__).resolve().parents[1] / "docs" / "schemas"
_EXAMPLES = _SCHEMAS / "examples"
_EXAMPLE_TYPES = {
    "adapter-config-example.json": "MatrixConfig",
    "adapter-config-meshtastic-example.json": "MeshtasticConfig",
    "adapter-config-meshcore-example.json": "MeshCoreConfig",
    "adapter-config-lxmf-example.json": "LxmfConfig",
    "adapter-config-xmpp-example.json": "XMPPConfig",
    "adapter-config-chatmail-example.json": "ChatmailConfig",
    "adapter-config-simplexchat-example.json": "SimplexChatConfig",
}


def _schema() -> dict:
    return json.loads((_SCHEMAS / "adapter-config.schema.json").read_text())


def _arms() -> dict[str, dict]:
    return {arm["title"]: arm for arm in _schema()["anyOf"]}


def test_untagged_union_uses_anyof_and_preserves_all_provider_arms() -> None:
    schema = _schema()
    assert "oneOf" not in schema
    assert len(schema["anyOf"]) == len(_EXAMPLE_TYPES)
    assert set(_arms()) == set(_EXAMPLE_TYPES.values())
    Draft202012Validator.check_schema(schema)


@pytest.mark.parametrize(
    "payload",
    [
        {"adapter_id": "radio"},
        {"adapter_id": "radio", "relay_prefix": ""},
        {"adapter_id": "radio", "relay_prefix": "{sender5}: "},
        {"adapter_id": "radio", "connection_type": "tcp", "relay_prefix": ""},
    ],
)
def test_overlapping_valid_adapter_shapes_are_not_rejected(payload: dict) -> None:
    schema = _schema()
    if "relay_prefix" in payload and isinstance(payload["relay_prefix"], str):
        matches = sum(
            Draft202012Validator(arm).is_valid(payload) for arm in schema["anyOf"]
        )
        assert matches > 1
    assert Draft202012Validator(schema).is_valid(payload)


@pytest.mark.parametrize("name", sorted(_EXAMPLE_TYPES))
def test_example_validates_against_its_selected_adapter_arm(name: str) -> None:
    payload = json.loads((_EXAMPLES / name).read_text())
    assert Draft202012Validator(_arms()[_EXAMPLE_TYPES[name]]).is_valid(payload)
    assert Draft202012Validator(_schema()).is_valid(payload)


@pytest.mark.parametrize(
    "field", ["radio_relay_prefix", "meshcore_relay_prefix", "lxmf_relay_prefix"]
)
def test_removed_config_keys_are_not_accepted_by_any_adapter(field: str) -> None:
    assert not Draft202012Validator(_schema()).is_valid(
        {"adapter_id": "radio", field: "{sender5}: "}
    )


def test_selected_transport_still_controls_nullable_prefix_semantics() -> None:
    arms = _arms()
    empty = {"adapter_id": "radio", "relay_prefix": ""}
    disabled = {"adapter_id": "radio", "relay_prefix": None}
    assert Draft202012Validator(arms["MeshtasticConfig"]).is_valid(empty)
    assert Draft202012Validator(arms["MeshtasticConfig"]).is_valid(disabled)
    assert Draft202012Validator(arms["MeshCoreConfig"]).is_valid(empty)
    assert not Draft202012Validator(arms["MeshCoreConfig"]).is_valid(disabled)
    assert not Draft202012Validator(_schema()).is_valid(
        {"adapter_id": "radio", "relay_prefix": 12}
    )


def test_all_adapter_arms_reject_undeclared_properties() -> None:
    assert not Draft202012Validator(_schema()).is_valid(
        {"adapter_id": "radio", "unexpected_compatibility_key": "value"}
    )


@pytest.mark.parametrize(
    "module,class_name,old_field,default",
    [
        (
            "medre.config.adapters.meshtastic",
            "MeshtasticConfig",
            "radio_relay_prefix",
            None,
        ),
        (
            "medre.config.adapters.meshcore",
            "MeshCoreConfig",
            "meshcore_relay_prefix",
            "",
        ),
        (
            "medre.config.adapters.lxmf",
            "LxmfConfig",
            "lxmf_relay_prefix",
            "",
        ),
    ],
)
def test_pinned_adapter_rejects_old_key_and_accepts_new_key(
    module: str, class_name: str, old_field: str, default: str | None
) -> None:
    # The core tier runs without adapter distributions installed; the
    # pinned dataclass contract is only asserted where the module exists.
    config_type = getattr(pytest.importorskip(module), class_name)
    assert config_type(adapter_id="test-radio").relay_prefix == default
    assert (
        config_type(adapter_id="test-radio", relay_prefix="").validate().relay_prefix
        == ""
    )
    with pytest.raises(TypeError):
        config_type(adapter_id="test-radio", **{old_field: "compat"})


def test_unicode_matrix_sender_truncation_uses_core_declared_formatter() -> None:
    from medre.core.rendering.attribution import RelayAttribution, format_relay_prefix

    result = format_relay_prefix(
        "{sender5}[M]: ",
        RelayAttribution(source_sender_label="🦉àbcde"),
        truncatable=("sender", "sender_short", "origin_label"),
    )
    assert result.rendered_prefix == "🦉àbcd[M]: "
    assert result.unknown_variables == ()
    assert result.formatting_error is None
