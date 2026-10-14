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
