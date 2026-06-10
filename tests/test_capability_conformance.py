"""Capability conformance tests: validates that machine-readable capability
JSON files match the actual AdapterCapabilities declared in adapter source code.

Reads each ``*-capabilities.json`` alongside the transport profile markdown and
compares every documented capability against the corresponding adapter class.

Missing fields and value mismatches cause hard failures.  The suite also
verifies that every AdapterCapabilities field is present in each JSON file
and that every JSON key corresponds to a valid capability field.
"""

from __future__ import annotations

import json
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import patch

import pytest

from medre.core.contracts.adapter import AdapterCapabilities

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TRANSPORTS = ("matrix", "meshtastic", "meshcore", "lxmf")

_SENTINEL = object()

PROFILES_DIR = (
    Path(__file__).resolve().parent.parent / "docs" / "spec" / "transport-profiles"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_OPTIONAL_SDK_PREFIXES = ("nio", "meshtastic", "meshcore", "RNS", "LXMF")


def _is_optional_sdk_import_error(exc: ImportError) -> bool:
    """Return True if *exc* was caused by a missing optional external SDK.

    Known optional SDKs: nio (Matrix), meshtastic, meshcore, RNS/LXMF
    (Reticulum).  MEDRE-internal ``ImportError`` (e.g. a typo in
    ``medre.adapters.*``) must propagate so that the test fails loudly.
    """
    return exc.name is not None and any(
        exc.name == prefix or exc.name.startswith(prefix + ".")
        for prefix in _OPTIONAL_SDK_PREFIXES
    )


def _load_capabilities_json(transport: str) -> dict[str, Any]:
    """Load the capabilities JSON for *transport*."""
    path = PROFILES_DIR / f"{transport}-capabilities.json"
    assert path.exists(), f"Missing capabilities file: {path}"
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    assert (
        data["transport"] == transport
    ), f"JSON transport field mismatch: expected {transport!r}, got {data['transport']!r}"
    return data["capabilities"]


def _get_adapter_capabilities(transport: str) -> AdapterCapabilities:
    """Import the adapter module and return its declared AdapterCapabilities.

    Raises ``pytest.skip`` when optional SDKs are not installed, so the
    suite runs cleanly in reduced-dependency environments.
    MEDRE-internal ``ImportError`` is re-raised (test failure) rather than
    skipped.
    """
    if transport == "matrix":
        try:
            from medre.adapters.matrix.adapter import _MATRIX_CAPABILITIES
        except ImportError as exc:
            if _is_optional_sdk_import_error(exc):
                pytest.skip(f"Matrix SDK not available: {exc}")
            raise
        return _MATRIX_CAPABILITIES

    if transport == "lxmf":
        try:
            from medre.adapters.lxmf.adapter import _LXMF_CAPABILITIES
        except ImportError as exc:
            if _is_optional_sdk_import_error(exc):
                pytest.skip(f"LXMF SDK not available: {exc}")
            raise
        return _LXMF_CAPABILITIES

    if transport == "meshtastic":
        try:
            from medre.adapters.meshtastic.adapter import MeshtasticAdapter
            from medre.config.adapters.meshtastic import MeshtasticConfig
        except ImportError as exc:
            if _is_optional_sdk_import_error(exc):
                pytest.skip(f"Meshtastic SDK not available: {exc}")
            raise

        config = MeshtasticConfig(adapter_id="test_conformance")
        adapter = MeshtasticAdapter(config)
        return adapter._capabilities

    if transport == "meshcore":
        try:
            from medre.adapters.meshcore.adapter import MeshCoreAdapter
            from medre.config.adapters.meshcore import MeshCoreConfig
        except ImportError as exc:
            if _is_optional_sdk_import_error(exc):
                pytest.skip(f"MeshCore SDK not available: {exc}")
            raise

        config = MeshCoreConfig(adapter_id="test_conformance")
        adapter = MeshCoreAdapter(config)
        return adapter._capabilities

    raise ValueError(f"Unknown transport: {transport}")


# ---------------------------------------------------------------------------
# Parameterised conformance test
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_capability_values_match_code(transport: str) -> None:
    """Every capability value in the JSON must match the adapter source code."""
    json_caps = _load_capabilities_json(transport)
    code_caps = _get_adapter_capabilities(transport)

    mismatches: list[str] = []

    for key, json_val in json_caps.items():
        actual = getattr(code_caps, key, _SENTINEL)
        if actual is _SENTINEL:
            mismatches.append(
                f"  {key}: documented but absent from AdapterCapabilities"
            )
            continue
        if actual != json_val:
            mismatches.append(f"  {key}: json={json_val!r}, code={actual!r}")

    msg_lines = [f"Capability value mismatches for {transport}:"]
    msg_lines.extend(mismatches)
    assert not mismatches, "\n".join(msg_lines)


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_no_undocumented_capabilities(transport: str) -> None:
    """Every AdapterCapabilities field must appear in the JSON."""
    json_caps = _load_capabilities_json(transport)
    _ = _get_adapter_capabilities(transport)  # triggers pytest.skip if SDK unavailable

    missing = []
    for field in dataclass_fields(AdapterCapabilities):
        if field.name not in json_caps:
            missing.append(field.name)

    assert (
        not missing
    ), f"{transport}: undocumented AdapterCapabilities fields: {missing}"


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_json_keys_are_valid_capability_fields(transport: str) -> None:
    """Every key in the JSON must be a real AdapterCapabilities field."""
    json_caps = _load_capabilities_json(transport)
    valid_fields = {f.name for f in dataclass_fields(AdapterCapabilities)}

    invalid = sorted(set(json_caps.keys()) - valid_fields)
    assert not invalid, (
        f"Unknown capability keys in {transport}-capabilities.json: {invalid}\n"
        f"Valid fields: {sorted(valid_fields)}"
    )


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_markdown_references_capability_json(transport: str) -> None:
    """Each transport profile markdown must reference its capability JSON file."""
    md_path = PROFILES_DIR / f"{transport}.md"
    json_filename = f"{transport}-capabilities.json"
    content = md_path.read_text()
    assert json_filename in content, (
        f"{transport}.md does not reference {json_filename}. "
        f"Add a link to the machine-readable capability declaration."
    )


# ---------------------------------------------------------------------------
# Regression: ImportError discrimination in _get_adapter_capabilities
# ---------------------------------------------------------------------------


class TestIsOptionalSdkImportError:
    """Unit tests for the _is_optional_sdk_import_error helper."""

    def test_known_sdk_top_level_names_match(self) -> None:
        for name in _OPTIONAL_SDK_PREFIXES:
            assert _is_optional_sdk_import_error(
                ImportError(name=name)
            ), f"{name} should be recognised as an optional SDK"

    def test_sdk_submodule_names_match(self) -> None:
        assert _is_optional_sdk_import_error(ImportError(name="nio.client"))
        assert _is_optional_sdk_import_error(ImportError(name="RNS.Interfaces"))
        assert _is_optional_sdk_import_error(ImportError(name="meshtastic.protobuf"))

    def test_medre_internal_names_do_not_match(self) -> None:
        assert not _is_optional_sdk_import_error(
            ImportError(name="medre.adapters.matrix.adapter")
        )
        assert not _is_optional_sdk_import_error(
            ImportError(name="medre.adapters.lxmf.adapter")
        )
        assert not _is_optional_sdk_import_error(
            ImportError(name="medre.config.adapters.meshtastic")
        )

    def test_none_name_does_not_match(self) -> None:
        """ImportError without a .name attribute must not be treated as optional."""
        assert not _is_optional_sdk_import_error(ImportError("something broke"))


class TestGetAdapterCapabilitiesImportErrorPropagation:
    """Regression: MEDRE-internal ImportError propagates; optional SDK absence
    triggers pytest.skip."""

    @staticmethod
    def _make_import_raising_for(target_module: str, error_name: str) -> object:
        """Return a ``__import__`` replacement that raises for *target_module*."""
        import builtins

        real_import = builtins.__import__

        def _fake_import(
            name: str,
            globals: Mapping[str, object] | None = None,
            locals: Mapping[str, object] | None = None,
            fromlist: tuple[str, ...] = (),
            level: int = 0,
        ) -> object:
            if name == target_module:
                raise ImportError(f"simulated: {error_name}", name=error_name)
            return real_import(name, globals, locals, fromlist, level)

        return _fake_import

    def test_internal_import_error_propagates(self) -> None:
        """MEDRE-internal ImportError must re-raise, not pytest.skip."""
        import builtins
        import sys

        key = "medre.adapters.matrix.adapter"
        saved = sys.modules.pop(key, None)
        try:
            fake = self._make_import_raising_for(
                key, "medre.adapters.matrix.buggy_module"
            )
            with patch.object(builtins, "__import__", fake):
                with pytest.raises(
                    ImportError, match="medre.adapters.matrix.buggy_module"
                ):
                    _get_adapter_capabilities("matrix")
        finally:
            if saved is not None:
                sys.modules[key] = saved

    def test_optional_sdk_import_error_skips(self) -> None:
        """Missing optional SDK (e.g. nio) triggers pytest.skip."""
        import builtins
        import sys

        key = "medre.adapters.matrix.adapter"
        saved = sys.modules.pop(key, None)
        try:
            fake = self._make_import_raising_for(key, "nio")
            with patch.object(builtins, "__import__", fake):
                with pytest.raises(
                    pytest.skip.Exception, match="Matrix SDK not available"
                ):
                    _get_adapter_capabilities("matrix")
        finally:
            if saved is not None:
                sys.modules[key] = saved
