"""mmrelay reaction sender-name resolution via namespaced keys.

Focused tests for Meshtastic-originated reaction rendering where
KEY_LONGNAME / KEY_SHORTNAME resolve from Meshtastic-native namespaced
keys (``meshtastic.longname`` / ``meshtastic.shortname``) — the primary
source emitted by the codec.

Extracted from ``tests/test_matrix_reaction_mmrelay.py`` to keep that
file under the line ceiling. Helpers are copied (not shared) so this
file stays self-contained.
"""

from __future__ import annotations

import pytest

from medre.adapters.matrix.renderer import MatrixRenderer
from medre.core.events.canonical import CanonicalEvent, EventRelation
from medre.core.events.kinds import EventKind
from medre.core.events.metadata import EventMetadata, NativeMetadata
from medre.core.rendering.renderer import RenderingContext
from medre.interop.mmrelay import KEY_LONGNAME, KEY_SHORTNAME
from tests.helpers.matrix_stubs import StubMeshtasticConfig as _StubMeshtasticConfig

# Source-config mapping for Meshtastic-originated reactions.
_SRC_MESHTASTIC = {
    "mesh-1": _StubMeshtasticConfig(adapter_id="mesh-1", mmrelay_compatibility=True)
}


def _make_mesh_reaction(
    key: str = "👍",
    body: str = "👍",
    fallback_text: str | None = None,
    rel_metadata: dict | None = None,
    native_data: dict | None = None,
    longname: str = "TestNode",
    shortname: str = "TN",
    packet_id: str = "pkt-42",
    source_adapter: str = "mesh-1",
) -> CanonicalEvent:
    """Build a canonical reaction originating from Meshtastic (no Matrix target ref)."""
    rel = EventRelation(
        relation_type="reaction",
        target_event_id=None,
        target_native_ref=None,
        key=key,
        fallback_text=fallback_text,
        metadata=rel_metadata or {},
    )
    nd = (
        native_data
        if native_data is not None
        else {
            "longname": longname,
            "shortname": shortname,
            "packet_id": packet_id,
            "from_id": "!abcdef01",
        }
    )
    return CanonicalEvent(
        event_id="evt-mesh-reaction-001",
        event_kind=EventKind.MESSAGE_REACTED,
        schema_version=1,
        timestamp=__import__("datetime").datetime.now(
            tz=__import__("datetime").timezone.utc
        ),
        source_adapter=source_adapter,
        source_transport_id="!node-1",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=(rel,),
        payload={"body": body, "msgtype": "m.text"},
        metadata=EventMetadata(native=NativeMetadata(data=nd)),
    )


@pytest.mark.asyncio
async def test_reaction_longname_from_namespaced_key() -> None:
    """KEY_LONGNAME resolves from meshtastic.longname (primary source)."""
    renderer = MatrixRenderer(source_configs=_SRC_MESHTASTIC)
    event = _make_mesh_reaction(
        native_data={
            "meshtastic.longname": "Namespaced Node",
            "packet_id": "pkt-1",
            "from_id": "!abcdef01",
        },
    )
    result = await renderer.render(
        event,
        RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
    )
    assert result.payload[KEY_LONGNAME] == "Namespaced Node"


@pytest.mark.asyncio
async def test_reaction_shortname_from_namespaced_key() -> None:
    """KEY_SHORTNAME resolves from meshtastic.shortname (primary source)."""
    renderer = MatrixRenderer(source_configs=_SRC_MESHTASTIC)
    event = _make_mesh_reaction(
        native_data={
            "meshtastic.shortname": "NN",
            "packet_id": "pkt-1",
            "from_id": "!abcdef01",
        },
    )
    result = await renderer.render(
        event,
        RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
    )
    assert result.payload[KEY_SHORTNAME] == "NN"


@pytest.mark.asyncio
async def test_reaction_namespaced_longname_wins_over_bare() -> None:
    """Namespaced meshtastic.longname takes precedence over bare longname."""
    renderer = MatrixRenderer(source_configs=_SRC_MESHTASTIC)
    event = _make_mesh_reaction(
        native_data={
            "meshtastic.longname": "Primary",
            "longname": "Legacy",
            "packet_id": "pkt-1",
            "from_id": "!abcdef01",
        },
    )
    result = await renderer.render(
        event,
        RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
    )
    assert result.payload[KEY_LONGNAME] == "Primary"
