"""Route and runtime topology boundary enforcement tests.

These tests verify architectural boundaries for the routing and runtime
topology layer, complementing the cross-transport boundary tests:

1. Runtime (medre.runtime.*) must not import SDKs or concrete adapter packages.
2. Core routing must not import runtime (dependency direction: runtime → core).
3. Sessions must not know about routes or routing.
4. Adapters must not orchestrate routes (must not import route_engine).
5. Codecs must remain pure (must not import routing or runtime).
6. Renderers must not route (must not import routing modules).
7. Runtime route models must remain transport-agnostic (no SDK imports).

See: Contract 49 (Routing and Bridge), Contract 50 (Runtime Topology).
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Helpers (mirroring test_cross_transport_boundaries style)
# ---------------------------------------------------------------------------

_SDK_PACKAGES = ("nio", "meshtastic", "meshcore", "RNS", "lxmf")
"""Third-party transport SDK package names."""

_ADAPTER_TRANSPORTS = ("matrix", "meshtastic", "meshcore", "lxmf")

_CONCRETE_ADAPTER_PREFIXES = tuple(
    f"medre.adapters.{t}" for t in _ADAPTER_TRANSPORTS
)


def _load_module(name: str):
    """Import a module by dotted name; skip if not importable."""
    try:
        return importlib.import_module(name)
    except Exception:
        return None


def _read_module_source(module) -> str:
    """Read the source file of a loaded module."""
    assert module.__file__ is not None, f"{module} has no __file__"
    with open(module.__file__) as f:
        return f.read()


def _import_lines(source: str) -> list[str]:
    """Extract top-level import/from-import lines from source."""
    return [
        line.strip()
        for line in source.splitlines()
        if line.strip().startswith(("import ", "from "))
    ]


def _method_defs(source: str) -> list[str]:
    """Extract all ``def <name>`` definitions from source."""
    return re.findall(r"def\s+(\w+)", source)


def _adapter_modules(transport: str) -> list[str]:
    """Return known module dotted names for a transport adapter package."""
    base = f"medre.adapters.{transport}"
    mod = _load_module(base)
    if mod is None or mod.__file__ is None:
        return []
    pkg_dir = Path(mod.__file__).parent
    modules = [base]
    for py in sorted(pkg_dir.glob("*.py")):
        if py.name == "__init__.py":
            continue
        modules.append(f"{base}.{py.stem}")
    return modules


# ===================================================================
# Boundary 1: Runtime must not import SDKs or concrete adapters
# ===================================================================


class TestRuntimeBoundary:
    """medre.runtime.* modules must not import SDKs or concrete adapter packages."""

    _RUNTIME_MODULES = [
        "medre.runtime",
        "medre.runtime.app",
        "medre.runtime.builder",
        "medre.runtime.errors",
        "medre.runtime.routes",
        "medre.runtime.route_engine",
    ]

    @pytest.fixture(params=_RUNTIME_MODULES)
    def runtime_module(self, request):
        mod = _load_module(request.param)
        if mod is None:
            pytest.skip(f"{request.param} not importable")
        return mod

    def test_runtime_does_not_import_sdks(self, runtime_module) -> None:
        """Runtime modules must not import transport SDK packages."""
        source = _read_module_source(runtime_module)
        for line in _import_lines(source):
            for sdk in _SDK_PACKAGES:
                assert not (
                    line.startswith(f"import {sdk}")
                    or line.startswith(f"from {sdk}")
                ), (
                    f"{runtime_module.__name__} imports SDK {sdk!r} "
                    f"in: {line!r}"
                )

    def test_runtime_does_not_import_concrete_adapters(
        self, runtime_module
    ) -> None:
        """Runtime modules must not import concrete adapter packages."""
        source = _read_module_source(runtime_module)
        for line in _import_lines(source):
            for prefix in _CONCRETE_ADAPTER_PREFIXES:
                assert prefix not in line, (
                    f"{runtime_module.__name__} imports concrete adapter "
                    f"in: {line!r}"
                )


# ===================================================================
# Boundary 2: Core routing must not import runtime
# ===================================================================


class TestCoreRoutingBoundary:
    """Core routing must not import runtime — dependency is one-directional."""

    _CORE_ROUTING_MODULES = [
        "medre.core.routing",
        "medre.core.routing.models",
        "medre.core.routing.router",
    ]

    @pytest.fixture(params=_CORE_ROUTING_MODULES)
    def core_routing_module(self, request):
        mod = _load_module(request.param)
        if mod is None:
            pytest.skip(f"{request.param} not importable")
        return mod

    def test_core_routing_does_not_import_runtime(
        self, core_routing_module
    ) -> None:
        """Core routing modules must not import medre.runtime.*."""
        source = _read_module_source(core_routing_module)
        for line in _import_lines(source):
            assert "medre.runtime" not in line, (
                f"{core_routing_module.__name__} imports runtime "
                f"in: {line!r}"
            )

    def test_core_routing_does_not_import_sdks(
        self, core_routing_module
    ) -> None:
        """Core routing modules must not import transport SDKs."""
        source = _read_module_source(core_routing_module)
        for line in _import_lines(source):
            for sdk in _SDK_PACKAGES:
                assert not (
                    line.startswith(f"import {sdk}")
                    or line.startswith(f"from {sdk}")
                ), (
                    f"{core_routing_module.__name__} imports SDK {sdk!r} "
                    f"in: {line!r}"
                )

    def test_core_routing_does_not_import_concrete_adapters(
        self, core_routing_module
    ) -> None:
        """Core routing modules must not import concrete adapter packages."""
        source = _read_module_source(core_routing_module)
        for line in _import_lines(source):
            for prefix in _CONCRETE_ADAPTER_PREFIXES:
                assert prefix not in line, (
                    f"{core_routing_module.__name__} imports concrete adapter "
                    f"in: {line!r}"
                )


# ===================================================================
# Boundary 3: Sessions must not know about routes
# ===================================================================


_SESSION_INFOS = [
    ("matrix", "medre.adapters.matrix.session", "MatrixSession"),
    ("meshtastic", "medre.adapters.meshtastic.session", "MeshtasticSession"),
    ("meshcore", "medre.adapters.meshcore.session", "MeshCoreSession"),
    ("lxmf", "medre.adapters.lxmf.session", "LxmfSession"),
]


class TestSessionRoutingBoundary:
    """Session modules must not import routing or runtime routes."""

    @pytest.fixture(params=_SESSION_INFOS, ids=[s[0] for s in _SESSION_INFOS])
    def session_info(self, request):
        return request.param

    def test_session_does_not_import_runtime_routes(
        self, session_info
    ) -> None:
        """Session must not import medre.runtime.routes or route_engine."""
        _transport, mod_name, _cls_name = session_info
        mod = _load_module(mod_name)
        if mod is None:
            pytest.skip(f"{mod_name} not importable")
        source = _read_module_source(mod)
        for line in _import_lines(source):
            assert "medre.runtime" not in line, (
                f"Session must not import runtime; found: {line!r}"
            )

    def test_session_does_not_import_core_routing(
        self, session_info
    ) -> None:
        """Session must not import medre.core.routing.*."""
        _transport, mod_name, _cls_name = session_info
        mod = _load_module(mod_name)
        if mod is None:
            pytest.skip(f"{mod_name} not importable")
        source = _read_module_source(mod)
        for line in _import_lines(source):
            assert "medre.core.routing" not in line, (
                f"Session must not import core routing; found: {line!r}"
            )

    def test_session_does_not_reference_router_class(
        self, session_info
    ) -> None:
        """Session source must not reference Router class."""
        _transport, mod_name, _cls_name = session_info
        mod = _load_module(mod_name)
        if mod is None:
            pytest.skip(f"{mod_name} not importable")
        source = _read_module_source(mod)
        # Check for Router references (but allow "router" in comments/docstrings)
        for line in _import_lines(source):
            assert "Router" not in line, (
                f"Session must not reference Router; found: {line!r}"
            )

    def test_session_does_not_reference_routeconfig(
        self, session_info
    ) -> None:
        """Session source must not reference RouteConfig or route_engine."""
        _transport, mod_name, _cls_name = session_info
        mod = _load_module(mod_name)
        if mod is None:
            pytest.skip(f"{mod_name} not importable")
        source = _read_module_source(mod)
        for line in _import_lines(source):
            assert "RouteConfig" not in line, (
                f"Session must not reference RouteConfig; found: {line!r}"
            )
            assert "route_engine" not in line, (
                f"Session must not reference route_engine; found: {line!r}"
            )


# ===================================================================
# Boundary 4: Adapters must not orchestrate routes
# ===================================================================


class TestAdapterRoutingBoundary:
    """Adapter modules must not import route_engine or Router directly."""

    @pytest.fixture(params=_ADAPTER_TRANSPORTS)
    def transport(self, request):
        return request.param

    def test_adapters_do_not_import_route_engine(self, transport) -> None:
        """No adapter module imports medre.runtime.route_engine."""
        modules = _adapter_modules(transport)
        violations: list[str] = []
        for mod_name in modules:
            mod = _load_module(mod_name)
            if mod is None:
                continue
            source = _read_module_source(mod)
            for line in _import_lines(source):
                if "medre.runtime.route_engine" in line:
                    violations.append(
                        f"{mod_name} imports route_engine in: {line!r}"
                    )
        assert not violations, (
            "Adapter route_engine import violations:\n"
            + "\n".join(violations)
        )

    def test_adapters_do_not_import_runtime_routes(self, transport) -> None:
        """No adapter module imports medre.runtime.routes."""
        modules = _adapter_modules(transport)
        violations: list[str] = []
        for mod_name in modules:
            mod = _load_module(mod_name)
            if mod is None:
                continue
            source = _read_module_source(mod)
            for line in _import_lines(source):
                if "medre.runtime.routes" in line:
                    violations.append(
                        f"{mod_name} imports runtime.routes in: {line!r}"
                    )
        assert not violations, (
            "Adapter runtime.routes import violations:\n"
            + "\n".join(violations)
        )


# ===================================================================
# Boundary 5: Codecs must remain pure (no routing/runtime imports)
# ===================================================================


_CODEC_INFOS = [
    ("matrix", "medre.adapters.matrix.codec", "MatrixCodec"),
    ("meshtastic", "medre.adapters.meshtastic.codec", "MeshtasticCodec"),
    ("meshcore", "medre.adapters.meshcore.codec", "MeshCoreCodec"),
    ("lxmf", "medre.adapters.lxmf.codec", "LxmfCodec"),
]


class TestCodecRoutingBoundary:
    """Codec modules must not import routing or runtime modules."""

    @pytest.fixture(params=_CODEC_INFOS, ids=[c[0] for c in _CODEC_INFOS])
    def codec_info(self, request):
        return request.param

    def test_codec_does_not_import_routing(self, codec_info) -> None:
        """Codec source must not import medre.core.routing.*."""
        _transport, mod_name, _cls_name = codec_info
        mod = _load_module(mod_name)
        if mod is None:
            pytest.skip(f"{mod_name} not importable")
        source = _read_module_source(mod)
        for line in _import_lines(source):
            assert "medre.core.routing" not in line, (
                f"Codec must not import core routing; found: {line!r}"
            )

    def test_codec_does_not_import_runtime(self, codec_info) -> None:
        """Codec source must not import medre.runtime.*."""
        _transport, mod_name, _cls_name = codec_info
        mod = _load_module(mod_name)
        if mod is None:
            pytest.skip(f"{mod_name} not importable")
        source = _read_module_source(mod)
        for line in _import_lines(source):
            assert "medre.runtime" not in line, (
                f"Codec must not import runtime; found: {line!r}"
            )

    def test_codec_does_not_reference_router(self, codec_info) -> None:
        """Codec source must not reference Router class."""
        _transport, mod_name, _cls_name = codec_info
        mod = _load_module(mod_name)
        if mod is None:
            pytest.skip(f"{mod_name} not importable")
        source = _read_module_source(mod)
        for line in _import_lines(source):
            assert "Router" not in line, (
                f"Codec must not reference Router; found: {line!r}"
            )


# ===================================================================
# Boundary 6: Renderers must not route
# ===================================================================


_RENDERER_INFOS = [
    ("matrix", "medre.adapters.matrix.renderer", "MatrixRenderer"),
    ("meshtastic", "medre.adapters.meshtastic.renderer", "MeshtasticRenderer"),
    ("meshcore", "medre.adapters.meshcore.renderer", "MeshCoreRenderer"),
    ("lxmf", "medre.adapters.lxmf.renderer", "LxmfRenderer"),
]


class TestRendererRoutingBoundary:
    """Renderer modules must not import routing modules."""

    @pytest.fixture(params=_RENDERER_INFOS, ids=[r[0] for r in _RENDERER_INFOS])
    def renderer_info(self, request):
        return request.param

    def test_renderer_does_not_import_routing(self, renderer_info) -> None:
        """Renderer source must not import medre.core.routing.*."""
        _transport, mod_name, _cls_name = renderer_info
        mod = _load_module(mod_name)
        if mod is None:
            pytest.skip(f"{mod_name} not importable")
        source = _read_module_source(mod)
        for line in _import_lines(source):
            assert "medre.core.routing" not in line, (
                f"Renderer must not import core routing; found: {line!r}"
            )

    def test_renderer_does_not_import_runtime(self, renderer_info) -> None:
        """Renderer source must not import medre.runtime.*."""
        _transport, mod_name, _cls_name = renderer_info
        mod = _load_module(mod_name)
        if mod is None:
            pytest.skip(f"{mod_name} not importable")
        source = _read_module_source(mod)
        for line in _import_lines(source):
            assert "medre.runtime" not in line, (
                f"Renderer must not import runtime; found: {line!r}"
            )

    def test_renderer_does_not_reference_route_objects(
        self, renderer_info
    ) -> None:
        """Renderer source must not reference Route/Router classes."""
        _transport, mod_name, _cls_name = renderer_info
        mod = _load_module(mod_name)
        if mod is None:
            pytest.skip(f"{mod_name} not importable")
        source = _read_module_source(mod)
        for line in _import_lines(source):
            assert "Router" not in line, (
                f"Renderer must not reference Router; found: {line!r}"
            )
            assert "RouteConfig" not in line, (
                f"Renderer must not reference RouteConfig; found: {line!r}"
            )


# ===================================================================
# Boundary 7: Runtime route models are transport-agnostic
# ===================================================================


class TestRouteModelTransportAgnosticism:
    """Runtime route model modules must not reference transport-specific concepts."""

    _ROUTE_MODEL_MODULES = [
        "medre.runtime.routes",
        "medre.runtime.route_engine",
    ]

    @pytest.fixture(params=_ROUTE_MODEL_MODULES)
    def route_model_module(self, request):
        mod = _load_module(request.param)
        if mod is None:
            pytest.skip(f"{request.param} not importable")
        return mod

    def test_route_models_do_not_import_sdks(self, route_model_module) -> None:
        """Route model modules must not import transport SDKs."""
        source = _read_module_source(route_model_module)
        for line in _import_lines(source):
            for sdk in _SDK_PACKAGES:
                assert not (
                    line.startswith(f"import {sdk}")
                    or line.startswith(f"from {sdk}")
                ), (
                    f"{route_model_module.__name__} imports SDK {sdk!r} "
                    f"in: {line!r}"
                )

    def test_route_models_do_not_import_concrete_adapters(
        self, route_model_module
    ) -> None:
        """Route model modules must not import concrete adapter packages."""
        source = _read_module_source(route_model_module)
        for line in _import_lines(source):
            for prefix in _CONCRETE_ADAPTER_PREFIXES:
                assert prefix not in line, (
                    f"{route_model_module.__name__} imports concrete adapter "
                    f"in: {line!r}"
                )

    def test_route_models_do_not_reference_matrix_concepts(
        self, route_model_module
    ) -> None:
        """Route model source must not reference Matrix-specific concepts."""
        source = _read_module_source(route_model_module)
        for line in _import_lines(source):
            # Allow the word "matrix" in comments/docstrings only in import lines
            assert "nio" not in line.lower() or "medre" in line, (
                f"{route_model_module.__name__} references nio "
                f"in: {line!r}"
            )
