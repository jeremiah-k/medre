# medre-test-tier: compatibility
"""Import side-effect tests for reusable modules.

Verifies that importing lightweight/reusable modules does not:
- Configure logging
- Attach root handlers
- Import runtime.builder, core.engine.pipeline, core.storage
- Import adapter wrapper modules or protocol SDKs

All tests use **subprocess isolation** to avoid ``sys.modules``
poisoning that would break subsequent tests in the same process.
"""

from __future__ import annotations

import pytest

from tests.helpers.import_side_effects import (
    _FORBIDDEN_SDKS,
    _FORBIDDEN_SIDE_EFFECTS,
)
from tests.helpers.import_side_effects import (
    check_in_subprocess as _check_in_subprocess,
)

# Leaf modules — codecs, renderers, interop constants.
_LIGHTWEIGHT_MODULES: list[str] = [
    "medre.adapters.matrix.codec",
    "medre.adapters.matrix.renderer",
    "medre.adapters.meshtastic.codec",
    "medre.adapters.meshtastic.renderer",
    "medre.adapters.meshcore.codec",
    "medre.adapters.meshcore.renderer",
    "medre.adapters.lxmf.codec",
    "medre.adapters.lxmf.renderer",
]

# SDK packages that should not be pulled in by codec/renderer imports
# (imported as _FORBIDDEN_SDKS from architecture_report._SDK_PACKAGES above)

# Package roots — heavier modules tested alongside leaf modules.
_PACKAGE_ROOTS = (
    "medre.adapters.matrix",
    "medre.adapters.meshtastic",
    "medre.adapters.meshcore",
    "medre.adapters.lxmf",
)


# ---------------------------------------------------------------------------
# Leaf modules — codecs, renderers, interop constants
# ---------------------------------------------------------------------------


class TestNoLoggingSideEffects:
    """Importing lightweight modules must not configure logging."""

    @pytest.mark.parametrize("module_name", _LIGHTWEIGHT_MODULES)
    def test_import_does_not_change_root_logger_level(self, module_name: str) -> None:
        result = _check_in_subprocess(module_name)
        assert not result[
            "root_level_changed"
        ], f"importing '{module_name}' changed root logger level"

    @pytest.mark.parametrize("module_name", _LIGHTWEIGHT_MODULES)
    def test_import_does_not_add_root_handlers(self, module_name: str) -> None:
        result = _check_in_subprocess(module_name)
        assert (
            result["new_handlers"] == 0
        ), f"importing '{module_name}' added {result['new_handlers']} root handler(s)"


class TestNoForbiddenTransitiveImports:
    """Lightweight modules must not transitively import forbidden modules."""

    _FORBIDDEN = _FORBIDDEN_SIDE_EFFECTS + _FORBIDDEN_SDKS

    @pytest.mark.parametrize("module_name", _LIGHTWEIGHT_MODULES)
    def test_no_forbidden_transitive_imports(self, module_name: str) -> None:
        result = _check_in_subprocess(module_name)
        assert not result[
            "forbidden_loaded"
        ], f"importing '{module_name}' pulled in: {result['forbidden_loaded']}"


# ---------------------------------------------------------------------------
# Package roots
# ---------------------------------------------------------------------------


class TestPackageRootImportSideEffects:
    """Package roots must not pull heavy deps — tested in cold subprocesses."""

    @pytest.mark.parametrize("module_name", _PACKAGE_ROOTS)
    def test_no_forbidden_transitive_imports(self, module_name: str) -> None:
        result = _check_in_subprocess(module_name)
        assert not result[
            "forbidden_loaded"
        ], f"importing '{module_name}' pulled in: {result['forbidden_loaded']}"

    @pytest.mark.parametrize("module_name", _PACKAGE_ROOTS)
    def test_no_logging_side_effects(self, module_name: str) -> None:
        result = _check_in_subprocess(module_name)
        assert not result[
            "root_level_changed"
        ], f"importing '{module_name}' changed root logger level"
        assert (
            result["new_handlers"] == 0
        ), f"importing '{module_name}' added {result['new_handlers']} root handler(s)"
