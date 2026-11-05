"""Implemented-pipeline-phase documentation guard tests.

Asserts that the implemented-stage list stated in the spec docs matches
the `PipelinePhase` enum in `src/medre/core/engine/phases.py`:

  1. docs/spec/architecture.md must name the enum members as the
     implemented top-level stages.
  2. docs/spec/principles.md must name the enum values as the implemented
     phases and label the reserved stages as having no code path today.
"""

from __future__ import annotations

import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parent.parent
_ARCHITECTURE_MD = _ROOT / "docs" / "spec" / "architecture.md"
_PRINCIPLES_MD = _ROOT / "docs" / "spec" / "principles.md"

#: Marker introducing the canonical implemented-stage list in each doc.
_PHASE_ENUM_MARKER = "`PipelinePhase` enum in `src/medre/core/engine/phases.py`:"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read(path: Path) -> str:
    """Read file contents as UTF-8 string."""
    return path.read_text(encoding="utf-8")


def _slice_section(text: str, start_marker: str, end_marker: str) -> str:
    """Slice *text* from *start_marker* (inclusive) to *end_marker* (exclusive).

    Raises ``AssertionError`` if *start_marker* is not found, preventing
    silent -1-based slicing that would return the entire document tail.
    """
    start = text.find(start_marker)
    assert start != -1, f"Section start marker not found: {start_marker!r}"
    end = text.find(end_marker, start + len(start_marker))
    if end == -1:
        return text[start:]
    return text[start:end]


def _stated_phases(section: str, token_pattern: str) -> list[str]:
    """Extract the backticked phase list following the enum marker.

    Prose is wrapped near 88 columns, so the section is whitespace-
    normalized first; the extraction covers the sentence that carries the
    canonical list, not the whole document.
    """
    normalized = " ".join(section.split())
    assert (
        _PHASE_ENUM_MARKER in normalized
    ), "Docs must define the implemented stages via the `PipelinePhase` enum"
    sentence = normalized.split(_PHASE_ENUM_MARKER, 1)[1].split(". ", 1)[0]
    return re.findall(token_pattern, sentence)


# ===========================================================================
# 1. architecture.md implemented-stage list vs the enum
# ===========================================================================


class TestArchitecturePhaseList:
    """architecture.md §1 must list the `PipelinePhase` members."""

    def test_lists_enum_member_names_in_order(self) -> None:
        """The implemented-stage sentence must match the enum exactly."""
        from medre.core.engine.phases import PipelinePhase

        section = _slice_section(
            _read(_ARCHITECTURE_MD),
            "## 1. Pipeline Overview",
            "## 2. Stage Descriptions",
        )
        stated = _stated_phases(section, r"`([A-Z_]+)`")
        expected = [phase.name for phase in PipelinePhase]
        assert stated == expected, (
            "architecture.md implemented-stage list does not match "
            f"PipelinePhase: docs state {stated}, enum defines {expected}"
        )


# ===========================================================================
# 2. principles.md implemented-phase list vs the enum
# ===========================================================================


class TestPrinciplesPhaseList:
    """principles.md §4 must name the `PipelinePhase` values."""

    def test_names_enum_values_in_order(self) -> None:
        """The implemented-phase sentence must match the enum exactly."""
        from medre.core.engine.phases import PipelinePhase

        section = _slice_section(
            _read(_PRINCIPLES_MD),
            "## 4. Pipeline over Callback",
            "## 5. Versioned Envelope, Explicit Extensions",
        )
        stated = _stated_phases(section, r"`([a-z_]+)`")
        expected = [phase.value for phase in PipelinePhase]
        assert stated == expected, (
            "principles.md implemented-phase list does not match "
            f"PipelinePhase: docs state {stated}, enum defines {expected}"
        )

    def test_reserved_stages_labeled_as_not_executing(self) -> None:
        """Reserved stages must be labeled as future, not executing today."""
        section = _slice_section(
            _read(_PRINCIPLES_MD),
            "## 4. Pipeline over Callback",
            "## 5. Versioned Envelope, Explicit Extensions",
        )
        normalized = " ".join(section.split())
        assert "reserved future extension points" in normalized, (
            "principles.md must label the enrich/transform/event-policy "
            "stages as reserved future extension points"
        )
        assert "no code path today" in normalized, (
            "principles.md must state that the reserved stages have no "
            "code path today"
        )
