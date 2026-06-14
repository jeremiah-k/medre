"""Durable isolation guards for mmrelay wire-format compatibility.

Locks the architectural boundary that mmrelay external Matrix wire fields
(the ``KEY_*`` constants defined in :mod:`medre.interop.mmrelay`) and the
``derive_meshnet_value`` helper remain confined to the Matrix
interop/renderer/codec surface.  These guards prevent regression where a
core planning module or a non-Matrix adapter accidentally imports mmrelay
wire-field constants and turns an external wire-compat shape into a core
model concept.

The guards complement the existing behavioral tests in
``test_mmrelay_sender_name_resolution.py``,
``test_mmrelay_reaction_sender_names.py``, and
``test_origin_label_precedence.py`` by adding:

1. **Structural (AST) isolation guard** — no mmrelay wire-field constant
   or ``derive_meshnet_value`` is imported outside the allowed Matrix
   interop/renderer/codec modules.
2. **Behavioral displayname guard** — through the full ``render()`` path
   (both the ``_inject_mmrelay_metadata`` text path and the reaction
   emote-fallback path), a Matrix ``displayname`` in native metadata
   never populates ``KEY_LONGNAME`` / ``KEY_SHORTNAME``.
3. **Behavioral KEY_MESHNET guard** — the rendered ``KEY_MESHNET`` value
   is derived purely from resolved origin labels (route/context then
   adapter registry), never from a native ``meshtastic_meshnet`` wire
   field carried in the event metadata.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from medre.adapters.matrix.renderer import MatrixRenderer
from medre.core.events.canonical import CanonicalEvent, EventRelation
from medre.core.events.kinds import EventKind
from medre.core.events.metadata import EventMetadata, NativeMetadata
from medre.core.rendering.renderer import RenderingContext
from medre.interop.mmrelay import (
    KEY_LONGNAME,
    KEY_MESHNET,
    KEY_SHORTNAME,
    derive_meshnet_value,
)
from tests.helpers.ast_imports import all_imports, parse_python
from tests.helpers.matrix_stubs import StubMeshtasticConfig as _StubMeshtasticConfig
from tests.helpers.matrix_stubs import StubSourceAttribution as _StubSourceAttribution

# ---------------------------------------------------------------------------
# Structural isolation guard (AST-based)
# ---------------------------------------------------------------------------

# mmrelay wire-field constants + helper that define the external Matrix wire
# compatibility surface.  These MUST stay confined to the Matrix
# interop/renderer/codec modules.
_GUARDED_MMRELAY_SYMBOLS: frozenset[str] = frozenset(
    {
        "KEY_ID",
        "KEY_LONGNAME",
        "KEY_SHORTNAME",
        "KEY_MESHNET",
        "KEY_PORTNUM",
        "KEY_TEXT",
        "KEY_REPLY_ID",
        "KEY_EMOJI",
        "KEY_REACTION_KEY",
        "derive_meshnet_value",
    }
)

# Modules allowed to import the guarded mmrelay symbols: the definition
# site plus the two Matrix consumers (codec decodes wire fields from
# inbound Matrix content; renderer re-emits them on outbound Matrix
# content).  Core planning and non-Matrix adapters are NOT permitted.
_ALLOWED_MODULES: frozenset[str] = frozenset(
    {
        "medre.interop.mmrelay",
        "medre.adapters.matrix.codec",
        "medre.adapters.matrix.renderer",
    }
)


def _src_root() -> Path:
    """Return the ``src/medre`` package root."""
    import medre

    return Path(medre.__file__).resolve().parent


def _module_dotted(path: Path, src_root: Path) -> str:
    """Return the dotted module name for *path* relative to *src_root*."""
    rel = path.resolve().relative_to(src_root)
    parts = list(rel.parts)
    parts[-1] = parts[-1][:-3]  # strip ".py"
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(["medre", *parts])


def _find_guarded_imports(path: Path) -> set[str]:
    """Return the set of guarded mmrelay symbols imported by *path*."""
    tree = parse_python(str(path))
    records = all_imports(tree, file_path=str(path))
    found: set[str] = set()
    for rec in records:
        # rec.module for ``from medre.interop.mmrelay import KEY_LONGNAME``
        # resolves to ``medre.interop.mmrelay.KEY_LONGNAME``.
        if rec.module.startswith("medre.interop.mmrelay."):
            symbol = rec.module.split(".", 3)[-1]
            if symbol in _GUARDED_MMRELAY_SYMBOLS:
                found.add(symbol)
    return found


def test_mmrelay_wire_constants_isolated_to_matrix_surface() -> None:
    """No guarded mmrelay symbol is imported outside the allowed surface.

    Scans every ``.py`` file under ``src/medre/`` via AST import analysis
    and asserts that the mmrelay wire-field constants and
    ``derive_meshnet_value`` are referenced only by:
      * ``medre.interop.mmrelay`` (definition site)
      * ``medre.adapters.matrix.codec`` (inbound Matrix wire decode)
      * ``medre.adapters.matrix.renderer`` (outbound Matrix wire encode)

    Core planning, core rendering, and non-Matrix adapters must not import
    these symbols — doing so would leak an external Matrix wire-compat
    shape into the core model.
    """
    src_root = _src_root()
    violations: list[str] = []

    for path in sorted(src_root.rglob("*.py")):
        module_name = _module_dotted(path, src_root)
        if module_name in _ALLOWED_MODULES:
            continue
        found = _find_guarded_imports(path)
        if found:
            violations.append(
                f"{module_name} ({path.name}) imports guarded mmrelay "
                f"symbols: {sorted(found)}"
            )

    assert not violations, (
        "mmrelay wire-field isolation violated — guarded symbols imported "
        "outside the Matrix interop/renderer/codec surface:\n  "
        + "\n  ".join(violations)
    )


def test_allowed_matrix_modules_actively_use_guarded_surface() -> None:
    """The Matrix codec and renderer DO import the guarded surface.

    This locks the expectation that the allowed modules are genuine
    consumers of the mmrelay wire surface (so the allowlist is not
    vacuously permissive).  If a refactor removes the mmrelay import from
    a Matrix module, this guard forces a conscious update rather than a
    silent widening of the allowlist.
    """
    src_root = _src_root()
    codec_path = src_root / "adapters" / "matrix" / "codec.py"
    renderer_path = src_root / "adapters" / "matrix" / "renderer.py"

    codec_imports = _find_guarded_imports(codec_path)
    renderer_imports = _find_guarded_imports(renderer_path)

    # Each consumer must import at least the longname/shortname/meshnet
    # wire trio (the PC-required surface) — plus the renderer must import
    # the meshnet derivation helper.
    required_trio = {"KEY_LONGNAME", "KEY_SHORTNAME", "KEY_MESHNET"}
    assert required_trio.issubset(codec_imports), (
        f"matrix codec no longer imports the mmrelay name/meshnet trio; "
        f"got {sorted(codec_imports)}"
    )
    assert required_trio.issubset(renderer_imports), (
        f"matrix renderer no longer imports the mmrelay name/meshnet trio; "
        f"got {sorted(renderer_imports)}"
    )
    assert "derive_meshnet_value" in renderer_imports, (
        "matrix renderer no longer imports derive_meshnet_value; "
        "KEY_MESHNET derivation must stay on the Matrix surface."
    )


# ---------------------------------------------------------------------------
# Behavioral event / context builders
# ---------------------------------------------------------------------------


def _make_text_event(
    *,
    native_data: dict | None = None,
    source_adapter: str = "mesh-1",
    body: str = "hello",
) -> CanonicalEvent:
    """Build a canonical text event for mmrelay-compat rendering."""
    metadata = EventMetadata(native=NativeMetadata(data=native_data or {}))
    return CanonicalEvent(
        event_id="evt-iso-1",
        event_kind=EventKind.MESSAGE_CREATED,
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="!node-1",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"body": body, "msgtype": "m.text"},
        metadata=metadata,
    )


def _make_reaction_event(
    *,
    native_data: dict | None = None,
    source_adapter: str = "mesh-1",
) -> CanonicalEvent:
    """Build a canonical reaction event for the emote-fallback render path."""
    rel = EventRelation(
        relation_type="reaction",
        target_event_id=None,
        target_native_ref=None,
        key="\U0001f44d",
        fallback_text=None,
        metadata={},
    )
    metadata = EventMetadata(native=NativeMetadata(data=native_data or {}))
    return CanonicalEvent(
        event_id="evt-iso-reaction-1",
        event_kind=EventKind.MESSAGE_REACTED,
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="!node-1",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=(rel,),
        payload={"body": "\U0001f44d", "msgtype": "m.text"},
        metadata=metadata,
    )


# Source-config with mmrelay_compatibility enabled, so the renderer emits
# the full mmrelay wire surface on outbound Matrix content.
_SRC_MMRELAY = {
    "mesh-1": _StubMeshtasticConfig(
        adapter_id="mesh-1",
        mmrelay_compatibility=True,
    ),
}


# ---------------------------------------------------------------------------
# Behavioral guard: Matrix displayname never feeds KEY_LONGNAME/SHORTNAME
# ---------------------------------------------------------------------------


async def test_displayname_does_not_populate_names_inject_path() -> None:
    """Text render path: displayname never reaches KEY_LONGNAME/SHORTNAME.

    With mmrelay_compatibility enabled and a native metadata dict that
    carries only a Matrix ``displayname`` (no Meshtastic-native /
    mmrelay-wire / legacy bare name keys), the rendered Matrix content
    must emit empty ``KEY_LONGNAME`` / ``KEY_SHORTNAME`` rather than
    projecting the Matrix display name into the Meshtastic-shaped wire
    fields.
    """
    renderer = MatrixRenderer(source_configs=_SRC_MMRELAY)
    event = _make_text_event(
        native_data={
            "displayname": "Alice Display",
            "packet_id": "pkt-1",
            "from_id": "!abcdef01",
        }
    )
    result = await renderer.render(
        event,
        RenderingContext(
            target_adapter="matrix-1",
            delivery_strategy="direct",
            target_platform="matrix",
        ),
    )
    assert result.payload[KEY_LONGNAME] == ""
    assert result.payload[KEY_SHORTNAME] == ""
    # displayname must not leak into any other mmrelay wire field either.
    assert "Alice Display" not in result.payload.get(KEY_LONGNAME, "")
    assert "Alice Display" not in result.payload.get(KEY_SHORTNAME, "")


async def test_displayname_does_not_populate_names_reaction_path() -> None:
    """Reaction emote-fallback: displayname never reaches name fields.

    The reaction emote-fallback path (mmrelay_compat with no Matrix-native
    target) populates the full mmrelay wire surface.  A Matrix
    ``displayname`` must not leak into ``KEY_LONGNAME`` /
    ``KEY_SHORTNAME`` there either.
    """
    renderer = MatrixRenderer(source_configs=_SRC_MMRELAY)
    event = _make_reaction_event(
        native_data={
            "displayname": "Bob Display",
            "packet_id": "pkt-1",
            "from_id": "!abcdef01",
        }
    )
    result = await renderer.render(
        event,
        RenderingContext(
            target_adapter="matrix-1",
            delivery_strategy="direct",
            target_platform="matrix",
        ),
    )
    assert result.payload[KEY_LONGNAME] == ""
    assert result.payload[KEY_SHORTNAME] == ""


async def test_displayname_ignored_even_alongside_empty_names() -> None:
    """displayname is not a fallback when Meshtastic names are absent.

    Explicitly confirms the resolution chain terminates at empty string
    and does not reach for ``displayname`` as an extra fallback tier.
    """
    renderer = MatrixRenderer(source_configs=_SRC_MMRELAY)
    event = _make_text_event(
        native_data={
            "meshtastic.longname": "",  # empty namespaced value
            "displayname": "Charlie Display",
            "packet_id": "pkt-1",
        }
    )
    result = await renderer.render(
        event,
        RenderingContext(
            target_adapter="matrix-1",
            delivery_strategy="direct",
            target_platform="matrix",
        ),
    )
    # Empty namespaced value short-circuits to "" via the `or` chain;
    # displayname must NOT fill in.
    assert result.payload[KEY_LONGNAME] == ""
    assert result.payload[KEY_SHORTNAME] == ""


# ---------------------------------------------------------------------------
# Behavioral guard: KEY_MESHNET derives from resolved origin labels only
# ---------------------------------------------------------------------------


async def test_meshnet_from_route_label_not_native_wire_data() -> None:
    """Rendered KEY_MESHNET uses route label, not native meshtastic_meshnet.

    Even when the event carries a native ``meshtastic_meshnet`` wire value
    (e.g. captured from an inbound mmrelay Matrix event), the outbound
    ``KEY_MESHNET`` is derived purely from the resolved origin labels
    (route/context origin label takes top precedence).
    """
    renderer = MatrixRenderer(
        source_configs=_SRC_MMRELAY,
        source_attribution={
            "mesh-1": _StubSourceAttribution(
                adapter_id="mesh-1",
                origin_label="Adapter Net",
            ),
        },
    )
    event = _make_text_event(
        native_data={
            "meshtastic_meshnet": "Wire Meshnet",
            "longname": "Alice",
            "shortname": "A",
            "packet_id": "pkt-1",
        }
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
    assert result.payload[KEY_MESHNET] != "Wire Meshnet"


async def test_meshnet_from_adapter_label_not_native_wire_data() -> None:
    """Rendered KEY_MESHNET falls back to adapter label, not native wire data."""
    renderer = MatrixRenderer(
        source_configs=_SRC_MMRELAY,
        source_attribution={
            "mesh-1": _StubSourceAttribution(
                adapter_id="mesh-1",
                origin_label="Adapter Net",
            ),
        },
    )
    event = _make_text_event(
        native_data={
            "meshtastic_meshnet": "Wire Meshnet",
            "longname": "Alice",
            "shortname": "A",
            "packet_id": "pkt-1",
        }
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
    assert result.payload[KEY_MESHNET] != "Wire Meshnet"


async def test_meshnet_empty_when_no_labels_not_native_wire_data() -> None:
    """Rendered KEY_MESHNET is empty (not native wire data) when no labels."""
    renderer = MatrixRenderer(source_configs=_SRC_MMRELAY)
    event = _make_text_event(
        native_data={
            "meshtastic_meshnet": "Wire Meshnet",
            "longname": "Alice",
            "shortname": "A",
            "packet_id": "pkt-1",
        }
    )
    result = await renderer.render(
        event,
        RenderingContext(
            target_adapter="matrix-1",
            delivery_strategy="direct",
            target_platform="matrix",
        ),
    )
    assert result.payload[KEY_MESHNET] == ""
    assert result.payload[KEY_MESHNET] != "Wire Meshnet"


# ---------------------------------------------------------------------------
# Unit-level locks on derive_meshnet_value precedence (origin-label only)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source_label, adapter_label, expected",
    [
        ("Route Net", "Adapter Net", "Route Net"),
        (None, "Adapter Net", "Adapter Net"),
        ("", "Adapter Net", ""),
        (None, None, ""),
        ("", "", ""),
    ],
    ids=[
        "route-wins",
        "adapter-fallback",
        "empty-route-suppresses-adapter",
        "both-none-empty",
        "both-empty",
    ],
)
def test_derive_meshnet_value_origin_label_precedence(
    source_label: str | None,
    adapter_label: str | None,
    expected: str,
) -> None:
    """derive_meshnet_value resolves only from origin labels (never wire data).

    Locks the precedence: source origin label (``""`` preserved as
    intentionally blank) > adapter origin label > empty string.  The
    helper takes no wire-data argument by construction — this test makes
    that contract explicit and durable.
    """
    assert derive_meshnet_value(source_label, adapter_label) == expected
