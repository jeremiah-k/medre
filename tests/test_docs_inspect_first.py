"""Inspect-first investigation consistency tests.

Asserts that docs present inspect as the primary investigation surface,
with trace/evidence available as deeper tools.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parent.parent
OPS_DIR = _ROOT / "docs" / "ops"

TARGET_DOCS = [
    OPS_DIR / "operator-workflows.md",
    OPS_DIR / "running-medre.md",
    OPS_DIR / "recovery-and-replay.md",
    OPS_DIR / "diagnostics-and-evidence.md",
    OPS_DIR / "troubleshooting.md",
    OPS_DIR / "configuration.md",
]


def _read(path: Path) -> str:
    """Read file contents as UTF-8 string."""
    return path.read_text(encoding="utf-8")


def _all_doc_text() -> str:
    """Concatenate all target docs for global searches."""
    return "\n".join(_read(p) for p in TARGET_DOCS)


def _h2_section(text: str, heading: str) -> str:
    """Return an H2 section, failing loudly when the documented contract moved."""
    start = text.find(heading)
    assert start >= 0, f"required documentation section not found: {heading}"
    end = text.find("\n## ", start + len(heading))
    if end < 0:
        end = len(text)
    return text[start:end]


# Docs that contain general operator workflows (incident response, post-run
# inspection, crash recovery). These must present inspect as the primary
# investigation path, with trace/evidence/recover framed as specialized.
_INSPECT_FIRST_WORKFLOW_DOCS = [
    OPS_DIR / "recovery-and-replay.md",
    OPS_DIR / "diagnostics-and-evidence.md",
    OPS_DIR / "troubleshooting.md",
    OPS_DIR / "operator-workflows.md",
]


# ===========================================================================
# 9. Operator walkthrough uses inspect-based investigation
# ===========================================================================


class TestWalkthroughInspectSurface:
    """The operator walkthrough should use inspect commands as the primary
    investigation surface, with trace/evidence available as deeper tools."""

    def test_walkthrough_mentions_inspect(self) -> None:
        """operator-workflows.md must reference 'medre inspect'."""
        text = _read(OPS_DIR / "operator-workflows.md")
        assert "medre inspect" in text, (
            "operator-workflows.md must reference 'medre inspect' as the "
            "primary investigation command."
        )

    def test_walkthrough_inspect_step_before_trace(self) -> None:
        """In the walkthrough, inspect appears before trace in the flow."""
        text = _read(OPS_DIR / "operator-workflows.md")
        inspect_pos = text.find("medre inspect")
        trace_pos = text.find("medre trace")
        assert inspect_pos >= 0, "operator-workflows.md must contain medre inspect"
        assert trace_pos >= 0, "operator-workflows.md must contain medre trace"
        assert inspect_pos < trace_pos, (
            "operator-workflows.md should present inspect before trace "
            "(inspect is the primary investigation surface)."
        )

    def test_walkthrough_inspect_uses_storage_path(self) -> None:
        """Inspect examples in the walkthrough use --storage-path."""
        text = _read(OPS_DIR / "operator-workflows.md")
        # Find inspect command lines.
        inspect_lines = [
            line
            for line in text.splitlines()
            if "medre inspect" in line
            and "--storage-path" not in line
            and line.strip().startswith("medre inspect")
        ]
        # Allow non-CLI-context mentions (table rows, prose).
        for line in inspect_lines:
            if line.strip().startswith("medre inspect") and "config" in line.lower():
                pytest.fail(
                    f"operator-workflows.md has inspect command using --config "
                    f"instead of --storage-path: {line.strip()}"
                )


# ===========================================================================
# 14. Inspect-first investigation consistency
# ===========================================================================


class TestInspectFirstConsistency:
    """General operator workflow docs must present `medre inspect` as the
    primary investigation path. Trace, evidence, and recover are specialized
    commands documented where appropriate but not presented as default first
    steps in general workflows."""

    @pytest.mark.parametrize(
        "doc_path",
        _INSPECT_FIRST_WORKFLOW_DOCS,
        ids=lambda p: p.name,
    )
    def test_workflow_doc_mentions_inspect(self, doc_path: Path) -> None:
        """Workflow docs must reference `medre inspect` as an investigation
        command."""
        assert doc_path.is_file(), f"required workflow doc missing: {doc_path}"
        text = _read(doc_path)
        assert "medre inspect" in text, (
            f"{doc_path.name} must reference 'medre inspect' as the "
            f"primary investigation command."
        )

    @pytest.mark.parametrize(
        "doc_path",
        _INSPECT_FIRST_WORKFLOW_DOCS,
        ids=lambda p: p.name,
    )
    def test_inspect_appears_before_trace_in_workflow(self, doc_path: Path) -> None:
        """In workflow docs, the first `medre inspect` reference should appear
        before or at the same position as the first `medre trace` reference
        in a general workflow context (not within a specialized trace command
        section)."""
        assert doc_path.is_file(), f"required workflow doc missing: {doc_path}"
        text = _read(doc_path)
        inspect_pos = text.find("medre inspect")
        trace_pos = text.find("medre trace")
        assert inspect_pos >= 0, f"{doc_path.name} must contain medre inspect"
        if trace_pos < 0:
            return
        assert inspect_pos <= trace_pos, (
            f"{doc_path.name} should present 'medre inspect' before "
            f"'medre trace' in the document flow. inspect is the primary "
            f"investigation surface."
        )

    def test_bridge_recovery_incident_workflow_inspect_first(self) -> None:
        """recovery-and-replay.md Section 0 incident workflow must present
        inspect as the primary step, not trace."""
        path = OPS_DIR / "recovery-and-replay.md"
        text = _read(path)
        section0 = _h2_section(text, "## Complete Incident Workflow")
        inspect_pos = section0.find("medre inspect event")
        trace_pos = section0.find("medre trace event")
        if inspect_pos < 0:
            pytest.fail(
                "recovery-and-replay.md Section 0 must include "
                "'medre inspect event' in the incident workflow."
            )
        if trace_pos >= 0:
            assert inspect_pos < trace_pos, (
                "recovery-and-replay.md Section 0 should present "
                "'medre inspect event' before 'medre trace event' "
                "in the incident workflow."
            )

    def test_bridge_evidence_bundle_post_run_inspect_primary(self) -> None:
        """diagnostics-and-evidence.md post-run inspection section must
        present inspect as the primary path, with trace as specialized."""
        path = OPS_DIR / "diagnostics-and-evidence.md"
        text = _read(path)
        section = _h2_section(text, "## Post-Run Inspection")
        # Inspect should appear before trace in this section
        inspect_pos = section.find("medre inspect")
        trace_pos = section.find("medre trace")
        assert inspect_pos >= 0, (
            "diagnostics-and-evidence.md post-run inspection must "
            "include 'medre inspect event'."
        )
        if trace_pos >= 0:
            assert inspect_pos < trace_pos, (
                "diagnostics-and-evidence.md post-run inspection should "
                "present 'medre inspect event' before 'medre trace event'."
            )

    def test_event_tracing_mentions_inspect_first_path(self) -> None:
        """operator-workflows.md must include an inspect-first cross-reference
        near the top of the document."""
        path = OPS_DIR / "operator-workflows.md"
        text = _read(path)
        assert "inspect event --timeline" in text, (
            "operator-workflows.md must cross-reference 'medre inspect event "
            "--timeline' as the preferred operator path."
        )

    def test_bridge_failure_drills_incident_workflow_inspect_first(self) -> None:
        """troubleshooting.md incident workflow cross-check section
        must present inspect as the primary step, not trace."""
        path = OPS_DIR / "troubleshooting.md"
        text = _read(path)
        section = _h2_section(text, "## Inspect Follow-Up Quick Reference")
        inspect_pos = section.find("medre inspect")
        trace_pos = section.find("medre trace")
        assert inspect_pos >= 0, (
            "troubleshooting.md inspect follow-up must include "
            "a 'medre inspect' command."
        )
        if trace_pos >= 0:
            assert inspect_pos < trace_pos, (
                "troubleshooting.md inspect follow-up should present "
                "'medre inspect' before 'medre trace'."
            )


# ===========================================================================
# 17. Primary workflow sections must not recommend trace as first step
# ===========================================================================


class TestTraceNotFirstStepInPrimaryWorkflows:
    """Primary operator workflow sections (Phase 2 inspect-first, incident
    Step 2) must not recommend ``medre trace event`` as the first or default
    investigation step.  ``medre inspect event`` is the primary path."""

    def test_operator_workflows_phase2_inspect_first(self) -> None:
        """The inspect-first operator section must lead with inspect, not trace."""
        path = OPS_DIR / "operator-workflows.md"
        text = _read(path)
        section = _h2_section(text, "## Inspect-First Investigation")
        # In the primary investigation section, inspect must precede trace aliases.
        inspect_pos = section.find("medre inspect")
        trace_pos = section.find("medre trace")
        assert (
            inspect_pos >= 0
        ), "operator-workflows.md inspect-first section must include 'medre inspect'."
        if trace_pos >= 0:
            assert inspect_pos < trace_pos, (
                "operator-workflows.md inspect-first section must present 'medre inspect' "
                "before 'medre trace'. inspect is the primary path."
            )

    def test_bridge_recovery_step2_inspect_first(self) -> None:
        """Step 2 in recovery-and-replay.md Section 0 must start with inspect."""
        path = OPS_DIR / "recovery-and-replay.md"
        text = _read(path)
        section = _h2_section(text, "## Complete Incident Workflow")
        step2_pos = section.find("# 2. Inspect the suspect event")
        assert step2_pos >= 0, "incident workflow must identify inspect as step 2"
        inspect_pos = section.find("medre inspect event", step2_pos)
        trace_pos = section.find("medre trace event", step2_pos)
        assert (
            inspect_pos >= 0
        ), "recovery-and-replay.md Step 2 must include 'medre inspect event'."
        if trace_pos >= 0:
            assert inspect_pos < trace_pos, (
                "recovery-and-replay.md Step 2 must present 'medre inspect event' "
                "before 'medre trace event'."
            )

    def test_runtime_operation_post_run_inspect_first(self) -> None:
        """Post-Run Evidence Inspection in running-medre.md must present
        inspect as the primary path."""
        path = OPS_DIR / "running-medre.md"
        section = _read(path)
        inspect_pos = section.find("medre inspect event")
        trace_pos = section.find("medre trace event")
        assert inspect_pos >= 0, (
            "running-medre.md Post-Run Evidence Inspection must include "
            "'medre inspect event'."
        )
        if trace_pos >= 0:
            assert inspect_pos < trace_pos, (
                "running-medre.md Post-Run Evidence Inspection must "
                "present 'medre inspect event' before 'medre trace event'."
            )
