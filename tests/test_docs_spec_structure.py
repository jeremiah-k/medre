"""Spec documentation structure guard tests.

Asserts that the new MEDRE documentation structure has all required
files and directories in place:

  1. Core spec files under docs/spec/
  2. Transport profile files under docs/spec/transport-profiles/
  3. Appendix files under docs/spec/appendices/
  4. Operator guide files under docs/ops/
  5. Developer guide files under docs/dev/
  6. Schema files under docs/schemas/
  7. Change-tracking structure under docs/changes/
"""

from __future__ import annotations

from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parent.parent

SPEC_DIR = _ROOT / "docs" / "spec"
TRANSPORT_PROFILES_DIR = SPEC_DIR / "transport-profiles"
APPENDICES_DIR = SPEC_DIR / "appendices"
OPS_DIR = _ROOT / "docs" / "ops"
DEV_DIR = _ROOT / "docs" / "dev"
SCHEMAS_DIR = _ROOT / "docs" / "schemas"
CHANGES_DIR = _ROOT / "docs" / "changes"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _check_exists(path: Path, label: str) -> None:
    """Assert a path exists, failing with a descriptive message."""
    if not path.exists():
        pytest.fail(f"Required {label} not found: {path.relative_to(_ROOT)}")


# ===========================================================================
# 1. Core spec files
# ===========================================================================


class TestCoreSpecFiles:
    """Required core specification files must exist under docs/spec/."""

    REQUIRED_FILES = [
        "event-model.md",
        "adapter-runtime.md",
        "routing-delivery.md",
        "storage.md",
        "diagnostics-evidence.md",
        "principles.md",
        "architecture.md",
        "identity-addressing.md",
        "metadata.md",
        "configuration.md",
        "security-privacy.md",
        "conformance.md",
        "index.md",
    ]

    @pytest.mark.parametrize(
        "filename",
        REQUIRED_FILES,
        ids=lambda f: f,
    )
    def test_core_spec_file_exists(self, filename: str) -> None:
        """Core spec file must exist at docs/spec/<filename>."""
        path = SPEC_DIR / filename
        _check_exists(path, f"spec file '{filename}'")

    def test_spec_readme_exists(self) -> None:
        """docs/spec/README.md must exist as the spec index."""
        _check_exists(SPEC_DIR / "README.md", "spec README")


# ===========================================================================
# 2. Transport profile files
# ===========================================================================


class TestTransportProfiles:
    """Required transport profile files must exist under
    docs/spec/transport-profiles/."""

    REQUIRED_FILES = [
        "matrix.md",
        "meshtastic.md",
        "meshcore.md",
        "lxmf.md",
    ]

    @pytest.mark.parametrize(
        "filename",
        REQUIRED_FILES,
        ids=lambda f: f,
    )
    def test_transport_profile_exists(self, filename: str) -> None:
        """Transport profile must exist at docs/spec/transport-profiles/<name>."""
        path = TRANSPORT_PROFILES_DIR / filename
        _check_exists(path, f"transport profile '{filename}'")

    def test_transport_profiles_directory_exists(self) -> None:
        """docs/spec/transport-profiles/ directory must exist."""
        assert (
            TRANSPORT_PROFILES_DIR.is_dir()
        ), f"Required directory missing: {TRANSPORT_PROFILES_DIR.relative_to(_ROOT)}"


# ===========================================================================
# 3. Appendix files
# ===========================================================================


class TestAppendixFiles:
    """Required appendix files must exist under docs/spec/appendices/."""

    REQUIRED_FILES = [
        "glossary.md",
        "failure-taxonomy.md",
        "evidence-levels.md",
        "transport-limitations.md",
        "release-readiness.md",
    ]

    @pytest.mark.parametrize(
        "filename",
        REQUIRED_FILES,
        ids=lambda f: f,
    )
    def test_appendix_file_exists(self, filename: str) -> None:
        """Appendix file must exist at docs/spec/appendices/<name>."""
        path = APPENDICES_DIR / filename
        _check_exists(path, f"appendix file '{filename}'")

    def test_appendices_directory_exists(self) -> None:
        """docs/spec/appendices/ directory must exist."""
        assert (
            APPENDICES_DIR.is_dir()
        ), f"Required directory missing: {APPENDICES_DIR.relative_to(_ROOT)}"


# ===========================================================================
# 4. Operator guide files (docs/ops/)
# ===========================================================================


class TestOpsFiles:
    """Required operator guide files must exist under docs/ops/."""

    REQUIRED_FILES = [
        "install.md",
        "configuration.md",
        "running-medre.md",
        "operator-workflows.md",
        "diagnostics-and-evidence.md",
        "recovery-and-replay.md",
        "troubleshooting.md",
    ]

    @pytest.mark.parametrize(
        "filename",
        REQUIRED_FILES,
        ids=lambda f: f,
    )
    def test_ops_file_exists(self, filename: str) -> None:
        """Operator guide file must exist at docs/ops/<name>."""
        path = OPS_DIR / filename
        _check_exists(path, f"ops file '{filename}'")

    def test_ops_directory_exists(self) -> None:
        """docs/ops/ directory must exist."""
        assert (
            OPS_DIR.is_dir()
        ), f"Required directory missing: {OPS_DIR.relative_to(_ROOT)}"


# ===========================================================================
# 5. Developer guide files (docs/dev/)
# ===========================================================================


class TestDevFiles:
    """Required developer guide files must exist under docs/dev/."""

    REQUIRED_FILES = [
        "testing.md",
        "adapter-authoring.md",
        "documentation-style.md",
        "change-process.md",
        "live-test-harness.md",
        "source-audits.md",
        "reference-repos.md",
        "README.md",
    ]

    @pytest.mark.parametrize(
        "filename",
        REQUIRED_FILES,
        ids=lambda f: f,
    )
    def test_dev_file_exists(self, filename: str) -> None:
        """Developer guide file must exist at docs/dev/<name>."""
        path = DEV_DIR / filename
        _check_exists(path, f"dev file '{filename}'")

    def test_dev_directory_exists(self) -> None:
        """docs/dev/ directory must exist."""
        assert (
            DEV_DIR.is_dir()
        ), f"Required directory missing: {DEV_DIR.relative_to(_ROOT)}"


# ===========================================================================
# 6. Schema files (docs/schemas/)
# ===========================================================================


class TestSchemaFiles:
    """Required JSON Schema files must exist under docs/schemas/."""

    REQUIRED_SCHEMAS = [
        "canonical-event.schema.json",
        "delivery-receipt.schema.json",
        "delivery-result.schema.json",
        "diagnostics.schema.json",
        "evidence-bundle.schema.json",
        "adapter-config.schema.json",
        "routing-config.schema.json",
        "runtime-snapshot.schema.json",
    ]

    @pytest.mark.parametrize(
        "filename",
        REQUIRED_SCHEMAS,
        ids=lambda f: f,
    )
    def test_schema_file_exists(self, filename: str) -> None:
        """Schema file must exist at docs/schemas/<name>."""
        path = SCHEMAS_DIR / filename
        _check_exists(path, f"schema file '{filename}'")

    def test_schemas_directory_exists(self) -> None:
        """docs/schemas/ directory must exist."""
        assert (
            SCHEMAS_DIR.is_dir()
        ), f"Required directory missing: {SCHEMAS_DIR.relative_to(_ROOT)}"

    def test_examples_subdirectory_exists(self) -> None:
        """docs/schemas/examples/ directory must exist."""
        examples_dir = SCHEMAS_DIR / "examples"
        assert (
            examples_dir.is_dir()
        ), f"Required directory missing: {examples_dir.relative_to(_ROOT)}"


# ===========================================================================
# 7. Change-tracking structure (docs/changes/)
# ===========================================================================


class TestChangesStructure:
    """docs/changes/ must exist with README.md and unreleased/ subdirectory."""

    def test_changes_directory_exists(self) -> None:
        """docs/changes/ directory must exist."""
        assert (
            CHANGES_DIR.is_dir()
        ), f"Required directory missing: {CHANGES_DIR.relative_to(_ROOT)}"

    def test_changes_readme_exists(self) -> None:
        """docs/changes/README.md must exist."""
        _check_exists(
            CHANGES_DIR / "README.md",
            "changes README",
        )

    def test_unreleased_subdirectory_exists(self) -> None:
        """docs/changes/unreleased/ directory must exist."""
        unreleased = CHANGES_DIR / "unreleased"
        assert (
            unreleased.is_dir()
        ), f"Required directory missing: {unreleased.relative_to(_ROOT)}"


# ===========================================================================
# 8. Ops subdirectory files
# ===========================================================================


class TestOpsSubdirectories:
    """Required files in ops/ subdirectories must exist."""

    TRANSPORT_SETUP_FILES = [
        "matrix.md",
        "meshtastic.md",
        "meshcore.md",
        "lxmf.md",
    ]

    LIVE_VALIDATION_FILES = [
        "matrix.md",
        "meshtastic.md",
        "meshcore.md",
        "lxmf.md",
    ]

    @pytest.mark.parametrize(
        "filename",
        TRANSPORT_SETUP_FILES,
        ids=lambda f: f,
    )
    def test_transport_setup_file_exists(self, filename: str) -> None:
        """Transport setup file must exist at docs/ops/transport-setup/<name>."""
        path = OPS_DIR / "transport-setup" / filename
        _check_exists(path, f"transport-setup file '{filename}'")

    def test_transport_setup_directory_exists(self) -> None:
        """docs/ops/transport-setup/ directory must exist."""
        ts_dir = OPS_DIR / "transport-setup"
        assert (
            ts_dir.is_dir()
        ), f"Required directory missing: {ts_dir.relative_to(_ROOT)}"

    @pytest.mark.parametrize(
        "filename",
        LIVE_VALIDATION_FILES,
        ids=lambda f: f,
    )
    def test_live_validation_file_exists(self, filename: str) -> None:
        """Live validation file must exist at docs/ops/live-validation/<name>."""
        path = OPS_DIR / "live-validation" / filename
        _check_exists(path, f"live-validation file '{filename}'")

    def test_live_validation_directory_exists(self) -> None:
        """docs/ops/live-validation/ directory must exist."""
        lv_dir = OPS_DIR / "live-validation"
        assert (
            lv_dir.is_dir()
        ), f"Required directory missing: {lv_dir.relative_to(_ROOT)}"
