"""Released-artifact continuity qualification (A3 rows D-0 and D-1).

The scenarios live in ``scripts/verify_released_artifact_continuity.py`` and
consume locally cached published PyPI wheels (sha256-pinned there).  The
integration cases below skip cleanly when the cache is absent; a run that is
not skipped is a real qualification — the driver refuses cached artifacts
whose digest does not match the pinned published digests, and every check
asserts actual installed-wheel behavior.

Exact cache-population and execution commands are documented in the driver's
module docstring (``python scripts/verify_released_artifact_continuity.py
--help``).  The unit guards below run everywhere: they pin the driver's
digest/floor/skip semantics so a silent fake-green cannot be introduced.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "verify_released_artifact_continuity.py"
)

_CORE_WHEEL = "medre-0.1.0-py3-none-any.whl"
_ADAPTER_WHEEL = "medre_adapter_meshtastic-0.1.1-py3-none-any.whl"


def _load_driver() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "medre_verify_released_artifact_continuity", _SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cache_has_pinned_artifacts() -> bool:
    driver = _load_driver()
    found = driver.cache_status(driver.default_artifact_cache())
    return _CORE_WHEEL in found and _ADAPTER_WHEEL in found


def _run_driver(tmp_path: Path, scenario: str) -> tuple[int, dict[str, object]]:
    _load_driver()
    summary_path = tmp_path / f"{scenario}-summary.json"
    proc = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--scenario",
            scenario,
            "--sandbox",
            str(tmp_path / "sandbox"),
            "--summary-json",
            str(summary_path),
        ],
        capture_output=True,
        text=True,
        timeout=1200,
        check=False,
    )
    assert proc.returncode == 0, (
        f"{scenario} driver failed (exit {proc.returncode}): "
        f"{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}"
    )
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary[scenario]["status"] == "passed", summary[scenario]
    return proc.returncode, summary


# ---------------------------------------------------------------------------
# Unit guards (always run; no artifact cache required)
# ---------------------------------------------------------------------------


def _write_wheel(path: Path, metadata_lines: list[str]) -> Path:
    dist_info = path.stem.split("-")[0] + "-1.0.0.dist-info"
    metadata = "\n".join(metadata_lines) + "\n"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{dist_info}/METADATA", metadata)
    return path


def test_adapter_core_floor_read_from_wheel_metadata(tmp_path: Path) -> None:
    driver = _load_driver()
    wheel = _write_wheel(
        tmp_path / "medre_adapter_x-0.1.1-py3-none-any.whl",
        [
            "Metadata-Version: 2.4",
            "Version: 0.1.1",
            "Requires-Dist: medre<0.2.0,>=0.1.0",
        ],
    )
    assert driver._adapter_declared_core_floor(wheel) == "0.1.0"


def test_adapter_wheel_without_core_requirement_is_refused(tmp_path: Path) -> None:
    driver = _load_driver()
    wheel = _write_wheel(
        tmp_path / "medre_adapter_x-0.1.1-py3-none-any.whl",
        ["Metadata-Version: 2.4", "Version: 0.1.1"],
    )
    with pytest.raises(driver.ScenarioFailure, match="declares no medre requirement"):
        driver._adapter_declared_core_floor(wheel)


def test_wheel_version_readback_uses_metadata(tmp_path: Path) -> None:
    driver = _load_driver()
    wheel = _write_wheel(
        tmp_path / "medre-9.9.9-py3-none-any.whl",
        ["Metadata-Version: 2.4", "Version: 9.9.9"],
    )
    assert driver._wheel_version(wheel) == "9.9.9"


def test_cached_artifact_digest_mismatch_is_a_loud_failure(
    tmp_path: Path,
) -> None:
    driver = _load_driver()
    (tmp_path / _CORE_WHEEL).write_bytes(b"not the published artifact")
    with pytest.raises(driver.ScenarioFailure, match="digest mismatch"):
        driver._verify_cached_artifacts(tmp_path, [_CORE_WHEEL])


def test_missing_cached_artifact_is_refused(tmp_path: Path) -> None:
    driver = _load_driver()
    with pytest.raises(driver.ScenarioFailure, match="missing from cache"):
        driver._verify_cached_artifacts(tmp_path, [_CORE_WHEEL])


def test_empty_cache_reports_skipped_and_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    driver = _load_driver()
    exit_code = driver.main(
        [
            "--scenario",
            "d0",
            "--artifact-cache",
            str(tmp_path / "absent-cache"),
            "--summary-json",
            str(tmp_path / "summary.json"),
        ]
    )
    assert exit_code == 0
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["d0"]["status"] == "skipped"
    assert _CORE_WHEEL in summary["d0"]["reason"]
    assert _ADAPTER_WHEEL in summary["d0"]["reason"]
    stdout = capsys.readouterr().out
    assert json.loads(stdout)["d0"]["status"] == "skipped"


# ---------------------------------------------------------------------------
# Integration: real qualification against the cached published artifacts.
# Skips cleanly when the artifact cache is absent; never skips mid-scenario.
# ---------------------------------------------------------------------------


requires_artifact_cache = pytest.mark.skipif(
    not _cache_has_pinned_artifacts(),
    reason=(
        "released-artifact cache absent; populate it per "
        "scripts/verify_released_artifact_continuity.py (module docstring)"
    ),
)


@requires_artifact_cache
def test_released_010_database_reopens_on_current_wheel(tmp_path: Path) -> None:
    """D-0: actual 0.1.0-created stopped database to the current wheel."""
    _returncode, summary = _run_driver(tmp_path, "d0")
    d0 = summary["d0"]

    # Producer: authentic 0.1.0 release, populated and stopped via console.
    assert d0["producer"]["versions"]["medre"] == "0.1.0"
    smoke = d0["producer"]["smoke"]
    assert smoke["status"] == "passed"
    assert smoke["shutdown_status"] == "stopped"
    assert smoke["storage_backend"] == "sqlite"
    assert smoke["receipt_statuses"] == ["sent"]
    assert d0["producer"]["released_status_healthy"] is True

    # Original preserved; read-only assessment did not modify it.
    original = d0["original_database"]
    assert original["sha256"]
    assert original["unchanged_after_all_steps"] is True
    assert original["wal_bytes"] in (None, 0)

    # Writable reopen on a copy: two successful startups, stable identities.
    reopen = d0["reopen"]
    assert reopen["schema_version_stamp"] == "1"
    assert reopen["identity_counts"]["canonical_events"] >= 1
    assert reopen["identity_counts"]["delivery_receipts"] >= 1
    assert reopen["identity_counts"]["native_message_refs"] >= 1
    assert reopen["identity_counts"]["delivery_outbox"] >= 1
    assert len(reopen["opens"]) == 2
    for index, opened in enumerate(reopen["opens"], start=1):
        assert opened["open"] == index
        assert opened["state_equal"] is True
        assert opened["identities_equal"] is True
        assert opened["foreign_keys_clean"] is True
        assert opened["schema_version_stamp"] == "1"
    assert reopen["migration_executed"] is False
    assert reopen["consumer_cli_catalog_current"] is True
    assert reopen["consumer_cli_healthy"] is True

    # Candidate wheel provenance recorded (worktree-built, version-matched).
    candidate = d0["artifacts"]["candidate_wheel"]
    assert candidate["version"] == "0.1.1"
    assert candidate["sha256"]
    assert candidate["build_command"][-1] == "--no-isolation"

    # Separate unsupported fixtures refuse with the established language.
    assert len(d0["refusal"]) == 2
    for refusal in d0["refusal"]:
        assert refusal["startup_failed"] is True
        assert refusal["storage_initialization_error"] is True
        assert refusal["bytes_preserved"] is True
        assert len(refusal["refusal_language_present"]) == 3

    # Consumer imports come from the throwaway venv, core registers nothing.
    assert d0["candidate_import_probe"]["transports"] == []


@requires_artifact_cache
def test_published_provider_floor_pair_bridge_smoke(tmp_path: Path) -> None:
    """D-1: released Meshtastic 0.1.1 wheel at its declared core floor."""
    _returncode, summary = _run_driver(tmp_path, "d1")
    d1 = summary["d1"]

    # The floor is read from the adapter wheel's METADATA, not assumed.
    assert d1["declared_core_floor"] == "0.1.0"
    installed = d1["installed_versions"]
    assert installed["medre"] == "0.1.0"
    assert installed["medre-adapter-meshtastic"] == "0.1.1"
    # The declared SDK dependency actually resolved and installed.
    assert installed["mtjk"] is not None
    assert installed["PyPubSub"] is not None

    probe = d1["import_probe"]
    assert probe["transports"] == ["meshtastic"]
    assert probe["sample_transports"] == ["meshtastic"]
    assert probe["adapter_modules_from_venv"] is True
    assert "site-packages" in probe["medre_file"]

    smoke = d1["smoke"]
    assert smoke["status"] == "passed"
    assert smoke["shutdown_status"] == "stopped"
    assert smoke["storage_backend"] == "sqlite"
    assert smoke["receipt_statuses"] == ["sent"]
    assert smoke["target_adapters"] == ["meshtastic_2"]
