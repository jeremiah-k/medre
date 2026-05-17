"""Inspect-first investigation consistency tests.

Asserts that docs present inspect as the primary investigation surface,
with trace/evidence available as deeper tools.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parent.parent
RUNBOOKS_DIR = _ROOT / "docs" / "runbooks"

TARGET_DOCS = [
    RUNBOOKS_DIR / "alpha-walkthrough.md",
    RUNBOOKS_DIR / "bridge-operation.md",
    RUNBOOKS_DIR / "bridge-recovery.md",
    RUNBOOKS_DIR / "replay-operation.md",
    RUNBOOKS_DIR / "bridge-evidence-bundle.md",
    RUNBOOKS_DIR / "event-tracing.md",
    RUNBOOKS_DIR / "bridge-failure-drills.md",
    RUNBOOKS_DIR / "configuration.md",
]


def _read(path: Path) -> str:
    """Read file contents as UTF-8 string."""
    return path.read_text(encoding="utf-8")


def _all_doc_text() -> str:
    """Concatenate all target docs for global searches."""
    return "\n".join(_read(p) for p in TARGET_DOCS)


# Docs that contain general operator workflows (incident response, post-run
# inspection, crash recovery). These must present inspect as the primary
# investigation path, with trace/evidence/recover framed as specialized.
_INSPECT_FIRST_WORKFLOW_DOCS = [
    RUNBOOKS_DIR / "bridge-recovery.md",
    RUNBOOKS_DIR / "bridge-evidence-bundle.md",
    RUNBOOKS_DIR / "bridge-failure-drills.md",
    RUNBOOKS_DIR / "event-tracing.md",
]


# ===========================================================================
# 9. Alpha walkthrough uses inspect-based investigation
# ===========================================================================


class TestAlphaWalkthroughInspectSurface:
    """The alpha walkthrough should use inspect commands as the primary
    investigation surface, with trace/evidence available as deeper tools."""

    def test_walkthrough_mentions_inspect(self) -> None:
        """alpha-walkthrough.md must reference 'medre inspect'."""
        text = _read(RUNBOOKS_DIR / "alpha-walkthrough.md")
        assert "medre inspect" in text, (
            "alpha-walkthrough.md must reference 'medre inspect' as the "
            "primary investigation command."
        )

    def test_walkthrough_inspect_step_before_trace(self) -> None:
        """In the walkthrough, inspect appears before trace in the flow."""
        text = _read(RUNBOOKS_DIR / "alpha-walkthrough.md")
        inspect_pos = text.find("medre inspect")
        trace_pos = text.find("medre trace")
        if inspect_pos < 0 or trace_pos < 0:
            pytest.skip("Both inspect and trace must be in walkthrough")
        assert inspect_pos < trace_pos, (
            "alpha-walkthrough.md should present inspect before trace "
            "(inspect is the primary investigation surface)."
        )

    def test_walkthrough_inspect_uses_storage_path(self) -> None:
        """Inspect examples in the walkthrough use --storage-path."""
        text = _read(RUNBOOKS_DIR / "alpha-walkthrough.md")
        # Find inspect command lines.
        inspect_lines = [
            line for line in text.splitlines()
            if "medre inspect" in line and "--storage-path" not in line
            and line.strip().startswith("medre inspect")
        ]
        # Allow non-CLI-context mentions (table rows, prose).
        for line in inspect_lines:
            if line.strip().startswith("medre inspect") and "config" in line.lower():
                pytest.fail(
                    f"alpha-walkthrough.md has inspect command using --config "
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
        if not doc_path.exists():
            pytest.skip(f"{doc_path.name} not found")
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
        if not doc_path.exists():
            pytest.skip(f"{doc_path.name} not found")
        text = _read(doc_path)
        inspect_pos = text.find("medre inspect")
        trace_pos = text.find("medre trace")
        if inspect_pos < 0 or trace_pos < 0:
            pytest.skip("Both inspect and trace must be present")
        assert inspect_pos <= trace_pos, (
            f"{doc_path.name} should present 'medre inspect' before "
            f"'medre trace' in the document flow. inspect is the primary "
            f"investigation surface."
        )

    def test_bridge_recovery_incident_workflow_inspect_first(self) -> None:
        """bridge-recovery.md Section 0 incident workflow must present
        inspect as the primary step, not trace."""
        path = RUNBOOKS_DIR / "bridge-recovery.md"
        if not path.exists():
            pytest.skip("bridge-recovery.md not found")
        text = _read(path)
        # Find Section 0
        section0_start = text.find("## 0.")
        if section0_start < 0:
            pytest.skip("Section 0 not found")
        # Find next section header
        section1_start = text.find("\n## 1.", section0_start)
        if section1_start < 0:
            section1_start = len(text)
        section0 = text[section0_start:section1_start]
        # In Section 0, inspect should appear before trace in workflow steps
        inspect_pos = section0.find("medre inspect event")
        trace_pos = section0.find("medre trace event")
        if inspect_pos < 0:
            pytest.fail(
                "bridge-recovery.md Section 0 must include "
                "'medre inspect event' in the incident workflow."
            )
        if trace_pos >= 0:
            assert inspect_pos < trace_pos, (
                "bridge-recovery.md Section 0 should present "
                "'medre inspect event' before 'medre trace event' "
                "in the incident workflow."
            )

    def test_bridge_evidence_bundle_post_run_inspect_primary(self) -> None:
        """bridge-evidence-bundle.md post-run inspection section must
        present inspect as the primary path, with trace as specialized."""
        path = RUNBOOKS_DIR / "bridge-evidence-bundle.md"
        if not path.exists():
            pytest.skip("bridge-evidence-bundle.md not found")
        text = _read(path)
        # Find the post-run inspection section
        section_pos = text.find("### 1.6 Post-Run Inspection")
        if section_pos < 0:
            pytest.skip("Post-Run Inspection section not found")
        section_end = text.find("\n## ", section_pos + 1)
        if section_end < 0:
            section_end = len(text)
        section = text[section_pos:section_end]
        # Inspect should appear before trace in this section
        inspect_pos = section.find("medre inspect event")
        trace_pos = section.find("medre trace event")
        assert inspect_pos >= 0, (
            "bridge-evidence-bundle.md post-run inspection must "
            "include 'medre inspect event'."
        )
        if trace_pos >= 0:
            assert inspect_pos < trace_pos, (
                "bridge-evidence-bundle.md post-run inspection should "
                "present 'medre inspect event' before 'medre trace event'."
            )

    def test_event_tracing_mentions_inspect_first_path(self) -> None:
        """event-tracing.md must include an inspect-first cross-reference
        near the top of the document."""
        path = RUNBOOKS_DIR / "event-tracing.md"
        if not path.exists():
            pytest.skip("event-tracing.md not found")
        text = _read(path)
        assert "inspect event --timeline" in text, (
            "event-tracing.md must cross-reference 'medre inspect event "
            "--timeline' as the preferred operator path."
        )

    def test_bridge_failure_drills_incident_workflow_inspect_first(self) -> None:
        """bridge-failure-drills.md incident workflow cross-check section
        must present inspect as the primary step, not trace."""
        path = RUNBOOKS_DIR / "bridge-failure-drills.md"
        if not path.exists():
            pytest.skip("bridge-failure-drills.md not found")
        text = _read(path)
        section_pos = text.find("## 11. Incident Workflow Cross-Check")
        if section_pos < 0:
            pytest.skip("Incident Workflow Cross-Check section not found")
        section_end = text.find("\n## ", section_pos + 1)
        if section_end < 0:
            section_end = len(text)
        section = text[section_pos:section_end]
        inspect_pos = section.find("medre inspect event")
        trace_pos = section.find("medre trace event")
        assert inspect_pos >= 0, (
            "bridge-failure-drills.md incident workflow must include "
            "'medre inspect event'."
        )
        if trace_pos >= 0:
            assert inspect_pos < trace_pos, (
                "bridge-failure-drills.md incident workflow should present "
                "'medre inspect event' before 'medre trace event'."
            )


# ===========================================================================
# 17. Primary workflow sections must not recommend trace as first step
# ===========================================================================


class TestTraceNotFirstStepInPrimaryWorkflows:
    """Primary operator workflow sections (Phase 2 inspect-first, incident
    Step 2) must not recommend ``medre trace event`` as the first or default
    investigation step.  ``medre inspect event`` is the primary path."""

    def test_alpha_walkthrough_phase2_inspect_first(self) -> None:
        """Phase 2 in alpha-walkthrough.md must start with inspect, not trace."""
        path = RUNBOOKS_DIR / "alpha-walkthrough.md"
        if not path.exists():
            pytest.skip("alpha-walkthrough.md not found")
        text = _read(path)
        # Find Phase 2 section
        phase2 = text.find("### Phase 2:")
        if phase2 < 0:
            pytest.skip("Phase 2 section not found")
        phase3 = text.find("### Phase 3:", phase2)
        if phase3 < 0:
            phase3 = len(text)
        section = text[phase2:phase3]
        # In Phase 2, inspect must appear before any trace command
        inspect_pos = section.find("medre inspect")
        trace_pos = section.find("medre trace")
        assert inspect_pos >= 0, (
            "alpha-walkthrough.md Phase 2 must include 'medre inspect'."
        )
        if trace_pos >= 0:
            assert inspect_pos < trace_pos, (
                "alpha-walkthrough.md Phase 2 must present 'medre inspect' "
                "before 'medre trace'. inspect is the primary path."
            )

    def test_bridge_recovery_step2_inspect_first(self) -> None:
        """Step 2 in bridge-recovery.md Section 0 must start with inspect."""
        path = RUNBOOKS_DIR / "bridge-recovery.md"
        if not path.exists():
            pytest.skip("bridge-recovery.md not found")
        text = _read(path)
        section0 = text.find("## 0.")
        if section0 < 0:
            pytest.skip("Section 0 not found")
        section1 = text.find("\n## 1.", section0)
        if section1 < 0:
            section1 = len(text)
        s0 = text[section0:section1]
        # Step 2 must have inspect before trace
        step2 = s0.find("### Step 2:")
        if step2 < 0:
            pytest.skip("Step 2 not found in Section 0")
        step3 = s0.find("### Step 3:", step2)
        if step3 < 0:
            step3 = len(s0)
        step2_text = s0[step2:step3]
        inspect_pos = step2_text.find("medre inspect event")
        trace_pos = step2_text.find("medre trace event")
        assert inspect_pos >= 0, (
            "bridge-recovery.md Step 2 must include 'medre inspect event'."
        )
        if trace_pos >= 0:
            assert inspect_pos < trace_pos, (
                "bridge-recovery.md Step 2 must present 'medre inspect event' "
                "before 'medre trace event'."
            )

    def test_runtime_operation_post_run_inspect_first(self) -> None:
        """Post-Run Evidence Inspection in runtime-operation.md must present
        inspect as the primary path."""
        path = RUNBOOKS_DIR / "runtime-operation.md"
        if not path.exists():
            pytest.skip("runtime-operation.md not found")
        text = _read(path)
        section_pos = text.find("### Post-Run Evidence Inspection")
        if section_pos < 0:
            pytest.skip("Post-Run Evidence Inspection section not found")
        section_end = text.find("\n## ", section_pos + 1)
        if section_end < 0:
            section_end = len(text)
        section = text[section_pos:section_end]
        inspect_pos = section.find("medre inspect event")
        trace_pos = section.find("medre trace event")
        assert inspect_pos >= 0, (
            "runtime-operation.md Post-Run Evidence Inspection must include "
            "'medre inspect event'."
        )
        if trace_pos >= 0:
            assert inspect_pos < trace_pos, (
                "runtime-operation.md Post-Run Evidence Inspection must "
                "present 'medre inspect event' before 'medre trace event'."
            )
