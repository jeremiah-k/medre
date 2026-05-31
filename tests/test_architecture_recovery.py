"""Architecture guard tests for the recovery package.

These tests enforce structural invariants:
- ``__init__.py`` is documentation-only (no imports, no __all__).
- Canonical module imports work.
- Package-root re-exports do NOT exist (no facade).
- No ``__getattr__`` dynamic attribute lookup on the package.
"""

from __future__ import annotations

import pathlib


def _init_path() -> pathlib.Path:
    """Return the path to recovery ``__init__.py``."""
    import medre.core.recovery as pkg

    return pathlib.Path(pkg.__file__)


# ---------------------------------------------------------------------------
# __init__.py structure
# ---------------------------------------------------------------------------


class TestInitDocumentationOnly:
    """``__init__.py`` must contain no import statements or __all__."""

    def test_no_import_statements(self) -> None:
        source = _init_path().read_text()
        lines = source.splitlines()
        code_lines = [
            ln for ln in lines if ln.strip() and not ln.strip().startswith("#")
        ]
        for ln in code_lines:
            assert "import " not in ln, f"__init__.py contains import: {ln!r}"

    def test_no_dunder_all(self) -> None:
        source = _init_path().read_text()
        assert "__all__" not in source, "__init__.py must not define __all__"

    def test_no_getattr(self) -> None:
        source = _init_path().read_text()
        assert "__getattr__" not in source, "__init__.py must not define __getattr__"


# ---------------------------------------------------------------------------
# Canonical module imports
# ---------------------------------------------------------------------------


class TestCanonicalImports:
    """Canonical module paths must be importable."""

    def test_models_importable(self) -> None:
        from medre.core.recovery.models import (  # noqa: F401
            RecoveryOwnershipAction,
            RecoveryOwnershipStatus,
            RecoverySummary,
            StartupRecoveryLedger,
        )

    def test_builder_importable(self) -> None:
        from medre.core.recovery.builder import (  # noqa: F401
            build_recovery_summary,
            build_startup_recovery_ledger,
        )

    def test_classification_importable(self) -> None:
        from medre.core.recovery.classification import (  # noqa: F401
            classify_startup_reclamation,
        )

    def test_recovery_source_importable(self) -> None:
        from medre.core.recovery.recovery_source import (  # noqa: F401
            RecoverySource,
        )


# ---------------------------------------------------------------------------
# No facade re-exports
# ---------------------------------------------------------------------------


class TestNoFacade:
    """Package-root ``from medre.core.recovery import X`` must NOT work."""

    def test_no_recovery_ownership_action(self) -> None:
        import medre.core.recovery as pkg

        assert not hasattr(pkg, "RecoveryOwnershipAction")

    def test_no_recovery_ownership_status(self) -> None:
        import medre.core.recovery as pkg

        assert not hasattr(pkg, "RecoveryOwnershipStatus")

    def test_no_recovery_summary(self) -> None:
        import medre.core.recovery as pkg

        assert not hasattr(pkg, "RecoverySummary")

    def test_no_startup_recovery_ledger(self) -> None:
        import medre.core.recovery as pkg

        assert not hasattr(pkg, "StartupRecoveryLedger")

    def test_no_build_recovery_summary(self) -> None:
        import medre.core.recovery as pkg

        assert not hasattr(pkg, "build_recovery_summary")

    def test_no_build_startup_recovery_ledger(self) -> None:
        import medre.core.recovery as pkg

        assert not hasattr(pkg, "build_startup_recovery_ledger")

    def test_no_classify_startup_reclamation(self) -> None:
        import medre.core.recovery as pkg

        assert not hasattr(pkg, "classify_startup_reclamation")

    def test_no_recovery_source(self) -> None:
        import medre.core.recovery as pkg

        assert not hasattr(pkg, "RecoverySource")

    def test_no_dunder_getattr(self) -> None:
        """The package module must not have __getattr__."""
        import medre.core.recovery as pkg

        assert not hasattr(pkg, "__getattr__")

    def test_no_dunder_all(self) -> None:
        """The package module must not define __all__."""
        import medre.core.recovery as pkg

        assert not hasattr(pkg, "__all__")
