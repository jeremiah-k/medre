"""Offline, SDK-free topology projections of full expanded routing targets.

The graph contains configured *potential* delivery legs, not transport health,
actual membership, or guarantees of forwarding. It never contacts adapters.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from html import escape
from typing import TYPE_CHECKING
from unicodedata import category

from medre.config.route_expansion import expand_route_config
from medre.runtime.route_plan import build_route_plan

if TYPE_CHECKING:
    from medre.config.model import RuntimeConfig


@dataclass(frozen=True)
class Endpoint:
    """One exact configured address; display abbreviations never define identity."""

    adapter: str
    context: str | None
    destination: str | None = None


@dataclass(frozen=True)
class Connection:
    """A directed configured delivery possibility with its source route provenance."""

    source: Endpoint
    target: Endpoint
    route_id: str


@dataclass(frozen=True)
class RouteDiagram:
    """Immutable offline topology plus disabled definitions and blocking plan errors."""

    adapters: tuple[tuple[str, str, bool], ...]
    connections: tuple[Connection, ...]
    disabled_routes: tuple[str, ...]
    errors: tuple[str, ...]


_EndpointKey = tuple[str, bool, str, bool, str]


def _endpoint_key(endpoint: Endpoint) -> _EndpointKey:
    """Order exact values without conflating wildcard and empty contexts."""
    return (
        endpoint.adapter,
        endpoint.context is not None,
        endpoint.context or "",
        endpoint.destination is not None,
        endpoint.destination or "",
    )


def _connection_key(
    connection: Connection,
) -> tuple[_EndpointKey, _EndpointKey, str]:
    return (
        _endpoint_key(connection.source),
        _endpoint_key(connection.target),
        connection.route_id,
    )


def build_route_diagram(config: RuntimeConfig) -> RouteDiagram:
    """Use the runtime expansion authority, including EVERY target in a leg."""
    plan = build_route_plan(config)
    valid = {
        entry.route_id for entry in plan.routes if entry.enabled and not entry.error
    }
    connections: set[Connection] = set()
    for rc in config.routes.routes:
        if rc.route_id not in valid:
            continue
        for leg in expand_route_config(rc):
            route = leg.route
            source = Endpoint(route.source.adapter or "?", route.source.channel)
            for target in route.targets:
                destination = target.destination
                identifier = None
                if destination is not None:
                    identifier = (
                        destination.destination_hash or destination.destination_name
                    )
                    identifier = f"{destination.kind}:{identifier or '?'}"
                connections.add(
                    Connection(
                        source=source,
                        target=Endpoint(
                            target.adapter or "?", target.channel, identifier
                        ),
                        route_id=rc.route_id,
                    )
                )
    return RouteDiagram(
        adapters=tuple(
            sorted((a.adapter_id, a.transport, a.enabled) for a in plan.adapters)
        ),
        connections=tuple(sorted(connections, key=_connection_key)),
        disabled_routes=tuple(sorted(e.route_id for e in plan.routes if not e.enabled)),
        errors=tuple(
            [f"{e.route_id}: {e.error}" for e in plan.routes if e.error]
            + [
                f"exclusive conflict: {c.route_a_id} / {c.route_b_id}"
                for c in plan.conflicts
            ]
        ),
    )


def _short(value: str | None, *, full: bool) -> str:
    """Abbreviate room/address identifiers unless exact output is requested.

    Abbreviation is lossy: two distinct identifiers can render identically in
    the text, Mermaid, and JSON endpoint fields. Connections and route entries
    are never merged — only the displayed identifier is shortened. Pass
    ``full=True`` (the ``--full-context`` CLI flag) for exact identifiers.
    """
    if value is None:
        return "*"
    if not full and (value.startswith(("!", "@", "#")) or len(value) >= 32):
        return value[:9] + "…" if len(value) > 12 else value
    return value


def _short_destination(value: str | None, *, full: bool) -> str:
    """Abbreviate the identifier inside a kind-prefixed structured target.

    Structured addresses are displayed as ``kind:identifier``.  Applying
    ``_short`` to the combined string can expose a short private room/handle:
    its leading ``kind:`` hides the sigil used by the ordinary abbreviation
    rule, while the total length remains below the long-address threshold.
    """
    if value is None:
        return "*"
    kind, separator, identifier = value.partition(":")
    if separator and kind and identifier:
        return f"{kind}:{_short(identifier, full=full)}"
    return _short(value, full=full)


def _endpoint_ids(diagram: RouteDiagram) -> dict[Endpoint, str]:
    """Assign snapshot-local identities before abbreviating any display values."""
    endpoints = {c.source for c in diagram.connections} | {
        c.target for c in diagram.connections
    }
    return {
        endpoint: f"n{i}"
        for i, endpoint in enumerate(sorted(endpoints, key=_endpoint_key))
    }


def _endpoint_labels(diagram: RouteDiagram, *, full: bool) -> dict[Endpoint, str]:
    """Disambiguate colliding labels with the same node IDs used by JSON/Mermaid."""
    ids = _endpoint_ids(diagram)
    labels = {
        endpoint: (
            f"{endpoint.adapter} · "
            + (
                _short_destination(endpoint.destination, full=full)
                if endpoint.destination is not None
                else _short(endpoint.context, full=full)
            )
        )
        for endpoint in ids
    }
    counts: dict[str, int] = defaultdict(int)
    for label in labels.values():
        counts[label] += 1
    return {
        endpoint: f"{label} [{ids[endpoint]}]" if counts[label] > 1 else label
        for endpoint, label in labels.items()
    }


def _terminal_text(value: str) -> str:
    """Render configured control/format characters visibly without executing them."""
    return "".join(
        (
            char.encode("unicode_escape").decode("ascii")
            if category(char) in {"Cc", "Cf"} and char not in {"\u200c", "\u200d"}
            else char
        )
        for char in value
    )


def _mermaid_text(value: str) -> str:
    """Escape user-configured values so they are text, never Mermaid syntax."""
    cleaned = _terminal_text(value)
    return (
        escape(cleaned, quote=True)
        .replace("&quot;", "#quot;")
        .replace("&#x27;", "#39;")
    )


def _components(connections: tuple[Connection, ...]) -> list[tuple[Endpoint, ...]]:
    neighbours: dict[Endpoint, set[Endpoint]] = defaultdict(set)
    for c in connections:
        neighbours[c.source].add(c.target)
        neighbours[c.target].add(c.source)
    groups: list[tuple[Endpoint, ...]] = []
    visited: set[Endpoint] = set()
    for start in sorted(neighbours, key=_endpoint_key):
        if start in visited:
            continue
        group: set[Endpoint] = set()
        stack = [start]
        while stack:
            current = stack.pop()
            if current in group:
                continue
            group.add(current)
            stack.extend(neighbours[current] - group)
        visited.update(group)
        groups.append(tuple(sorted(group, key=_endpoint_key)))
    return groups


def render_mermaid(
    diagram: RouteDiagram, *, view: str = "contexts", full_context: bool = False
) -> str:
    """Render an adapter overview or connected endpoint groups as Mermaid."""
    if view not in ("contexts", "adapters"):
        raise ValueError(f"unrecognized diagram view: {view}")
    lines = [
        "flowchart LR",
        "    %% Configured routes, not current connectivity or delivery health",
    ]
    if diagram.errors:
        lines.append(
            f'    issues["Incomplete preview: {len(diagram.errors)} configuration issue(s)"]'
        )
    if not diagram.connections:
        lines.append('    empty["No enabled route connections"]')
    if view == "adapters":
        adapter_ids = {a[0]: f"a{i}" for i, a in enumerate(diagram.adapters)}
        for aid, transport, enabled in diagram.adapters:
            status = "" if enabled else " (disabled)"
            lines.append(
                f'    {adapter_ids[aid]}["{_mermaid_text(transport + ":" + aid + status)}"]'
            )
        connections_by_pair: dict[tuple[str, str], set[str]] = defaultdict(set)
        for edge in diagram.connections:
            connections_by_pair[(edge.source.adapter, edge.target.adapter)].add(
                edge.route_id
            )
        consumed_pairs: set[tuple[str, str]] = set()
        for (source_adapter, target_adapter), route_ids in sorted(
            connections_by_pair.items()
        ):
            pair = (source_adapter, target_adapter)
            if pair in consumed_pairs:
                continue
            if source_adapter not in adapter_ids or target_adapter not in adapter_ids:
                continue
            reverse = (target_adapter, source_adapter)
            consumed_pairs.update((pair, reverse))
            reverse_ids = (
                connections_by_pair.get(reverse, set())
                if source_adapter != target_adapter
                else set()
            )
            shared = route_ids & reverse_ids
            groups = (
                (source_adapter, target_adapter, "<-->", shared),
                (source_adapter, target_adapter, "-->", route_ids - shared),
                (target_adapter, source_adapter, "-->", reverse_ids - shared),
            )
            for start, end, arrow, selected in groups:
                if not selected:
                    continue
                # Count configured groups, including shared groups only once.
                label = f"{len(selected)} route" + ("s" if len(selected) != 1 else "")
                lines.append(
                    f"    {adapter_ids[start]} {arrow}|{label}| {adapter_ids[end]}"
                )
    else:
        node_ids = _endpoint_ids(diagram)
        labels = _endpoint_labels(diagram, full=full_context)
        for i, component in enumerate(_components(diagram.connections), start=1):
            lines.append(f'    subgraph g{i}["Connected route group {i}"]')
            lines.append("        direction LR")
            for endpoint in component:
                label = _mermaid_text(labels[endpoint])
                lines.append(f'        {node_ids[endpoint]}["{label}"]')
            lines.append("    end")
        # One edge per endpoint pair in the visual overview. Keep the
        # route-specific parallel edges in the text and JSON projections.
        directions = {(c.source, c.target) for c in diagram.connections}
        consumed: set[tuple[Endpoint, Endpoint]] = set()
        for source, target in sorted(
            directions,
            key=lambda pair: (_endpoint_key(pair[0]), _endpoint_key(pair[1])),
        ):
            if (source, target) in consumed:
                continue
            consumed.add((source, target))
            arrow = "-->"
            if (target, source) in directions and source != target:
                arrow = "<-->"
                consumed.add((target, source))
            lines.append(f"    {node_ids[source]} {arrow} {node_ids[target]}")
    return "\n".join(lines) + "\n"


def diagram_as_dict(
    diagram: RouteDiagram, *, full_context: bool = False
) -> dict[str, object]:
    """Serialize graph identities separately from abbreviated, non-redacted labels.

    Endpoint IDs are deterministic within this snapshot and independent of
    full-context display. They are neither native IDs nor persistent identities
    across configuration edits. Short identifiers can remain visible by default.
    """
    node_ids = _endpoint_ids(diagram)

    def endpoint_dict(value: Endpoint) -> dict[str, str | None]:
        return {
            "id": node_ids[value],
            "adapter": value.adapter,
            "context": (
                None
                if value.context is None
                else _short(value.context, full=full_context)
            ),
            "destination": (
                None
                if value.destination is None
                else _short_destination(value.destination, full=full_context)
            ),
        }

    return {
        "scope": "configured_offline",
        "adapters": [
            {"adapter_id": aid, "transport": transport, "enabled": enabled}
            for aid, transport, enabled in diagram.adapters
        ],
        "connections": [
            {
                "source": endpoint_dict(c.source),
                "target": endpoint_dict(c.target),
                "route_id": c.route_id,
            }
            for c in diagram.connections
        ],
        "disabled_routes": list(diagram.disabled_routes),
        "errors": list(diagram.errors),
    }


def render_adapter_summary(diagram: RouteDiagram, *, full_context: bool = False) -> str:
    """A terminal-width-friendly index of routes per configured adapter."""
    labels = _endpoint_labels(diagram, full=full_context)
    output = ["Configured route endpoints (offline, not live membership)"]
    if not diagram.adapters and not diagram.connections:
        output.append("No enabled route connections")
    for aid, transport, enabled in diagram.adapters:
        incoming = [c for c in diagram.connections if c.target.adapter == aid]
        outgoing = [c for c in diagram.connections if c.source.adapter == aid]
        output.append("")
        output.append(
            f"{transport}:{aid} {'enabled' if enabled else 'disabled'} "
            f"({len(incoming)} incoming, {len(outgoing)} outgoing connections)"
        )
        if not incoming and not outgoing:
            output.append("  (no enabled route connections)")
        for c in outgoing:
            output.append(
                f"  → {labels[c.target]} " f"from {labels[c.source]} [{c.route_id}]"
            )
        for c in incoming:
            output.append(
                f"  ← {labels[c.source]} " f"to {labels[c.target]} [{c.route_id}]"
            )
    if diagram.disabled_routes:
        output.extend(["", "Disabled routes: " + ", ".join(diagram.disabled_routes)])
    for error in diagram.errors:
        output.extend(["", f"ERROR: {error}"])
    return "\n".join(_terminal_text(line) for line in output) + "\n"
