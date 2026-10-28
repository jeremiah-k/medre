"""Local image execution refuses failed, stalled or unsupported rendering."""

from __future__ import annotations

import subprocess

import pytest

from medre.cli.diagram_output import GraphvizError, render_graphviz


@pytest.mark.parametrize("failure", ["exit", "timeout", "start", "empty"])
def test_failed_renderer_publishes_no_private_diagnostics(monkeypatch, failure):
    monkeypatch.setattr("medre.cli.diagram_output.shutil.which", lambda _: "/local/dot")

    def run(arguments, **kwargs):
        assert arguments == ["/local/dot", "-Tsvg"]
        assert kwargs["timeout"] == 20 and kwargs["input"] == b"private label"
        kwargs["stderr"].write(b"private label")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(arguments, 20)
        if failure == "start":
            raise OSError("private label")
        return subprocess.CompletedProcess(arguments, 1 if failure == "exit" else 0)

    monkeypatch.setattr("medre.cli.diagram_output.subprocess.run", run)
    with pytest.raises(GraphvizError) as caught:
        render_graphviz("private label", "svg")
    assert "private label" not in str(caught.value)


def test_renderer_accepts_only_closed_image_formats():
    with pytest.raises(ValueError, match="unsupported"):
        render_graphviz("digraph {}", "svg; bad command")
