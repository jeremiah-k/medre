"""Unit guards for the installed-wheel proof helper."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_installed_package.py"


def _load_proof_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("medre_installed_package_proof", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_requirement_check_accepts_declared_installed_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = _load_proof_module()
    monkeypatch.setattr(proof.importlib.metadata, "version", lambda _name: "84.0.0")

    proof._verify_build_requirements(["setuptools==84.0.0"])


def test_build_requirement_check_rejects_version_drift(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    proof = _load_proof_module()
    monkeypatch.setattr(proof.importlib.metadata, "version", lambda _name: "83.0.0")

    with pytest.raises(SystemExit) as exc_info:
        proof._verify_build_requirements(["setuptools==84.0.0"])

    assert exc_info.value.code == 1
    assert "--no-isolation would use the wrong backend tooling" in capsys.readouterr().err


def test_build_requirement_check_rejects_non_exact_requirement(
    capsys: pytest.CaptureFixture[str],
) -> None:
    proof = _load_proof_module()

    with pytest.raises(SystemExit) as exc_info:
        proof._verify_build_requirements(["setuptools>=84"])

    assert exc_info.value.code == 1
    assert "must be an exact pin" in capsys.readouterr().err


def test_source_build_cleanup_removes_stale_setuptools_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proof = _load_proof_module()
    monkeypatch.setattr(proof, "_REPO_ROOT", tmp_path)
    (tmp_path / "build" / "lib").mkdir(parents=True)
    (tmp_path / "build" / "lib" / "stale.py").write_text("stale")
    egg_info = tmp_path / "src" / "medre.egg-info"
    egg_info.mkdir(parents=True)
    (egg_info / "SOURCES.txt").write_text("stale")

    proof._clear_source_build_artifacts()

    assert not (tmp_path / "build").exists()
    assert not egg_info.exists()
