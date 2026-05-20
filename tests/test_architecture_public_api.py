"""Architecture boundary tests: no public API commitment yet.

Ensures that importing top-level packages does not pull in heavy
implementation modules, and that public facade paths are not required.
"""
from __future__ import annotations

import sys

import pytest


class TestTopLevelImportLightweight:
    """Importing 'medre' must not pull in heavy modules."""

    _FORBIDDEN_AFTER_IMPORT: tuple[str, ...] = (
        "medre.runtime.builder",
        "medre.core.engine.pipeline",
        "medre.core.storage",
        "medre.adapters.matrix.adapter",
        "medre.adapters.meshtastic.adapter",
        "nio",
        "meshtastic",
    )

    def test_import_medre_is_lightweight(self) -> None:
        already = {m for m in self._FORBIDDEN_AFTER_IMPORT if m in sys.modules}
        import medre  # noqa: F811
        for m in self._FORBIDDEN_AFTER_IMPORT:
            if m in sys.modules and m not in already:
                pytest.fail(f"importing 'medre' pulled in heavy module: {m}")

    def test_medre_has_no_substantive_all(self) -> None:
        import medre  # noqa: F811
        # __all__ should not contain runtime builders, adapters, pipeline
        if hasattr(medre, "__all__"):
            api_symbols = set(medre.__all__)  # pyright: ignore[reportAttributeAccessIssue]
            forbidden = {"RuntimeBuilder", "MedreApp", "PipelineRunner",
                        "PipelineConfig", "MatrixAdapter", "MeshtasticAdapter",
                        "sanitize_error", "sanitize_for_log"}
            found = api_symbols & forbidden
            assert not found, (
                f"medre.__all__ contains public API symbols: {found}"
            )


class TestObservabilityFacadeRemoved:
    """medre.observability.sanitization must not exist as a facade."""

    def test_observability_sanitization_module_does_not_exist(self) -> None:
        """The medre.observability.sanitization re-export module must not exist."""
        import medre.observability
        from pathlib import Path
        init_dir = Path(medre.observability.__file__).parent
        sanitization_file = init_dir / "sanitization.py"
        assert not sanitization_file.exists(), (
            f"medre.observability.sanitization.py still exists at "
            f"{sanitization_file} — it should have been removed as a "
            f"re-export facade"
        )

    def test_from_medre_observability_import_sanitize_error_fails(self) -> None:
        """from medre.observability import sanitize_error should not work."""
        with pytest.raises(ImportError):
            from medre.observability import sanitize_error  # type: ignore[unused-import]
