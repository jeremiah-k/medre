"""Core-only regression contracts for offline native authoring and explainability."""

from __future__ import annotations

import io
import os
from argparse import Namespace
from dataclasses import replace
from unittest.mock import patch

import pytest

from medre import adapter_registry
from medre.adapter_spec import NativeSetupField
from medre.config._yaml import dump_yaml_config, parse_yaml_config
from medre.config.errors import ConfigValidationError
from medre.config.loader import parse_runtime_config
from medre.config.routes import RouteDirectionality
from medre.config.paths import resolve
from medre.runtime.bridge_setup import BridgeRequest, draft_bridge
from medre.runtime.route_explain import explain_routes
from tests.helpers.bridge_setup import CONFIG, install_provider
from tests.helpers.cli import _run_cli_raw


@pytest.fixture(autouse=True)
def provider(monkeypatch, tmp_path):
    spec = install_provider(monkeypatch)
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path / "state"))
    altered = replace(
        spec,
        native_setup_fields=(
            NativeSetupField("credential", "Secret credential", secret=True),
            NativeSetupField("origin_label", "Origin name"),
        ),
    )
    monkeypatch.setattr(
        adapter_registry,
        "ADAPTER_REGISTRY",
        adapter_registry.AdapterTypeRegistry((altered,)),
    )


class Terminal(io.StringIO):
    def isatty(self):
        return True


def test_guided_route_edit_retains_source_and_unrelated_data(tmp_path):
    first = draft_bridge(
        CONFIG, BridgeRequest("left", "right", (("room", "one"),)), paths=resolve()
    )
    template = parse_yaml_config(first.yaml)
    template["runtime"]["name"] = "keep-me"

    updated = draft_bridge(
        dump_yaml_config(template),
        BridgeRequest(
            "left",
            "right",
            (("room", "two"),),
            replace_route=True,
        ),
        paths=resolve(),
    )
    original = parse_yaml_config(first.yaml)
    changed = parse_yaml_config(updated.yaml)
    assert original["routes"]["bridge"]["context_map"]["room"]["dest_context"] == "one"
    assert changed["routes"]["bridge"]["context_map"]["room"]["dest_context"] == "two"
    assert changed["runtime"]["name"] == "keep-me"
    assert changed["adapters"] == original["adapters"]


def test_guided_route_edit_refuses_advanced_route_and_unknown_id():
    initial = draft_bridge(
        CONFIG, BridgeRequest("left", "right", (("room", "one"),)), paths=resolve()
    )
    doc = parse_yaml_config(initial.yaml)

    doc["routes"]["bridge"]["priority"] = 3
    with pytest.raises(ConfigValidationError, match="not a simple guided"):
        draft_bridge(
            dump_yaml_config(doc),
            BridgeRequest("left", "right", (("room", "two"),), replace_route=True),
            paths=resolve(),
        )
    with pytest.raises(ConfigValidationError, match="missing route"):
        draft_bridge(
            initial.yaml,
            BridgeRequest(
                "left",
                "right",
                (("room", "two"),),
                route_id="missing",
                replace_route=True,
            ),
            paths=resolve(),
        )
    with pytest.raises(ConfigValidationError, match="Cannot change the endpoints"):
        draft_bridge(
            initial.yaml,
            BridgeRequest("right", "left", (("room", "two"),), replace_route=True),
            paths=resolve(),
        )
    with pytest.raises(ConfigValidationError, match="already exists"):
        draft_bridge(
            initial.yaml,
            BridgeRequest("left", "right", (("room", "two"),)),
            paths=resolve(),
        )


def test_edit_preserves_attribution_when_labels_omitted():
    first = draft_bridge(
        CONFIG,
        BridgeRequest(
            "left",
            "right",
            (("room", "one"),),
            source_origin_label="Shown from A",
            dest_origin_label="Shown from B",
        ),
        paths=resolve(),
    )
    edited = draft_bridge(
        first.yaml,
        BridgeRequest("left", "right", (("room", "new"),), replace_route=True),
        paths=resolve(),
    )
    route = parse_yaml_config(edited.yaml)["routes"]["bridge"]
    assert route["source_origin_label"] == "Shown from A"
    assert route["dest_origin_label"] == "Shown from B"


def test_cli_replace_never_modifies_input_file(tmp_path):
    original = draft_bridge(
        CONFIG, BridgeRequest("left", "right", (("room", "one"),)), paths=resolve()
    ).yaml
    inp = tmp_path / "in.yaml"
    out = tmp_path / "out.yaml"
    inp.write_text(original)
    _, err, code = _run_cli_raw(
        "config",
        "bridge",
        "--config",
        str(inp),
        "--output",
        str(out),
        "--source",
        "left",
        "--dest",
        "right",
        "--direction",
        "source_to_dest",
        "--route-id",
        "bridge",
        "--replace-route",
        "--map",
        "room",
        "two",
    )
    assert code == 0, err
    assert inp.read_text() == original
    assert (
        parse_yaml_config(out.read_text())["routes"]["bridge"]["context_map"]["room"][
            "dest_context"
        ]
        == "two"
    )
    if os.name == "posix":
        assert out.stat().st_mode & 0o777 == 0o600


def test_explain_uses_production_source_filters_and_stops_at_policy():
    draft = draft_bridge(
        CONFIG, BridgeRequest("left", "right", (("room", "topic"),)), paths=resolve()
    )
    config = parse_runtime_config(parse_yaml_config(draft.yaml), resolve())
    forward = explain_routes(
        config, source_adapter="left", source_context="room", event_kind="message.text"
    )
    assert len(forward.matches) == 1
    assert len(forward.excluded) == 1
    assert forward.matches[0].targets[0]["context"] == "topic"
    assert forward.matches[0].targets[0]["policy"] == "policy_allowed"
    assert forward.to_dict()["scope"] == "offline_source_matching_only"
    bad_kind = explain_routes(
        config,
        source_adapter="left",
        source_context="room",
        event_kind="message.edited",
    )
    assert not bad_kind.matches
    bad_context = explain_routes(
        config, source_adapter="left", source_context="other", event_kind="message.text"
    )
    assert not bad_context.matches
    with pytest.raises(ConfigValidationError, match="Unknown adapter"):
        explain_routes(
            config,
            source_adapter="typo",
            source_context="room",
            event_kind="message.text",
        )


def test_explain_uses_production_allowlist_and_does_not_invent_sender():
    initial = draft_bridge(
        CONFIG, BridgeRequest("left", "right", (("room", "topic"),)), paths=resolve()
    )
    doc = parse_yaml_config(initial.yaml)
    doc["routes"]["bridge"]["policy"]["sender_allowlist"] = ["approved"]

    config = parse_runtime_config(doc, resolve())
    unknown = explain_routes(
        config, source_adapter="left", source_context="room", event_kind="message.text"
    )
    assert unknown.matches[0].targets[0]["policy"] == "sender_required"
    denied = explain_routes(
        config,
        source_adapter="left",
        source_context="room",
        event_kind="message.text",
        sender_id="other",
    )
    assert denied.matches[0].targets[0]["policy"] == "policy_denied"
    assert denied.matches[0].targets[0]["reason"] == "sender_not_allowed"
    allowed = explain_routes(
        config,
        source_adapter="left",
        source_context="room",
        event_kind="message.text",
        sender_id="approved",
    )
    assert allowed.matches[0].targets[0]["policy"] == "policy_allowed"
    unrestricted = parse_runtime_config(parse_yaml_config(CONFIG), resolve())
    assert not explain_routes(
        unrestricted,
        source_adapter="left",
        source_context="room",
        event_kind="plugin.future",
    ).matches


def test_native_wizard_uses_provider_metadata_and_secret_input(tmp_path, monkeypatch):
    from medre.cli import native_commands

    output = tmp_path / "native.yaml"
    monkeypatch.setattr("sys.stdin", Terminal("Private Chat\n"))
    with (
        patch.object(native_commands.getpass, "getpass", return_value="do-not-log"),
        patch("medre.cli.bridge_commands._confirm_save", return_value=True),
    ):
        native_commands._config_native(
            Namespace(
                transport="independent_chat",
                adapter_id="new_chat",
                config=None,
                output=str(output),
            )
        )
    raw = parse_yaml_config(output.read_text())
    assert raw["adapters"]["independent_chat"]["new_chat"]["credential"] == "do-not-log"
    assert raw["adapters"]["independent_chat"]["new_chat"]["adapter_kind"] == "real"
    if os.name == "posix":
        assert output.stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / "state").exists()


def test_native_wizard_refuses_replacing_output_or_existing_adapter(
    tmp_path, monkeypatch
):
    from medre.cli import native_commands

    output = tmp_path / "native.yaml"
    output.write_text("unchanged")
    monkeypatch.setattr("sys.stdin", Terminal(""))
    with pytest.raises(SystemExit) as err:
        native_commands._config_native(
            Namespace(
                transport="independent_chat",
                adapter_id="new_chat",
                config=None,
                output=str(output),
            )
        )
    assert err.value.code == 2
    assert output.read_text() == "unchanged"
    output.unlink()
    existing = tmp_path / "input.yaml"
    existing.write_text(CONFIG)
    with pytest.raises(SystemExit) as err:
        native_commands._config_native(
            Namespace(
                transport="independent_chat",
                adapter_id="left",
                config=str(existing),
                output=str(output),
            )
        )
    assert err.value.code == 2
    assert not output.exists()


def test_native_requires_terminal_without_reading_secret(tmp_path, monkeypatch):
    from medre.cli import native_commands

    monkeypatch.setattr("sys.stdin", io.StringIO("should-not-read"))
    with pytest.raises(SystemExit) as err:
        native_commands._config_native(
            Namespace(
                transport="independent_chat",
                adapter_id="new_chat",
                config=None,
                output=str(tmp_path / "nope.yaml"),
            )
        )
    assert err.value.code == 2
    assert not (tmp_path / "nope.yaml").exists()


def test_cli_route_explain_json_is_bounded_and_machine_readable(tmp_path):
    import json

    authored = draft_bridge(
        CONFIG,
        BridgeRequest("left", "right", (("room", "topic"),)),
        paths=resolve(),
    )
    config_file = tmp_path / "bridge.yaml"
    config_file.write_text(authored.yaml)
    stdout, stderr, code = _run_cli_raw(
        "routes",
        "explain",
        "--config",
        str(config_file),
        "--source-adapter",
        "left",
        "--source-context",
        "room",
        "--event-kind",
        "message.text",
        "--json",
    )
    assert code == 0, stderr
    data = json.loads(stdout)
    assert data["matches"][0]["targets"][0]["adapter"] == "right"
    assert data["scope"] == "offline_source_matching_only"
    assert (
        "not delivery promises" in data["caveat"]
        or "do not prove delivery" in data["caveat"]
    )


def test_explain_marks_disabled_and_unknown_kind_suppression():
    draft = draft_bridge(
        CONFIG,
        BridgeRequest("left", "right", (("room", "topic"),)),
        paths=resolve(),
    )
    raw = parse_yaml_config(draft.yaml)
    raw["routes"]["disabled"] = {
        "source_adapters": ["left"],
        "dest_adapters": ["right"],
        "enabled": False,
    }
    config = parse_runtime_config(raw, resolve())
    known = explain_routes(
        config, source_adapter="left", source_context="room", event_kind="message.text"
    )
    assert any(leg.reason == "route disabled" for leg in known.excluded)
    # Removing the event-type allowlist gives an unregistered kind a source match,
    # but the capability resolver still suppresses it before delivery.
    raw["routes"]["bridge"]["policy"] = {}
    config = parse_runtime_config(raw, resolve())
    unknown = explain_routes(
        config,
        source_adapter="left",
        source_context="room",
        event_kind="plugin.unregistered",
    )
    assert unknown.matches[0].targets[0]["policy"] == "unsupported_kind"


def test_native_validation_error_redacts_secret(tmp_path, monkeypatch, capsys):
    from medre.cli import native_commands

    def reject(*args, **kwargs):
        raise ValueError("credential do-not-log is invalid")

    monkeypatch.setattr(native_commands, "BridgeAuthoring", reject)
    monkeypatch.setattr("sys.stdin", Terminal("Private Chat\n"))
    with patch.object(native_commands.getpass, "getpass", return_value="do-not-log"):
        with pytest.raises(SystemExit) as err:
            native_commands._config_native(
                Namespace(
                    transport="independent_chat",
                    adapter_id="new_chat",
                    config=None,
                    output=str(tmp_path / "native.yaml"),
                )
            )
    assert err.value.code == 2
    assert "do-not-log" not in capsys.readouterr().err
    assert not (tmp_path / "native.yaml").exists()


def test_native_form_rejects_unknown_or_undeclared_secret(monkeypatch, tmp_path):
    from medre.cli import native_commands

    initial = adapter_registry.get_adapter_spec("independent_chat")
    assert initial is not None
    for invalid in (
        NativeSetupField("not_a_real_field", "No such field"),
        NativeSetupField("credential", "Sensitive but undeclared?", secret=True),
    ):
        # The provider contract must reject an undeclared secret, not just a
        # syntactically valid field. Other genuine secrets stay declared.
        spec = replace(
            initial,
            native_setup_fields=(invalid,),
            support_secret_fields=(),
        )
        monkeypatch.setattr(
            adapter_registry,
            "ADAPTER_REGISTRY",
            adapter_registry.AdapterTypeRegistry((spec,)),
        )
        monkeypatch.setattr("sys.stdin", Terminal("unused\n"))
        with pytest.raises(SystemExit) as err:
            native_commands._config_native(
                Namespace(
                    transport="independent_chat",
                    adapter_id="bot",
                    config=None,
                    output=str(tmp_path / "blocked.yaml"),
                )
            )
        assert err.value.code == 2
        assert not (tmp_path / "blocked.yaml").exists()


def test_replacement_precheck_refuses_advanced_route_without_context_input():
    from medre.runtime.bridge_setup import BridgeAuthoring

    first = draft_bridge(
        CONFIG, BridgeRequest("left", "right", (("room", "one"),)), paths=resolve()
    )
    document = parse_yaml_config(first.yaml)
    document["routes"]["bridge"]["priority"] = 7
    authoring = BridgeAuthoring(dump_yaml_config(document), paths=resolve())
    with pytest.raises(ConfigValidationError, match="not a simple guided"):
        authoring.precheck(
            BridgeRequest("left", "right", (), replace_route=True),
            require_contexts=False,
        )
    with pytest.raises(ConfigValidationError, match="Cannot change the endpoints"):
        BridgeAuthoring(first.yaml, paths=resolve()).precheck(
            BridgeRequest("right", "left", (), replace_route=True),
            require_contexts=False,
        )


def test_interactive_replacement_keeps_one_way_default(monkeypatch, tmp_path):
    from tests.helpers.cli import _run_cli_raw

    first = draft_bridge(
        CONFIG,
        BridgeRequest(
            "left",
            "right",
            (("room", "one"),),
            directionality=RouteDirectionality.SOURCE_TO_DEST,
        ),
        paths=resolve(),
    )
    inp = tmp_path / "input.yaml"
    inp.write_text(first.yaml)
    out = tmp_path / "updated.yaml"
    monkeypatch.setattr("sys.stdin", Terminal("left\nright\n\nroom\ntwo\n\n\n\nyes\n"))
    _, stderr, code = _run_cli_raw(
        "config",
        "bridge",
        "--config",
        str(inp),
        "--output",
        str(out),
        "--replace-route",
        "--route-id",
        "bridge",
    )
    assert code == 0, stderr
    new = parse_yaml_config(out.read_text())
    assert new["routes"]["bridge"]["directionality"] == "source_to_dest"
    assert inp.read_text() == first.yaml


def test_native_next_step_paths_are_unused_and_distinct(tmp_path):
    from medre.cli.native_commands import _next_output_path

    existing = tmp_path / "bridge.yaml"
    existing.write_text("original")
    (tmp_path / "bridge-bridge.yaml").write_text("other")
    proposed = _next_output_path(existing, "bridge")
    assert proposed.name == "bridge-bridge-2.yaml"
    assert not proposed.exists()
    assert (
        _next_output_path(tmp_path / "native.yaml", "next").name == "native-next.yaml"
    )


def test_route_explanation_text_closes_destination_descriptor(tmp_path):
    initial = draft_bridge(
        CONFIG, BridgeRequest("left", "right", (("room", "topic"),)), paths=resolve()
    )
    config_file = tmp_path / "routes.yaml"
    config_file.write_text(initial.yaml)
    stdout, stderr, code = _run_cli_raw(
        "routes",
        "explain",
        "--config",
        str(config_file),
        "--source-adapter",
        "left",
        "--source-context",
        "room",
    )
    assert code == 0, stderr
    assert "right (context: topic)" in stdout
