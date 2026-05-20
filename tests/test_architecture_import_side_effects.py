"""Import side-effect tests for reusable modules.

Verifies that importing lightweight/reusable modules does not:
- Configure logging
- Attach root handlers
- Import runtime.builder, core.engine.pipeline, core.storage
- Import adapter wrapper modules or protocol SDKs
"""

from __future__ import annotations

import importlib
import logging
import sys

import pytest

# Modules to test for import side effects.
_LIGHTWEIGHT_MODULES: list[str] = [
    # Package roots are covered separately by subprocess-based tests below;
    # this list covers leaf modules that are safe to import freshly in-process.
    "medre.interop.mmrelay",
    "medre.core.observability.sanitization",
    "medre.adapters.matrix.codec",
    "medre.adapters.matrix.renderer",
    "medre.adapters.meshtastic.codec",
    "medre.adapters.meshtastic.renderer",
    "medre.adapters.meshcore.codec",
    "medre.adapters.meshcore.renderer",
    "medre.adapters.lxmf.codec",
    "medre.adapters.lxmf.renderer",
]

# Modules that should NOT be imported as a side effect
_FORBIDDEN_SIDE_EFFECTS: tuple[str, ...] = (
    "medre.runtime.builder",
    "medre.core.engine.pipeline",
    "medre.core.storage",
    "medre.core.storage.sqlite",
    "medre.adapters.matrix.adapter",
    "medre.adapters.meshtastic.adapter",
    "medre.adapters.meshcore.adapter",
    "medre.adapters.lxmf.adapter",
)

# SDK packages that should not be pulled in by codec/renderer imports
_FORBIDDEN_SDKS: tuple[str, ...] = (
    "nio",
    "meshtastic",
    "meshcore",
    "RNS",
    "lxmf",
    "LXMF",
    "aiohttp",
    "serial",
    "serial_asyncio",
)


def _import_fresh(module_name: str) -> None:
    """Force a fresh import by removing module+submodules from sys.modules."""
    to_remove = [
        name
        for name in list(sys.modules)
        if name == module_name or name.startswith(f"{module_name}.")
    ]
    for name in to_remove:
        sys.modules.pop(name, None)
    importlib.invalidate_caches()
    importlib.import_module(module_name)


class TestNoLoggingSideEffects:
    """Importing lightweight modules must not configure logging."""

    @pytest.mark.parametrize("module_name", _LIGHTWEIGHT_MODULES)
    def test_import_does_not_change_root_logger_level(self, module_name: str) -> None:
        root = logging.getLogger()
        level_before = root.level
        _import_fresh(module_name)
        assert (
            root.level == level_before
        ), f"Importing {module_name} changed root logger level"

    @pytest.mark.parametrize("module_name", _LIGHTWEIGHT_MODULES)
    def test_import_does_not_add_root_handlers(self, module_name: str) -> None:
        root = logging.getLogger()
        handler_ids_before = {id(h) for h in root.handlers}
        _import_fresh(module_name)
        handler_ids_after = {id(h) for h in root.handlers}
        new_handlers = handler_ids_after - handler_ids_before
        assert (
            not new_handlers
        ), f"Importing {module_name} added root logger handlers: {new_handlers}"


class TestNoForbiddenTransitiveImports:
    """Lightweight modules must not transitively import forbidden modules."""

    _FORBIDDEN = _FORBIDDEN_SIDE_EFFECTS + _FORBIDDEN_SDKS

    @pytest.mark.parametrize("module_name", _LIGHTWEIGHT_MODULES)
    def test_no_forbidden_transitive_imports(self, module_name: str) -> None:
        # Snapshot already-loaded modules before import.
        # Do NOT pop from sys.modules — that would poison cached state
        # for subsequent tests.
        already = {m for m in self._FORBIDDEN if m in sys.modules}
        _import_fresh(module_name)
        newly = [m for m in self._FORBIDDEN if m in sys.modules and m not in already]
        assert (
            not newly
        ), f"Importing {module_name} pulled in forbidden modules: {newly}"


# ---------------------------------------------------------------------------
# Package roots — tested in cold subprocesses to avoid sys.modules poisoning.
# ---------------------------------------------------------------------------
_PACKAGE_ROOTS = (
    "medre",
    "medre.adapters",
    "medre.adapters.matrix",
    "medre.adapters.meshtastic",
    "medre.adapters.meshcore",
    "medre.adapters.lxmf",
    "medre.config",
    "medre.config.adapters",
    "medre.runtime",
    "medre.runtime.evidence",
    "medre.runtime.run_session",
)


def _check_root_in_subprocess(module_name: str) -> dict:
    """Import module_name in a cold subprocess, return side-effect report."""
    import json
    import subprocess
    import textwrap

    code = textwrap.dedent(f"""\
        import json, sys, logging
        forbidden_side = {list(_FORBIDDEN_SIDE_EFFECTS)!r}
        forbidden_sdks = {list(_FORBIDDEN_SDKS)!r}
        forbidden = set(forbidden_side + forbidden_sdks)

        before = set(sys.modules)
        root_level_before = logging.root.level
        root_handlers_before = len(logging.root.handlers)

        import {module_name}  # noqa: F401

        after = set(sys.modules)
        new_modules = after - before
        forbidden_loaded = sorted(m for m in new_modules
                                   if any(m == f or m.startswith(f + ".") for f in forbidden))

        result = {{
            "forbidden_loaded": forbidden_loaded,
            "root_level_changed": logging.root.level != root_level_before,
            "new_handlers": len(logging.root.handlers) - root_handlers_before,
        }}
        print(json.dumps(result))
    """)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return json.loads(proc.stdout.strip())


class TestPackageRootImportSideEffects:
    """Package roots must not pull heavy deps — tested in cold subprocesses."""

    @pytest.mark.parametrize("module_name", _PACKAGE_ROOTS)
    def test_no_forbidden_transitive_imports(self, module_name: str) -> None:
        result = _check_root_in_subprocess(module_name)
        assert not result[
            "forbidden_loaded"
        ], f"importing '{module_name}' pulled in: {result['forbidden_loaded']}"

    @pytest.mark.parametrize("module_name", _PACKAGE_ROOTS)
    def test_no_logging_side_effects(self, module_name: str) -> None:
        result = _check_root_in_subprocess(module_name)
        assert not result[
            "root_level_changed"
        ], f"importing '{module_name}' changed root logger level"
        assert (
            result["new_handlers"] == 0
        ), f"importing '{module_name}' added {result['new_handlers']} root handler(s)"
