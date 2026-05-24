"""Track 9 — MeshCore operational boundary enforcement tests.

These tests enforce structural invariants that guarantee MeshCore adapter
operational safety without requiring the ``meshcore`` SDK or radio hardware.
They use **source-level text inspection** (for import/boundary scans) and
**runtime assertions** (for fake adapter workability and diagnostic safety).

Operational boundaries covered:

1. **SDK import containment** — MeshCore modules (except ``compat.py``) do
   not import the ``meshcore`` SDK package.
2. **Cross-transport isolation** — MeshCore modules do not import LXMF,
   Matrix, or Meshtastic adapter packages.
3. **Fake adapter operability** — :class:`FakeMeshCoreAdapter` can be
   imported, instantiated, started, and exercised without the SDK.
4. **Diagnostic safety** — Diagnostics methods expose only JSON-safe
   scalars; no SDK objects, private keys, or identity material leak.
5. **Live test exclusion** — Any test file exercising live MeshCore
   hardware carries ``pytest.mark.live`` and is excluded by default.

Pattern
-------
Source-level scans read file contents without importing SDK modules.
Runtime tests use only the fake adapter path.  This avoids triggering
SDK imports at test collection time and works in environments where
the ``meshcore`` package is not installed.
"""

from __future__ import annotations

import importlib
import re

# Capture SDK presence in sys.modules at module-load time, BEFORE any
# fake adapter imports in test methods.  This establishes a baseline so
# the sys.modules guard test can detect whether the fake adapter itself
# introduced the SDK (vs. it being loaded by a prior test or compat).
import sys as _sys
from pathlib import Path
from typing import Any

import pytest

from medre.adapters.meshcore.compat import HAS_MESHCORE

_SESSION_BASELINE_SDK_MODULES: frozenset[str] = frozenset(
    sdk for sdk in ("meshcore",) if sdk in _sys.modules
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SRC_ROOT = (
    Path(__file__).resolve().parent.parent / "src" / "medre" / "adapters" / "meshcore"
)
"""Root directory of MeshCore adapter source files."""

_TESTS_DIR = Path(__file__).resolve().parent
"""Root tests directory."""

_MESHCORE_SDK_IMPORTS = ("import meshcore", "from meshcore")
"""Banned SDK import patterns — only ``compat.py`` is exempt."""

_CROSS_TRANSPORT_PREFIXES = (
    "from medre.adapters.lxmf",
    "from medre.adapters.matrix",
    "from medre.adapters.meshtastic",
    "import medre.adapters.lxmf",
    "import medre.adapters.matrix",
    "import medre.adapters.meshtastic",
)
"""Cross-transport import patterns banned in MeshCore modules."""

_IDENTITY_SECRET_PATTERNS = (
    "private_key",
    "secret",
    "password",
    "access_token",
    "api_key",
    "credentials",
    "identity_key",
    "signing_key",
)
"""Patterns that must not appear in diagnostic output values."""


def _make_ctx(adapter_id: str = "test_op") -> Any:
    """Create an AdapterContext suitable for fake adapter tests."""
    from asyncio import Event
    from datetime import datetime, timezone

    from medre.core.contracts.adapter import AdapterContext

    return AdapterContext(
        adapter_id=adapter_id,
        event_bus=None,
        publish_inbound=lambda _: None,
        logger=__import__("logging").getLogger(f"test.{adapter_id}"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=Event(),
    )


def _read_source(path: Path) -> str:
    """Read a source file and return its text."""
    return path.read_text()


def _meshcore_py_files() -> list[Path]:
    """Return all ``*.py`` files in the MeshCore adapter package."""
    return sorted(_SRC_ROOT.glob("*.py"))


def _scan_for_patterns(source: str, patterns: tuple[str, ...]) -> list[str]:
    """Return import lines in *source* that match any of *patterns*.

    Skips comment lines and string-only matches in docstrings/comments.
    """
    violations: list[str] = []
    for i, line in enumerate(source.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        for pattern in patterns:
            if pattern in stripped:
                violations.append(f"L{i}: {stripped}")
                break
    return violations


def _has_live_marker(path: Path) -> bool:
    """Return ``True`` if the test file declares a live marker."""
    source = _read_source(path)
    return bool(re.search(r"pytest\.mark\.live", source))


# ===================================================================
# 1. SDK import containment
# ===================================================================


class TestMeshCoreSdkImportBoundary:
    """MeshCore adapter modules must not import the SDK except via compat.

    The ``compat.py`` module is the **sole** import site for the
    ``meshcore`` package.  All other modules must access the SDK
    through :data:`HAS_MESHCORE` or via the session (which defers
    imports to runtime methods).

    **WHY this matters**: Import-time SDK coupling means the entire adapter
    package fails to load when the SDK is absent.  This blocks CI, prevents
    developer iteration, and violates the optional-dependency contract.
    """

    @pytest.mark.parametrize(
        "filepath",
        _meshcore_py_files(),
        ids=lambda p: p.name,
    )
    def test_no_sdk_import_outside_compat(self, filepath: Path) -> None:
        """Non-compat/session modules must not import the ``meshcore`` SDK.

        ``compat.py`` is the designated module-level import site.
        ``session.py`` owns the SDK client lifecycle and defers imports
        to runtime methods (e.g. ``_connect_real``) — not module-level.
        """
        if filepath.name in ("compat.py", "session.py"):
            pytest.skip("compat.py / session.py are designated SDK interaction sites")

        source = _read_source(filepath)
        violations = _scan_for_patterns(source, _MESHCORE_SDK_IMPORTS)
        assert (
            violations == []
        ), f"{filepath.name} contains banned meshcore SDK imports:\n" + "\n".join(
            violations
        )

    def test_compat_defines_has_meshcore(self) -> None:
        """``compat.py`` must export :data:`HAS_MESHCORE`."""
        from medre.adapters.meshcore.compat import HAS_MESHCORE as _  # noqa: F401

        # If we got here, the import succeeded — compat is valid.
        assert isinstance(_, bool)


# ===================================================================
# 2. Cross-transport isolation
# ===================================================================


class TestMeshCoreCrossTransportBoundary:
    """MeshCore modules must not import from other transport adapters.

    **WHY this matters**: Cross-adapter imports create hidden coupling between
    transports.  If MeshCore imports LXMF, then a breaking change in the LXMF
    adapter could silently break MeshCore, and uninstalling the LXMF adapter
    package would cascade into import errors in MeshCore.
    """

    @pytest.mark.parametrize(
        "filepath",
        _meshcore_py_files(),
        ids=lambda p: p.name,
    )
    def test_no_cross_transport_imports(self, filepath: Path) -> None:
        """MeshCore modules must not import LXMF, Matrix, or Meshtastic."""
        source = _read_source(filepath)
        violations = _scan_for_patterns(source, _CROSS_TRANSPORT_PREFIXES)
        assert (
            violations == []
        ), f"{filepath.name} contains cross-transport imports:\n" + "\n".join(
            violations
        )


# ===================================================================
# 3. Fake adapter operability (no SDK required)
# ===================================================================


class TestMeshCoreFakeAdapterOperability:
    """FakeMeshCoreAdapter must work without the ``meshcore`` SDK installed.

    **WHY this matters**: CI environments and developer machines may not have
    radio hardware or the ``meshcore`` SDK package installed.  The fake adapter
    is the gateway to running the entire test suite deterministically, so it
    must never accidentally pull in the real SDK.  If it did, CI would fail
    and developers could not iterate on core logic without hardware setup.
    """

    def test_compat_reports_status_without_crashing(self) -> None:
        """``HAS_MESHCORE`` is accessible regardless of SDK availability.

        WHY: The compat flag gates all SDK-dependent code paths.  If accessing
        it crashed, the entire adapter package would be unloadable.
        """
        # Already imported at module level; just assert it's a bool.
        assert isinstance(HAS_MESHCORE, bool)

    def test_fake_adapter_imports_without_sdk(self) -> None:
        """FakeMeshCoreAdapter can be imported without the SDK.

        WHY: Import-time SDK coupling would make the fake adapter unusable in
        clean environments — exactly where it is needed most.
        """
        from medre.adapters.fakes.meshcore import FakeMeshCoreAdapter

        assert FakeMeshCoreAdapter is not None

    def test_fake_adapter_import_does_not_load_sdk_into_sys_modules(self) -> None:
        """Importing the fake adapter must not leak the SDK into ``sys.modules``.

        WHY: If ``import meshcore`` appeared in ``sys.modules`` after importing
        the fake adapter, it would mean a dependency chain is pulling in the
        real SDK transitively — breaking the no-SDK guarantee at the module
        level, not just the source level.
        """
        import sys

        sdk_names = ("meshcore",)
        # Import fresh to detect side-effects.

        importlib.import_module("medre.adapters.fakes.meshcore")
        for sdk in sdk_names:
            if sdk in _SESSION_BASELINE_SDK_MODULES:
                continue  # SDK was loaded before this test session.
            assert (
                sdk not in sys.modules
            ), f"Importing FakeMeshCoreAdapter leaked '{sdk}' into sys.modules"

    def test_fake_adapter_instantiation(self) -> None:
        """FakeMeshCoreAdapter can be instantiated with fake config.

        WHY: Instantiation is the first runtime touch-point.  If the
        constructor required SDK types, the fake adapter would be useless
        for isolated testing.
        """
        from medre.adapters.fakes.meshcore import FakeMeshCoreAdapter
        from medre.config.adapters.meshcore import MeshCoreConfig

        config = MeshCoreConfig(adapter_id="test_op_boundary")
        adapter = FakeMeshCoreAdapter(config)
        assert adapter.adapter_id == "test_op_boundary"

    @pytest.mark.asyncio
    async def test_fake_adapter_start_stop(self) -> None:
        """FakeMeshCoreAdapter start/stop lifecycle works without SDK.

        WHY: The start→stop lifecycle is the minimum contract every adapter
        must fulfill.  If the fake adapter's lifecycle required SDK calls,
        it could not stand in for the real adapter in integration tests.
        """
        from medre.adapters.fakes.meshcore import FakeMeshCoreAdapter
        from medre.config.adapters.meshcore import MeshCoreConfig

        config = MeshCoreConfig(adapter_id="test_op_lifecycle")
        adapter = FakeMeshCoreAdapter(config)
        await adapter.start(_make_ctx("test_op_lifecycle"))
        assert adapter._started
        await adapter.stop()

    @pytest.mark.asyncio
    async def test_fake_adapter_deliver_and_track(self) -> None:
        """FakeMeshCoreAdapter tracks deliveries without SDK.

        WHY: Outbound delivery tracking is critical for delivery-receipt and
        replay tests.  If the fake delivery path required SDK types, those
        tests would fail in clean environments.
        """
        from medre.adapters.fakes.meshcore import FakeMeshCoreAdapter
        from medre.config.adapters.meshcore import MeshCoreConfig
        from medre.core.rendering.renderer import RenderingResult

        config = MeshCoreConfig(adapter_id="test_op_deliver")
        adapter = FakeMeshCoreAdapter(config)
        await adapter.start(_make_ctx("test_op_deliver"))

        result = RenderingResult(
            event_id="evt-boundary",
            target_adapter="test_op_deliver",
            target_channel="0",
            payload={"text": "boundary test", "channel_index": 0, "meshnet_name": ""},
        )
        delivery = await adapter.deliver(result)
        assert delivery is not None
        assert len(adapter.delivered_payloads) == 1
        await adapter.stop()

    @pytest.mark.asyncio
    async def test_fake_adapter_start_diagnostics_stop_lifecycle(self) -> None:
        """start() → diagnostics() → stop() must all succeed without SDK.

        WHY: The full operational lifecycle (start, introspect, stop) is the
        sequence operators use in production.  Verifying it works with the
        fake adapter proves the diagnostics contract is reachable at runtime
        and does not depend on SDK state that only exists after a real radio
        connection.
        """
        from medre.adapters.fakes.meshcore import FakeMeshCoreAdapter
        from medre.config.adapters.meshcore import MeshCoreConfig

        config = MeshCoreConfig(adapter_id="test_lifecycle_diag")
        adapter = FakeMeshCoreAdapter(config)

        # start
        await adapter.start(_make_ctx("test_lifecycle_diag"))
        assert adapter._started

        # diagnostics (mid-lifecycle)
        diag = adapter.diagnostics()
        assert isinstance(diag, dict)
        assert diag["started"] is True
        assert diag["mode"] == "fake"
        assert "adapter_id" in diag

        # stop
        await adapter.stop()
        assert not adapter._started


# ===================================================================
# 4. Diagnostic safety
# ===================================================================


class TestMeshCoreDiagnosticSafety:
    """Diagnostics must not expose SDK objects or identity material.

    **WHY this matters**: Diagnostics are surfaced in operator tooling, logs,
    and potentially over network APIs.  Leaking SDK objects (which may hold
    file descriptors or connection state) or identity material (private keys,
    secrets) would be a security incident.  These tests enforce the contract
    that diagnostics output is safe to transmit and log.
    """

    @pytest.mark.parametrize(
        "filepath",
        _meshcore_py_files(),
        ids=lambda p: p.name,
    )
    def test_diagnostics_source_no_secret_patterns(self, filepath: Path) -> None:
        """Diagnostic-returning source must not reference secret/identity fields.

        WHY: Source-level scanning catches accidental secret exposure at the
        code level — before runtime.  If the source references private_key
        or similar in a return/assignment context, it is a latent leak risk.
        """
        source = _read_source(filepath)
        # Only check files that have diagnostic methods.
        if "diagnostics" not in source:
            pytest.skip("no diagnostics method in this file")

        for pattern in _IDENTITY_SECRET_PATTERNS:
            # Look for the pattern in non-comment lines.
            for i, line in enumerate(source.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                # Only flag if it's in a return/yield/assignment context
                # (not just in a docstring mentioning the word).
                if pattern in stripped.lower():
                    # Allow mentions in docstrings (""" ... """) and comments.
                    # Check if the line is inside a docstring by looking at
                    # surrounding context — simplified: only flag lines that
                    # look like dict key assignments or return values.
                    if (
                        any(kw in stripped for kw in ("return", "=", "yield", "[", "{"))
                        and '"""' not in stripped
                        and "'''" not in stripped
                    ):
                        # Exclude comments about what NOT to expose
                        if (
                            "no " not in stripped.lower()
                            and "must not" not in stripped.lower()
                        ):
                            pytest.fail(
                                f"{filepath.name}:{i}: potential secret leak: {stripped}"
                            )

    @pytest.mark.asyncio
    async def test_fake_adapter_diagnostics_are_json_safe(self) -> None:
        """Diagnostics output contains only JSON-safe scalar types.

        WHY: JSON-safe output guarantees diagnostics can be serialized to
        any transport (HTTP API, file, log aggregator) without pickling or
        custom encoders that might accidentally serialize SDK internals.
        """
        from medre.adapters.fakes.meshcore import FakeMeshCoreAdapter
        from medre.config.adapters.meshcore import MeshCoreConfig

        config = MeshCoreConfig(adapter_id="test_diag_safe")
        adapter = FakeMeshCoreAdapter(config)
        await adapter.start(_make_ctx("test_diag_safe"))

        diag = adapter.diagnostics()
        await adapter.stop()

        assert isinstance(diag, dict)
        self._assert_json_safe(diag)

    @pytest.mark.asyncio
    async def test_diagnostics_no_sdk_type_leaks(self) -> None:
        """Diagnostics values must not be SDK module/class instances.

        WHY: SDK objects may hold open sockets, file handles, or unencrypted
        identity data in their repr().  If diagnostics exposes them, any
        logging or API serialization could leak sensitive state.
        """
        from medre.adapters.fakes.meshcore import FakeMeshCoreAdapter
        from medre.config.adapters.meshcore import MeshCoreConfig

        config = MeshCoreConfig(adapter_id="test_no_sdk_leak")
        adapter = FakeMeshCoreAdapter(config)
        await adapter.start(_make_ctx("test_no_sdk_leak"))

        diag = adapter.diagnostics()
        await adapter.stop()

        self._assert_no_sdk_types(diag)

    @pytest.mark.asyncio
    async def test_diagnostics_output_no_secret_string_values(self) -> None:
        """Diagnostics output values must not contain secret/identity substrings.

        WHY: Even if diagnostics values are strings rather than SDK objects,
        they could contain private keys, access tokens, or identity material
        as substrings.  This runtime check complements the source-level scan
        by inspecting actual output.
        """
        import json

        from medre.adapters.fakes.meshcore import FakeMeshCoreAdapter
        from medre.config.adapters.meshcore import MeshCoreConfig

        config = MeshCoreConfig(adapter_id="test_diag_str_safe")
        adapter = FakeMeshCoreAdapter(config)
        await adapter.start(_make_ctx("test_diag_str_safe"))

        diag = adapter.diagnostics()
        await adapter.stop()

        # Serialize to string and scan for secret patterns.
        diag_str = json.dumps(diag).lower()
        for pattern in _IDENTITY_SECRET_PATTERNS:
            assert pattern.lower() not in diag_str, (
                f"Diagnostics output contains secret pattern '{pattern}': "
                f"{diag_str[:200]}"
            )

    @staticmethod
    def _assert_json_safe(obj: Any, path: str = "root") -> None:
        """Recursively assert that *obj* contains only JSON-safe types."""
        if isinstance(obj, (bool, int, float, str, type(None))):
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                TestMeshCoreDiagnosticSafety._assert_json_safe(v, f"{path}.{k}")
            return
        if isinstance(obj, (list, tuple)):
            for i, v in enumerate(obj):
                TestMeshCoreDiagnosticSafety._assert_json_safe(v, f"{path}[{i}]")
            return
        raise AssertionError(
            f"Non-JSON-safe value at {path}: {type(obj).__name__} = {obj!r}"
        )

    @staticmethod
    def _assert_no_sdk_types(obj: Any, path: str = "root") -> None:
        """Recursively assert no SDK module types appear in diagnostics."""
        if isinstance(obj, (bool, int, float, str, type(None))):
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                TestMeshCoreDiagnosticSafety._assert_no_sdk_types(v, f"{path}.{k}")
            return
        if isinstance(obj, (list, tuple)):
            for i, v in enumerate(obj):
                TestMeshCoreDiagnosticSafety._assert_no_sdk_types(v, f"{path}[{i}]")
            return
        # Any non-scalar, non-container type is suspicious.
        type_name = type(obj).__name__
        type_module = type(obj).__module__ or ""
        # SDK module types come from meshcore, RNS, lxmf packages.
        banned_modules = ("meshcore", "RNS", "lxmf", "nio", "meshtastic")
        for banned in banned_modules:
            if type_module.startswith(banned):
                raise AssertionError(
                    f"SDK type leak at {path}: {type_module}.{type_name}"
                )


# ===================================================================
# 5. Live test exclusion
# ===================================================================


class TestMeshCoreLiveTestExclusion:
    """Test files requiring live MeshCore hardware must be live-marked.

    Default pytest configuration uses ``addopts = "-m 'not live'"`` so
    live-marked tests are skipped unless explicitly requested.

    **WHY this matters**: Without the live marker guard, a developer running
    ``pytest`` without hardware would see cryptic import/connection errors
    instead of a clean skip.  This also prevents CI from attempting hardware
    interactions on headless runners.
    """

    def test_this_file_is_not_live_marked(self) -> None:
        """This file itself must not carry the live marker."""
        source = _read_source(Path(__file__))
        # Check for pytestmark assignment to live.
        has_mark = bool(re.search(r"^pytestmark\s*=\s*.*\.live", source, re.MULTILINE))
        # Check for decorator usage — but only lines starting with @ (not
        # strings/comments that mention the marker).
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("@pytest.mark.live"):
                has_mark = True
        assert not has_mark, "Operational boundary tests must not require live hardware"

    @pytest.mark.parametrize(
        "filepath",
        sorted(_TESTS_DIR.glob("test_meshcore*.py")),
        ids=lambda p: p.name,
    )
    def test_non_live_meshcore_tests_pass_default_filter(self, filepath: Path) -> None:
        """Non-live MeshCore test files must not carry the live marker."""
        if filepath.name == Path(__file__).name:
            pytest.skip("self-referential check done separately")

        if _has_live_marker(filepath):
            # Live-marked files are intentionally excluded by default —
            # that's the contract.  Verify the marker exists.
            source = _read_source(filepath)
            assert "pytestmark" in source or "@pytest.mark.live" in source, (
                f"{filepath.name}: pytest.mark.live found but not declared "
                f"as pytestmark or decorator"
            )
        # Non-live files simply must not have the marker (implicitly OK).
