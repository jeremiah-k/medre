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
        # Cold import: remove pre-existing entries so test is reliable
        for m in list(self._FORBIDDEN_AFTER_IMPORT) + ["medre"]:
            sys.modules.pop(m, None)
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
    """medre.observability must not exist as a package."""

    def test_import_medre_observability_raises_module_not_found(self) -> None:
        """importing medre.observability must raise ModuleNotFoundError."""
        with pytest.raises(ModuleNotFoundError):
            import medre.observability  # type: ignore[import-not-found,unused-import]  # noqa: F401

    def test_observability_sanitization_module_does_not_exist(self) -> None:
        """The medre.observability.sanitization re-export module must not exist."""
        from pathlib import Path
        # The module lives at medre.core.observability.sanitization now
        # Check the old facade path doesn't exist
        src_dir = Path(__file__).resolve().parents[1] / "src" / "medre"
        old_path = src_dir / "observability" / "sanitization.py"
        assert not old_path.exists(), (
            f"medre.observability.sanitization.py still exists"
        )

    def test_from_medre_observability_import_fails(self) -> None:
        """medre.observability should not be importable as a facade."""
        import medre.core.observability.sanitization  # noqa: F401 — canonical path works
        with pytest.raises(ImportError):
            from medre.observability import sanitize_error  # type: ignore[unused-import]


class TestConfigFacadeRemoved:
    """Config packages must not expose convenience re-exports."""

    def test_config_has_no_all(self) -> None:
        import importlib
        mod = importlib.import_module("medre.config")
        if hasattr(mod, "__all__"):
            assert mod.__all__ == [], (
                f"medre.config exposes __all__: {mod.__all__}"
            )

    def test_config_adapters_has_no_all(self) -> None:
        import importlib
        mod = importlib.import_module("medre.config.adapters")
        if hasattr(mod, "__all__"):
            assert mod.__all__ == [], (
                f"medre.config.adapters exposes __all__: {mod.__all__}"
            )


class TestRunSessionFacadeRemoved:
    """medre.runtime.run_session must not expose convenience re-exports."""

    def test_run_session_has_no_bridge_session(self) -> None:
        import importlib
        mod = importlib.import_module("medre.runtime.run_session")
        assert not hasattr(mod, "run_bridge_session"), (
            "medre.runtime.run_session should not re-export run_bridge_session"
        )

    def test_run_session_has_no_scenario_category(self) -> None:
        import importlib
        mod = importlib.import_module("medre.runtime.run_session")
        assert not hasattr(mod, "scenario_category"), (
            "medre.runtime.run_session should not re-export scenario_category"
        )


class TestEvidenceFacadeRemoved:
    """medre.runtime.evidence must not expose convenience re-exports."""

    def test_evidence_has_no_collect_bundle(self) -> None:
        import importlib
        mod = importlib.import_module("medre.runtime.evidence")
        assert not hasattr(mod, "collect_evidence_bundle"), (
            "medre.runtime.evidence should not re-export collect_evidence_bundle"
        )
