"""Native campaign gating and independent receipt assertions."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
import yaml

from scripts.adapter_campaign.plan import TRANSPORTS, load_plan
from scripts.adapter_campaign.run import native_receipt


def manifest(tmp_path, **overrides):
    data = {
        "version": 1,
        "runtime_config": "private-runtime.yaml",
        "endpoints": {
            name: {
                "transport": name,
                "channel": "test",
                "expected_sender": "bot",
                "expected_prefixes": {
                    source: "" for source in TRANSPORTS if source != name
                },
                "peer": {},
            }
            for name in TRANSPORTS
        },
        **overrides,
    }
    path = tmp_path / "campaign.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def test_full_plan_names_all_forty_two_directed_pairs(tmp_path):
    plan = load_plan(manifest(tmp_path))
    assert plan.full
    assert len(plan.pairs) == len(set(plan.pairs)) == 42
    assert all(source != target for source, target in plan.pairs)
    assert plan.runtime_file == tmp_path / "private-runtime.yaml"


@pytest.mark.parametrize("version", [True, "1", None, 2])
def test_manifest_version_requires_integer_one(tmp_path, version):
    with pytest.raises(ValueError, match="version 1"):
        load_plan(manifest(tmp_path, version=version))


def test_partial_plan_requires_explicit_selection(tmp_path):
    path = manifest(tmp_path)
    data = yaml.safe_load(path.read_text())
    del data["endpoints"]["chatmail"]
    for entry in data["endpoints"].values():
        entry["expected_prefixes"].pop("chatmail", None)
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match="all seven"):
        load_plan(path)
    plan = load_plan(path, {"matrix", "xmpp"})
    assert not plan.full
    assert set(plan.pairs) == {("matrix", "xmpp"), ("xmpp", "matrix")}


@pytest.mark.parametrize(
    "field,value",
    [
        ("timeout_seconds", float("nan")),
        ("timeout_seconds", True),
        ("pacing_seconds", 0),
        ("settle_seconds", -1),
        ("timeout_seconds", 301),
    ],
)
def test_unbounded_or_unpaced_radio_plan_is_rejected(tmp_path, field, value):
    with pytest.raises(ValueError, match="seconds"):
        load_plan(manifest(tmp_path, **{field: value}))


@pytest.mark.parametrize("selected", [{"matrix"}, {"matrix", "unknown"}])
def test_invalid_partial_transport_selection_is_rejected(tmp_path, selected):
    with pytest.raises(ValueError):
        load_plan(manifest(tmp_path), selected)


def test_duplicate_transport_endpoints_do_not_qualify_as_full_coverage(tmp_path):
    path = manifest(tmp_path)
    data = yaml.safe_load(path.read_text())
    data["endpoints"]["matrix2"] = {**deepcopy(data["endpoints"]["matrix"]), "peer": {}}
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match="exactly one"):
        load_plan(path)


@pytest.mark.parametrize("change", ["sender", "channel", "duplicate", "body"])
def test_native_delivery_requires_expected_sender_scope_and_body(change):
    record = {
        "kind": "received",
        "sender": "bot",
        "channel": "room",
        "text": "MR-proof café ✓",
    }
    endpoint = {
        "transport": "matrix",
        "expected_sender": "bot",
        "channel": "room",
        "peer": {},
    }
    records = [record]
    if change == "duplicate":
        records.append(dict(record))
    elif change == "body":
        record["text"] = "MR-proof missing UTF-8 body"
    else:
        record[change] = "other"
    with pytest.raises(AssertionError):
        native_receipt(
            SimpleNamespace(name="matrix", records=records), endpoint, "MR-proof"
        )


def test_cli_displays_only_harness_authored_validation_messages(
    tmp_path, capsys, monkeypatch
):
    from scripts.adapter_campaign import __main__ as cli

    path = manifest(tmp_path, timeout_seconds=0)
    assert cli.main(["--config", str(path)]) == 1
    assert "timeout_seconds must be finite" in capsys.readouterr().out

    def parser_failure(*args):
        raise ValueError("access_token=private-fixture")

    monkeypatch.setattr(cli, "load_plan", parser_failure)
    assert cli.main(["--config", str(path)]) == 1
    assert "private-fixture" not in capsys.readouterr().out


def test_simplex_bot_and_peer_cannot_share_native_database(tmp_path):
    from scripts.adapter_campaign.plan import runtime_config

    fixture = tmp_path / "lab"
    fixture.mkdir()
    path = manifest(fixture)
    data = yaml.safe_load(path.read_text())
    data["endpoints"] = {
        name: value
        for name, value in data["endpoints"].items()
        if name in {"simplexchat", "xmpp"}
    }
    for endpoint in data["endpoints"].values():
        endpoint["expected_prefixes"] = {source: "" for source in data["endpoints"]}
    data["endpoints"]["simplexchat"]["peer"] = {"database_prefix": "/private/shared"}
    path.write_text(yaml.safe_dump(data))
    # Reject ownership overlap before a native peer can touch the profile.
    (fixture / "private-runtime.yaml").write_text(
        yaml.safe_dump(
            {
                "adapters": {
                    "simplexchat": {
                        "simplexchat": {
                            "adapter_kind": "real",
                            "database_prefix": "/private/shared",
                        }
                    },
                    "xmpp": {"xmpp": {"adapter_kind": "real"}},
                }
            }
        )
    )
    plan = load_plan(path, {"simplexchat", "xmpp"})
    assert plan.runtime_file == fixture / "private-runtime.yaml"
    with pytest.raises(ValueError, match="separate native profiles"):
        runtime_config(plan, tmp_path / "run")
