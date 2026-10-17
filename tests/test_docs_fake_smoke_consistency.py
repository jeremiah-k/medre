"""Protect operator-facing smoke instructions from overstating their evidence."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT / "docs" / "ops"


def test_operator_workflow_smoke_is_persistent_without_config_rewrites():
    guide = (OPS / "operator-workflows.md").read_text(encoding="utf-8")
    assert "--storage-path /tmp/medre-walkthrough.db" in guide
    assert "medre config sample > /tmp/medre-walkthrough.yaml" in guide
    assert "# Edit storage section" not in guide
    assert "inbound codec decoding" in guide.lower()
    assert "--source-adapter INSTANCE_ID" in guide


def test_diagnostics_smoke_examples_share_the_generated_config():
    guide = (OPS / "diagnostics-and-evidence.md").read_text(encoding="utf-8")
    assert "medre config sample > /tmp/medre-smoke.yaml" in guide
    assert "--storage-path /tmp/medre-smoke.db" in guide
    assert "/tmp/medre-sqlite.yaml" not in guide
    assert "inbound codec decoding" in guide.lower()
    assert "does not accept a bridge configuration" in guide


def test_onboarding_distinguishes_sample_from_bidirectional_bridge():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    guide = (OPS / "getting-started.md").read_text(encoding="utf-8")
    assert "one-way diagnostic route" in readme
    assert "one-way diagnostic route" in guide
    assert "--source-context CONTEXT" in guide
