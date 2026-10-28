# medre-test-tier: compatibility
"""SDK-free expanded route visualization and text/Mermaid safety checks."""

from __future__ import annotations

from pathlib import Path

from medre.config.loader import load_config
from medre.runtime.route_diagram import (
    Connection,
    Endpoint,
    RouteDiagram,
    build_route_diagram,
    render_adapter_summary,
    render_mermaid,
)
from tests.helpers.cli import _run_cli_raw
from tests.helpers.route_diagram import FOURWAY_CONFIG as _CONFIG


def test_diagram_uses_all_fanout_targets_and_reverse_expansion(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(_CONFIG)
    config, _, _ = load_config(str(config_file))
    diagram = build_route_diagram(config)
    assert len(diagram.connections) == 10  # 3 forward, 3 reverse, 2+2 mapped
    target_adapters = {
        c.target.adapter for c in diagram.connections if c.route_id == "all_radios"
    }
    assert len(target_adapters) == 4
    forward = Connection(
        Endpoint("FXLF", "3"), Endpoint("FXLT", "2"), "different_channels"
    )
    reverse = Connection(
        Endpoint("FXLT", "2"), Endpoint("FXLF", "3"), "different_channels"
    )
    assert forward in diagram.connections
    assert reverse in diagram.connections
    assert diagram.disabled_routes == ("paused",)
    assert diagram.errors == ()


def test_cli_prints_both_views_and_keeps_credentials_private(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(_CONFIG)
    formats = (
        ("text", "contexts"),
        ("mermaid", "contexts"),
        ("mermaid", "adapters"),
        ("json", "contexts"),
    )
    for fmt, view in formats:
        out, err, code = _run_cli_raw(
            "routes",
            "diagram",
            "--config",
            str(config_file),
            "--format",
            fmt,
            "--view",
            view,
        )
        assert code == 0, err
        assert "this-token-must-not-appear" not in out
        assert "FXLF" in out and "FXLT" in out
        assert "!long-private-room:example.invalid" not in out
    out, _, code = _run_cli_raw(
        "routes",
        "diagram",
        "--config",
        str(config_file),
        "--format",
        "mermaid",
        "--full-context",
    )
    assert code == 0
    assert "!long-private-room:example.invalid" in out


def test_mermaid_escapes_configured_labels() -> None:
    endpoint = Endpoint('bad" ]\nclick A href "evil', "X<danger>")
    diagram = RouteDiagram(
        adapters=((endpoint.adapter, "fake", True), ("safe", "fake", True)),
        connections=(Connection(endpoint, Endpoint("safe", "0"), "route"),),
        disabled_routes=(),
        errors=(),
    )
    mermaid = render_mermaid(diagram, full_context=True)
    assert "\nclick A href" not in mermaid
    assert "#quot;" in mermaid and "#60;danger#62;" in mermaid
    assert "click A href" in mermaid  # treated as inert label text
    assert "safe · 0" in mermaid


def test_terminal_summary_is_per_adapter_and_reports_disabled() -> None:
    diagram = RouteDiagram(
        adapters=(("a", "fake", True), ("b", "fake", False)),
        connections=(Connection(Endpoint("a", "0"), Endpoint("b", "2"), "link"),),
        disabled_routes=("paused",),
        errors=(),
    )
    summary = render_adapter_summary(diagram)
    assert "fake:a enabled" in summary
    assert "fake:b disabled" in summary
    assert "→ b · 2" in summary
    assert "← a · 0" in summary
    assert "Disabled routes: paused" in summary


def test_invalid_graph_is_identified_in_mermaid_and_stderr(tmp_path: Path) -> None:
    """A saved partial Mermaid preview cannot be mistaken for validated topology."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        _CONFIG.replace(
            "dest_adapters: [FXLF, FXLT, meshcore]", "dest_adapters: [missing]"
        )
    )
    out, err, code = _run_cli_raw(
        "routes", "diagram", "--config", str(config_file), "--format", "mermaid"
    )
    assert code != 0
    assert "incomplete" in err.lower()
    assert "Incomplete preview" in out
