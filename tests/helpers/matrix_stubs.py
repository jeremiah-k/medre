"""Shared duck-typed stub configs for Matrix renderer / relay tests.

Extracted from multiple test files to eliminate duplication.  Each stub
provides the minimal attribute surface that MatrixRenderer (or
MeshtasticRenderer) inspects via duck-typed attribute access.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class StubMeshtasticConfig:
    """Minimal duck-typed MeshtasticConfig for source-config resolution.

    NOTE: ``meshnet_name`` is retained for mmrelay KEY_MESHNET wire-compat
    testing only -- legacy mmrelay-compatible sources still populate it so the
    renderer can emit the ``{meshnet_name}`` template variable for wire
    compatibility.  It MUST NOT be passed by non-mmrelay call sites.
    """

    adapter_id: str = "mesh-1"
    meshnet_name: str = ""
    mmrelay_compatibility: bool = False


@dataclass(slots=True)
class StubSourceAttribution:
    """Minimal duck-typed SourceAttributionConfig for renderer tests."""

    adapter_id: str = ""
    origin_label: str = ""


@dataclass(slots=True)
class StubMatrixConfig:
    """Minimal duck-typed MatrixConfig for target-local relay_prefix tests."""

    adapter_id: str = "matrix-1"
    relay_prefix: str = ""
