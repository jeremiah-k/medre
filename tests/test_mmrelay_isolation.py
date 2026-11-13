# medre-test-tier: compatibility
"""Durable isolation guards for the retired mmrelay interop namespace.

Locks the architectural boundary that the retired ``medre.interop``
namespace stays out of the MEDRE tree and that the Matrix ``displayname``
trust boundary holds: through the sender projection and the full
``render()`` path, a Matrix ``displayname`` in native metadata never
becomes participant identity.

The guards add:

1. **Retired-namespace ban (AST)** — no module under the MEDRE tree may
   import ``medre.interop`` or any submodule of it, in any import shape.
   Synthetic matcher probes lock the ban's coverage of whole-module,
   symbol-level, and package-level import forms.
2. **Behavioral displayname trust boundary** — through the sender
   projection and the full ``render()`` path, a Matrix ``displayname``
   in native metadata never becomes participant identity: it never
   reaches the projected sender label or a rendered attribution prefix.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from medre.adapters.matrix.renderer import MatrixRenderer
from medre.core.events.canonical import CanonicalEvent, EventRelation
from medre.core.events.kinds import EventKind
from medre.core.events.metadata import EventMetadata, NativeMetadata
from medre.core.rendering.renderer import RenderingContext
from tests.helpers.ast_imports import all_imports, parse_python
from tests.helpers.matrix import matrix_payload_content
from tests.helpers.matrix_stubs import StubMatrixConfig as _StubMatrixConfig
from tests.helpers.native_metadata import meshtastic_native_data

# ---------------------------------------------------------------------------
# Retired-namespace ban (AST-based)
# ---------------------------------------------------------------------------

# The interop package was retired with the mmrelay compatibility module;
# the last adapter import left the tree at the pinned Meshtastic revision.
_RETIRED_NAMESPACE = "medre.interop"


def _src_roots() -> tuple[Path, ...]:
    """Return core and installed contributors to the MEDRE namespace."""
    import medre

    return tuple(Path(path).resolve() for path in medre.__path__)


def _module_dotted(path: Path, src_root: Path) -> str:
    """Return the dotted module name for *path* relative to *src_root*."""
    rel = path.resolve().relative_to(src_root)
    parts = list(rel.parts)
    parts[-1] = parts[-1][:-3]  # strip ".py"
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(["medre", *parts])


def _is_interop_import(module: str) -> bool:
    """Return True when *module* resolves into the retired namespace."""
    return module == _RETIRED_NAMESPACE or module.startswith(_RETIRED_NAMESPACE + ".")


def _find_interop_imports(path: Path) -> list[str]:
    """Return the retired-namespace import targets found in *path*.

    Every import shape is covered: ``import medre.interop`` and
    ``import medre.interop.mmrelay [as <alias>]`` bind the retired
    modules directly; ``from medre.interop.mmrelay import KEY_LONGNAME``
    records both the parent module and the imported symbol path; and
    ``from medre.interop import mmrelay`` records both the package and
    the submodule.  Any record resolving into ``medre.interop`` is a
    violation.
    """
    tree = parse_python(str(path))
    records = all_imports(tree, file_path=str(path))
    return sorted({rec.module for rec in records if _is_interop_import(rec.module)})


def test_interop_namespace_is_retired_from_medre_tree() -> None:
    """No module in the MEDRE tree imports the retired namespace.

    Scans every ``.py`` file under every contributor to the ``medre``
    package and fails on any import resolving into ``medre.interop`` —
    whole-module, aliased, or symbol-level.  Reintroducing the namespace
    would resurrect a historical Matrix wire-compat surface the frozen
    schema rejects.
    """
    violations: list[str] = []

    for src_root in _src_roots():
        for path in sorted(src_root.rglob("*.py")):
            module_name = _module_dotted(path, src_root)
            found = _find_interop_imports(path)
            if found:
                violations.append(
                    f"{module_name} ({path.name}) imports the retired "
                    f"medre.interop namespace: {found}"
                )

    assert (
        not violations
    ), "medre.interop is retired — imports found in the MEDRE tree:\n  " + "\n  ".join(
        violations
    )


def test_matcher_flags_whole_module_interop_import(tmp_path: Path) -> None:
    """``import medre.interop.mmrelay as mmrelay`` is flagged.

    Regression coverage for the whole-module binding shape: the alias
    makes every former constant reachable through attribute access, so
    the import itself is the violation.
    """
    probe = tmp_path / "probe_whole_module.py"
    probe.write_text(
        "import medre.interop.mmrelay as mmrelay\n" "x = mmrelay.KEY_LONGNAME\n",
        encoding="utf-8",
    )
    found = _find_interop_imports(probe)
    assert found == [
        "medre.interop.mmrelay"
    ], f"whole-module interop import must be flagged; got {found}"


def test_matcher_flags_from_import_of_retired_symbol(tmp_path: Path) -> None:
    """``from medre.interop.mmrelay import KEY_LONGNAME`` is flagged."""
    probe = tmp_path / "probe_from_import.py"
    probe.write_text(
        "from medre.interop.mmrelay import KEY_LONGNAME\n",
        encoding="utf-8",
    )
    found = _find_interop_imports(probe)
    assert found == [
        "medre.interop.mmrelay",
        "medre.interop.mmrelay.KEY_LONGNAME",
    ], f"symbol-level interop import must be flagged; got {found}"


def test_matcher_flags_package_level_interop_import(tmp_path: Path) -> None:
    """``from medre.interop import mmrelay`` flags package and submodule."""
    probe = tmp_path / "probe_package_level.py"
    probe.write_text(
        "from medre.interop import mmrelay\n",
        encoding="utf-8",
    )
    found = _find_interop_imports(probe)
    assert found == [
        "medre.interop",
        "medre.interop.mmrelay",
    ], f"package-level interop import must be flagged; got {found}"


def test_matcher_flags_bare_package_import(tmp_path: Path) -> None:
    """``import medre.interop`` alone is flagged."""
    probe = tmp_path / "probe_bare_package.py"
    probe.write_text(
        "import medre.interop\n",
        encoding="utf-8",
    )
    found = _find_interop_imports(probe)
    assert found == [
        "medre.interop"
    ], f"bare package import must be flagged; got {found}"


def test_matcher_ignores_live_namespaces(tmp_path: Path) -> None:
    """Imports outside the retired namespace produce no findings."""
    probe = tmp_path / "probe_live_namespaces.py"
    probe.write_text(
        "from medre.core.events.kinds import EventKind\n" "import medre.runtime\n",
        encoding="utf-8",
    )
    assert _find_interop_imports(probe) == []


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


# Target-local prefix so the projected sender identity is observable in the
# rendered attribution.
_STUB_MATRIX_CONFIG = {
    "matrix-1": _StubMatrixConfig(
        adapter_id="matrix-1",
        relay_prefix="[{sender}] ",
    ),
}


# ---------------------------------------------------------------------------
# Behavioral trust boundary: Matrix displayname never becomes participant
# identity through the sender projection or a rendered prefix
# ---------------------------------------------------------------------------


async def test_displayname_does_not_populate_names_inject_path() -> None:
    """Text render path: displayname never reaches the projected sender label.

    Native metadata that carries only a Matrix ``displayname`` (no
    Meshtastic-native identity) projects an empty sender label: the
    rendered prefix brackets stay empty rather than filling from the
    display name.
    """
    renderer = MatrixRenderer(configs=_STUB_MATRIX_CONFIG)
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
    body = str(matrix_payload_content(result)["body"])
    assert body == "[] hello"


async def test_displayname_does_not_populate_names_reaction_path() -> None:
    """Reaction emote-fallback: displayname never becomes sender identity."""
    renderer = MatrixRenderer(configs=_STUB_MATRIX_CONFIG)
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
    body = str(matrix_payload_content(result)["body"])
    assert body == "\n [] reacted \U0001f44d to an earlier message"


async def test_displayname_ignored_even_alongside_empty_names() -> None:
    """displayname is not a fallback when Meshtastic names are absent.

    Explicitly confirms the projection chain terminates at the empty
    namespace value and does not reach for ``displayname`` as an extra
    fallback tier.
    """
    renderer = MatrixRenderer(configs=_STUB_MATRIX_CONFIG)
    native = meshtastic_native_data({"longname": "", "shortname": ""})
    native["displayname"] = "Charlie Display"
    event = _make_text_event(native_data=native)
    result = await renderer.render(
        event,
        RenderingContext(
            target_adapter="matrix-1",
            delivery_strategy="direct",
            target_platform="matrix",
        ),
    )
    body = str(matrix_payload_content(result)["body"])
    # The projection chain terminates at the transport ID fallback; the
    # displayname must NOT fill in.
    assert body == "[!node-1] hello"
