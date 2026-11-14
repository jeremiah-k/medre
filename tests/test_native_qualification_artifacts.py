"""Qualification artifacts fail on provenance drift without touching native SDKs."""

from __future__ import annotations

import copy
import hashlib
import importlib.metadata
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.ci import native_qualification as qualification
from scripts.ci.native_qualification import build_record, check_record


def _commit(root: Path) -> str:
    subprocess.run(
        ["git", "-C", str(root), "add", "."], check=True, capture_output=True
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Qualification Test",
            "-c",
            "user.email=proof@example.invalid",
            "commit",
            "-qm",
            "proof fixture",
        ],
        check=True,
        capture_output=True,
    )
    return subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()


@pytest.fixture
def proof(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root, provider = tmp_path / "core", tmp_path / "provider checkout"
    for repository in (root, provider):
        repository.mkdir()
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
        (repository / "harness.py").write_text("# fixture harness\n")
    (provider / "pyproject.toml").write_text(
        '[project]\nname="medre-adapter-meshcore"\ndependencies=["medre>=0.1", "meshcore==1.2.3"]\n'
    )
    revision = _commit(provider)
    (root / ".github").mkdir()
    (root / ".github/adapter-compatibility.json").write_text(
        json.dumps(
            [
                {
                    "transport": "meshcore",
                    "repository": "owner/medre-adapter-meshcore",
                    "ref": revision,
                    "distribution": "medre-adapter-meshcore",
                    "extras": "",
                    "local_integration": True,
                },
                {
                    "transport": "matrix",
                    "repository": "owner/medre-adapter-matrix",
                    "ref": "c" * 40,
                    "distribution": "medre-adapter-matrix",
                    "extras": "",
                    "local_integration": False,
                },
            ]
        )
    )
    schema = root / "docs/schemas/native-qualification.schema.json"
    schema.parent.mkdir(parents=True)
    schema.write_bytes(
        (Path(__file__).resolve().parents[1] / schema.relative_to(root)).read_bytes()
    )
    _commit(root)
    real_version = importlib.metadata.version
    real_distribution = importlib.metadata.distribution
    monkeypatch.setattr(
        importlib.metadata,
        "version",
        lambda name: "1.2.3" if name == "meshcore" else real_version(name),
    )
    monkeypatch.setattr(
        importlib.metadata,
        "distribution",
        lambda name: (
            SimpleNamespace(
                read_text=lambda _: json.dumps(
                    {"url": provider.as_uri(), "dir_info": {"editable": True}}
                )
            )
            if name == "medre-adapter-meshcore"
            else real_distribution(name)
        ),
    )
    for name in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"):
        monkeypatch.delenv(name, raising=False)
    observation = {
        "event_id": "evt",
        "delivery_plan_id": "plan",
        "target_adapter": "radio",
        "target_channel": "2",
        "outbox_id": "outbox",
        "attempt_number": 1,
        "receipt_id": "receipt",
        "native_message_id": None,
        "scope": "provider_acceptance",
        "tier": "local_integration",
        "result": "accepted",
        "evidence_fingerprint": "b" * 64,
    }
    record = build_record(
        root,
        provider,
        transport="meshcore",
        observation=observation,
        binding=("current", "exact"),
        observer_kind="companion_protocol",
        witness_hashes=["a" * 64],
        harnesses=[("core", "harness.py"), ("provider", "harness.py")],
    )
    return root, provider, record


def test_clean_pinned_proof_is_reproducible(proof):
    root, provider, record = proof
    assert check_record(root, provider, record) == []
    assert record["core"]["dirty"] is False
    assert record["provider"]["sdk_versions"] == {"meshcore": "1.2.3"}
    assert record["ci"] == {"run_id": None, "run_attempt": None}
    assert record["observation"]["native_message_id"] is None


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ("core_revision", "core revision differs"),
        ("provider_revision", "provider or SDK provenance differs"),
        ("sdk", "provider or SDK provenance differs"),
        ("witness_count", "observer count differs"),
        ("recipient_claim", "claim scope"),
        ("invented_recipient", "does not support this observer kind"),
        ("extra_content", "qualification schema"),
        ("digest", "record digest differs"),
        ("traversal", "escapes its repository"),
        ("missing_provider_harness", "both core and provider harnesses"),
    ],
)
def test_altered_proof_is_refused(proof, change: str, expected: str):
    root, provider, original = proof
    record = copy.deepcopy(original)
    if change == "core_revision":
        record["core"]["revision"] = "c" * 40
    elif change == "provider_revision":
        record["provider"]["revision"] = "c" * 40
    elif change == "sdk":
        record["provider"]["sdk_versions"]["meshcore"] = "0.0.0"
    elif change == "witness_count":
        record["observer"]["count"] = 2
    elif change == "recipient_claim":
        record["observation"]["scope"] = "recipient_observation"
    elif change == "invented_recipient":
        record["observation"]["scope"] = "recipient_observation"
        record["observer"]["kind"] = "recipient_callback"
    elif change == "extra_content":
        record["observation"]["body"] = "must never be included"
    elif change == "digest":
        record["record_sha256"] = "c" * 64
    elif change == "traversal":
        record["harnesses"][0]["path"] = "../provider/harness.py"
    else:
        record["harnesses"][1] = {
            **record["harnesses"][0],
            "path": "docs/schemas/native-qualification.schema.json",
        }
    if change != "digest":
        # Recompute the digest of false declarations to exercise the
        # independent provenance checks, rather than only the digest check.
        unsigned = {
            key: value for key, value in record.items() if key != "record_sha256"
        }
        record["record_sha256"] = hashlib.sha256(
            json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    assert any(expected in error for error in check_record(root, provider, record))


def test_core_harness_bytes_are_digest_checked(proof):
    root, provider, record = proof
    (root / "harness.py").write_text("# changed after observation\n")
    assert "harness digest differs" in check_record(
        root, provider, record, require_clean=False
    )


def test_modified_provider_checkout_invalidates_a_record(proof):
    root, provider, record = proof
    (provider / "harness.py").write_text("# changed after observation\n")
    assert any(
        "modified or untracked" in error
        for error in check_record(root, provider, record)
    )


def test_ci_execution_identity_cannot_be_reused(proof, monkeypatch: pytest.MonkeyPatch):
    root, provider, record = proof
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    assert "CI execution identity differs" in check_record(root, provider, record)


def test_dirty_local_record_cannot_be_promoted_to_ci_proof(proof):
    root, provider, record = proof
    record["core"]["dirty"] = True
    assert "qualification requires clean core source" in check_record(
        root, provider, record
    )


@pytest.mark.parametrize("layout", ["symlink", "duplicate", "missing"])
def test_cli_distinguishes_aliases_duplicates_and_missing_records(
    proof, monkeypatch: pytest.MonkeyPatch, layout: str
):
    root, provider, record = proof
    numbered = root / "record-0.json"
    numbered.write_text(json.dumps(record))
    alias = root / "record-current.json"
    if layout == "symlink":
        try:
            alias.symlink_to(numbered)
        except OSError:
            if os.name == "nt":
                pytest.skip("Windows runner lacks symlink creation privileges")
            raise
    elif layout == "duplicate":
        alias.write_text(json.dumps(record))
    monkeypatch.setattr(
        qualification, "__file__", str(root / "scripts/ci/native_qualification.py")
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "native_qualification.py",
            str(root / "record-*.json"),
            *([str(root / "absent-*.json")] if layout == "missing" else []),
            "--provider",
            str(provider),
            "--expect-records",
            "2" if layout == "duplicate" else "1",
        ],
    )
    if layout == "symlink":
        qualification.main()
    else:
        with pytest.raises(
            SystemExit,
            match="Duplicate" if layout == "duplicate" else "No qualification",
        ):
            qualification.main()


def test_installed_sdk_mismatch_refuses_record(proof, monkeypatch):
    root, provider, record = proof
    real_version = importlib.metadata.version
    monkeypatch.setattr(
        importlib.metadata,
        "version",
        lambda name: "1.2.4" if name == "meshcore" else real_version(name),
    )
    assert "installed SDK differs from the provider requirement" in check_record(
        root, provider, record
    )


def test_candidate_record_requires_explicit_matching_snapshot(
    proof, monkeypatch, capsys
):
    root, provider, baseline = proof
    (provider / "harness.py").write_text("# candidate harness\n")
    candidate_sha = _commit(provider)
    refs = json.dumps({"meshcore": candidate_sha, "matrix": "c" * 40})
    record = build_record(
        root,
        provider,
        transport="meshcore",
        observation=baseline["observation"],
        observer_kind="companion_protocol",
        witness_hashes=["a" * 64],
        harnesses=[("core", "harness.py"), ("provider", "harness.py")],
        binding=("current", "exact"),
        candidate_refs=refs,
    )
    assert record["provider"]["revision"] == candidate_sha
    assert check_record(root, provider, record, candidate_refs=refs) == []
    assert (
        "candidate qualification requires the same explicit snapshot"
        in check_record(root, provider, record)
    )
    assert check_record(root, provider, record, candidate_refs="{}")
    assert check_record(
        root,
        provider,
        record,
        candidate_refs=json.dumps(
            {"meshcore": baseline["provider"]["revision"], "matrix": "c" * 40}
        ),
    )
    # A CI environment selects the fixture's candidate; it cannot silently
    # change the standalone verifier's default committed-pin authority.
    monkeypatch.setenv("MEDRE_CI_CANDIDATE_REFS", refs)
    assert (
        "candidate qualification requires the same explicit snapshot"
        in check_record(root, provider, record)
    )
    path = root / "candidate.json"
    path.write_text(json.dumps(record))
    monkeypatch.setattr(
        qualification, "__file__", str(root / "scripts/ci/native_qualification.py")
    )
    argv = [
        "native_qualification.py",
        str(path),
        "--provider",
        str(provider),
        "--expect-records",
        "1",
    ]
    monkeypatch.setattr("sys.argv", argv)
    with pytest.raises(SystemExit, match="same explicit snapshot"):
        qualification.main()
    monkeypatch.setattr("sys.argv", [*argv, "--candidate-refs", refs])
    qualification.main()
    assert "Verified 1" in capsys.readouterr().out


def test_candidate_identity_binds_other_transports_and_explicit_mode(proof):
    root, provider, baseline = proof
    # The observed provider remains at its committed SHA; only another
    # transport differs. Candidate proof must still be distinguishable.
    refs = {"meshcore": baseline["provider"]["revision"], "matrix": "d" * 40}
    record = build_record(
        root,
        provider,
        transport="meshcore",
        observation=baseline["observation"],
        observer_kind="companion_protocol",
        witness_hashes=["a" * 64],
        harnesses=[("core", "harness.py"), ("provider", "harness.py")],
        binding=("current", "exact"),
        candidate_refs=json.dumps(refs),
    )
    assert record["schema_version"] == 2
    assert check_record(root, provider, record, candidate_refs=json.dumps(refs)) == []
    assert (
        check_record(
            root,
            provider,
            record,
            candidate_refs=json.dumps(dict(reversed(list(refs.items())))),
        )
        == []
    )
    changed = json.dumps({**refs, "matrix": "e" * 40})
    assert "candidate snapshot digest differs" in check_record(
        root, provider, record, candidate_refs=changed
    )
    assert (
        "candidate qualification requires the same explicit snapshot"
        in check_record(root, provider, record)
    )
    assert (
        "committed-pin qualification cannot verify a candidate snapshot"
        in check_record(root, provider, baseline, candidate_refs=json.dumps(refs))
    )
    assert baseline["schema_version"] == 1
    assert "candidate_snapshot_sha256" not in baseline
