"""Candidate CI snapshots cannot promote pins or change repository identities."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/ci/adapter_matrix.py"
SPEC = importlib.util.spec_from_file_location("candidate_adapter_matrix", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_candidates_only_replace_refs_without_mutating_manifest():
    entries = MODULE.resolve_adapters(MODULE._MANIFEST, resolve=False)
    original = json.dumps(entries)
    refs = {entry["transport"]: "a" * 40 for entry in entries}
    selected = MODULE.candidate_snapshot(entries, json.dumps(refs))
    assert json.dumps(entries) == original
    assert all(entry["ref"] == "a" * 40 for entry in selected)
    for baseline, candidate in zip(entries, selected, strict=True):
        assert {k: v for k, v in baseline.items() if k != "ref"} == {
            k: v for k, v in candidate.items() if k != "ref"
        }


@pytest.mark.parametrize(
    "refs",
    [
        {},
        [],
        {"matrix": "a" * 40},
        {"matrix": "refs/heads/main", "xmpp": "a" * 40},
        {"matrix": 123, "xmpp": "a" * 40},
        {"matrix": "A" * 40, "xmpp": "a" * 40},
        {"matrix": "a" * 40, "xmpp": "a" * 40, "foreign": "a" * 40},
    ],
)
def test_candidate_snapshot_rejects_incomplete_or_moving_refs(refs):
    with pytest.raises(ValueError, match="candidate refs"):
        MODULE.candidate_snapshot(
            [{"transport": "matrix"}, {"transport": "xmpp"}], json.dumps(refs)
        )


@pytest.mark.parametrize("mode", ["--check-merged", "--check-local", "--sync-local"])
def test_candidate_refs_cannot_be_used_for_verification_or_pin_promotion(mode):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), mode, "--candidate-refs", '{"matrix":"bad"}'],
        text=True,
        capture_output=True,
    )
    assert result.returncode == 2
    assert "cannot be combined" in result.stderr


def test_candidate_cli_rejects_partial_snapshot_before_writing(tmp_path):
    output = tmp_path / "github-output"
    output.write_text("existing=unchanged\n")
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--github-output",
            str(output),
            "--candidate-refs",
            json.dumps({"matrix": "a" * 40}),
        ],
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0
    assert "candidate refs must name every manifest transport" in result.stderr
    assert output.read_text() == "existing=unchanged\n"


def test_candidate_snapshot_rejects_duplicate_transport():
    sha = "a" * 40
    with pytest.raises(ValueError, match="duplicate transport"):
        MODULE.candidate_snapshot(
            [{"transport": "matrix"}], f'{{"matrix":"{sha}","matrix":"{sha}"}}'
        )


def test_candidate_resolve_does_not_contact_replaced_baseline(
    tmp_path, monkeypatch, capsys
):
    entries = MODULE.resolve_adapters(MODULE._MANIFEST, resolve=False)
    for entry in entries:
        entry["ref"] = "refs/heads/deleted-baseline"
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(entries))
    refs = {entry["transport"]: "a" * 40 for entry in entries}
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--manifest",
            str(manifest),
            "--resolve",
            "--candidate-refs",
            json.dumps(refs),
        ],
    )

    def unexpected_resolution(*args, **kwargs):
        pytest.fail("candidate selection must not resolve replaced refs")

    monkeypatch.setattr(MODULE.subprocess, "run", unexpected_resolution)
    MODULE.main()
    selected = json.loads(capsys.readouterr().out)
    assert {
        row["transport"]: row["ref"] for row in selected["adapters"]["include"]
    } == (refs)
    assert json.loads(manifest.read_text()) == entries


def test_empty_candidate_argument_is_identical_to_omission():
    command = [sys.executable, str(SCRIPT), "--resolve"]
    baseline = subprocess.check_output(command, text=True)
    empty = subprocess.check_output([*command, "--candidate-refs", ""], text=True)
    assert json.loads(empty) == json.loads(baseline)
