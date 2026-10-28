"""Offline graphs retain exact identities while presenting abbreviated labels."""

from __future__ import annotations

import pytest

from medre.runtime.route_diagram import (
    Connection,
    Endpoint,
    RouteDiagram,
    diagram_as_dict,
    render_adapter_summary,
    render_mermaid,
)


def _diagram(*connections: Connection, errors: tuple[str, ...] = ()) -> RouteDiagram:
    """Create a transport-neutral graph without installing native providers."""
    return RouteDiagram(
        adapters=(("a", "fake", True), ("b", "fake", True)),
        connections=connections,
        disabled_routes=(),
        errors=errors,
    )


def test_json_endpoint_ids_survive_abbreviation_collisions() -> None:
    """Display aliases never erase the distinction between different native contexts."""
    first = Endpoint("a", "!same-pref-one:private.invalid")
    second = Endpoint("a", "!same-pref-two:private.invalid")
    target = Endpoint("b", "0")
    diagram = _diagram(
        Connection(first, target, "first"), Connection(second, target, "second")
    )
    result = diagram_as_dict(diagram)
    connections = result["connections"]
    assert connections[0]["source"]["context"] == connections[1]["source"]["context"]
    assert connections[0]["source"]["id"] != connections[1]["source"]["id"]
    assert connections[0]["target"]["id"] == connections[1]["target"]["id"]
    full = diagram_as_dict(diagram, full_context=True)
    assert [c["source"]["id"] for c in connections] == [
        c["source"]["id"] for c in full["connections"]
    ]
    for renderer in (render_mermaid, render_adapter_summary):
        output = renderer(diagram)
        assert "[n0]" in output and "[n1]" in output
        assert first.context not in output and second.context not in output


def test_short_identifiers_are_abbreviations_not_redactions() -> None:
    """Short configured names remain visible and must not be advertised as anonymous."""
    result = diagram_as_dict(
        _diagram(Connection(Endpoint("a", "!private"), Endpoint("b", "0"), "link"))
    )
    assert result["connections"][0]["source"]["context"] == "!private"


@pytest.mark.parametrize("control", ["\x1b[2J", "\x9b2J", "\u202e", "\n", "\r"])
def test_text_output_cannot_execute_configured_terminal_controls(control: str) -> None:
    """Configured contexts cannot clear a terminal, forge lines, or reverse labels."""
    diagram = _diagram(
        Connection(Endpoint("a", f"private{control}room"), Endpoint("b", "0"), "link")
    )
    output = render_adapter_summary(diagram, full_context=True)
    if control not in ("\n", "\r"):
        assert control not in output
    assert "private" + control + "room" not in output
    assert control.encode("unicode_escape").decode("ascii") in output


@pytest.mark.parametrize("view", ["contexts", "adapters"])
def test_mermaid_reports_incomplete_and_empty_previews(view: str) -> None:
    """Exporting a partial graph cannot hide its plan errors from a graph reader."""
    output = render_mermaid(_diagram(errors=("bad route: unknown adapter",)), view=view)
    assert "Incomplete preview: 1 configuration issue(s)" in output
    assert "No enabled route connections" in output


def test_adapter_overview_collapses_only_matching_reverse_route_groups() -> None:
    """A reciprocal bridge is one visual edge without conflating different route groups."""
    a, b = Endpoint("a", "0"), Endpoint("b", "1")
    matching = render_mermaid(
        _diagram(Connection(a, b, "bridge"), Connection(b, a, "bridge")),
        view="adapters",
    )
    assert matching.count("<-->") == 1
    assert " -->|" not in matching
    different = render_mermaid(
        _diagram(Connection(a, b, "forward"), Connection(b, a, "reverse")),
        view="adapters",
    )
    assert "<-->" not in different
    assert different.count("-->|1 route|") == 2


def test_endpoint_order_distinguishes_wildcard_from_empty_context() -> None:
    """Snapshot node numbering cannot depend on hash iteration for unequal endpoints."""
    target = Endpoint("b", "0")
    diagram = _diagram(
        Connection(Endpoint("a", ""), target, "empty"),
        Connection(Endpoint("a", None), target, "wildcard"),
    )
    nodes = {
        c["route_id"]: c["source"]["id"]
        for c in diagram_as_dict(diagram)["connections"]
    }
    assert nodes == {"empty": "n1", "wildcard": "n0"}


def test_display_safety_preserves_unicode_text_joiners() -> None:
    """Legitimate script and emoji joiners remain readable in transport labels."""
    context = "👩\u200d💻 a\u200cb"
    diagram = _diagram(Connection(Endpoint("a", context), Endpoint("b", "0"), "link"))
    assert context in render_adapter_summary(diagram, full_context=True)
    assert context in render_mermaid(diagram, full_context=True)


@pytest.mark.parametrize(
    ("destination", "abbreviated"),
    [
        ("channel:#private-room", "channel:#private-…"),
        ("matrix_room:!private-room", "matrix_room:!private-…"),
        ("contact:@private-user", "contact:@private-…"),
        ("lxmf_destination:" + "a" * 32, "lxmf_destination:" + "a" * 9 + "…"),
        ("lxmf_destination:" + "a" * 33, "lxmf_destination:" + "a" * 9 + "…"),
    ],
)
def test_structured_destination_abbreviates_identifier_after_kind(
    destination: str, abbreviated: str
) -> None:
    """The kind prefix cannot bypass identifier formatting or change node identity."""
    source = Endpoint("a", "0")
    target = Endpoint("b", None, destination)
    diagram = _diagram(Connection(source, target, "structured"))
    brief = diagram_as_dict(diagram)["connections"][0]["target"]
    exact = diagram_as_dict(diagram, full_context=True)["connections"][0]["target"]
    assert brief["destination"] == abbreviated
    assert exact["destination"] == destination
    assert brief["id"] == exact["id"]
    text = render_adapter_summary(diagram)
    assert destination not in text
    assert abbreviated in text
    assert destination in render_adapter_summary(diagram, full_context=True)
    # Mermaid escapes # as an entity. Test the escaped source, not its
    # unescaped label, so display safety cannot be mistaken for data loss.
    mermaid_label = abbreviated.replace("#", "#35;")
    full_mermaid_label = destination.replace("#", "#35;")
    assert mermaid_label in render_mermaid(diagram)
    assert full_mermaid_label in render_mermaid(diagram, full_context=True)


def test_adapter_overview_separates_shared_and_directional_route_groups() -> None:
    """Shared bridges stay bidirectional beside additional one-way routes."""
    a, b = Endpoint("a", "0"), Endpoint("b", "1")
    diagram = _diagram(
        Connection(a, b, "bridge"),
        Connection(b, a, "bridge"),
        Connection(a, b, "outbound"),
        Connection(b, a, "inbound"),
    )
    output = render_mermaid(diagram, view="adapters")
    assert output.count("<-->|1 route|") == 1
    assert output.count(" -->|1 route|") == 2
    assert "a0 -->|1 route| a1" in output
    assert "a1 -->|1 route| a0" in output


def test_adapter_overview_keeps_self_route_directed() -> None:
    """A self-route is a single direction, not its own reciprocal bridge."""
    a = Endpoint("a", "0")
    output = render_mermaid(_diagram(Connection(a, a, "self")), view="adapters")
    assert "<-->" not in output
    assert output.count("a0 -->|1 route| a0") == 1


def test_empty_text_diagram_has_an_explicit_result() -> None:
    """An empty configuration reports its state rather than only a heading."""
    output = render_adapter_summary(RouteDiagram((), (), (), ()))
    assert "No enabled route connections" in output
