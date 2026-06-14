"""Comprehensive boundary and storage surface tests.

These tests protect structural invariants of the MEDRE framework — ensuring
core/route-engine/config-model/reusable-adapter-module boundaries via
comprehensive AST-based import checks, and enforcing the clean import
surface after SQLite storage decomposition (sections T–X).

TRACK 6 — Boundary/Regression Tests
"""

from __future__ import annotations

import ast as _ast
import re
from pathlib import Path

from tests.helpers.ast_imports import all_imports as _all_imports_new
from tests.helpers.ast_imports import (
    import_matches,
)
from tests.helpers.ast_imports import runtime_scope_imports as _runtime_scope_new
from tests.helpers.import_scanner import (
    scan_multiple_dirs_for_plain_imports,
    scan_multiple_dirs_for_prefixes,
)

# ---------------------------------------------------------------------------
# Local helpers wrapping ImportRecord-based API for violation checking
# ---------------------------------------------------------------------------


def _runtime_imports(source: str, file_path: str | None = None):
    """Parse source and return runtime-scope ImportRecords."""
    tree = _ast.parse(source)
    return _runtime_scope_new(tree, file_path=file_path)


def _all_imports(source: str, file_path: str | None = None):
    """Parse source and return all ImportRecords."""
    tree = _ast.parse(source)
    return _all_imports_new(tree, file_path=file_path)


def _check_banned_ast(
    imports, banned_prefixes: tuple[str, ...], *, rel_path: str
) -> list[str]:
    """Check ImportRecords for banned import prefixes."""
    violations: list[str] = []
    for r in imports:
        if import_matches(r.module, banned_prefixes):
            violations.append(f"{rel_path}:{r.lineno}: imports {r.module}")
    return violations


# ===================================================================
# T) Core boundary — comprehensive AST-based check
# ===================================================================


class TestCoreBoundaryComprehensive:
    """Core modules must not import from adapters, runtime.builder, CLI, or transport SDKs.

    After the capacity/sanitization moves, core should be fully self-contained
    with only stdlib, medre.core.*, and a few generic dependencies.
    """

    _BANNED_PREFIXES: tuple[str, ...] = (
        "medre.adapters",
        "medre.runtime.builder",
        "medre.runtime.route_engine",
        "medre.runtime.app",
        "medre.cli",
        "medre.config",
        "medre.runtime",
        # Transport SDKs
        "nio",
        "meshtastic",
        "aiohttp",
        "serial",
        "serial_asyncio",
        "meshcore",
        "RNS",
        "lxmf",
        "LXMF",
    )

    def test_core_files_have_no_banned_imports(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        core_dir = repo_root / "src" / "medre" / "core"
        assert core_dir.exists(), f"core directory not found: {core_dir}"

        violations: list[str] = []
        for py_file in sorted(core_dir.rglob("*.py")):
            if "__pycache__" in str(py_file):
                continue
            rel = str(py_file.relative_to(repo_root))
            source = py_file.read_text()
            try:
                imports = _runtime_imports(source, file_path=str(py_file))
            except SyntaxError:
                violations.append(f"{rel}: syntax error, cannot parse")
                continue
            violations.extend(
                _check_banned_ast(imports, self._BANNED_PREFIXES, rel_path=rel)
            )

        assert violations == [], "Core files contain banned imports:\n" + "\n".join(
            violations
        )


# ===================================================================
# U) Route engine boundary — comprehensive check
# ===================================================================


class TestRouteEngineBoundaryComprehensive:
    """Route engine must not import adapter implementations or SDKs.

    It may use platform strings like 'matrix' and 'meshtastic' for
    channel_room_map expansion, but must not import adapter modules.
    """

    _BANNED_PREFIXES: tuple[str, ...] = (
        "medre.adapters",
        "nio",
        "meshtastic",
        "aiohttp",
        "serial",
        "serial_asyncio",
        "meshcore",
        "RNS",
        "lxmf",
        "LXMF",
        "medre.runtime.builder",
        "medre.runtime.app",
        "medre.cli",
    )

    def test_route_engine_no_banned_imports(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        route_engine = repo_root / "src" / "medre" / "runtime" / "route_engine.py"
        assert route_engine.exists(), f"route_engine.py not found: {route_engine}"

        rel = str(route_engine.relative_to(repo_root))
        source = route_engine.read_text()
        imports = _all_imports(source, file_path=str(route_engine))
        violations = _check_banned_ast(imports, self._BANNED_PREFIXES, rel_path=rel)

        assert (
            violations == []
        ), "route_engine.py contains banned imports:\n" + "\n".join(violations)


# ===================================================================
# V) Config model boundary — comprehensive check
# ===================================================================


class TestConfigModelBoundaryComprehensive:
    """config/model.py may import adapter config dataclasses but not
    adapter implementations.

    Allowed: medre.config.adapters.* (dataclasses only)
    Disallowed: medre.adapters.*.adapter, medre.adapters.*.session,
                medre.runtime.builder, medre.runtime.route_engine,
                medre.core.engine, nio, meshtastic, aiohttp, serial
    """

    _BANNED_TOP_LEVEL: tuple[str, ...] = (
        "medre.adapters.matrix.adapter",
        "medre.adapters.matrix.session",
        "medre.adapters.meshtastic.adapter",
        "medre.adapters.meshtastic.session",
        "medre.adapters.meshcore.adapter",
        "medre.adapters.meshcore.session",
        "medre.adapters.lxmf.adapter",
        "medre.adapters.lxmf.session",
        "medre.runtime.builder",
        "medre.runtime.route_engine",
        "medre.core.engine",
        # SDKs
        "nio",
        "meshtastic",
        "aiohttp",
        "serial",
        "serial_asyncio",
        "meshcore",
        "RNS",
        "lxmf",
        "LXMF",
    )

    # medre.config.routes is a same-package config module — allowed at top level
    _CONFIG_ROUTES_MODULE = "medre.config.routes"

    def test_config_model_no_banned_imports(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        model_file = repo_root / "src" / "medre" / "config" / "model.py"
        assert model_file.exists(), f"config/model.py not found: {model_file}"

        rel = str(model_file.relative_to(repo_root))
        source = model_file.read_text()

        # Check runtime-scope imports for banned items
        rt_imports = _runtime_imports(source, file_path=str(model_file))
        violations = _check_banned_ast(rt_imports, self._BANNED_TOP_LEVEL, rel_path=rel)

        assert (
            violations == []
        ), "config/model.py contains banned imports:\n" + "\n".join(violations)


# ===================================================================
# W) Reusable adapter module boundary
# ===================================================================


class TestReusableAdapterModuleBoundary:
    """Reusable adapter modules (codec/renderer/session) must not import
    runtime/builder/pipeline/storage/CLI or other transport adapter modules.
    """

    _BANNED_PREFIXES: tuple[str, ...] = (
        "medre.runtime",
        "medre.cli",
        "medre.core.engine",
        "medre.core.storage",
    )

    # Heavy SDK packages banned at top-level for codec/renderer files.
    _HEAVY_SDKS: tuple[str, ...] = (
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

    # Modules to scan.  Tuple of (path_suffix, transport_name).
    _MODULE_SPECS: list[tuple[str, str]] = [
        ("src/medre/adapters/matrix/codec.py", "matrix"),
        ("src/medre/adapters/matrix/renderer.py", "matrix"),
        ("src/medre/adapters/matrix/session.py", "matrix"),
        ("src/medre/adapters/meshtastic/codec.py", "meshtastic"),
        ("src/medre/adapters/meshtastic/renderer.py", "meshtastic"),
        ("src/medre/adapters/meshtastic/session.py", "meshtastic"),
        ("src/medre/adapters/meshcore/codec.py", "meshcore"),
        ("src/medre/adapters/meshcore/renderer.py", "meshcore"),
        ("src/medre/adapters/meshcore/session.py", "meshcore"),
        ("src/medre/adapters/lxmf/codec.py", "lxmf"),
        ("src/medre/adapters/lxmf/renderer.py", "lxmf"),
        ("src/medre/adapters/lxmf/session.py", "lxmf"),
        ("src/medre/interop/mmrelay.py", ""),
    ]

    def _check_module(self, py_file: Path, rel: str, transport: str) -> list[str]:
        """Check a single module for boundary violations."""
        source = py_file.read_text()
        violations: list[str] = []

        try:
            _ast.parse(source)
        except SyntaxError:
            return [f"{rel}: syntax error, cannot parse"]

        is_codec_or_renderer = py_file.name in ("codec.py", "renderer.py")

        # Gather runtime-scope imports (catches try/except/with blocks)
        all_imports_list = _all_imports(source, file_path=str(py_file))
        rt_imports = _runtime_imports(source, file_path=str(py_file))

        # 1. Check all imports for banned prefixes (runtime, cli, core.engine, core.storage)
        for r in all_imports_list:
            if import_matches(r.module, self._BANNED_PREFIXES):
                violations.append(
                    f"{rel}:{r.lineno}: imports {r.module} (banned: {self._BANNED_PREFIXES[0]}...)"
                )

        # 2. Check own-adapter.module import (e.g. matrix/codec.py importing matrix/adapter)
        if transport:
            own_adapter = f"medre.adapters.{transport}.adapter"
            for r in all_imports_list:
                if r.module == own_adapter or r.module.startswith(own_adapter + "."):
                    violations.append(
                        f"{rel}:{r.lineno}: imports {r.module} "
                        f"(circular: reusable module importing own adapter)"
                    )

        # 2b. Cross-adapter isolation: reusable modules must not import
        #     other transport adapter packages (e.g. matrix/codec importing
        #     meshtastic/*).  interop modules are exempt.
        #     Underscore-prefixed modules (e.g. _attribution_dispatch) are
        #     shared infrastructure and exempt from the cross-adapter check.
        if transport:
            for r in all_imports_list:
                if not r.module.startswith("medre.adapters."):
                    continue
                # e.g. "medre.adapters.meshtastic.codec"
                parts = r.module.split(".")
                if len(parts) >= 3:
                    other_transport = parts[2]
                    if (
                        other_transport != transport
                        and other_transport != ""
                        and not other_transport.startswith("_")
                    ):
                        violations.append(
                            f"{rel}:{r.lineno}: imports {r.module} "
                            f"(cross-adapter: {transport} module importing "
                            f"{other_transport})"
                        )

        # 3. Codec/renderer must NOT have runtime-scope heavy SDK imports
        if is_codec_or_renderer:
            for r in rt_imports:
                for sdk in self._HEAVY_SDKS:
                    if r.module == sdk or r.module.startswith(sdk + "."):
                        violations.append(
                            f"{rel}:{r.lineno}: top-level SDK import {r.module} "
                            "(codec/renderer must not import heavy SDKs)"
                        )
                        break

        return violations

    def test_reusable_modules_boundary(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        violations: list[str] = []

        for path_suffix, transport in self._MODULE_SPECS:
            py_file = repo_root / path_suffix
            if not py_file.exists():
                continue
            rel = str(py_file.relative_to(repo_root))
            violations.extend(self._check_module(py_file, rel, transport))

        assert (
            violations == []
        ), "Reusable adapter module boundary violations:\n" + "\n".join(violations)


# ===================================================================
# X) Storage clean import surface
# ===================================================================


class TestStorageCleanSurface:
    """Enforce the clean import surface after SQLite storage decomposition.

    After the SQLite storage decomposition:
    - ``src/medre/core/storage/sqlite.py`` must not exist (moved to package).
    - ``src/medre/core/storage/__init__.py`` must not re-export storage symbols.
    - ``src/medre/core/storage/sqlite/__init__.py`` must not re-export symbols.
    - No file under src/, tests/, or docs/ may import from the old
      ``medre.core.storage`` package root or ``medre.core.storage.sqlite``
      package root.
    """

    # Forbidden import lines (prefix match).
    _FORBIDDEN_IMPORTS = (
        "from medre.core.storage import ",
        "from medre.core.storage.sqlite import ",
    )

    # Forbidden plain import package roots (exact word-boundary match).
    _FORBIDDEN_PLAIN_IMPORT_ROOTS = (
        "medre.core.storage",
        "medre.core.storage.sqlite",
    )

    # Files excluded from the scan (these files reference the forbidden
    # patterns as literal strings for the scan logic or intentionally
    # import package roots to verify no symbols are exposed).
    _EXCLUDED_FILES = frozenset(
        {
            "test_boundary_storage.py",
            "test_architecture_public_api.py",
            "test_replay_typechecking.py",
        }
    )

    def test_old_sqlite_module_does_not_exist(self) -> None:
        """src/medre/core/storage/sqlite.py must not exist."""
        repo_root = Path(__file__).resolve().parents[1]
        old_module = repo_root / "src" / "medre" / "core" / "storage" / "sqlite.py"
        assert (
            not old_module.exists()
        ), f"Old monolith module still exists: {old_module}"

    def test_storage_init_does_not_re_export(self) -> None:
        """storage/__init__.py must not contain SQLiteStorage or backend symbols."""
        repo_root = Path(__file__).resolve().parents[1]
        init = repo_root / "src" / "medre" / "core" / "storage" / "__init__.py"
        assert init.exists()
        text = init.read_text()
        forbidden_symbols = (
            "SQLiteStorage",
            "EventFilter",
            "DeliveryOutboxItem",
            "StorageBackend",
            "StorageGuarantees",
            "StorageError",
            "DuplicateEventError",
            "EventNotFoundError",
            "StorageInitializationError",
            "SchemaValidationError",
        )
        for sym in forbidden_symbols:
            assert (
                sym not in text
            ), f"storage/__init__.py contains forbidden symbol '{sym}'"

    def test_sqlite_init_does_not_re_export(self) -> None:
        """storage/sqlite/__init__.py must not contain SQLiteStorage or STALE_QUEUED_GRACE_SECONDS."""
        repo_root = Path(__file__).resolve().parents[1]
        init = (
            repo_root / "src" / "medre" / "core" / "storage" / "sqlite" / "__init__.py"
        )
        assert init.exists()
        text = init.read_text()
        forbidden_symbols = (
            "SQLiteStorage",
            "STALE_QUEUED_GRACE_SECONDS",
        )
        for sym in forbidden_symbols:
            assert (
                sym not in text
            ), f"storage/sqlite/__init__.py contains forbidden symbol '{sym}'"

    def test_no_root_or_sqlite_facade_imports_in_src(self) -> None:
        """No src/ file may import from medre.core.storage or .sqlite root."""
        repo_root = Path(__file__).resolve().parents[1]
        violations = scan_multiple_dirs_for_prefixes(
            (repo_root / "src",),
            self._FORBIDDEN_IMPORTS,
        )
        # Filter out excluded test files if they were somehow scanned
        violations = [
            v
            for v in violations
            if Path(v.split(":")[0]).name not in self._EXCLUDED_FILES
        ]
        assert (
            violations == []
        ), "Forbidden facade imports found in src/:\n" + "\n".join(violations)

    def test_no_root_or_sqlite_facade_imports_in_tests(self) -> None:
        """No tests/ file may import from medre.core.storage or .sqlite root."""
        repo_root = Path(__file__).resolve().parents[1]
        violations = scan_multiple_dirs_for_prefixes(
            (repo_root / "tests",),
            self._FORBIDDEN_IMPORTS,
        )
        violations = [
            v
            for v in violations
            if Path(v.split(":")[0]).name not in self._EXCLUDED_FILES
        ]
        assert (
            violations == []
        ), "Forbidden facade imports found in tests/:\n" + "\n".join(violations)

    def test_no_root_or_sqlite_facade_imports_in_docs(self) -> None:
        """No docs/ file may reference old medre.core.storage or .sqlite root imports."""
        repo_root = Path(__file__).resolve().parents[1]
        docs_dir = repo_root / "docs"
        if not docs_dir.exists():
            return

        violations: list[str] = []
        for md_file in sorted(docs_dir.rglob("*.md")):
            text = md_file.read_text(encoding="utf-8")
            for i, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if any(
                    stripped.startswith(prefix) for prefix in self._FORBIDDEN_IMPORTS
                ):
                    violations.append(
                        f"{md_file.relative_to(repo_root)}:{i}: {stripped}"
                    )
        assert (
            violations == []
        ), "Forbidden facade imports found in docs/:\n" + "\n".join(violations)

    def test_no_plain_import_of_storage_root_in_src(self) -> None:
        """No src/ file may plain-import medre.core.storage or .sqlite root."""
        repo_root = Path(__file__).resolve().parents[1]
        violations = scan_multiple_dirs_for_plain_imports(
            (repo_root / "src",),
            self._FORBIDDEN_PLAIN_IMPORT_ROOTS,
        )
        assert violations == [], "Forbidden plain imports found in src/:\n" + "\n".join(
            violations
        )

    def test_no_plain_import_of_storage_root_in_tests(self) -> None:
        """No tests/ file may plain-import medre.core.storage or .sqlite root."""
        repo_root = Path(__file__).resolve().parents[1]
        violations = scan_multiple_dirs_for_plain_imports(
            (repo_root / "tests",),
            self._FORBIDDEN_PLAIN_IMPORT_ROOTS,
        )
        # Exclude files which reference the patterns as literal strings.
        violations = [
            v
            for v in violations
            if Path(v.split(":")[0]).name not in self._EXCLUDED_FILES
        ]
        assert (
            violations == []
        ), "Forbidden plain imports found in tests/:\n" + "\n".join(violations)

    def test_no_plain_import_of_storage_root_in_docs(self) -> None:
        """No docs/ file may plain-import medre.core.storage or .sqlite root."""
        repo_root = Path(__file__).resolve().parents[1]
        docs_dir = repo_root / "docs"
        if not docs_dir.exists():
            return

        violations: list[str] = []
        pattern = re.compile(
            r"^import\s+("
            + "|".join(re.escape(p) for p in self._FORBIDDEN_PLAIN_IMPORT_ROOTS)
            + r")(\s|$)"
        )
        for md_file in sorted(docs_dir.rglob("*.md")):
            text = md_file.read_text(encoding="utf-8")
            for i, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if pattern.search(stripped):
                    violations.append(
                        f"{md_file.relative_to(repo_root)}:{i}: {stripped}"
                    )
        assert (
            violations == []
        ), "Forbidden plain imports found in docs/:\n" + "\n".join(violations)
