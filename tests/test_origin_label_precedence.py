# medre-test-tier: compatibility
"""Tests for route/context origin_label precedence and generic variables
across transports.

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
- Unknown {meshnet_name} remains an unresolved placeholder.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from medre.adapters.lxmf.renderer import LxmfRenderer
from medre.adapters.matrix.renderer import MatrixRenderer
from medre.adapters.meshcore.renderer import MeshCoreRenderer
from medre.adapters.meshtastic.renderer import MeshtasticRenderer
from medre.config.adapters.lxmf import LxmfConfig
from medre.config.adapters.meshcore import MeshCoreConfig
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.events import (
    CanonicalEvent,
    EventMetadata,
    NativeMetadata,
)
from medre.core.rendering.renderer import RenderingContext
from tests.helpers.matrix import matrix_payload_content
from tests.helpers.matrix_stubs import StubMatrixConfig as _StubMatrixConfig
from tests.helpers.matrix_stubs import StubSourceAttribution as _StubSourceAttribution
from tests.helpers.native_metadata import (
    lxmf_native_data,
    matrix_native_data,
    meshcore_native_data,
    meshtastic_native_data,
)

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
    assert matrix_payload_content(result)["body"] == "[Route Label] hello"


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
    assert matrix_payload_content(result)["body"] == "[Adapter Label] hello"


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
    content = matrix_payload_content(result)
    assert content["body"] == "[] hello"
    assert "None" not in content["body"]


# ===================================================================
# Meshtastic: origin_label precedence
# ===================================================================


async def test_meshtastic_route_label_overrides_adapter_label() -> None:
    """Route context origin_label overrides adapter registry."""
    config = MeshtasticConfig(
        adapter_id="mesh-1",
        relay_prefix="[{origin_label}] ",
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
        relay_prefix="[{origin_label}] ",
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
        relay_prefix="[{origin_label}] ",
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
        relay_prefix="[{origin_label}] ",
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
        relay_prefix="[{origin_label}] ",
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
        configs={
            "lxmf_node": LxmfConfig(
                adapter_id="lxmf_node",
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
        configs={
            "lxmf_node": LxmfConfig(
                adapter_id="lxmf_node",
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
        native_data=meshtastic_native_data({"longname": "RadioOp", "shortname": "RO"}),
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
    assert matrix_payload_content(result)["body"] == "[RadioOp/Route East] hello"


async def test_matrix_to_meshtastic_sender_short_and_origin_label() -> None:
    """Matrix→Meshtastic: {sender_short} and {origin_label} in prefix."""
    config = MeshtasticConfig(
        adapter_id="mesh-1",
        relay_prefix="<{sender_short}/{origin_label}> ",
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
        native_data=matrix_native_data(
            {
                "sender": "@alice:matrix.org",
                "sender_display_name": "Alice",
            }
        ),
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
        relay_prefix="({sender_id}@{origin_label}) ",
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
        native_data=meshcore_native_data({"pubkey_prefix": "a1b2c3", "channel": 0}),
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
        relay_prefix="[{sender_id}@{origin_label}] ",
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
        native_data=lxmf_native_data({"source_hash": "feedface"}),
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
        relay_prefix="[{sender}] ",
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
        with pytest.raises(ValueError, match="formatting_exception"):
            await renderer.render(
                event,
                RenderingContext(
                    target_adapter="mc-1",
                    delivery_strategy="direct",
                    target_platform="meshcore",
                ),
            )


async def test_lxmf_exception_does_not_prepend_raw_template() -> None:
    """LXMF: formatting_exception does not prepend raw template text."""
    renderer = LxmfRenderer(
        configs={
            "lxmf_node": LxmfConfig(
                adapter_id="lxmf_node",
                relay_prefix="[{sender}] ",
            ),
        },
    )
    event = _make_event(source_adapter="src-a")

    # Patch inside the defining module so the real format_relay_prefix
    # exercises its own try/except handler.  This avoids the brittle
    # pattern of patching the renderer module's imported binding, which
    # can fail to take effect in some CI environments.
    with patch(
        "medre.core.rendering.attribution._build_variable_map",
        side_effect=RuntimeError("boom"),
    ):
        with pytest.raises(ValueError, match="formatting_exception"):
            await renderer.render(
                event,
                RenderingContext(
                    target_adapter="lxmf_node",
                    delivery_strategy="direct",
                    target_platform="lxmf",
                ),
            )


# ===================================================================
# Unknown {meshnet_name} remains unknown
# ===================================================================


async def test_meshnet_name_unknown_in_meshtastic_prefix() -> None:
    """{meshnet_name} is not a known template variable."""
    config = MeshtasticConfig(
        adapter_id="mesh-1",
        relay_prefix="[{meshnet_name}] ",
    )
    renderer = MeshtasticRenderer(configs={"mesh-1": config})
    event = _make_event(source_adapter="src-a")
    with pytest.raises(ValueError, match="unknown placeholder"):
        await renderer.render(
            event,
            RenderingContext(
                target_adapter="mesh-1",
                delivery_strategy="direct",
                target_platform="meshtastic",
            ),
        )


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
    body = str(matrix_payload_content(result).get("body", ""))
    assert body.startswith("[] ")  # empty label, NOT "[Adapter Label]"
    assert "Adapter Label" not in body


async def test_meshtastic_empty_route_label_suppresses_adapter_label() -> None:
    """Meshtastic: source_origin_label='' suppresses adapter origin_label."""
    config = MeshtasticConfig(
        adapter_id="mesh-1",
        relay_prefix="[{origin_label}] ",
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
        relay_prefix="[{origin_label}] ",
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
        configs={
            "lxmf-1": LxmfConfig(
                adapter_id="lxmf-1",
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
            target_adapter="lxmf-1",
            delivery_strategy="direct",
            target_platform="lxmf",
            source_origin_label="",
        ),
    )
    assert result.payload["content"].startswith("[] ")
    assert "Adapter Label" not in result.payload["content"]
