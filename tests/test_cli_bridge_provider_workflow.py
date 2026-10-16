# medre-test-tier: compatibility
"""Installed provider samples form a usable context-bound bridge through the CLI."""

import json
import os

import pytest

from medre.config._yaml import parse_yaml_config
from medre.config.sample import generate_sample_config
from tests.helpers.cli import _run_cli_raw


def test_guided_sample_to_both_context_specific_smokes(monkeypatch, tmp_path):
    for key in list(os.environ):
        if key.startswith(("MEDRE_", "XDG_")):
            monkeypatch.delenv(key)
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path / "state"))
    adapters = parse_yaml_config(generate_sample_config())["adapters"]
    if not {"matrix", "meshtastic"} <= adapters.keys():
        pytest.skip("Requires installed Matrix and Meshtastic sample providers")
    source = next(iter(adapters["matrix"]))
    dest = next(iter(adapters["meshtastic"]))
    path = tmp_path / "bridge.yaml"
    stdout, stderr, code = _run_cli_raw(
        "config",
        "bridge",
        "--output",
        str(path),
        "--source",
        source,
        "--dest",
        dest,
        "--direction",
        "bidirectional",
        "--map",
        "!room:example.com",
        "1",
        "--json",
    )
    assert code == 0, stderr
    expected = json.loads(stdout)
    assert expected["total_legs"] == 2
    assert {item["adapter_id"] for item in expected["adapters"]} == {source, dest}
    assert set(parse_yaml_config(path.read_text())["adapters"]) == {
        "matrix",
        "meshtastic",
    }
    assert not (tmp_path / "state").exists()
    _, stderr, code = _run_cli_raw("config", "check", "--config", str(path))
    assert code == 0, stderr
    stdout, stderr, code = _run_cli_raw(
        "routes", "plan", "--config", str(path), "--json"
    )
    assert code == 0 and json.loads(stdout) == expected, stderr
    for adapter, context, target in (
        (source, "!room:example.com", dest),
        (dest, "1", source),
    ):
        stdout, stderr, code = _run_cli_raw(
            "smoke",
            "--config",
            str(path),
            "--source-adapter",
            adapter,
            "--source-context",
            context,
            "--json",
        )
        report = json.loads(stdout)
        assert code == 0 and report["status"] == "passed", report
        assert report["source_adapter"] == adapter
        assert report["source_context"] == context
        assert report["target_adapters"] == [target]
        assert report["shutdown_status"] == "stopped"
        assert (
            len([r for r in report["delivery_receipts"] if r["status"] == "sent"]) == 1
        )
