"""Keep ingress, runtime-delivery and replay loop claims distinct."""

from pathlib import Path

_DOCS = Path(__file__).resolve().parents[1] / "docs/ops"


def test_loop_layers_agree_across_operator_guides() -> None:
    runtime = (_DOCS / "running-medre.md").read_text()
    troubleshooting = (_DOCS / "troubleshooting.md").read_text()
    for label in (
        "Direct loop detection",
        "Self-loop guard",
        "Native-ref duplicate suppression",
        "Route-trace guard",
    ):
        assert label in runtime
    for label in (
        "Config-Time Detection",
        "Self-Loop Guard",
        "Native-reference duplicate suppression",
        "Route-Trace Loop Prevention",
    ):
        assert label in troubleshooting
    assert "four loop safeguards" in troubleshooting
    assert "cross-instance" in troubleshooting.lower()
