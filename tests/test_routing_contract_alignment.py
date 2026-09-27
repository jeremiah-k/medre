"""Routing contract alignment guards.

The routing specification's dataclass snippets are normative examples: an
operator reading ``docs/spec/routing-delivery.md`` must see the same fields,
in the same order, with the same required-vs-defaulted shape, as the runtime
routing models.  A drifted snippet — for example a defaulted field ordered
before required ones, or a required field the runtime actually defaults —
makes the example unconstructible or misleading when pasted, a defect class
caught by hand twice already (the unconstructible ``Route`` example during
the route-priority work, and the ``RouteSource`` example missing
``origin_label``).  These guards pin spec-to-code alignment mechanically so
routing keeps one source of truth.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path
from typing import Any

from medre.core.routing.models import (
    Route,
    RouteDestination,
    RouteSource,
    RouteTarget,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
ROUTING_SPEC = _REPO_ROOT / "docs" / "spec" / "routing-delivery.md"

#: Runtime dataclasses that the routing spec documents as snippets.  Every
#: registered model MUST have a snippet, and the snippet MUST match the
#: runtime field names, order, and default-presence.  The routing spec also
#: carries snippets for delivery and planning models owned by other
#: specifications, which are deliberately out of scope here.
_DOCUMENTED_RUNTIME_TYPES: dict[str, type[Any]] = {
    "Route": Route,
    "RouteSource": RouteSource,
    "RouteTarget": RouteTarget,
    "RouteDestination": RouteDestination,
}

_FIELD_RE = re.compile(r"^    ([a-z][a-z0-9_]*)\s*:(.*)$")
_CLASS_RE = re.compile(r"^class (\w+)\s*[(:]")
_DECORATOR_RE = re.compile(r"^@")

#: One documented field: ``(field name, has_default)``.
DocumentedField = tuple[str, bool]


def _documented_dataclasses(
    path: Path,
) -> dict[str, tuple[DocumentedField, ...]]:
    """Return ``{class name: ((field, has_default), ...)}`` for snippets.

    Parses fenced ``python`` blocks and collects the ordered fields of each
    dataclass-decorated ``class`` whose body is a plain annotation-style
    field list (the style the routing spec uses for its model examples).
    Non-dataclass snippets such as enumerations are ignored, and classes
    with no recognised fields are recorded with an empty tuple so
    malformed snippets cannot evade the guards.
    """
    documented: dict[str, tuple[DocumentedField, ...]] = {}
    in_python_fence = False
    decorated = False
    current_class: str | None = None
    current_fields: list[DocumentedField] = []

    def _flush() -> None:
        nonlocal current_class, current_fields, decorated
        if current_class is not None:
            documented[current_class] = tuple(current_fields)
        current_class = None
        current_fields = []
        decorated = False

    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_python_fence:
                _flush()
            in_python_fence = not in_python_fence and stripped.startswith("```python")
            continue
        if not in_python_fence:
            continue

        if _DECORATOR_RE.match(line):
            decorated = True
            continue

        class_match = _CLASS_RE.match(line)
        if class_match:
            is_snippet = decorated
            _flush()
            if is_snippet:
                current_class = class_match.group(1)
            continue
        field_match = _FIELD_RE.match(line)
        if field_match and current_class is not None:
            current_fields.append((field_match.group(1), "=" in field_match.group(2)))
        elif line.strip() and not line.startswith("    "):
            # Top-level statement inside the fence ends the dataclass block.
            _flush()

    _flush()
    return documented


def _runtime_fields(runtime_type: type[Any]) -> tuple[DocumentedField, ...]:
    """Return the runtime dataclass fields as ``(field name, has_default)``."""
    fields: list[DocumentedField] = []
    for field in dataclasses.fields(runtime_type):
        has_default = (
            field.default is not dataclasses.MISSING
            or field.default_factory is not dataclasses.MISSING  # type: ignore[misc]
        )
        fields.append((field.name, has_default))
    return tuple(fields)


class TestRoutingSpecShapeAlignment:
    """The routing spec's model snippets match the runtime dataclasses."""

    def test_route_snippet_is_documented(self) -> None:
        """The spec carries a Route dataclass example to align against."""
        assert "Route" in _documented_dataclasses(ROUTING_SPEC)

    def test_documented_snippets_are_registered(self) -> None:
        """Every documented routing model maps to a known runtime type.

        Adding a new ``Route*`` model snippet to the routing spec without
        registering it here (or removing a registration) must be a
        deliberate act.  Non-routing snippets in the same spec page are
        owned by their own contract guards.  A ``Route*`` snippet whose
        body parses to no fields is rejected: the spec's model examples
        are plain field lists, so an unrecognised body means the snippet
        or this parser has drifted.
        """
        documented = _documented_dataclasses(ROUTING_SPEC)
        routing_snippets = {name for name in documented if name.startswith("Route")}
        unknown = routing_snippets - set(_DOCUMENTED_RUNTIME_TYPES)
        assert not unknown, (
            f"Unregistered routing model snippets in the routing spec: "
            f"{sorted(unknown)}. Register them in _DOCUMENTED_RUNTIME_TYPES "
            f"or drop the snippet."
        )
        empty = sorted(
            name
            for name in routing_snippets & set(_DOCUMENTED_RUNTIME_TYPES)
            if not documented[name]
        )
        assert not empty, (
            f"Routing model snippets with no recognised fields: {empty}. "
            f"The spec's model examples must be plain field lists."
        )

    def test_registered_snippets_are_documented(self) -> None:
        """Every registered model still has a snippet in the spec.

        Deleting a model example from the specification must not silently
        shrink the aligned surface to whichever classes remain.
        """
        documented = _documented_dataclasses(ROUTING_SPEC)
        missing = sorted(
            name for name in _DOCUMENTED_RUNTIME_TYPES if name not in documented
        )
        assert not missing, (
            f"Registered routing models with no spec snippet: {missing}. "
            f"Restore the example or retire the registration."
        )

    def test_documented_fields_match_runtime_shape(self) -> None:
        """Each snippet lists the runtime fields, in order, with defaults.

        Field order is part of the contract: positional construction from
        the spec example must bind the same values the runtime binds.
        Default-presence is part of the contract too: a snippet showing a
        required field the runtime defaults (or vice versa) misleads
        readers about constructibility.
        """
        documented = _documented_dataclasses(ROUTING_SPEC)
        for class_name, runtime_type in _DOCUMENTED_RUNTIME_TYPES.items():
            documented_fields = documented[class_name]
            runtime_fields = _runtime_fields(runtime_type)
            assert documented_fields == runtime_fields, (
                f"{class_name} snippet in {ROUTING_SPEC.name} lists "
                f"{documented_fields} but the runtime dataclass has "
                f"{runtime_fields}; the spec example must match exactly."
            )
