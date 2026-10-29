"""A source export cannot substitute current branches for immutable pins."""

import json
import subprocess

import pytest

from scripts.export_qualified_stack import export


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


@pytest.fixture
def source(tmp_path):
    core = tmp_path / "source" / "medre"
    provider = tmp_path / "source" / "medre-adapter-fake"
    for repo in (core, provider):
        repo.mkdir(parents=True)
        git(repo, "init", "-q")
        git(repo, "config", "user.name", "Test")
        git(repo, "config", "user.email", "test@example.invalid")
        (repo / "README.md").write_text("fixture")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "fixture")
    pin = git(provider, "rev-parse", "HEAD")
    (core / ".github").mkdir()
    (core / ".github" / "adapter-compatibility.json").write_text(
        json.dumps(
            [
                {
                    "transport": "fake",
                    "repository": "test/medre-adapter-fake",
                    "ref": pin,
                    "extras": "",
                    "local_integration": False,
                }
            ]
        )
    )
    git(core, "add", ".")
    git(core, "commit", "-qm", "select pin")
    return core, provider, pin


def test_exact_pins_export_to_standalone_git_bundles(tmp_path, source):
    core, provider, pin = source
    # A later feature-branch commit cannot replace the selected provider.
    (provider / "README.md").write_text("candidate")
    git(provider, "add", ".")
    git(provider, "commit", "-qm", "candidate")
    output = tmp_path / "qualified"
    data = export(tmp_path / "source", output)
    provider_record = next(x for x in data["repositories"] if x["transport"] == "fake")
    assert provider_record["pinned_commit"] == pin
    assert provider_record["checkout_matches_pin"] is False
    assert (output / "bundles" / "medre-adapter-fake.bundle").is_file()
    assert output.exists()
    with pytest.raises(ValueError, match="already exists"):
        export(tmp_path / "source", output)


def test_missing_pin_fails_before_any_output(tmp_path, source):
    core, _, _ = source
    manifest = core / ".github/adapter-compatibility.json"
    entries = json.loads(manifest.read_text())
    entries[0]["ref"] = "a" * 40
    manifest.write_text(json.dumps(entries))
    git(core, "add", ".")
    git(core, "commit", "-qm", "absent pin")
    with pytest.raises(ValueError, match="pinned commit missing.*medre-adapter-fake"):
        export(tmp_path / "source", tmp_path / "result")
    assert not (tmp_path / "result").exists()


@pytest.mark.parametrize("ref", ["refs/heads/main", "refs/tags/release"])
def test_moving_refs_are_refused_before_export(tmp_path, source, ref):
    core, provider, pin = source
    git(provider, "update-ref", ref, pin)
    manifest = core / ".github/adapter-compatibility.json"
    entries = json.loads(manifest.read_text())
    entries[0]["ref"] = ref
    manifest.write_text(json.dumps(entries))
    git(core, "add", ".")
    git(core, "commit", "-qm", "moving ref")
    with pytest.raises(ValueError, match="immutable"):
        export(tmp_path / "source", tmp_path / "qualified")
    assert not (tmp_path / "qualified").exists()


def test_manifest_is_read_from_selected_core_commit(tmp_path, source):
    core, _, pin = source
    # Git status can hide local edits; checkout bytes cannot select release pins.
    git(
        core, "update-index", "--assume-unchanged", ".github/adapter-compatibility.json"
    )
    (core / ".github/adapter-compatibility.json").write_text("[]")
    data = export(tmp_path / "source", tmp_path / "qualified")
    assert len(data["repositories"]) == 2
    assert data["repositories"][1]["pinned_commit"] == pin


def test_competing_output_directory_is_preserved(tmp_path, source, monkeypatch):
    from scripts import export_qualified_stack as exporter

    output = tmp_path / "qualified"
    original_git = exporter._git
    verifications = 0

    def competing_git(directory, *args, **kwargs):
        nonlocal verifications
        result = original_git(directory, *args, **kwargs)
        if args[:2] == ("bundle", "verify"):
            verifications += 1
            if verifications == 2:
                output.mkdir()
                (output / "keep").write_text("another exporter owns this directory")
        return result

    monkeypatch.setattr(exporter, "_git", competing_git)
    with pytest.raises(OSError):
        export(tmp_path / "source", output)
    assert sorted(path.name for path in output.iterdir()) == ["keep"]


@pytest.mark.parametrize("untracked", [False, True])
def test_dirty_provider_refuses_publication(tmp_path, source, untracked):
    _, provider, _ = source
    (provider / ("untracked.txt" if untracked else "README.md")).write_text("dirty")
    with pytest.raises(ValueError, match="Checkout is dirty: medre-adapter-fake"):
        export(tmp_path / "source", tmp_path / "result")
    assert not (tmp_path / "result").exists()


def test_empty_manifest_is_refused_by_shared_resolver(tmp_path, source):
    core, _, _ = source
    (core / ".github/adapter-compatibility.json").write_text("[]")
    git(core, "add", ".")
    git(core, "commit", "-qm", "empty manifest")
    with pytest.raises(ValueError, match="nonempty list"):
        export(tmp_path / "source", tmp_path / "result")
    assert not (tmp_path / "result").exists()


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_invalid_operation_timeout_refuses_before_export(tmp_path, timeout):
    with pytest.raises(ValueError, match="finite and positive"):
        export(tmp_path / "missing", tmp_path / "result", git_timeout=timeout)
    assert not (tmp_path / "result").exists()


def test_cli_git_failure_includes_stderr(monkeypatch, capsys):
    import sys

    from scripts import export_qualified_stack as exporter

    def refuse(*args, **kwargs):
        raise subprocess.CalledProcessError(
            128, ["git", "fetch"], stderr="fatal: fixture reason"
        )

    monkeypatch.setattr(exporter, "export", refuse)
    monkeypatch.setattr(sys, "argv", ["export", "source", "output"])
    with pytest.raises(SystemExit) as exc:
        exporter.main()
    assert exc.value.code == 2
    assert "fatal: fixture reason" in capsys.readouterr().err
