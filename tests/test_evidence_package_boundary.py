"""Package-boundary tests for ``medre.runtime.evidence``.

Ensures the package exports only its intended public API and does not
re-export private helpers or constants from internal modules.
"""

import importlib

import pytest


@pytest.fixture()
def evidence_pkg():
    return importlib.import_module("medre.runtime.evidence")


class TestEvidencePackageBoundary:
    """The evidence package must expose only its declared public API."""

    def test_all_exports_only_collect_evidence_bundle(self, evidence_pkg) -> None:
        """``__all__`` lists exactly ``["collect_evidence_bundle"]``."""
        assert evidence_pkg.__all__ == ["collect_evidence_bundle"]

    def test_collect_evidence_bundle_is_callable(self, evidence_pkg) -> None:
        """Public entry point is present and callable."""
        assert callable(evidence_pkg.collect_evidence_bundle)

    PRIVATE_NAMES = [
        "_section_ok",
        "_section_partial",
        "_section_error",
        "_section_skipped",
        "_compute_overall_status",
        "SCHEMA_VERSION",
    ]

    @pytest.mark.parametrize("name", PRIVATE_NAMES)
    def test_private_name_not_exported(self, evidence_pkg, name: str) -> None:
        """Private helpers must not be attributes of the package root."""
        assert not hasattr(evidence_pkg, name), (
            f"{name!r} is leaked from medre.runtime.evidence — "
            f"import it from medre.runtime.evidence._helpers instead."
        )

    def test_private_helpers_live_in_helpers_module(self) -> None:
        """Canonical internal module still exports all helpers."""
        from medre.runtime.evidence._helpers import (
            SCHEMA_VERSION,
            _compute_overall_status,
            _section_error,
            _section_ok,
            _section_partial,
            _section_skipped,
        )

        assert callable(_section_ok)
        assert callable(_section_partial)
        assert callable(_section_error)
        assert callable(_section_skipped)
        assert callable(_compute_overall_status)
        assert isinstance(SCHEMA_VERSION, int)
