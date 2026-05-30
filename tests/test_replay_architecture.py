"""Replay package architecture tests.

Enforce structural constraints on the replay package decomposition:

- No replay.py monolith can be re-introduced.
- Replay package root does not re-export public symbols.
- No facade imports or compatibility aliases exist.
- Canonical import paths are enforced.
- Replay submodules stay within their stage responsibility.

TRACK 6 — Boundary/Regression Tests
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.helpers.source_reader import source_of as _source_of

_SRC = Path(__file__).resolve().parents[1] / "src" / "medre"
_REPLAY_DIR = _SRC / "core" / "engine" / "replay"


# ===================================================================
# A) No replay.py monolith
# ===================================================================


class TestNoReplayMonolith:
    """replay.py must not exist as a sibling of the replay/ package."""

    def test_replay_py_does_not_exist(self) -> None:
        monolith = _SRC / "core" / "engine" / "replay.py"
        assert not monolith.exists(), (
            f"Monolith {monolith} must not exist — "
            "replay is a package (replay/), not a single file."
        )


# ===================================================================
# B) No package-root re-exports
# ===================================================================


class TestNoPackageRootReexports:
    """replay/__init__.py must not re-export public symbols.

    Uses AST analysis to detect re-export patterns that would break the
    decomposition invariant.  Checks for import statements, assignments,
    and __all__ definitions referencing public replay symbols — while
    correctly ignoring symbol names that appear in docstrings, comments,
    type hints, or string literals.
    """

    _PUBLIC_SYMBOLS = frozenset(
        {
            "ReplayEngine",
            "ReplayMode",
            "ReplayRequest",
            "ReplayResult",
            "ReplaySummary",
            "ReplayState",
            "ReplayRouteAttribution",
            "collect_replay_summary",
            "collect_replay_state",
        }
    )

    def test_init_has_no_symbol_exports(self) -> None:
        """replay/__init__.py must not import or assign public symbols."""
        source = _source_of("medre.core.engine.replay")
        tree = ast.parse(source)

        for node in ast.iter_child_nodes(tree):
            # from .submodule import Symbol / from ... import Symbol
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    assert alias.name not in self._PUBLIC_SYMBOLS, (
                        f"replay/__init__.py must not import {alias.name} "
                        f"from {node.module!r} — import from concrete "
                        f"submodules instead"
                    )
            # Symbol = ... / Symbol: type = ...
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if (
                        isinstance(target, ast.Name)
                        and target.id in self._PUBLIC_SYMBOLS
                    ):
                        raise AssertionError(
                            f"replay/__init__.py must not assign "
                            f"{target.id} — export from concrete "
                            f"submodules instead"
                        )
            if isinstance(node, ast.AnnAssign) and node.target:
                if (
                    isinstance(node.target, ast.Name)
                    and node.target.id in self._PUBLIC_SYMBOLS
                ):
                    raise AssertionError(
                        f"replay/__init__.py must not assign "
                        f"{node.target.id} — export from concrete "
                        f"submodules instead"
                    )

    def test_init_has_no_imports_from_submodules(self) -> None:
        """__init__.py must not import from replay submodules."""
        source = _source_of("medre.core.engine.replay")
        tree = ast.parse(source)

        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                assert not node.module.startswith("medre.core.engine.replay."), (
                    f"replay/__init__.py must not import from submodules: "
                    f"from {node.module} import ..."
                )


# ===================================================================
# C) No facade or __getattr__ patterns
# ===================================================================


class TestNoFacadePatterns:
    """No __getattr__ or lazy-import facades in the replay package."""

    def test_init_has_no_getattr(self) -> None:
        source = _source_of("medre.core.engine.replay")
        assert (
            "__getattr__" not in source
        ), "replay/__init__.py must not use __getattr__ for lazy imports"

    def test_init_has_no_all(self) -> None:
        source = _source_of("medre.core.engine.replay")
        assert "__all__" not in source, "replay/__init__.py must not define __all__"


# ===================================================================
# D) Canonical import paths
# ===================================================================


class TestCanonicalImports:
    """Verify that importing from concrete submodules works correctly."""

    def test_engine_import(self) -> None:
        from medre.core.engine.replay.engine import ReplayEngine

        assert ReplayEngine is not None

    def test_types_import(self) -> None:
        from medre.core.engine.replay.types import (
            ReplayMode,
        )

        assert ReplayMode.STRICT is not None

    def test_summary_import(self) -> None:
        from medre.core.engine.replay.summary import (
            ReplaySummary,
        )

        assert ReplaySummary is not None

    def test_helpers_import(self) -> None:
        from medre.core.engine.replay.helpers import (
            _resolve_stages,
        )

        assert callable(_resolve_stages)

    def test_protocols_import(self) -> None:
        from medre.core.engine.replay.protocols import (
            _RealPipelineProtocol,
            _StubPipelineProtocol,
        )

        assert _RealPipelineProtocol is not None
        assert _StubPipelineProtocol is not None

    def test_routing_import(self) -> None:
        from medre.core.engine.replay.routing import (
            _ReplayRoutingMixin,
        )

        assert _ReplayRoutingMixin is not None

    def test_delivery_import(self) -> None:
        from medre.core.engine.replay.delivery import (
            _ReplayDeliveryMixin,
        )

        assert _ReplayDeliveryMixin is not None

    def test_selection_import(self) -> None:
        from medre.core.engine.replay.selection import _ReplaySelectionMixin

        assert _ReplaySelectionMixin is not None

    def test_store_import(self) -> None:
        from medre.core.engine.replay.store import _ReplayStoreMixin

        assert _ReplayStoreMixin is not None

    def test_planning_import(self) -> None:
        from medre.core.engine.replay.planning import _ReplayPlanningMixin

        assert _ReplayPlanningMixin is not None

    def test_rendering_import(self) -> None:
        from medre.core.engine.replay.rendering import _ReplayRenderingMixin

        assert _ReplayRenderingMixin is not None


# ===================================================================
# E) ReplayEngine mixin MRO
# ===================================================================


class TestReplayEngineMRO:
    """ReplayEngine must inherit all stage mixins in correct MRO order."""

    def test_engine_inherits_all_mixins(self) -> None:
        from medre.core.engine.replay.engine import ReplayEngine

        mro_names = [c.__name__ for c in ReplayEngine.__mro__]
        assert "_ReplayDeliveryMixin" in mro_names
        assert "_ReplayRenderingMixin" in mro_names
        assert "_ReplayPlanningMixin" in mro_names
        assert "_ReplayRoutingMixin" in mro_names
        assert "_ReplayStoreMixin" in mro_names
        assert "_ReplaySelectionMixin" in mro_names
        assert "_ReplayEngineBase" in mro_names

    def test_engine_has_stage_methods(self) -> None:
        from medre.core.engine.replay.engine import ReplayEngine

        assert hasattr(ReplayEngine, "_stage_store")
        assert hasattr(ReplayEngine, "_stage_route")
        assert hasattr(ReplayEngine, "_stage_plan")
        assert hasattr(ReplayEngine, "_stage_render")
        assert hasattr(ReplayEngine, "_stage_deliver")
        assert hasattr(ReplayEngine, "_iter_by_ids")
        assert hasattr(ReplayEngine, "count_matching")
        assert hasattr(ReplayEngine, "_replay_missing")


# ===================================================================
# F) Replay submodules are transport-agnostic
# ===================================================================


class TestReplaySubmoduleBoundaries:
    """All replay submodules must remain transport-agnostic."""

    _REPLAY_SUBMODULES = (
        "medre.core.engine.replay.engine",
        "medre.core.engine.replay.types",
        "medre.core.engine.replay.summary",
        "medre.core.engine.replay.helpers",
        "medre.core.engine.replay.protocols",
        "medre.core.engine.replay.selection",
        "medre.core.engine.replay.store",
        "medre.core.engine.replay.planning",
        "medre.core.engine.replay.rendering",
        "medre.core.engine.replay.routing",
        "medre.core.engine.replay.delivery",
    )

    @pytest.mark.parametrize("module_name", _REPLAY_SUBMODULES)
    def test_no_sdk_imports(self, module_name: str) -> None:
        from medre.runtime.architecture_report import _SDK_PACKAGES
        from tests.helpers.import_scanner import banned_imports, import_lines

        try:
            source = _source_of(module_name)
        except (FileNotFoundError, ModuleNotFoundError):
            pytest.skip(f"{module_name} not found")
        lines = import_lines(source)
        found = banned_imports(lines, _SDK_PACKAGES)
        assert found == [], f"{module_name} imports transport SDKs: {found}"

    @pytest.mark.parametrize("module_name", _REPLAY_SUBMODULES)
    def test_no_concrete_adapter_imports(self, module_name: str) -> None:
        from tests.helpers.import_scanner import (
            ADAPTER_PREFIXES,
            banned_imports,
            import_lines,
        )

        try:
            source = _source_of(module_name)
        except (FileNotFoundError, ModuleNotFoundError):
            pytest.skip(f"{module_name} not found")
        lines = import_lines(source)
        found = banned_imports(lines, ADAPTER_PREFIXES)
        assert found == [], f"{module_name} imports concrete adapters: {found}"


# ===================================================================
# G) engine.py is thin orchestration
# ===================================================================


class TestEngineIsThin:
    """engine.py must remain a thin orchestration layer."""

    def test_engine_remains_thin(self) -> None:
        engine_path = _REPLAY_DIR / "engine.py"
        lines = engine_path.read_text().splitlines()
        # Count non-blank, non-comment lines
        code_lines = [
            ln for ln in lines if ln.strip() and not ln.strip().startswith("#")
        ]
        assert len(code_lines) < 300, (
            f"engine.py has {len(code_lines)} non-blank non-comment lines; "
            "stage logic belongs in mixins, not engine.py"
        )

    def test_engine_has_no_stage_implementations(self) -> None:
        """engine.py must not define _stage_store, _stage_route, etc."""
        source = _source_of("medre.core.engine.replay.engine")
        stage_methods = (
            "_stage_store",
            "_stage_route",
            "_stage_plan",
            "_stage_render",
            "_stage_deliver",
        )
        for method in stage_methods:
            assert f"def {method}" not in source, (
                f"engine.py must not define {method}() — "
                f"it belongs in a stage mixin"
            )

    def test_engine_has_no_filter_functions(self) -> None:
        """engine.py must not contain standalone filter functions."""
        source = _source_of("medre.core.engine.replay.engine")
        forbidden = (
            "_filter_plans_by_adapter",
            "_filter_plans_by_capability",
            "_filter_replay_loops",
            "_clean_routing_metadata",
            "_replay_delivery_envelope",
            "_request_to_filter",
            "_event_matches_filters",
            "_resolve_stages",
            "_elapsed_ms",
        )
        for name in forbidden:
            assert f"def {name}" not in source, (
                f"engine.py must not define {name}() — "
                f"it belongs in a helper/mixin module"
            )
