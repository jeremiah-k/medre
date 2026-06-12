"""Shared duck-typed stub configs for Matrix renderer / relay tests.

Extracted from multiple test files to eliminate duplication.  Each stub
provides the minimal attribute surface that MatrixRenderer (or
MeshtasticRenderer) inspects via duck-typed attribute access.
"""

from __future__ import annotations


class StubMeshtasticConfig:
    """Minimal duck-typed MeshtasticConfig for source-config resolution.

    NOTE: ``meshnet_name`` is vestigial -- the renderer no longer uses it as a
    template variable, but the field is kept here because some tests still
    reference it via attribute access.
    """

    def __init__(
        self,
        adapter_id: str = "mesh-1",
        meshnet_name: str = "",
        mmrelay_compatibility: bool = False,
    ) -> None:
        self.adapter_id = adapter_id
        self.meshnet_name = meshnet_name
        self.mmrelay_compatibility = mmrelay_compatibility


class StubSourceAttribution:
    """Minimal duck-typed SourceAttributionConfig for renderer tests."""

    def __init__(
        self,
        adapter_id: str = "",
        origin_label: str = "",
        meshnet_name: str = "",
    ) -> None:
        self.adapter_id = adapter_id
        self.origin_label = origin_label
        self.meshnet_name = meshnet_name


class StubMatrixConfig:
    """Minimal duck-typed MatrixConfig for target-local relay_prefix tests."""

    def __init__(
        self,
        adapter_id: str = "matrix-1",
        relay_prefix: str = "",
    ) -> None:
        self.adapter_id = adapter_id
        self.relay_prefix = relay_prefix
