"""Cross-transport boundary enforcement tests.

These tests verify architectural boundaries across ALL four alpha transports
(Matrix, Meshtastic, MeshCore, LXMF) uniformly:

1. Core import boundary: core packages must not import concrete adapter
   packages (medre.adapters.{matrix,meshtastic,meshcore,lxmf}) or transport
   SDKs (nio, meshtastic, meshcore, RNS, lxmf).  Importing from
   medre.adapters.base (protocol/base types) is permitted.

2. Runtime import boundary: runtime/diagnostics/health/capability modules
   must not import transport SDKs directly.

3. Adapter isolation: each adapter package must not import sibling adapter
   packages.

4. Renderer boundary: renderer modules/classes must not call adapter/session
   deliver/send/start/stop or manage lifecycle.

5. Session boundary: session modules must not import or publish canonical
   event types directly; they should normalize transport-local data/callbacks
   only.

6. Codec boundary: codec modules must not manage lifecycle/start/stop/reconnect
   or instantiate SDK clients/routers.
"""

from __future__ import annotations

import importlib
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.rendering.renderer import RenderingResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ADAPTER_TRANSPORTS = ("matrix", "meshtastic", "meshcore", "lxmf")
"""Four alpha transport names."""

_SDK_PACKAGES = ("nio", "meshtastic", "meshcore", "RNS", "lxmf")
"""Third-party transport SDK package names that must not appear in core."""

_CONCRETE_ADAPTER_PREFIXES = tuple(
    f"medre.adapters.{t}" for t in _ADAPTER_TRANSPORTS
)
"""Fully-qualified concrete adapter package prefixes."""


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


def _load_module(name: str):
    """Import a module by dotted name; skip if not importable."""
    try:
        return importlib.import_module(name)
    except Exception:
        return None


def _adapter_modules(transport: str) -> list[str]:
    """Return known module dotted names for a transport adapter package."""
    base = f"medre.adapters.{transport}"
    # Discover actual submodules by scanning the package directory
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


def _sibling_transports(transport: str) -> tuple[str, ...]:
    """Return the other three transports (siblings)."""
    return tuple(t for t in _ADAPTER_TRANSPORTS if t != transport)


# ===================================================================
# Boundary 1: Core import boundary
# ===================================================================


class TestCoreImportBoundary:
    """Core packages must not import concrete adapter packages or SDKs."""

    # All core modules to scan
    _CORE_MODULES = [
        "medre.core.events",
        "medre.core.events.canonical",
        "medre.core.events.bus",
        "medre.core.events.kinds",
        "medre.core.events.metadata",
        "medre.core.events.schema",
        "medre.core.engine.pipeline",
        "medre.core.rendering.renderer",
        "medre.core.rendering.text",
        "medre.core.runtime",
        "medre.core.runtime.diagnostics",
        "medre.core.runtime.health",
        "medre.core.runtime.capabilities",
        "medre.core.observability.logging",
        "medre.core.observability.metrics",
        "medre.core.lifecycle.states",
        "medre.core.lifecycle.manager",
        "medre.core.routing.models",
        "medre.core.routing.router",
        "medre.core.storage.backend",
        "medre.core.planning.delivery_plan",
        "medre.core.planning.fallback_resolution",
        "medre.core.planning.relation_resolution",
        "medre.core.transforms",
    ]

    @pytest.fixture(params=_CORE_MODULES)
    def core_module(self, request):
        """Parametrized fixture for each core module."""
        mod = _load_module(request.param)
        if mod is None:
            pytest.skip(f"{request.param} not importable")
        return mod

    def test_core_does_not_import_concrete_adapters(self, core_module) -> None:
        """Core module source must not import concrete adapter packages."""
        source = _read_module_source(core_module)
        for line in _import_lines(source):
            for prefix in _CONCRETE_ADAPTER_PREFIXES:
                assert prefix not in line, (
                    f"{core_module.__name__} imports concrete adapter "
                    f"in: {line!r}"
                )

    def test_core_does_not_import_transport_sdks(self, core_module) -> None:
        """Core module source must not import transport SDK packages."""
        source = _read_module_source(core_module)
        for line in _import_lines(source):
            for sdk in _SDK_PACKAGES:
                # Allow "meshtastic" as a word in comments/docstrings;
                # only check import lines.
                assert not (
                    line.startswith(f"import {sdk}")
                    or line.startswith(f"from {sdk}")
                ), (
                    f"{core_module.__name__} imports SDK {sdk!r} in: {line!r}"
                )

    def test_core_does_not_import_nio(self, core_module) -> None:
        """Core module source must not import the nio Matrix SDK."""
        source = _read_module_source(core_module)
        for line in _import_lines(source):
            assert "nio" not in line.lower() or "medre" in line, (
                f"{core_module.__name__} imports nio in: {line!r}"
            )


# ===================================================================
# Boundary 2: Runtime import boundary
# ===================================================================


class TestRuntimeImportBoundary:
    """Runtime/diagnostics/health/capability modules must not import
    transport SDKs."""

    _RUNTIME_MODULES = [
        "medre.core.runtime.diagnostics",
        "medre.core.runtime.health",
        "medre.core.runtime.capabilities",
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
# Boundary 3: Adapter isolation (no cross-transport imports)
# ===================================================================


class TestAdapterIsolation:
    """Each adapter package must not import sibling adapter packages."""

    @pytest.fixture(params=_ADAPTER_TRANSPORTS)
    def transport(self, request):
        return request.param

    def test_adapter_modules_do_not_import_siblings(self, transport) -> None:
        """All modules in a transport adapter must not import siblings."""
        siblings = _sibling_transports(transport)
        modules = _adapter_modules(transport)

        violations: list[str] = []
        for mod_name in modules:
            mod = _load_module(mod_name)
            if mod is None:
                continue
            source = _read_module_source(mod)
            for line in _import_lines(source):
                for sibling in siblings:
                    sibling_prefix = f"medre.adapters.{sibling}"
                    if sibling_prefix in line:
                        violations.append(
                            f"{mod_name} imports sibling {sibling!r} "
                            f"in: {line!r}"
                        )
        assert not violations, (
            "Adapter isolation violations:\n" + "\n".join(violations)
        )

    def test_adapter_modules_do_not_reference_sibling_sdks(
        self, transport
    ) -> None:
        """Adapter modules must not reference sibling SDK names in imports."""
        siblings = _sibling_transports(transport)
        # Map transport names to their SDK names
        sdk_map = {
            "matrix": "nio",
            "meshtastic": "meshtastic",
            "meshcore": "meshcore",
            "lxmf": "RNS",
        }
        sibling_sdks = [sdk_map[s] for s in siblings if s in sdk_map]
        # LXMF also has 'lxmf' as an SDK
        if "lxmf" in siblings:
            sibling_sdks.append("lxmf")

        modules = _adapter_modules(transport)
        violations: list[str] = []
        for mod_name in modules:
            mod = _load_module(mod_name)
            if mod is None:
                continue
            source = _read_module_source(mod)
            for line in _import_lines(source):
                for sdk in sibling_sdks:
                    # Allow references to own compat module
                    if f"medre.adapters.{transport}" in line:
                        continue
                    if line.startswith(f"import {sdk}") or line.startswith(
                        f"from {sdk}"
                    ):
                        violations.append(
                            f"{mod_name} imports sibling SDK {sdk!r} "
                            f"in: {line!r}"
                        )
        assert not violations, (
            "Sibling SDK import violations:\n" + "\n".join(violations)
        )


# ===================================================================
# Boundary 4: Renderer boundary (no delivery/lifecycle)
# ===================================================================

# Renderer classes to test
_RENDERER_INFOS = [
    ("matrix", "medre.adapters.matrix.renderer", "MatrixRenderer"),
    ("meshtastic", "medre.adapters.meshtastic.renderer", "MeshtasticRenderer"),
    ("meshcore", "medre.adapters.meshcore.renderer", "MeshCoreRenderer"),
    ("lxmf", "medre.adapters.lxmf.renderer", "LxmfRenderer"),
]


class TestRendererBoundary:
    """Renderer modules/classes must not deliver or manage lifecycle."""

    @pytest.fixture(params=_RENDERER_INFOS, ids=[r[0] for r in _RENDERER_INFOS])
    def renderer_info(self, request):
        """Returns (transport, module_name, class_name)."""
        return request.param

    def test_renderer_has_no_deliver_method(self, renderer_info) -> None:
        """Renderer class has no deliver method."""
        _transport, mod_name, cls_name = renderer_info
        mod = _load_module(mod_name)
        cls = getattr(mod, cls_name)
        instance = cls()
        assert not hasattr(instance, "deliver"), (
            f"{cls_name} must not have a deliver method"
        )

    def test_renderer_has_no_send_method(self, renderer_info) -> None:
        """Renderer class has no send method."""
        _transport, mod_name, cls_name = renderer_info
        mod = _load_module(mod_name)
        cls = getattr(mod, cls_name)
        instance = cls()
        assert not hasattr(instance, "send"), (
            f"{cls_name} must not have a send method"
        )

    def test_renderer_has_no_start_method(self, renderer_info) -> None:
        """Renderer class has no start method."""
        _transport, mod_name, cls_name = renderer_info
        mod = _load_module(mod_name)
        cls = getattr(mod, cls_name)
        instance = cls()
        assert not hasattr(instance, "start"), (
            f"{cls_name} must not have a start method"
        )

    def test_renderer_has_no_stop_method(self, renderer_info) -> None:
        """Renderer class has no stop method."""
        _transport, mod_name, cls_name = renderer_info
        mod = _load_module(mod_name)
        cls = getattr(mod, cls_name)
        instance = cls()
        assert not hasattr(instance, "stop"), (
            f"{cls_name} must not have a stop method"
        )

    def test_renderer_has_no_connect_method(self, renderer_info) -> None:
        """Renderer class has no connect method."""
        _transport, mod_name, cls_name = renderer_info
        mod = _load_module(mod_name)
        cls = getattr(mod, cls_name)
        instance = cls()
        assert not hasattr(instance, "connect"), (
            f"{cls_name} must not have a connect method"
        )

    def test_renderer_has_no_publish_method(self, renderer_info) -> None:
        """Renderer class has no publish method."""
        _transport, mod_name, cls_name = renderer_info
        mod = _load_module(mod_name)
        cls = getattr(mod, cls_name)
        instance = cls()
        assert not hasattr(instance, "publish"), (
            f"{cls_name} must not have a publish method"
        )

    def test_renderer_source_has_no_lifecycle_definitions(
        self, renderer_info
    ) -> None:
        """Renderer source has no def for lifecycle/delivery methods."""
        _transport, mod_name, _cls_name = renderer_info
        mod = _load_module(mod_name)
        source = _read_module_source(mod)
        defs = _method_defs(source)
        forbidden = {
            "deliver", "send", "start", "stop", "connect",
            "reconnect", "publish",
        }
        found = forbidden & set(defs)
        assert not found, (
            f"{mod_name} defines forbidden methods: {found}"
        )

    def test_renderer_source_does_not_import_adapter_or_session(
        self, renderer_info
    ) -> None:
        """Renderer source does not import adapter or session modules."""
        _transport, mod_name, _cls_name = renderer_info
        mod = _load_module(mod_name)
        source = _read_module_source(mod)
        for line in _import_lines(source):
            # Check for import of the adapter.py module (not medre.adapters.* namespace)
            assert (
                f"medre.adapters.{_transport}.adapter" not in line
            ), (
                f"Renderer must not import adapter module; found: {line!r}"
            )
            # Check for import of the session module
            assert (
                f"medre.adapters.{_transport}.session" not in line
            ), (
                f"Renderer must not import session module; found: {line!r}"
            )

    async def test_renderer_returns_rendering_result(
        self, renderer_info
    ) -> None:
        """Renderer.render() returns RenderingResult, not CanonicalEvent."""
        _transport, mod_name, cls_name = renderer_info
        mod = _load_module(mod_name)
        cls = getattr(mod, cls_name)
        renderer = cls()

        event = CanonicalEvent(
            event_id="evt-1",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter=f"{_transport}-1",
            source_transport_id="node-1",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "test"},
            metadata=EventMetadata(),
        )
        result = await renderer.render(event, f"{_transport}_node")
        assert isinstance(result, RenderingResult)
        assert not isinstance(result, CanonicalEvent)


# ===================================================================
# Boundary 5: Session boundary (no canonical event publishing)
# ===================================================================

_SESSION_INFOS = [
    ("matrix", "medre.adapters.matrix.session", "MatrixSession"),
    ("meshtastic", "medre.adapters.meshtastic.session", "MeshtasticSession"),
    ("meshcore", "medre.adapters.meshcore.session", "MeshCoreSession"),
    ("lxmf", "medre.adapters.lxmf.session", "LxmfSession"),
]


class TestSessionBoundary:
    """Session modules must not import/publish canonical event types."""

    @pytest.fixture(params=_SESSION_INFOS, ids=[s[0] for s in _SESSION_INFOS])
    def session_info(self, request):
        return request.param

    def test_session_does_not_import_canonical_event(
        self, session_info
    ) -> None:
        """Session source must not import CanonicalEvent or core.events."""
        _transport, mod_name, _cls_name = session_info
        mod = _load_module(mod_name)
        source = _read_module_source(mod)
        for line in _import_lines(source):
            # Sessions should not import canonical event types
            assert "canonical" not in line, (
                f"Session must not import canonical event module; "
                f"found: {line!r}"
            )
            assert "medre.core.events" not in line, (
                f"Session must not import core events; found: {line!r}"
            )

    def test_session_does_not_import_event_kinds(self, session_info) -> None:
        """Session source must not import EventKind."""
        _transport, mod_name, _cls_name = session_info
        mod = _load_module(mod_name)
        source = _read_module_source(mod)
        assert "EventKind" not in source, (
            f"Session must not reference EventKind; "
            f"found in {mod_name}"
        )

    def test_session_does_not_define_publish(self, session_info) -> None:
        """Session source must not define publish/emit/broadcast methods."""
        _transport, mod_name, _cls_name = session_info
        mod = _load_module(mod_name)
        source = _read_module_source(mod)
        defs = _method_defs(source)
        forbidden = {"publish", "emit", "broadcast", "fire"}
        found = forbidden & set(defs)
        assert not found, (
            f"{mod_name} defines forbidden publish methods: {found}"
        )

    def test_session_does_not_import_rendering(self, session_info) -> None:
        """Session source must not import rendering modules."""
        _transport, mod_name, _cls_name = session_info
        mod = _load_module(mod_name)
        source = _read_module_source(mod)
        for line in _import_lines(source):
            assert "rendering" not in line, (
                f"Session must not import rendering; found: {line!r}"
            )

    def test_session_does_not_import_routing(self, session_info) -> None:
        """Session source must not import routing modules."""
        _transport, mod_name, _cls_name = session_info
        mod = _load_module(mod_name)
        source = _read_module_source(mod)
        for line in _import_lines(source):
            assert "routing" not in line.lower() or "medre.adapters" not in line, (
                f"Session must not import routing; found: {line!r}"
            )

    def test_session_normalizes_to_plain_dicts(self, session_info) -> None:
        """Session source should normalize data to plain dicts/callbacks,
        not return CanonicalEvent objects.

        This is verified by confirming the session module does not
        construct CanonicalEvent instances.
        """
        _transport, mod_name, _cls_name = session_info
        mod = _load_module(mod_name)
        source = _read_module_source(mod)
        assert "CanonicalEvent(" not in source, (
            f"Session must not construct CanonicalEvent; found in {mod_name}"
        )


# ===================================================================
# Boundary 6: Codec boundary (no lifecycle/SDK client management)
# ===================================================================

_CODEC_INFOS = [
    ("matrix", "medre.adapters.matrix.codec", "MatrixCodec"),
    ("meshtastic", "medre.adapters.meshtastic.codec", "MeshtasticCodec"),
    ("meshcore", "medre.adapters.meshcore.codec", "MeshCoreCodec"),
    ("lxmf", "medre.adapters.lxmf.codec", "LxmfCodec"),
]


class TestCodecBoundary:
    """Codec modules must not manage lifecycle or instantiate SDK clients."""

    @pytest.fixture(params=_CODEC_INFOS, ids=[c[0] for c in _CODEC_INFOS])
    def codec_info(self, request):
        return request.param

    def _make_codec(self, transport: str, mod_name: str, cls_name: str):
        """Instantiate a codec for the given transport."""
        mod = _load_module(mod_name)
        assert mod is not None
        cls = getattr(mod, cls_name)

        matrix_cfg = _load_module("medre.adapters.matrix.config")
        assert matrix_cfg is not None
        meshtastic_cfg = _load_module("medre.adapters.meshtastic.config")
        assert meshtastic_cfg is not None
        meshcore_cfg = _load_module("medre.adapters.meshcore.config")
        assert meshcore_cfg is not None
        lxmf_cfg = _load_module("medre.adapters.lxmf.config")
        assert lxmf_cfg is not None

        config_map = {
            "matrix": (
                "test",
                matrix_cfg.MatrixConfig(
                    adapter_id="test",
                    homeserver="https://example.com",
                    user_id="@bot:example.com",
                    access_token="tok",
                ),
            ),
            "meshtastic": (
                "test",
                meshtastic_cfg.MeshtasticConfig(adapter_id="test"),
            ),
            "meshcore": (
                "test",
                meshcore_cfg.MeshCoreConfig(adapter_id="test"),
            ),
            "lxmf": (
                "test",
                lxmf_cfg.LxmfConfig(adapter_id="test"),
            ),
        }
        return cls(*config_map[transport])

    def test_codec_has_no_lifecycle_methods(self, codec_info) -> None:
        """Codec class has no start/stop/reconnect/connect methods."""
        transport, mod_name, cls_name = codec_info
        codec = self._make_codec(transport, mod_name, cls_name)

        for method in ("start", "stop", "reconnect", "connect", "send"):
            assert not hasattr(codec, method), (
                f"{cls_name} must not have a {method} method"
            )

    def test_codec_source_has_no_lifecycle_definitions(self, codec_info) -> None:
        """Codec source has no def for lifecycle methods."""
        _transport, mod_name, _cls_name = codec_info
        mod = _load_module(mod_name)
        source = _read_module_source(mod)
        defs = _method_defs(source)
        forbidden = {
            "start", "stop", "reconnect", "connect",
            "deliver", "send", "publish",
        }
        found = forbidden & set(defs)
        assert not found, (
            f"{mod_name} defines forbidden methods: {found}"
        )

    def test_codec_does_not_import_session(self, codec_info) -> None:
        """Codec source must not import session modules."""
        _transport, mod_name, _cls_name = codec_info
        mod = _load_module(mod_name)
        source = _read_module_source(mod)
        for line in _import_lines(source):
            assert ".session" not in line, (
                f"Codec must not import session; found: {line!r}"
            )

    def test_codec_does_not_import_adapter(self, codec_info) -> None:
        """Codec source must not import its own adapter module."""
        _transport, mod_name, _cls_name = codec_info
        mod = _load_module(mod_name)
        source = _read_module_source(mod)
        for line in _import_lines(source):
            # Allow imports from medre.adapters.base
            if "medre.adapters.base" in line:
                continue
            assert f"medre.adapters.{_transport}.adapter" not in line, (
                f"Codec must not import adapter module; found: {line!r}"
            )

    def test_codec_does_not_import_sdk_directly(self, codec_info) -> None:
        """Codec source must not import SDK packages directly.

        Codecs are SDK-agnostic pure decoders/encoders.
        """
        _transport, mod_name, _cls_name = codec_info
        mod = _load_module(mod_name)
        source = _read_module_source(mod)
        for line in _import_lines(source):
            for sdk in _SDK_PACKAGES:
                assert not (
                    line.startswith(f"import {sdk}")
                    or line.startswith(f"from {sdk}")
                ), (
                    f"Codec must not import SDK {sdk!r} directly; "
                    f"found: {line!r}"
                )

    def test_codec_does_not_instantiate_sdk_clients(self, codec_info) -> None:
        """Codec source must not instantiate SDK client/router objects.

        Checks that the source does not contain constructor calls for
        known SDK client types.
        """
        _transport, mod_name, _cls_name = codec_info
        mod = _load_module(mod_name)
        source = _read_module_source(mod)
        sdk_constructors = [
            "AsyncClient(", "MatrixClient(",  # nio
            "StreamInterface(", "SerialInterface(",  # meshtastic
            "MeshCore(",  # meshcore SDK
            "LXMRouter(", "Reticulum(",  # RNS/LXMF
        ]
        for ctor in sdk_constructors:
            assert ctor not in source, (
                f"Codec must not instantiate SDK client via {ctor!r}; "
                f"found in {mod_name}"
            )

    def test_codec_does_not_import_routing_or_planning(
        self, codec_info
    ) -> None:
        """Codec must not import routing or planning modules."""
        _transport, mod_name, _cls_name = codec_info
        mod = _load_module(mod_name)
        source = _read_module_source(mod)
        for line in _import_lines(source):
            assert "routing" not in line, (
                f"Codec must not import routing; found: {line!r}"
            )
            assert "planning" not in line, (
                f"Codec must not import planning; found: {line!r}"
            )
            assert "storage" not in line or "medre.adapters" not in line, (
                f"Codec must not import storage; found: {line!r}"
            )

    def test_codec_has_no_route_match_plan_methods(self, codec_info) -> None:
        """Codec instance has no route/match/plan methods."""
        transport, mod_name, cls_name = codec_info
        codec = self._make_codec(transport, mod_name, cls_name)

        for method in ("route", "match", "plan", "deliver", "publish"):
            assert not hasattr(codec, method), (
                f"{cls_name} must not have a {method} method"
            )
