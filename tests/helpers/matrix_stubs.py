"""Shared duck-typed stub configs for Matrix renderer / relay tests.

Extracted from multiple test files to eliminate duplication.  Each stub
provides the minimal attribute surface that MatrixRenderer (or
MeshtasticRenderer) inspects via duck-typed attribute access.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class StubMeshtasticConfig:
    """Minimal duck-typed MeshtasticConfig for source-config resolution."""

    adapter_id: str = "mesh-1"
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
