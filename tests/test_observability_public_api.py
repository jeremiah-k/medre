"""Tests for the public medre.observability.sanitization re-export.

Verifies that:
- The user-facing import path ``medre.observability.sanitization`` works
- Functions are the same callables as the core implementation
- Importing the module has no side effects (no logging config, no runtime imports)
"""

from __future__ import annotations

import importlib
import logging
import sys

from medre.core.observability.sanitization import sanitize_error as _core_sanitize_error  # noqa: E402
from medre.core.observability.sanitization import (  # noqa: E402
    sanitize_for_log as _core_sanitize_for_log,
)


class TestPublicSanitizationImport:
    """The public re-export path must work."""

    def test_import_from_package(self) -> None:
        from medre.observability import sanitize_error, sanitize_for_log

        assert sanitize_error is _core_sanitize_error
        assert sanitize_for_log is _core_sanitize_for_log

    def test_import_module(self) -> None:
        import medre.observability.sanitization as mod

        assert mod.sanitize_error is _core_sanitize_error
        assert mod.sanitize_for_log is _core_sanitize_for_log

    def test_re_export_matches_all(self) -> None:
        import medre.observability.sanitization as mod

        for name in mod.__all__:
            assert hasattr(mod, name)


class TestSanitizationImportNoSideEffects:
    """Importing the public sanitization module must not pull in
    runtime/builder/pipeline/storage or configure logging."""

    _FORBIDDEN_MODULES = [
        "medre.runtime.app",
        "medre.runtime.builder",
        "medre.core.engine.pipeline",
        "medre.core.storage",
        "medre.core.storage.sqlite",
    ]

    def test_import_does_not_configure_logging(self) -> None:
        module_name = "medre.observability.sanitization"
        sys.modules.pop(module_name, None)
        root = logging.getLogger()
        handler_ids_before = {id(h) for h in root.handlers}
        importlib.import_module(module_name)
        handler_ids_after = {id(h) for h in root.handlers}
        assert handler_ids_after == handler_ids_before, (
            "Importing medre.observability.sanitization added root logger handlers"
        )

    def test_import_does_not_pull_forbidden_modules(self) -> None:
        module_name = "medre.observability.sanitization"
        sys.modules.pop(module_name, None)
        already = {m for m in self._FORBIDDEN_MODULES if m in sys.modules}
        importlib.import_module(module_name)
        newly = [m for m in self._FORBIDDEN_MODULES if m in sys.modules and m not in already]
        assert not newly, (
            f"Importing sanitization pulled in forbidden modules: {newly}"
        )
