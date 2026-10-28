# medre-test-tier: compatibility
"""Default local terminal topology, using the pinned pure-Python Termaid renderer."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from medre.runtime.route_diagram import (
    Connection,
    Endpoint,
    RouteDiagram,
    render_mermaid,
    render_terminal_diagram,
)
from tests.helpers.cli import _run_cli_raw
from tests.helpers.route_diagram import FOURWAY_CONFIG as _CONFIG

pytestmark = pytest.mark.usefixtures("isolated_config_env")


def _graph() -> RouteDiagram:
    return RouteDiagram(
        adapters=(("matrix", "fake", True), ("FXLF", "fake", True)),
        connections=(
            Connection(
                Endpoint("matrix", "!hidden-room:server"), Endpoint("FXLF", "0"), "r"
            ),
            Connection(
                Endpoint("FXLF", "0"), Endpoint("matrix", "!hidden-room:server"), "r"
            ),
        ),
        disabled_routes=(),
        errors=(),
    )


def test_terminal_renders_actual_connected_graph_not_mermaid_source() -> None:
    diagram = _graph()
    terminal = render_terminal_diagram(diagram)
    assert "Connected route group 1" in terminal
    assert "matrix" in terminal and "FXLF" in terminal
    assert "!hidden-room:server" not in terminal
    assert "┌" in terminal and "┘" in terminal
    # Both arrowheads must survive layout; compact gaps collapse them together.
    assert ("▲" in terminal and "▼" in terminal) or (
        "◄" in terminal and "►" in terminal
    )
    assert "subgraph" not in terminal and "flowchart" not in terminal
    assert "g1[" not in terminal
    assert render_mermaid(diagram).startswith("flowchart LR")


def test_ascii_terminal_and_empty_graph() -> None:
    ascii_graph = render_terminal_diagram(_graph(), ascii_only=True)
    assert "FXLF" in ascii_graph and "matrix" in ascii_graph
    assert "┌" not in ascii_graph and "+" in ascii_graph
    assert ("^" in ascii_graph and "v" in ascii_graph) or (
        "<" in ascii_graph and ">" in ascii_graph
    )
    empty = render_terminal_diagram(RouteDiagram((), (), (), ()))
    assert "No enabled route" in empty


def test_incomplete_terminal_exposes_warning_in_graph() -> None:
    result = render_terminal_diagram(RouteDiagram((), (), (), ("unresolved route",)))
    assert "Incomplete preview" in result


def test_cli_default_is_terminal_and_text_still_available(tmp_path: Path) -> None:
    config = tmp_path / "bridge.yaml"
    config.write_text(_CONFIG)
    out, err, code = _run_cli_raw("routes", "diagram", str(config))
    assert code == 0, err
    assert "FXLF" in out and "matrix" in out
    assert "Connected route group" in out and "┌" in out
    remapped_pair = out.split("Connected route group 2", 1)[1].split(
        "Connected route group 3", 1
    )[0]
    assert ("▲" in remapped_pair and "▼" in remapped_pair) or (
        "◄" in remapped_pair and "►" in remapped_pair
    )
    assert "this-token-must-not-appear" not in out
    assert "!long-private-room:example.invalid" not in out
    text, err, code = _run_cli_raw("routes", "diagram", str(config), "--format", "text")
    assert code == 0, err
    assert "Configured route endpoints" in text
    ascii_graph, err, code = _run_cli_raw("routes", "diagram", str(config), "--ascii")
    assert code == 0, err
    assert "┌" not in ascii_graph and "+" in ascii_graph


def test_terminal_export_private_and_no_clobber(tmp_path: Path) -> None:
    config = tmp_path / "bridge.yaml"
    config.write_text(_CONFIG)
    output = tmp_path / "terminal.txt"
    args = ("routes", "diagram", str(config), "--output", str(output))
    out, err, code = _run_cli_raw(*args)
    assert code == 0, err
    assert out == "" and "FXLF" in output.read_text(encoding="utf-8")
    if sys.platform != "win32":
        assert output.stat().st_mode & 0o777 == 0o600
    before = output.read_bytes()
    _, err, code = _run_cli_raw(*args)
    assert code != 0 and "exists" in err.lower()
    assert before == output.read_bytes()


def test_ascii_flag_only_applies_to_terminal(tmp_path: Path) -> None:
    config = tmp_path / "bridge.yaml"
    config.write_text(_CONFIG)
    out, err, code = _run_cli_raw(
        "routes", "diagram", str(config), "--ascii", "--format", "mermaid"
    )
    assert code != 0 and "--ascii requires" in err and out == ""


def test_terminal_scrubs_configured_control_characters() -> None:
    malicious = RouteDiagram(
        adapters=(("bad\x1b[31m", "fake", True), ("safe", "fake", True)),
        connections=(
            Connection(Endpoint("bad\x1b[31m", "0"), Endpoint("safe", "0"), "r"),
        ),
        disabled_routes=(),
        errors=(),
    )
    terminal = render_terminal_diagram(malicious)
    assert "\x1b" not in terminal
    assert r"\x1b" in terminal


def test_terminal_adapter_overview() -> None:
    terminal = render_terminal_diagram(_graph(), view="adapters")
    assert "fake:FXLF" in terminal and "fake:matrix" in terminal
    assert "route" in terminal


def test_configured_entity_codes_cannot_introduce_terminal_controls() -> None:
    name = "x#8238;y#10;z&<q>"
    graph = RouteDiagram(
        adapters=((name, "fake", True), ("safe", "fake", True)),
        connections=(Connection(Endpoint(name, "0"), Endpoint("safe", "0"), "r"),),
        disabled_routes=(),
        errors=(),
    )
    terminal = render_terminal_diagram(graph)
    assert "\u202e" not in terminal
    assert "#8238;" in terminal and "#10;" in terminal
    assert "&<q>" in terminal
    assert terminal.startswith("Configured routes (offline;")


def test_ascii_mode_escapes_unicode_labels_as_well_as_graph_borders() -> None:
    graph = RouteDiagram(
        adapters=(("réseau", "fake", True), ("safe", "fake", True)),
        connections=(Connection(Endpoint("réseau", "0"), Endpoint("safe", "0"), "r"),),
        disabled_routes=(),
        errors=(),
    )
    result = render_terminal_diagram(graph, ascii_only=True)
    assert result.isascii() and r"r\xe9seau" in result


def test_large_context_graph_can_use_the_adapter_overview() -> None:
    graph = RouteDiagram(
        adapters=(("one", "fake", True), ("two", "fake", True)),
        connections=tuple(
            Connection(Endpoint("one", str(n)), Endpoint("two", str(n)), "r")
            for n in range(101)
        ),
        disabled_routes=(),
        errors=(),
    )
    with pytest.raises(ValueError, match="terminal rendering limits"):
        render_terminal_diagram(graph)
    assert "fake:one" in render_terminal_diagram(graph, view="adapters")
