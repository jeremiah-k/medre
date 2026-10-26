"""Source exports must contain pinned Git objects and exact, clean checkouts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.verify_stack_sources import inspect_stack, main


def _git(repo: Path, *args: str) -> str:
    """Run a local fixture command and surface setup failures rather than masking them."""
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo: Path, message: str) -> str:
    """Create an attributable fixture commit and return its exact immutable identity."""
    _git(
        repo,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "--allow-empty",
        "-m",
        message,
    )
    return _git(repo, "rev-parse", "HEAD")


def _stack(root: Path, *, missing_pin: bool = False):
    """Create independently committed core and provider repositories plus their export."""
    core = root / "medre"
    core.joinpath(".github").mkdir(parents=True)
    repo = root / "medre-adapter-simplexchat"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "source.txt").write_text("qualified provider\n")
    _git(repo, "add", "source.txt")
    pinned = _commit(repo, "existing provider")
    current = _commit(repo, "native owner fixes")
    (core / ".github/adapter-compatibility.json").write_text(
        json.dumps(
            [
                {
                    "transport": "simplexchat",
                    "repository": "test/medre-adapter-simplexchat",
                    "ref": "0" * 40 if missing_pin else pinned,
                    "extras": "",
                    "local_integration": False,
                }
            ]
        )
    )
    _git(core, "init", "-q")
    _git(core, "add", ".github/adapter-compatibility.json")
    core_ref = _commit(core, "qualified compatibility snapshot")
    (root / "manifest.tsv").write_text(
        "repo\tbundle\tsha\tbranch\n"
        f"medre\tcore.bundle\t{core_ref}\tmain\n"
        f"medre-adapter-simplexchat\tfixture.bundle\t{current}\tmain\n"
    )
    _git(core, "bundle", "create", str(root / "core.bundle"), "HEAD")
    _git(repo, "bundle", "create", str(root / "fixture.bundle"), "HEAD")
    return core, repo, pinned, current


def test_newer_checkout_with_pinned_git_object_is_reported(tmp_path):
    """A complete export may retain an older qualified pin without moving its checkout."""
    _stack(tmp_path)
    records = inspect_stack(tmp_path)
    provider = next(row for row in records if row["transport"] == "simplexchat")
    assert provider["pinned_object_present"]
    assert not provider["checkout_at_pinned_ref"]
    assert provider["checkout_at_exported_ref"]
    assert provider["bundle_verified"]
    assert main([str(tmp_path)]) == 0
    assert main([str(tmp_path), "--require-pinned-heads"]) == 1


def test_export_missing_pinned_object_fails(tmp_path):
    """An absent qualified revision prevents an export from claiming reproducibility."""
    _stack(tmp_path, missing_pin=True)
    provider = next(
        row for row in inspect_stack(tmp_path) if row["transport"] == "simplexchat"
    )
    assert not provider["pinned_object_present"]
    assert main([str(tmp_path)]) == 1


@pytest.mark.parametrize("defect", ["empty_pins", "duplicate_row", "wrong_header"])
def test_ambiguous_or_empty_export_cannot_pass(tmp_path, defect):
    """Malformed manifests fail before repository inspection can produce a green report."""
    core, _, _, _ = _stack(tmp_path)
    manifest = tmp_path / "manifest.tsv"
    if defect == "empty_pins":
        (core / ".github/adapter-compatibility.json").write_text("[]")
    elif defect == "duplicate_row":
        manifest.write_text(
            manifest.read_text() + manifest.read_text().splitlines()[-1] + "\n"
        )
    else:
        manifest.write_text(
            manifest.read_text().replace(
                "repo\tbundle\tsha\tbranch", "unrelated\theader\tvalues\there"
            )
        )
    assert main([str(tmp_path)]) == 2


def test_core_export_identity_is_checked(tmp_path):
    """The manifest must identify core itself, not just its provider checkouts."""
    core, _, _, _ = _stack(tmp_path)
    _commit(core, "different core checkout")
    assert main([str(tmp_path)]) == 1


def test_dirty_provider_does_not_claim_exact_exported_source(tmp_path):
    """An unchanged HEAD does not prove that its working source is the exported revision."""
    _, repo, _, _ = _stack(tmp_path)
    (repo / "source.txt").write_text("modified after export\n")
    assert main([str(tmp_path)]) == 1


def test_documented_script_runs_outside_repository(tmp_path):
    """Direct invocation must resolve its shared manifest validator without pytest."""
    _stack(tmp_path)
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    script = Path(__file__).resolve().parents[1] / "scripts/verify_stack_sources.py"
    result = subprocess.run(
        [sys.executable, str(script), str(tmp_path)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr


def test_checkout_cleanliness_respects_git_text_normalization(tmp_path, monkeypatch):
    """Windows-style line endings remain clean under their configured Git policy."""
    configuration = tmp_path / "fixture-gitconfig"
    configuration.write_text("[core]\n\tautocrlf = true\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(configuration))
    _, repo, _, _ = _stack(tmp_path)
    (repo / "source.txt").write_bytes(b"qualified provider\r\n")
    blob = _git(repo, "rev-parse", "HEAD:source.txt")
    # Invalidate cached file statistics so both policies inspect actual bytes.
    _git(repo, "update-index", "--cacheinfo", "100644," + blob + ",source.txt")
    monkeypatch.setenv("GIT_OPTIONAL_LOCKS", "0")
    assert not _git(repo, "status", "--porcelain")
    provider = next(
        row for row in inspect_stack(tmp_path) if row["transport"] == "simplexchat"
    )
    assert provider["bundle_verified"]
    assert provider["checkout_clean"]


@pytest.mark.parametrize(
    "defect", ["missing", "corrupt", "incremental", "unrelated", "missing_blob"]
)
def test_bundle_must_reproduce_objects_without_checkout_assistance(
    tmp_path, defect, monkeypatch
):
    """A healthy checkout cannot conceal an incomplete or absent export artifact."""
    _, repo, pinned, _ = _stack(tmp_path)
    bundle = tmp_path / "fixture.bundle"
    if defect == "missing":
        bundle.unlink()
    elif defect == "corrupt":
        bundle.write_bytes(bundle.read_bytes()[:-15])
    elif defect == "incremental":
        _git(repo, "bundle", "create", str(bundle), pinned + "..HEAD")
    elif defect == "missing_blob":
        # Keep the commit and tree objects while omitting a referenced source
        # blob. Commit presence alone cannot prove this artifact is complete.
        header = bundle.read_bytes().split(b"\n\n", 1)[0] + b"\n\n"
        objects = [
            line.split()[0]
            for line in _git(repo, "rev-list", "--objects", "HEAD").splitlines()
        ]
        retained = [
            oid for oid in objects if _git(repo, "cat-file", "-t", oid) != "blob"
        ]
        pack = subprocess.run(
            ["git", "-C", str(repo), "pack-objects", "--stdout"],
            input=("\n".join(retained) + "\n").encode(),
            capture_output=True,
            check=True,
        ).stdout
        bundle.write_bytes(header + pack)
    else:
        unrelated = tmp_path / "unrelated-source"
        unrelated.mkdir()
        _git(unrelated, "init", "-q")
        _commit(unrelated, "unrelated source")
        _git(unrelated, "bundle", "create", str(bundle), "HEAD")
    # Even a complete ambient object database cannot repair the bundle.
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", str(repo / ".git/objects"))
    record = next(
        row for row in inspect_stack(tmp_path) if row["transport"] == "simplexchat"
    )
    assert record["pinned_object_present"]
    assert not record["bundle_verified"]
    assert main([str(tmp_path)]) == 1
