"""CI selection is data-driven, validated, and reproducible within each run."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts/ci/adapter_matrix.py"
_SPEC = importlib.util.spec_from_file_location("ci_adapter_matrix", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
matrix = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(matrix)


def _entry(**overrides: object) -> dict:
    return {
        "transport": "community_sensor",
        "repository": "someone/custom-sensor",
        "ref": "refs/heads/main",
        "extras": "",
        "local_integration": False,
        **overrides,
    }


def _manifest(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "adapters.json"
    path.write_text(json.dumps(entries))
    return path


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        {"transport": "private_chat"},
        {
            "transport": 7,
            "repository": "a/b",
            "ref": "a" * 40,
            "extras": "",
            "local_integration": False,
        },
    ],
)
def test_malformed_entries_have_configuration_diagnostics(tmp_path, bad):
    with pytest.raises(ValueError):
        matrix.resolve_adapters(_manifest(tmp_path, [bad]), resolve=False)


def test_resolves_once_to_full_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commit = "a" * 40
    calls = []

    def resolve(args: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(args)
        return SimpleNamespace(stdout=f"{commit}\trefs/heads/main\n")

    monkeypatch.setattr(matrix.subprocess, "run", resolve)
    entries = matrix.resolve_adapters(_manifest(tmp_path, [_entry()]), resolve=True)
    assert entries[0]["ref"] == commit
    assert len(calls) == 1
    assert (
        matrix.requirements(entries)
        == f"git+https://github.com/someone/custom-sensor.git@{commit}\n"
    )


def test_annotated_tag_resolves_to_commit_not_tag_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commit = "b" * 40
    monkeypatch.setattr(
        matrix.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout=f"{'a'*40}\trefs/tags/v1\n{commit}\trefs/tags/v1^{{}}\n"
        ),
    )
    entries = matrix.resolve_adapters(
        _manifest(tmp_path, [_entry(ref="refs/tags/v1")]), resolve=True
    )
    assert entries[0]["ref"] == commit


def test_pinned_commit_requires_no_remote_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected(*args: object, **kwargs: object) -> None:
        raise AssertionError("already pinned source was fetched")

    monkeypatch.setattr(matrix.subprocess, "run", unexpected)
    entries = matrix.resolve_adapters(
        _manifest(tmp_path, [_entry(ref="c" * 40)]), resolve=True
    )
    assert entries[0]["ref"] == "c" * 40


@pytest.mark.parametrize(
    "entries",
    [
        [_entry(), _entry()],
        [_entry(ref="main")],
        [_entry(repository="../../bad")],
        [_entry(transport="bad; command")],
        [_entry(extras="e2e; command")],
        [_entry(local_integration="false")],
        [_entry(distribution="../path")],
        [],
    ],
)
def test_invalid_manifest_fails_before_installation(
    tmp_path: Path, entries: list[dict]
) -> None:
    with pytest.raises(ValueError):
        matrix.resolve_adapters(_manifest(tmp_path, entries), resolve=False)


def test_installer_rejects_moving_refs() -> None:
    with pytest.raises(ValueError, match="full commit SHAs"):
        matrix.requirements([_entry()])


@pytest.mark.parametrize("status", ["ahead", "identical", "behind", "diverged"])
def test_merge_gate_distinguishes_landed_pins_from_candidate_branches(
    monkeypatch, status
):
    commit = "a" * 40
    calls = []

    def lookup(args, **kwargs):
        calls.append(args)
        data = (
            {"default_branch": "stable/core"}
            if len(calls) == 1
            else {
                "status": status,
                "merge_base_commit": {
                    "sha": commit if status in ("ahead", "identical") else "b" * 40
                },
            }
        )
        return SimpleNamespace(stdout=json.dumps(data))

    monkeypatch.setattr(matrix.subprocess, "run", lookup)
    if status in ("ahead", "identical"):
        matrix.check_merged([_entry(ref=commit)])
    else:
        with pytest.raises(ValueError, match="is not merged"):
            matrix.check_merged([_entry(ref=commit)])
    assert calls[1] == [
        "gh",
        "api",
        f"repos/someone/custom-sensor/compare/{commit}...stable%2Fcore",
    ]


def test_merge_gate_rejects_temporary_branch_without_network(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("moving reference reached GitHub")

    monkeypatch.setattr(matrix.subprocess, "run", unexpected)
    with pytest.raises(ValueError, match="full commit SHAs"):
        matrix.check_merged([_entry(ref="refs/heads/test-only")])


def test_merge_gate_checks_ancestry_not_only_github_status(monkeypatch):
    def lookup(args, **kwargs):
        return SimpleNamespace(
            stdout=json.dumps(
                {"default_branch": "main"}
                if "/compare/" not in args[-1]
                else {"status": "ahead", "merge_base_commit": {"sha": "b" * 40}}
            )
        )

    monkeypatch.setattr(matrix.subprocess, "run", lookup)
    with pytest.raises(ValueError, match="is not merged"):
        matrix.check_merged([_entry(ref="a" * 40)])


def test_merge_gate_fails_closed_when_github_is_unavailable(monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError("offline")

    monkeypatch.setattr(matrix.subprocess, "run", unavailable)
    with pytest.raises(ValueError, match="Cannot verify merged adapter"):
        matrix.check_merged([_entry(ref="a" * 40)])


def _create_local_adapter(
    parent: Path, repository: str, transport: str
) -> tuple[Path, str]:
    import subprocess

    checkout = parent / repository
    checkout.mkdir()
    subprocess.run(["git", "init", "--quiet", str(checkout)], check=True)
    (checkout / "pyproject.toml").write_text(
        "[project]\n"
        f'name = "{repository}"\n'
        '[project.entry-points."medre.adapters"]\n'
        f'{transport} = "example:adapter_spec"\n'
    )
    subprocess.run(["git", "-C", str(checkout), "add", "pyproject.toml"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "fixture",
        ],
        check=True,
    )
    sha = subprocess.check_output(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True
    ).strip()
    return checkout, sha


def test_sibling_snapshot_detects_stale_pins_and_syncs_atomically(
    tmp_path: Path,
) -> None:
    checkout, sha = _create_local_adapter(
        tmp_path, "medre-adapter-example", "community_sensor"
    )
    entry = _entry(repository="someone/medre-adapter-example", ref="b" * 40)
    path = _manifest(tmp_path, [entry])
    original = path.read_bytes()
    with pytest.raises(ValueError, match="pins differ from local checkouts"):
        matrix.reconcile_local([entry], repo_parent=tmp_path)
    assert path.read_bytes() == original
    updated = matrix.reconcile_local([entry], repo_parent=tmp_path, update=True)
    assert updated[0]["ref"] == sha
    assert entry["ref"] == "b" * 40
    matrix.write_manifest_atomic(path, updated)
    assert json.loads(path.read_text())[0]["ref"] == sha
    assert matrix.reconcile_local(updated, repo_parent=tmp_path) == updated
    (checkout / "new-untracked-file").write_text("dirty")
    with pytest.raises(ValueError, match="uncommitted changes"):
        matrix.reconcile_local(updated, repo_parent=tmp_path, update=True)
    assert json.loads(path.read_text())[0]["ref"] == sha


def test_sibling_snapshot_checks_distribution_and_entry_point(tmp_path: Path) -> None:
    checkout, sha = _create_local_adapter(
        tmp_path, "medre-adapter-example", "community_sensor"
    )
    entry = _entry(
        repository="someone/medre-adapter-example",
        ref=sha,
        distribution="medre-adapter-example",
    )
    assert matrix.reconcile_local([entry], repo_parent=tmp_path) == [entry]
    (checkout / "pyproject.toml").write_text(
        '[project]\nname = "other-project"\n'
        '[project.entry-points."medre.adapters"]\n'
        'community_sensor = "example:adapter_spec"\n'
    )
    with pytest.raises(ValueError, match="distribution or entry point"):
        matrix.reconcile_local([entry], repo_parent=tmp_path)


def test_snapshot_rejects_relative_repository_slug(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="invalid compatibility repository"):
        matrix.resolve_adapters(
            _manifest(tmp_path, [_entry(repository="someone/..")]), resolve=False
        )


def test_sibling_check_allows_distribution_name_different_from_repository(
    tmp_path: Path,
) -> None:
    checkout, sha = _create_local_adapter(
        tmp_path, "medre-adapter-example", "community_sensor"
    )
    (checkout / "pyproject.toml").write_text(
        '[project]\nname = "different-distribution"\n'
        '[project.entry-points."medre.adapters"]\n'
        'community_sensor = "example:adapter_spec"\n'
    )
    import subprocess

    subprocess.run(["git", "-C", str(checkout), "add", "pyproject.toml"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "rename",
        ],
        check=True,
    )
    sha = subprocess.check_output(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True
    ).strip()
    entry = _entry(repository="someone/medre-adapter-example", ref=sha)
    assert matrix.reconcile_local([entry], repo_parent=tmp_path) == [entry]


def test_sibling_distribution_names_use_python_normalization(tmp_path: Path) -> None:
    _checkout, sha = _create_local_adapter(
        tmp_path, "medre-adapter-example", "community_sensor"
    )
    entry = _entry(
        repository="someone/medre-adapter-example",
        ref=sha,
        distribution="MEDRE_adapter.example",
    )
    assert matrix.reconcile_local([entry], repo_parent=tmp_path) == [entry]


def test_failed_manifest_write_preserves_original_and_removes_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _manifest(tmp_path, [_entry()])
    original = path.read_bytes()

    def fail_write(*args: object, **kwargs: object) -> None:
        raise OSError("simulated write failure")

    monkeypatch.setattr(matrix.json, "dump", fail_write)
    with pytest.raises(OSError, match="write failure"):
        matrix.write_manifest_atomic(path, [_entry(ref="a" * 40)])
    assert path.read_bytes() == original
    assert list(tmp_path.glob(f".{path.name}.*")) == []


def test_namespaced_checkouts_distinguish_owners_with_the_same_repo_name(tmp_path):
    entries = []
    for owner in ("one", "two"):
        parent = tmp_path / owner
        parent.mkdir()
        _, sha = _create_local_adapter(parent, "adapter", "community_sensor")
        entries.append(
            _entry(repository=f"{owner}/adapter", ref=sha, distribution="adapter")
        )
    assert matrix.reconcile_local(entries, repo_parent=tmp_path) == entries
    assert matrix.reconcile_local([entries[0], entries[0]], repo_parent=tmp_path) == [
        entries[0],
        entries[0],
    ]


def test_local_pin_check_rejects_moving_refs_before_reading_checkouts(tmp_path):
    with pytest.raises(ValueError, match="full commit SHAs"):
        matrix.reconcile_local([_entry(ref="refs/heads/main")], repo_parent=tmp_path)
