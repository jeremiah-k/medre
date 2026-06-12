"""Tests for formatting_exception guards in MeshCore and LXMF renderers.

When ``format_relay_prefix`` catches an internal exception, it returns the
raw template as ``rendered_prefix`` with ``formatting_error`` starting with
``"formatting_exception:"``.  The renderers must NOT prepend the raw
template to user-facing text, but MUST still record all 6 normalized
metadata keys.

Since the formatter uses regex-based substitution (not ``str.format()``),
unmatched braces do NOT trigger exceptions.  Tests use ``unittest.mock``
to simulate the exception path.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

from medre.adapters.lxmf.renderer import LxmfRenderer
from medre.adapters.meshcore.renderer import MeshCoreRenderer
from medre.config.adapters.meshcore import MeshCoreConfig
from medre.core.events import (
    CanonicalEvent,
    EventMetadata,
    NativeMetadata,
)
from medre.core.rendering.attribution import PrefixFormatterResult
from medre.core.rendering.renderer import RenderingContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_meshcore_config(
    adapter_id: str = "mc-1",
    *,
    meshcore_relay_prefix: str = "",
    max_text_bytes: int = 512,
) -> MeshCoreConfig:
    return MeshCoreConfig(
        adapter_id=adapter_id,
        meshcore_relay_prefix=meshcore_relay_prefix,
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


def _make_exception_result(template: str) -> PrefixFormatterResult:
    """A PrefixFormatterResult simulating a formatting_exception."""
    return PrefixFormatterResult(
        rendered_prefix=template,
        template_used=template,
        variables_used=(),
        missing_variables=(),
        unknown_variables=(),
        formatting_error="formatting_exception: simulated error",
    )


# ===================================================================
# MeshCore formatting_exception guard
# ===================================================================


class TestMeshCoreFormattingExceptionGuard:
    """MeshCore renderer does not prepend raw template on formatting_exception."""

    async def test_mocked_exception_guard(self) -> None:
        """On formatting_exception, raw template is NOT prepended to text."""
        template = "[{origin_label}]: "
        cfg = _make_meshcore_config(meshcore_relay_prefix=template)
        renderer = MeshCoreRenderer(configs={"mc-1": cfg})
        event = _make_event()
        exc_result = _make_exception_result(template)
        with patch(
            "medre.adapters.meshcore.renderer.format_relay_prefix",
            return_value=exc_result,
        ):
            result = await renderer.render(
                event,
                RenderingContext(target_adapter="mc-1", delivery_strategy="direct"),
            )
        text = result.payload["text"]
        # Raw template NOT prepended — only original body text
        assert template not in text
        assert text == "hello world"

    async def test_mocked_metadata_records_all_six_keys(self) -> None:
        """All 6 normalized metadata keys are recorded even on exception."""
        template = "[{origin_label}]: "
        cfg = _make_meshcore_config(meshcore_relay_prefix=template)
        renderer = MeshCoreRenderer(configs={"mc-1": cfg})
        event = _make_event()
        exc_result = _make_exception_result(template)
        with patch(
            "medre.adapters.meshcore.renderer.format_relay_prefix",
            return_value=exc_result,
        ):
            result = await renderer.render(
                event,
                RenderingContext(target_adapter="mc-1", delivery_strategy="direct"),
            )
        meta = result.metadata
        assert meta["relay_prefix_template"] == template
        assert meta["relay_prefix_rendered"] == template
        assert meta["relay_prefix_variables_used"] == ()
        assert meta["relay_prefix_missing_variables"] == ()
        assert meta["relay_prefix_unknown_variables"] == ()
        assert meta["relay_prefix_formatting_error"] is not None
        assert meta["relay_prefix_formatting_error"].startswith("formatting_exception:")

    async def test_normal_unknown_placeholder_still_prepended(self) -> None:
        """Non-exception formatting (unknown placeholder) still prepends prefix."""
        # {meshnet_name} is unknown → rendered as literal, no exception
        template = "[{meshnet_name}]: "
        cfg = _make_meshcore_config(meshcore_relay_prefix=template)
        renderer = MeshCoreRenderer(configs={"mc-1": cfg})
        event = _make_event()
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="mc-1", delivery_strategy="direct"),
        )
        text = result.payload["text"]
        # Unknown placeholder → prefix prepended with literal {meshnet_name}
        assert text.startswith("[{meshnet_name}]: ")
        assert "hello world" in text


# ===================================================================
# LXMF formatting_exception guard
# ===================================================================


class TestLxmfFormattingExceptionGuard:
    """LXMF renderer does not prepend raw template on formatting_exception."""

    async def test_mocked_exception_guard(self) -> None:
        """On formatting_exception, raw template is NOT prepended to text."""
        template = "[{origin_label}]: "
        renderer = LxmfRenderer(relay_prefix=template)
        event = _make_event()
        exc_result = _make_exception_result(template)
        with patch(
            "medre.adapters.lxmf.renderer.format_relay_prefix",
            return_value=exc_result,
        ):
            result = await renderer.render(
                event,
                RenderingContext(target_adapter="lxmf-1", delivery_strategy="direct"),
            )
        text = result.payload["content"]
        # Raw template NOT prepended — only original body text
        assert template not in text
        assert text == "hello world"

    async def test_mocked_metadata_records_all_six_keys(self) -> None:
        """All 6 normalized metadata keys are recorded even on exception."""
        template = "[{origin_label}]: "
        renderer = LxmfRenderer(relay_prefix=template)
        event = _make_event()
        exc_result = _make_exception_result(template)
        with patch(
            "medre.adapters.lxmf.renderer.format_relay_prefix",
            return_value=exc_result,
        ):
            result = await renderer.render(
                event,
                RenderingContext(target_adapter="lxmf-1", delivery_strategy="direct"),
            )
        meta = result.metadata
        assert meta["relay_prefix_template"] == template
        assert meta["relay_prefix_rendered"] == template
        assert meta["relay_prefix_variables_used"] == ()
        assert meta["relay_prefix_missing_variables"] == ()
        assert meta["relay_prefix_unknown_variables"] == ()
        assert meta["relay_prefix_formatting_error"] is not None
        assert meta["relay_prefix_formatting_error"].startswith("formatting_exception:")

    async def test_normal_unknown_placeholder_still_prepended(self) -> None:
        """Non-exception formatting (unknown placeholder) still prepends prefix."""
        template = "[{meshnet_name}]: "
        renderer = LxmfRenderer(relay_prefix=template)
        event = _make_event()
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="lxmf-1", delivery_strategy="direct"),
        )
        text = result.payload["content"]
        # Unknown placeholder → prefix prepended with literal {meshnet_name}
        assert text.startswith("[{meshnet_name}]: ")
        assert "hello world" in text
