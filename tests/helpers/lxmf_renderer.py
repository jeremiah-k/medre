"""LXMF renderer fixtures using the supported target-config surface."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from medre.adapters.lxmf.renderer import LxmfRenderer
from medre.config.adapters.lxmf import LxmfConfig


def lxmf_renderer_with_prefix(
    prefix: str,
    *,
    target_adapter: str = "lxmf_node",
    metadata_embedding: bool = True,
    source_attribution: Mapping[str, Any] | None = None,
) -> LxmfRenderer:
    """Build an LXMF renderer with one target adapter prefix config."""
    config = LxmfConfig(
        adapter_id=target_adapter,
        connection_type="fake",
        lxmf_relay_prefix=prefix,
    )
    return LxmfRenderer(
        metadata_embedding=metadata_embedding,
        configs={target_adapter: config},
        source_attribution=dict(source_attribution or {}),
    )
