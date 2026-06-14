"""Tests for route/context origin_label precedence, generic variables
across transports, exception guards, and mmrelay interop isolation.

Covers:
- ctx.source_origin_label overrides adapter origin_label in prefix rendering
  (Matrix, Meshtastic, MeshCore, LXMF).
- Adapter origin_label used when no route/context label.
- Missing label is safe (empty, not "None").
- Generic variables across transports:
  Meshtastic→Matrix sender/origin_label, Matrix→Meshtastic sender_short/
  origin_label, MeshCore→Meshtastic sender_id/origin_label,
  LXMF→MeshCore sender_short/sender_id/origin_label.
- MeshCore/LXMF formatting_exception guard: raw template not prepended,
  prefix metadata still recorded.
- mmrelay derive_meshnet_value helper.
- Unknown {meshnet_name} remains unknown outside mmrelay compat.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

from medre.adapters.lxmf.renderer import LxmfRenderer
from medre.adapters.matrix.renderer import MatrixRenderer
from medre.adapters.meshcore.renderer import MeshCoreRenderer
from medre.adapters.meshtastic.renderer import MeshtasticRenderer
from medre.config.adapters.meshcore import MeshCoreConfig
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.events import (
    CanonicalEvent,
    EventMetadata,
    NativeMetadata,
)
from medre.core.rendering.renderer import RenderingContext
from medre.interop.mmrelay import (
    KEY_MESHNET,
    derive_meshnet_value,
)
from tests.helpers.matrix_stubs import StubMatrixConfig as _StubMatrixConfig
from tests.helpers.matrix_stubs import StubMeshtasticConfig as _StubMeshtasticConfig
from tests.helpers.matrix_stubs import StubSourceAttribution as _StubSourceAttribution

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    source_adapter: str = "src-a",
    payload: dict | None = None,
    native_data: dict | None = None,
) -> CanonicalEvent:
    metadata = EventMetadata()
    if native_data:
        metadata = EventMetadata(native=NativeMetadata(data=native_data))
    return CanonicalEvent(
        event_id="evt-prec-1",
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="transport-1",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload=payload or {"body": "hello"},
        metadata=metadata,
    )


# ===================================================================
# Matrix: origin_label precedence
# ===================================================================


async def test_matrix_route_label_overrides_adapter_label() -> None:
    """Route context origin_label overrides adapter registry label."""
    renderer = MatrixRenderer(
        configs={
            "matrix-1": _StubMatrixConfig(
                adapter_id="matrix-1",
                relay_prefix="[{origin_label}] ",
            ),
        },
        source_attribution={
            "src-a": _StubSourceAttribution(
                adapter_id="src-a",
                origin_label="Adapter Label",
            ),
        },
    )
    event = _make_event(source_adapter="src-a")
    result = await renderer.render(
        event,
        RenderingContext(
            target_adapter="matrix-1",
            delivery_strategy="direct",
            target_platform="matrix",
            source_origin_label="Route Label",
        ),
    )
    assert result.payload["body"] == "[Route Label] hello"


async def test_matrix_adapter_label_used_when_no_route_label() -> None:
    """Adapter registry label used when ctx.source_origin_label is None."""
    renderer = MatrixRenderer(
        configs={
            "matrix-1": _StubMatrixConfig(
                adapter_id="matrix-1",
                relay_prefix="[{origin_label}] ",
            ),
        },
        source_attribution={
            "src-a": _StubSourceAttribution(
                adapter_id="src-a",
                origin_label="Adapter Label",
            ),
        },
    )
    event = _make_event(source_adapter="src-a")
    result = await renderer.render(
        event,
        RenderingContext(
            target_adapter="matrix-1",
            delivery_strategy="direct",
            target_platform="matrix",
            source_origin_label=None,
        ),
    )
    assert result.payload["body"] == "[Adapter Label] hello"


async def test_matrix_missing_label_safe() -> None:
    """Missing label renders empty, not 'None'."""
    renderer = MatrixRenderer(
        configs={
            "matrix-1": _StubMatrixConfig(
                adapter_id="matrix-1",
                relay_prefix="[{origin_label}] ",
            ),
        },
    )
    event = _make_event(source_adapter="src-a")
    result = await renderer.render(
        event,
        RenderingContext(
            target_adapter="matrix-1",
            delivery_strategy="direct",
            target_platform="matrix",
        ),
    )
    assert result.payload["body"] == "[] hello"
    assert "None" not in result.payload["body"]


# ===================================================================
# Matrix: mmrelay KEY_MESHNET uses route origin_label
# ===================================================================


async def test_mmrelay_meshnet_from_route_label() -> None:
    """KEY_MESHNET uses route origin_label over adapter label."""
    renderer = MatrixRenderer(
        source_configs={
            "src-a": _StubMeshtasticConfig(
                adapter_id="src-a",
                mmrelay_compatibility=True,
            ),
        },
        source_attribution={
            "src-a": _StubSourceAttribution(
                adapter_id="src-a",
                origin_label="Adapter Net",
            ),
        },
    )
    event = _make_event(
        source_adapter="src-a",
        native_data={"longname": "Alice", "shortname": "A", "packet_id": "1"},
    )
    result = await renderer.render(
        event,
        RenderingContext(
            target_adapter="matrix-1",
            delivery_strategy="direct",
            target_platform="matrix",
            source_origin_label="Route Net",
        ),
    )
    assert result.payload[KEY_MESHNET] == "Route Net"


async def test_mmrelay_meshnet_fallback_to_adapter() -> None:
    """KEY_MESHNET falls back to adapter label when no route label."""
    renderer = MatrixRenderer(
        source_configs={
            "src-a": _StubMeshtasticConfig(
                adapter_id="src-a",
                mmrelay_compatibility=True,
            ),
        },
        source_attribution={
            "src-a": _StubSourceAttribution(
                adapter_id="src-a",
                origin_label="Adapter Net",
            ),
        },
    )
    event = _make_event(
        source_adapter="src-a",
        native_data={"longname": "Alice", "shortname": "A", "packet_id": "1"},
    )
    result = await renderer.render(
        event,
        RenderingContext(
            target_adapter="matrix-1",
            delivery_strategy="direct",
            target_platform="matrix",
        ),
    )
    assert result.payload[KEY_MESHNET] == "Adapter Net"


# ===================================================================
# Meshtastic: origin_label precedence
# ===================================================================


async def test_meshtastic_route_label_overrides_adapter_label() -> None:
    """Route context origin_label overrides adapter registry."""
    config = MeshtasticConfig(
        adapter_id="mesh-1",
        radio_relay_prefix="[{origin_label}] ",
    )
    renderer = MeshtasticRenderer(
        configs={"mesh-1": config},
        source_attribution={
            "src-a": _StubSourceAttribution(
                adapter_id="src-a",
                origin_label="Adapter Label",
            ),
        },
    )
    event = _make_event(source_adapter="src-a")
    result = await renderer.render(
        event,
        RenderingContext(
            target_adapter="mesh-1",
            delivery_strategy="direct",
            target_platform="meshtastic",
            source_origin_label="Route Label",
        ),
    )
    assert result.payload["text"] == "[Route Label] hello"


async def test_meshtastic_adapter_label_used_when_no_route_label() -> None:
    """Adapter registry label used when no route label."""
    config = MeshtasticConfig(
        adapter_id="mesh-1",
        radio_relay_prefix="[{origin_label}] ",
    )
    renderer = MeshtasticRenderer(
        configs={"mesh-1": config},
        source_attribution={
            "src-a": _StubSourceAttribution(
                adapter_id="src-a",
                origin_label="Adapter Label",
            ),
        },
    )
    event = _make_event(source_adapter="src-a")
    result = await renderer.render(
        event,
        RenderingContext(
            target_adapter="mesh-1",
            delivery_strategy="direct",
            target_platform="meshtastic",
        ),
    )
    assert result.payload["text"] == "[Adapter Label] hello"


async def test_meshtastic_missing_label_safe() -> None:
    """Missing label renders empty, not 'None'."""
    config = MeshtasticConfig(
        adapter_id="mesh-1",
        radio_relay_prefix="[{origin_label}] ",
    )
    renderer = MeshtasticRenderer(configs={"mesh-1": config})
    event = _make_event(source_adapter="src-a")
    result = await renderer.render(
        event,
        RenderingContext(
            target_adapter="mesh-1",
            delivery_strategy="direct",
            target_platform="meshtastic",
        ),
    )
    assert result.payload["text"] == "[] hello"
    assert "None" not in result.payload["text"]


# ===================================================================
# MeshCore: origin_label precedence
# ===================================================================


async def test_meshcore_route_label_overrides_adapter_label() -> None:
    """Route context origin_label overrides adapter registry."""
    config = MeshCoreConfig(
        adapter_id="mc-1",
        meshcore_relay_prefix="[{origin_label}] ",
    )
    renderer = MeshCoreRenderer(
        configs={"mc-1": config},
        source_attribution={
            "src-a": _StubSourceAttribution(
                adapter_id="src-a",
                origin_label="Adapter Label",
            ),
        },
    )
    event = _make_event(source_adapter="src-a")
    result = await renderer.render(
        event,
        RenderingContext(
            target_adapter="mc-1",
            delivery_strategy="direct",
            target_platform="meshcore",
            source_origin_label="Route Label",
        ),
    )
    assert result.payload["text"] == "[Route Label] hello"


async def test_meshcore_adapter_label_used_when_no_route_label() -> None:
    """Adapter registry label used when no route label."""
    config = MeshCoreConfig(
        adapter_id="mc-1",
        meshcore_relay_prefix="[{origin_label}] ",
    )
    renderer = MeshCoreRenderer(
        configs={"mc-1": config},
        source_attribution={
            "src-a": _StubSourceAttribution(
                adapter_id="src-a",
                origin_label="Adapter Label",
            ),
        },
    )
    event = _make_event(source_adapter="src-a")
    result = await renderer.render(
        event,
        RenderingContext(
            target_adapter="mc-1",
            delivery_strategy="direct",
            target_platform="meshcore",
        ),
    )
    assert result.payload["text"] == "[Adapter Label] hello"


# ===================================================================
# LXMF: origin_label precedence
# ===================================================================


async def test_lxmf_route_label_overrides_adapter_label() -> None:
    """Route context origin_label overrides adapter registry."""
    renderer = LxmfRenderer(
        relay_prefix="[{origin_label}] ",
        source_attribution={
            "src-a": _StubSourceAttribution(
                adapter_id="src-a",
                origin_label="Adapter Label",
            ),
        },
    )
    event = _make_event(source_adapter="src-a")
    result = await renderer.render(
        event,
        RenderingContext(
            target_adapter="lxmf_node",
            delivery_strategy="direct",
            target_platform="lxmf",
            source_origin_label="Route Label",
        ),
    )
    assert result.payload["content"] == "[Route Label] hello"


async def test_lxmf_adapter_label_used_when_no_route_label() -> None:
    """Adapter registry label used when no route label."""
    renderer = LxmfRenderer(
        relay_prefix="[{origin_label}] ",
        source_attribution={
            "src-a": _StubSourceAttribution(
                adapter_id="src-a",
                origin_label="Adapter Label",
            ),
        },
    )
    event = _make_event(source_adapter="src-a")
    result = await renderer.render(
        event,
        RenderingContext(
            target_adapter="lxmf_node",
            delivery_strategy="direct",
            target_platform="lxmf",
        ),
    )
    assert result.payload["content"] == "[Adapter Label] hello"


# ===================================================================
# Generic variables across transports
# ===================================================================


async def test_meshtastic_to_matrix_sender_and_origin_label() -> None:
    """Meshtastic→Matrix: {sender} and {origin_label} in prefix."""
    renderer = MatrixRenderer(
        configs={
            "matrix-1": _StubMatrixConfig(
                adapter_id="matrix-1",
                relay_prefix="[{sender}/{origin_label}] ",
            ),
        },
        source_attribution={
            "src-a": _StubSourceAttribution(
                adapter_id="src-a",
                origin_label="East Net",
            ),
        },
    )
    event = _make_event(
        source_adapter="src-a",
        native_data={"longname": "RadioOp", "shortname": "RO"},
    )
    result = await renderer.render(
        event,
        RenderingContext(
            target_adapter="matrix-1",
            delivery_strategy="direct",
            target_platform="matrix",
            source_origin_label="Route East",
        ),
    )
    assert result.payload["body"] == "[RadioOp/Route East] hello"


async def test_matrix_to_meshtastic_sender_short_and_origin_label() -> None:
    """Matrix→Meshtastic: {sender_short} and {origin_label} in prefix."""
    config = MeshtasticConfig(
        adapter_id="mesh-1",
        radio_relay_prefix="<{sender_short}/{origin_label}> ",
    )
    renderer = MeshtasticRenderer(
        configs={"mesh-1": config},
        source_attribution={
            "matrix-1": _StubSourceAttribution(
                adapter_id="matrix-1",
                origin_label="Matrix Hub",
            ),
        },
    )
    event = _make_event(
        source_adapter="matrix-1",
        native_data={
            "sender": "@alice:matrix.org",
            "displayname": "Alice",
        },
    )
    result = await renderer.render(
        event,
        RenderingContext(
            target_adapter="mesh-1",
            delivery_strategy="direct",
            target_platform="meshtastic",
            source_origin_label="Route Hub",
        ),
    )
    # sender_short = localpart = "alice", origin_label = "Route Hub"
    assert result.payload["text"] == "<alice/Route Hub> hello"


async def test_meshcore_to_meshtastic_sender_id_and_origin_label() -> None:
    """MeshCore→Meshtastic: {sender_id} and {origin_label} in prefix."""
    config = MeshtasticConfig(
        adapter_id="mesh-1",
        radio_relay_prefix="({sender_id}@{origin_label}) ",
    )
    renderer = MeshtasticRenderer(
        configs={"mesh-1": config},
        source_attribution={
            "mc-1": _StubSourceAttribution(
                adapter_id="mc-1",
                origin_label="MC Hub",
            ),
        },
    )
    event = _make_event(
        source_adapter="mc-1",
        native_data={"meshcore.pubkey_prefix": "a1b2c3", "meshcore.channel": 0},
    )
    result = await renderer.render(
        event,
        RenderingContext(
            target_adapter="mesh-1",
            delivery_strategy="direct",
            target_platform="meshtastic",
            source_origin_label="Route MC",
        ),
    )
    assert result.payload["text"] == "(a1b2c3@Route MC) hello"


async def test_lxmf_to_meshcore_sender_id_and_origin_label() -> None:
    """LXMF→MeshCore: {sender_id} and {origin_label} in prefix."""
    config = MeshCoreConfig(
        adapter_id="mc-1",
        meshcore_relay_prefix="[{sender_id}@{origin_label}] ",
    )
    renderer = MeshCoreRenderer(
        configs={"mc-1": config},
        source_attribution={
            "lxmf-1": _StubSourceAttribution(
                adapter_id="lxmf-1",
                origin_label="LXMF Hub",
            ),
        },
    )
    event = _make_event(
        source_adapter="lxmf-1",
        native_data={"source_hash": "feedface"},
    )
    result = await renderer.render(
        event,
        RenderingContext(
            target_adapter="mc-1",
            delivery_strategy="direct",
            target_platform="meshcore",
            source_origin_label="Route LXMF",
        ),
    )
    assert result.payload["text"] == "[feedface@Route LXMF] hello"


# ===================================================================
# MeshCore/LXMF formatting exception guard
# ===================================================================


async def test_meshcore_exception_does_not_prepend_raw_template() -> None:
    """MeshCore: formatting_exception does not prepend raw template text."""
    config = MeshCoreConfig(
        adapter_id="mc-1",
        meshcore_relay_prefix="[{sender}] ",
    )
    renderer = MeshCoreRenderer(configs={"mc-1": config})
    event = _make_event(source_adapter="src-a")

    # Patch inside the defining module so the real format_relay_prefix
    # exercises its own try/except handler.  This avoids the brittle
    # pattern of patching the renderer module's imported binding, which
    # can fail to take effect in some CI environments.
    with patch(
        "medre.core.rendering.attribution._build_variable_map",
        side_effect=RuntimeError("boom"),
    ):
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="mc-1",
                delivery_strategy="direct",
                target_platform="meshcore",
            ),
        )
    # Body should be unchanged — raw template NOT prepended.
    assert result.payload["text"] == "hello"
    # Metadata still records the error and rendered prefix.
    assert result.metadata.get("relay_prefix_formatting_error") is not None
    assert "formatting_exception" in str(
        result.metadata["relay_prefix_formatting_error"]
    )
    # Normalized prefix metadata is still recorded.
    assert result.metadata.get("relay_prefix_rendered") == "[{sender}] "


async def test_lxmf_exception_does_not_prepend_raw_template() -> None:
    """LXMF: formatting_exception does not prepend raw template text."""
    renderer = LxmfRenderer(relay_prefix="[{sender}] ")
    event = _make_event(source_adapter="src-a")

    # Patch inside the defining module so the real format_relay_prefix
    # exercises its own try/except handler.  This avoids the brittle
    # pattern of patching the renderer module's imported binding, which
    # can fail to take effect in some CI environments.
    with patch(
        "medre.core.rendering.attribution._build_variable_map",
        side_effect=RuntimeError("boom"),
    ):
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="lxmf_node",
                delivery_strategy="direct",
                target_platform="lxmf",
            ),
        )
    # Content should be unchanged — raw template NOT prepended.
    assert result.payload["content"] == "hello"
    # Metadata still records the error and rendered prefix.
    assert result.metadata.get("relay_prefix_formatting_error") is not None
    assert "formatting_exception" in str(
        result.metadata["relay_prefix_formatting_error"]
    )
    # Normalized prefix metadata is still recorded.
    assert result.metadata.get("relay_prefix_rendered") == "[{sender}] "


# ===================================================================
# mmrelay derive_meshnet_value helper
# ===================================================================


def test_derive_meshnet_source_origin_label_wins() -> None:
    assert derive_meshnet_value("Route Net", "Adapter Net") == "Route Net"


def test_derive_meshnet_adapter_origin_label_fallback() -> None:
    assert derive_meshnet_value(None, "Adapter Net") == "Adapter Net"


def test_derive_meshnet_empty_source_preserved() -> None:
    """Explicit empty string source_origin_label is preserved as ''."""
    assert derive_meshnet_value("", "Adapter Net") == ""


def test_derive_meshnet_none_source_falls_through() -> None:
    """None source_origin_label falls through to adapter label."""
    assert derive_meshnet_value(None, "Adapter Net") == "Adapter Net"


def test_derive_meshnet_both_empty() -> None:
    assert derive_meshnet_value(None, None) == ""
    assert derive_meshnet_value("", "") == ""


def test_derive_meshnet_source_only() -> None:
    assert derive_meshnet_value("Route Net") == "Route Net"


def test_derive_meshnet_nothing() -> None:
    assert derive_meshnet_value(None) == ""


# ===================================================================
# Unknown {meshnet_name} remains unknown
# ===================================================================


async def test_meshnet_name_unknown_in_meshtastic_prefix() -> None:
    """{meshnet_name} is not a known template variable outside mmrelay."""
    config = MeshtasticConfig(
        adapter_id="mesh-1",
        radio_relay_prefix="[{meshnet_name}] ",
    )
    renderer = MeshtasticRenderer(configs={"mesh-1": config})
    event = _make_event(source_adapter="src-a")
    result = await renderer.render(
        event,
        RenderingContext(
            target_adapter="mesh-1",
            delivery_strategy="direct",
            target_platform="meshtastic",
        ),
    )
    # {meshnet_name} is not resolved — left unchanged
    assert result.payload["text"] == "[{meshnet_name}] hello"
    assert result.metadata.get("relay_prefix_formatting_error") is not None


# ===================================================================
# Explicit empty origin label ("") suppresses adapter fallback
# ===================================================================


async def test_matrix_empty_route_label_suppresses_adapter_label() -> None:
    """Matrix: source_origin_label='' suppresses adapter origin_label."""
    renderer = MatrixRenderer(
        configs={
            "matrix-1": _StubMatrixConfig(
                adapter_id="matrix-1",
                relay_prefix="[{origin_label}] ",
            ),
        },
        source_attribution={
            "src-a": _StubSourceAttribution(
                adapter_id="src-a",
                origin_label="Adapter Label",
            ),
        },
    )
    event = _make_event(source_adapter="src-a")
    result = await renderer.render(
        event,
        RenderingContext(
            target_adapter="matrix-1",
            delivery_strategy="direct",
            target_platform="matrix",
            source_origin_label="",
        ),
    )
    body = result.payload.get("body", "")
    assert body.startswith("[] ")  # empty label, NOT "[Adapter Label]"
    assert "Adapter Label" not in body


async def test_meshtastic_empty_route_label_suppresses_adapter_label() -> None:
    """Meshtastic: source_origin_label='' suppresses adapter origin_label."""
    config = MeshtasticConfig(
        adapter_id="mesh-1",
        radio_relay_prefix="[{origin_label}] ",
    )
    renderer = MeshtasticRenderer(
        configs={"mesh-1": config},
        source_attribution={
            "src-a": _StubSourceAttribution(
                adapter_id="src-a",
                origin_label="Adapter Label",
            ),
        },
    )
    event = _make_event(source_adapter="src-a")
    result = await renderer.render(
        event,
        RenderingContext(
            target_adapter="mesh-1",
            delivery_strategy="direct",
            target_platform="meshtastic",
            source_origin_label="",
        ),
    )
    assert result.payload["text"].startswith("[] ")
    assert "Adapter Label" not in result.payload["text"]


async def test_meshcore_empty_route_label_suppresses_adapter_label() -> None:
    """MeshCore: source_origin_label='' suppresses adapter origin_label."""
    config = MeshCoreConfig(
        adapter_id="mc-1",
        meshcore_relay_prefix="[{origin_label}] ",
    )
    renderer = MeshCoreRenderer(
        configs={"mc-1": config},
        source_attribution={
            "src-a": _StubSourceAttribution(
                adapter_id="src-a",
                origin_label="Adapter Label",
            ),
        },
    )
    event = _make_event(source_adapter="src-a")
    result = await renderer.render(
        event,
        RenderingContext(
            target_adapter="mc-1",
            delivery_strategy="direct",
            target_platform="meshcore",
            source_origin_label="",
        ),
    )
    assert result.payload["text"].startswith("[] ")
    assert "Adapter Label" not in result.payload["text"]


async def test_lxmf_empty_route_label_suppresses_adapter_label() -> None:
    """LXMF: source_origin_label='' suppresses adapter origin_label."""
    renderer = LxmfRenderer(
        relay_prefix="[{origin_label}] ",
        source_attribution={
            "src-a": _StubSourceAttribution(
                adapter_id="src-a",
                origin_label="Adapter Label",
            ),
        },
    )
    event = _make_event(source_adapter="src-a")
    result = await renderer.render(
        event,
        RenderingContext(
            target_adapter="lxmf-1",
            delivery_strategy="direct",
            target_platform="lxmf",
            source_origin_label="",
        ),
    )
    assert result.payload["content"].startswith("[] ")
    assert "Adapter Label" not in result.payload["content"]


def test_mmrelay_key_meshnet_empty_when_route_label_empty() -> None:
    """mmrelay KEY_MESHNET is '' when route label is explicitly empty."""
    assert derive_meshnet_value("", "Adapter Net") == ""
    assert derive_meshnet_value("", "") == ""
    assert derive_meshnet_value("", None) == ""
