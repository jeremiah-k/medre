"""Capability conformance tests: validates that machine-readable capability
JSON files match the actual AdapterCapabilities declared in adapter source code.

Reads each ``*-capabilities.json`` alongside the transport profile markdown and
compares every documented capability against the corresponding adapter class.
Also reports *undocumented* capabilities — fields present in the code but not
in the JSON file.

Gaps are expected and reported as findings; value mismatches cause hard
failures.
"""

from __future__ import annotations

import json
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import Any

import pytest

from medre.core.contracts.adapter import AdapterCapabilities

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TRANSPORTS = ("matrix", "meshtastic", "meshcore", "lxmf")

PROFILES_DIR = (
    Path(__file__).resolve().parent.parent / "docs" / "spec" / "transport-profiles"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
    """
    if transport == "matrix":
        try:
            from medre.adapters.matrix.adapter import _MATRIX_CAPABILITIES
        except ImportError as exc:
            pytest.skip(f"Matrix SDK not available: {exc}")
        return _MATRIX_CAPABILITIES

    if transport == "lxmf":
        try:
            from medre.adapters.lxmf.adapter import _LXMF_CAPABILITIES
        except ImportError as exc:
            pytest.skip(f"LXMF SDK not available: {exc}")
        return _LXMF_CAPABILITIES

    if transport == "meshtastic":
        try:
            from medre.adapters.meshtastic.adapter import MeshtasticAdapter
            from medre.config.adapters.meshtastic import MeshtasticConfig
        except ImportError as exc:
            pytest.skip(f"Meshtastic SDK not available: {exc}")

        config = MeshtasticConfig(adapter_id="test_conformance")
        adapter = MeshtasticAdapter(config)
        return adapter._capabilities

    if transport == "meshcore":
        try:
            from medre.adapters.meshcore.adapter import MeshCoreAdapter
            from medre.config.adapters.meshcore import MeshCoreConfig
        except ImportError as exc:
            pytest.skip(f"MeshCore SDK not available: {exc}")

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
    _get_adapter_capabilities(transport)

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


# Sentinel for missing attributes (distinct from any real value including None).
_SENTINEL = object()
