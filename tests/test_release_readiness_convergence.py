"""Documentation convergence tests.

Verifies that the documentation vocabulary used across the repository stays
convergent with released contracts: the canonical capability status labels are
defined in one appendix, and operator docs keep the ``--config`` vs read-only
``--storage-path`` distinction.

Durable-language vocabulary (planning-cycle terms, stale process language,
stale alpha/beta branding) is enforced once, globally, by
``tests/test_docs_no_internal_planning_language.py`` over ``docs/``, ``src/``,
``tests/``, and ``examples/``; this file does not duplicate those scans.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parent.parent
_DOCS = _ROOT / "docs"
_EVIDENCE = _DOCS / "spec" / "appendices" / "evidence-levels.md"
_OPS = _DOCS / "ops"


def _read(path: Path) -> str:
    """Read file contents as UTF-8 string."""
    return path.read_text(encoding="utf-8")


# ===========================================================================
# 1. Status vocabulary convergence
# ===========================================================================


def test_evidence_levels_defines_status_labels() -> None:
    """evidence-levels.md defines the canonical capability status labels."""
    ev_text = _read(_EVIDENCE)

    status_labels = [
        "implemented-not-executed",
        "synthetic-tested",
        "docker-validated",
        "local-integration-validated",
        "live-validated",
    ]
    for label in status_labels:
        assert f"`{label}`" in ev_text, (
            f"evidence-levels.md must define '{label}' in its capability "
            f"status table"
        )


# ===========================================================================
# 2. CLI docs distinguish --config vs --storage-path
# ===========================================================================


def test_ops_docs_show_config_and_storage_path_distinction() -> None:
    """Ops docs must show both --config and --storage-path with distinct roles.

    --config is for runtime/replay commands that need route resolution.
    --storage-path is for read-only inspect commands that access SQLite
    directly without a config file.
    """
    # Check configuration.md for the distinction
    config_doc = _OPS / "configuration.md"
    if not config_doc.is_file():
        pytest.skip("docs/ops/configuration.md not found")

    text = _read(config_doc)
    assert "--config" in text, "configuration.md must document --config"
    assert "--storage-path" in text, "configuration.md must document --storage-path"
    # Verify they are described with different scopes
    text_lower = text.lower()
    assert (
        "read-only" in text_lower or "readonly" in text_lower
    ), "configuration.md must describe --storage-path as read-only"
