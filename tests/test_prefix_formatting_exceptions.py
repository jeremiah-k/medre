# medre-test-tier: compatibility
"""Tests for formatting_exception guards in MeshCore and LXMF renderers.

When ``format_relay_prefix`` catches an internal exception, it returns the
raw template as ``rendered_prefix`` with ``formatting_error`` starting with
``"formatting_exception:"``.  Renderers reject this failed evaluation instead of delivering a raw
template or silently omitting attribution.

Since the formatter uses regex-based substitution (not ``str.format()``),
unmatched braces do not trigger exceptions.  Tests trigger real exceptions
by patching the internal ``_build_variable_map`` helper in the defining
module, so ``format_relay_prefix`` exercises its own try/except handler.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from medre.adapters.meshcore.renderer import MeshCoreRenderer
from medre.config.adapters.meshcore import MeshCoreConfig
from medre.core.events import (
    CanonicalEvent,
    EventMetadata,
    NativeMetadata,
)
from medre.core.rendering.renderer import RenderingContext
from tests.helpers.lxmf_renderer import lxmf_renderer_with_prefix

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_meshcore_config(
    adapter_id: str = "mc-1",
    *,
    relay_prefix: str = "",
    max_text_bytes: int = 512,
) -> MeshCoreConfig:
    return MeshCoreConfig(
        adapter_id=adapter_id,
        relay_prefix=relay_prefix,
        max_text_bytes=max_text_bytes,
    )


def _make_event(
    source_adapter: str = "matrix-1",
    body: str = "hello world",
) -> CanonicalEvent:
    return CanonicalEvent(
        event_id="evt-1",
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="transport-1",
        source_channel_id="ch-1",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"body": body},
        metadata=EventMetadata(native=NativeMetadata(data={})),
    )


# ===================================================================
# MeshCore formatting_exception guard
# ===================================================================


async def test_meshcore_mocked_exception_guard() -> None:
    """On formatting_exception, raw template is NOT prepended to text."""
    template = "[{origin_label}]: "
    cfg = _make_meshcore_config(relay_prefix=template)
    renderer = MeshCoreRenderer(configs={"mc-1": cfg})
    event = _make_event()
    # Patch inside the defining module so the real format_relay_prefix
    # exercises its own try/except handler.  Avoids brittle imported
    # binding patches that may not take effect in some CI environments.
    with patch(
        "medre.core.rendering.attribution._build_variable_map",
        side_effect=RuntimeError("boom"),
    ):
        with pytest.raises(ValueError, match="formatting_exception"):
            await renderer.render(
                event,
                RenderingContext(target_adapter="mc-1", delivery_strategy="direct"),
            )


async def test_meshcore_unknown_placeholder_rejected() -> None:
    """Non-exception formatting (unknown placeholder) still prepends prefix."""
    # {meshnet_name} is unknown → rendered as literal, no exception
    template = "[{meshnet_name}]: "
    cfg = _make_meshcore_config(relay_prefix=template)
    renderer = MeshCoreRenderer(configs={"mc-1": cfg})
    event = _make_event()
    with pytest.raises(ValueError, match="unknown placeholder"):
        await renderer.render(
            event,
            RenderingContext(target_adapter="mc-1", delivery_strategy="direct"),
        )


# ===================================================================
# LXMF formatting_exception guard
# ===================================================================


async def test_lxmf_mocked_exception_guard() -> None:
    """On formatting_exception, raw template is NOT prepended to text."""
    template = "[{origin_label}]: "
    renderer = lxmf_renderer_with_prefix(template, target_adapter="lxmf-1")
    event = _make_event()
    # Patch inside the defining module so the real format_relay_prefix
    # exercises its own try/except handler.  Avoids brittle imported
    # binding patches that may not take effect in some CI environments.
    with patch(
        "medre.core.rendering.attribution._build_variable_map",
        side_effect=RuntimeError("boom"),
    ):
        with pytest.raises(ValueError, match="formatting_exception"):
            await renderer.render(
                event,
                RenderingContext(target_adapter="lxmf-1", delivery_strategy="direct"),
            )


async def test_lxmf_unknown_placeholder_rejected() -> None:
    """Non-exception formatting (unknown placeholder) still prepends prefix."""
    template = "[{meshnet_name}]: "
    renderer = lxmf_renderer_with_prefix(template, target_adapter="lxmf-1")
    event = _make_event()
    with pytest.raises(ValueError, match="unknown placeholder"):
        await renderer.render(
            event,
            RenderingContext(target_adapter="lxmf-1", delivery_strategy="direct"),
        )
