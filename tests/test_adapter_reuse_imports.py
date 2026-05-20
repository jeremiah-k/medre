"""Import side-effect tests for reusable adapter modules.

Verify that importing codec, renderer, and interop modules does not:
- Configure root logging (attach handlers, change levels)
- Pull in runtime.builder, core.engine.pipeline, core.storage, or adapter wrappers
- Call setup_logging

Part B — Import Side-Effect Tests
Part F — Logging Boundary Tests
"""

from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path

import pytest

from tests.helpers.import_ast import runtime_imports

# Modules to test — these should be importable without side effects.
_REUSABLE_MODULES = [
    "medre.adapters.matrix.codec",
    "medre.adapters.matrix.renderer",
    "medre.adapters.meshtastic.codec",
    "medre.adapters.meshtastic.renderer",
    "medre.adapters.meshcore.codec",
    "medre.adapters.meshcore.renderer",
    "medre.adapters.lxmf.codec",
    "medre.adapters.lxmf.renderer",
    "medre.interop.mmrelay",
]

# Modules that MUST NOT be imported as a side effect.
_FORBIDDEN_TRANSITIVE_MODULES = [
    "medre.runtime.builder",
    "medre.core.engine.pipeline",
    "medre.core.storage",
    "medre.core.storage.sqlite",
    "medre.adapters.matrix.adapter",
    "medre.adapters.meshtastic.adapter",
    "medre.adapters.meshcore.adapter",
    "medre.adapters.lxmf.adapter",
]


def _import_fresh(module_name: str):
    """Force a fresh import by clearing the module from sys.modules first."""
    to_remove = [
        name
        for name in list(sys.modules)
        if name == module_name or name.startswith(f"{module_name}.")
    ]
    for name in to_remove:
        sys.modules.pop(name, None)
    importlib.invalidate_caches()
    return importlib.import_module(module_name)


class TestNoLoggingSideEffects:
    """Importing reusable modules must not configure root logging."""

    def test_import_does_not_change_root_logger_level(self):
        """Root logger level must not change after importing reusable modules."""
        root = logging.getLogger()
        level_before = root.level
        len(root.handlers)

        for module_name in _REUSABLE_MODULES:
            _import_fresh(module_name)

        assert root.level == level_before, (
            f"Root logger level changed from {level_before} to {root.level} "
            f"after importing reusable modules"
        )

    def test_import_does_not_add_root_handlers(self):
        """No new handlers should be attached to root logger."""
        # This test runs in the same session so handlers may already exist.
        # Snapshot handler count, import, check no new ones.
        root = logging.getLogger()
        handler_ids_before = {id(h) for h in root.handlers}

        for module_name in _REUSABLE_MODULES:
            _import_fresh(module_name)

        handler_ids_after = {id(h) for h in root.handlers}
        new_handlers = handler_ids_after - handler_ids_before
        assert not new_handlers, (
            f"New handlers attached to root logger after importing reusable "
            f"modules: {new_handlers}"
        )


class TestNoForbiddenTransitiveImports:
    """Reusable modules must not transitively import runtime/adapter wrappers."""

    def test_no_runtime_builder_import(self):
        """Forbidden modules must not appear in sys.modules after importing."""
        # Snapshot which forbidden modules are already loaded
        already_loaded = {m for m in _FORBIDDEN_TRANSITIVE_MODULES if m in sys.modules}

        for module_name in _REUSABLE_MODULES:
            _import_fresh(module_name)

        newly_loaded = set()
        for m in _FORBIDDEN_TRANSITIVE_MODULES:
            if m in sys.modules and m not in already_loaded:
                newly_loaded.add(m)

        assert not newly_loaded, (
            f"Reusable module imports pulled in forbidden modules: "
            f"{sorted(newly_loaded)}"
        )


class TestSetupLoggingNotCalledOnImport:
    """setup_logging must not be called as an import side effect."""

    def test_no_medre_handler_after_importing_reusable_modules(self):
        """After importing reusable modules, root logger must not have a
        MEDRE-managed handler — which would indicate setup_logging was called."""
        root = logging.getLogger()
        before = {
            id(h) for h in root.handlers
            if getattr(h, "_medre_console_handler", False)
        }

        # Import all reusable modules explicitly (self-contained, no ordering dependency).
        for module_name in _REUSABLE_MODULES:
            _import_fresh(module_name)

        after = {
            id(h) for h in root.handlers
            if getattr(h, "_medre_console_handler", False)
        }
        assert after == before, (
            "Importing reusable modules attached MEDRE-managed root handlers"
        )


class TestCodecRendererSdkFree:
    """Codec and renderer modules must not import protocol SDKs at top level."""

    # SDK packages that codec/renderer should avoid
    _SDK_MODULES = ("nio", "meshtastic", "meshcore", "RNS", "lxmf")

    @pytest.mark.parametrize(
        "module_name",
        [
            "medre.adapters.matrix.codec",
            "medre.adapters.matrix.renderer",
            "medre.adapters.meshtastic.codec",
            "medre.adapters.meshtastic.renderer",
            "medre.adapters.meshcore.codec",
            "medre.adapters.meshcore.renderer",
            "medre.adapters.lxmf.codec",
            "medre.adapters.lxmf.renderer",
        ],
    )
    def test_no_sdk_import_at_top_level(self, module_name: str):
        """Verify no SDK packages appear in the module's runtime-scope imports."""
        mod = importlib.import_module(module_name)
        assert mod.__file__ is not None
        source = Path(mod.__file__).read_text()

        # Collect runtime-scope imports (catches module-level try/with/for blocks)
        runtime_import_names = set()
        for mod_name, _lineno in runtime_imports(source):
            top = mod_name.split(".")[0] if "." in mod_name else mod_name
            runtime_import_names.add(top)

        sdk_found = runtime_import_names & set(self._SDK_MODULES)
        assert not sdk_found, (
            f"{module_name} imports SDK at module level: {sdk_found}. "
            f"SDK imports should be deferred to session modules or "
            f"inside function bodies."
        )
