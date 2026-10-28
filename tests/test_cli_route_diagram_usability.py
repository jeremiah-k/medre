# medre-test-tier: compatibility
"""Operator-facing CLI navigation, selected config and private offline graphs."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from medre.runtime.route_diagram import (
    Connection,
    Endpoint,
    RouteDiagram,
    render_graphviz_dot,
)
from tests.helpers.cli import _run_cli_raw
from tests.helpers.route_diagram import FOURWAY_CONFIG as _CONFIG

pytestmark = pytest.mark.usefixtures("isolated_config_env")


def test_positional_diagram_config_and_no_overwrite(tmp_path: Path) -> None:
    config = tmp_path / "bridge.yaml"
    config.write_text(_CONFIG)
    output = tmp_path / "routes.mmd"
    out, err, code = _run_cli_raw(
        "routes", "diagram", str(config), "--format", "mermaid", "--output", str(output)
    )
    assert code == 0, err
    assert out == "" and output.read_text().startswith("flowchart LR")
    if sys.platform != "win32":
        assert output.stat().st_mode & 0o777 == 0o600
    before = output.read_bytes()
    _, err, code = _run_cli_raw(
        "routes", "diagram", str(config), "--format", "mermaid", "--output", str(output)
    )
    assert code != 0 and "exists" in err.lower() and output.read_bytes() == before
    _, err, code = _run_cli_raw(
        "routes", "diagram", str(config), "--config", str(config)
    )
    assert code != 0 and "either" in err


@pytest.mark.skipif(shutil.which("dot") is None, reason="Graphviz optional")
@pytest.mark.parametrize(
    "format,signature", [("svg", b"<svg"), ("png", b"\x89PNG"), ("pdf", b"%PDF")]
)
def test_diagram_renders_image_offline_private(
    tmp_path: Path, format: str, signature: bytes
) -> None:
    config = tmp_path / "bridge.yaml"
    config.write_text(_CONFIG)
    path = tmp_path / f"routes.{format}"
    out, err, code = _run_cli_raw(
        "routes", "diagram", str(config), "--format", format, "--output", str(path)
    )
    assert code == 0, err
    assert out == "" and signature in path.read_bytes()[:500]
    if sys.platform != "win32":
        assert path.stat().st_mode & 0o777 == 0o600


def test_missing_renderer_refuses_to_claim_render_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: None)
    config = tmp_path / "bridge.yaml"
    config.write_text(_CONFIG)
    path = tmp_path / "routes.svg"
    _, err, code = _run_cli_raw(
        "routes", "diagram", str(config), "--format", "svg", "--output", str(path)
    )
    assert code != 0 and "Graphviz" in err and not path.exists()


def test_graphviz_escapes_untrusted_label_as_data() -> None:
    hostile = Endpoint('bad"\nfoo -> bar;\n', "0")
    graph = RouteDiagram(
        adapters=((hostile.adapter, "fake", True), ("safe", "fake", True)),
        connections=(Connection(hostile, Endpoint("safe", "ch"), "r"),),
        disabled_routes=(),
        errors=(),
    )
    dot = render_graphviz_dot(graph, full_context=True)
    assert "foo -> bar;\n" not in dot
    assert "\\nfoo -> bar" in dot


def test_incomplete_graph_refuses_rendered_image(tmp_path: Path) -> None:
    config = tmp_path / "bad.yaml"
    config.write_text(
        _CONFIG.replace(
            "dest_adapters: [FXLF, FXLT, meshcore]", "dest_adapters: [unknown]"
        )
    )
    image = tmp_path / "invalid.svg"
    _, err, code = _run_cli_raw(
        "routes", "diagram", str(config), "--format", "svg", "--output", str(image)
    )
    assert code != 0
    assert "incomplete" in err.lower()
    assert not image.exists()
