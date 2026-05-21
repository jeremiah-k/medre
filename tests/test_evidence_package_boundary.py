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

    def test_no_all_at_package_root(self, evidence_pkg) -> None:
        """The package root must not expose ``__all__`` convenience exports."""
        assert (
            not hasattr(evidence_pkg, "__all__") or len(evidence_pkg.__all__) == 0
        ), "medre.runtime.evidence should not expose __all__"

    def test_collect_evidence_bundle_from_concrete_path(self) -> None:
        """``collect_evidence_bundle`` must be imported from concrete submodule."""
        from medre.runtime.evidence._bundle import collect_evidence_bundle

        assert callable(collect_evidence_bundle)

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

    def test_init_source_mentions_no_private_submodules(self, evidence_pkg) -> None:
        """``__init__.py`` source must not import or assign ``_helpers``."""
        import inspect

        source = inspect.getsource(evidence_pkg)
        # The word _helpers should never appear in __init__.py source.
        assert "_helpers" not in source, (
            "medre.runtime.evidence.__init__ must not reference _helpers — "
            "private submodule imports belong in internal modules, not the package root."
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
